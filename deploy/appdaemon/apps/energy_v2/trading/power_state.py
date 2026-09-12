"""Pure shadow overlay for DEYE inverter power-state eligibility.

This module never calls Home Assistant and never controls ``switch.deye``.  It
only annotates an existing 15-minute trading plan with a deterministic desired
power-state schedule for later review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, isfinite

from ..models import StrEnum
from .models import PlannerResult, TradingSlotPlan

_SLOT = timedelta(minutes=15)


class DeyePowerState(StrEnum):
    """Desired/eligible state; ``OFF`` is reserved for future observed state."""

    OFF_ELIGIBLE = "OFF_ELIGIBLE"
    OFF = "OFF"
    STARTING = "STARTING"
    READY = "READY"
    ON_REQUIRED = "ON_REQUIRED"


class DeyeTelemetryExpectation(StrEnum):
    """Future health-policy context, deliberately not wired into Phase 4."""

    DEYE_EXPECTED_OFF = "DEYE_EXPECTED_OFF"
    DEYE_EXPECTED_ON = "DEYE_EXPECTED_ON"


class AssumptionStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    MODEL_ASSUMPTION = "MODEL_ASSUMPTION"
    CONFIGURED = "CONFIGURED"


@dataclass(frozen=True)
class DeyePowerStateConfig:
    battery_name: str = "DEYE"
    activity_threshold_kwh_per_slot: float = 0.001
    startup_lead_time: timedelta = timedelta(minutes=10)
    minimum_on_time: timedelta = timedelta(minutes=30)
    minimum_off_time: timedelta = timedelta(minutes=30)
    shutdown_delay: timedelta = timedelta(minutes=15)
    deye_on_idle_power_w: float | None = None
    deye_startup_energy_wh: float | None = None
    activity_threshold_status: AssumptionStatus = AssumptionStatus.MODEL_ASSUMPTION
    timing_status: AssumptionStatus = AssumptionStatus.MODEL_ASSUMPTION
    idle_power_status: AssumptionStatus = AssumptionStatus.UNKNOWN
    startup_energy_status: AssumptionStatus = AssumptionStatus.UNKNOWN

    def validate(self) -> None:
        if not self.battery_name:
            raise ValueError("DEYE battery name is required")
        if not isfinite(self.activity_threshold_kwh_per_slot) or self.activity_threshold_kwh_per_slot < 0:
            raise ValueError("activity threshold must be finite and non-negative")
        for name, value in (
            ("startup lead time", self.startup_lead_time),
            ("minimum on time", self.minimum_on_time),
            ("minimum off time", self.minimum_off_time),
            ("shutdown delay", self.shutdown_delay),
        ):
            if value < timedelta(0):
                raise ValueError(f"{name} must be non-negative")
        for name, value in (
            ("idle power", self.deye_on_idle_power_w),
            ("startup energy", self.deye_startup_energy_wh),
        ):
            if value is not None and (not isfinite(value) or value < 0):
                raise ValueError(f"DEYE {name} must be finite and non-negative")
        if self.deye_on_idle_power_w is None and self.idle_power_status is not AssumptionStatus.UNKNOWN:
            raise ValueError("idle power without a value must have UNKNOWN status")
        if self.deye_on_idle_power_w is not None and self.idle_power_status is AssumptionStatus.UNKNOWN:
            raise ValueError("configured idle power must have an explicit non-UNKNOWN status")
        if self.deye_startup_energy_wh is None and self.startup_energy_status is not AssumptionStatus.UNKNOWN:
            raise ValueError("startup energy without a value must have UNKNOWN status")
        if self.deye_startup_energy_wh is not None and self.startup_energy_status is AssumptionStatus.UNKNOWN:
            raise ValueError("configured startup energy must have an explicit non-UNKNOWN status")


@dataclass(frozen=True)
class DeyePowerStateSlot:
    timestamp: datetime
    deye_power_state: DeyePowerState
    deye_required: bool
    reason: str
    next_activity_at: datetime | None
    telemetry_expectation: DeyeTelemetryExpectation
    activity_kwh: float
    overlay_conflict: bool


@dataclass(frozen=True)
class OffEligibleWindow:
    off_from: datetime
    on_again_by: datetime | None
    next_required_activity: datetime | None
    off_duration: timedelta


@dataclass(frozen=True)
class DeyePowerStateSchedule:
    slots: tuple[DeyePowerStateSlot, ...]
    off_windows: tuple[OffEligibleWindow, ...]
    total_on_duration: timedelta
    total_off_eligible_duration: timedelta
    number_of_on_transitions: int
    number_of_off_transitions: int
    longest_off_window: timedelta
    estimated_idle_energy_saved_kwh: float | None
    activity_threshold_status: AssumptionStatus
    timing_status: AssumptionStatus
    idle_power_status: AssumptionStatus
    startup_energy_status: AssumptionStatus


def build_deye_power_state_schedule(
    plan: PlannerResult | tuple[TradingSlotPlan, ...],
    config: DeyePowerStateConfig | None = None,
) -> DeyePowerStateSchedule:
    """Overlay power-state eligibility without modifying the trading plan."""

    config = config or DeyePowerStateConfig()
    config.validate()
    slots = plan.slots if isinstance(plan, PlannerResult) else plan
    _validate_slots(slots, config.battery_name)

    activity = tuple(_activity_kwh(slot, config.battery_name) for slot in slots)
    active_indexes = tuple(
        index for index, value in enumerate(activity) if value > config.activity_threshold_kwh_per_slot
    )
    on_blocks = _required_on_blocks(len(slots), active_indexes, config)
    required_indexes = {index for start, end in on_blocks for index in range(start, end + 1)}
    lead_slots = _duration_slots(config.startup_lead_time)

    annotated: list[DeyePowerStateSlot] = []
    for index, slot in enumerate(slots):
        next_index = next((item for item in active_indexes if item >= index), None)
        next_activity = slots[next_index].timestamp if next_index is not None else None
        has_activity = index in active_indexes
        required = index in required_indexes
        starting = required and not has_activity and next_index is not None and 0 < next_index - index <= lead_slots
        if has_activity:
            state = DeyePowerState.ON_REQUIRED
            reason = "PLANNED_DEYE_ACTIVITY"
        elif starting:
            state = DeyePowerState.STARTING
            reason = "STARTUP_LEAD_MODEL_ASSUMPTION"
        elif required:
            state = DeyePowerState.READY
            reason = "MINIMUM_ON_SHUTDOWN_OR_ANTI_CYCLING_MODEL_ASSUMPTION"
        else:
            state = DeyePowerState.OFF_ELIGIBLE
            reason = "NO_ACTIVITY_AND_OFF_CONDITIONS_SATISFIED"
        conflict = has_activity and state in (DeyePowerState.OFF_ELIGIBLE, DeyePowerState.OFF)
        annotated.append(
            DeyePowerStateSlot(
                timestamp=slot.timestamp,
                deye_power_state=state,
                deye_required=required,
                reason=reason,
                next_activity_at=next_activity,
                telemetry_expectation=(
                    DeyeTelemetryExpectation.DEYE_EXPECTED_OFF
                    if state is DeyePowerState.OFF_ELIGIBLE
                    else DeyeTelemetryExpectation.DEYE_EXPECTED_ON
                ),
                activity_kwh=activity[index],
                overlay_conflict=conflict,
            )
        )

    off_windows = _off_windows(tuple(annotated), slots, active_indexes)
    off_slots = sum(item.deye_power_state is DeyePowerState.OFF_ELIGIBLE for item in annotated)
    on_slots = len(annotated) - off_slots
    on_transitions, off_transitions = _transition_counts(tuple(annotated))
    saved = _estimated_saved_energy(off_slots, on_transitions, config)
    return DeyePowerStateSchedule(
        slots=tuple(annotated),
        off_windows=off_windows,
        total_on_duration=_SLOT * on_slots,
        total_off_eligible_duration=_SLOT * off_slots,
        number_of_on_transitions=on_transitions,
        number_of_off_transitions=off_transitions,
        longest_off_window=max((window.off_duration for window in off_windows), default=timedelta(0)),
        estimated_idle_energy_saved_kwh=saved,
        activity_threshold_status=config.activity_threshold_status,
        timing_status=config.timing_status,
        idle_power_status=config.idle_power_status,
        startup_energy_status=config.startup_energy_status,
    )


def _validate_slots(slots: tuple[TradingSlotPlan, ...], battery_name: str) -> None:
    if not slots:
        raise ValueError("at least one trading-plan slot is required")
    if any(right.timestamp - left.timestamp != _SLOT for left, right in zip(slots, slots[1:], strict=False)):
        raise ValueError("power-state overlay requires continuous 15-minute slots")
    if any(battery_name not in slot.batteries for slot in slots):
        raise ValueError(f"trading plan is missing battery: {battery_name}")


def _activity_kwh(slot: TradingSlotPlan, battery_name: str) -> float:
    battery = slot.batteries[battery_name]
    values = (
        battery.charge_from_pv_kwh,
        battery.discharge_to_load_kwh,
        battery.discharge_to_export_kwh,
    )
    if not all(isfinite(value) and value >= 0 for value in values):
        raise ValueError("DEYE planned activity must be finite and non-negative")
    return sum(values)


def _duration_slots(value: timedelta) -> int:
    return ceil(value / _SLOT) if value > timedelta(0) else 0


def _required_on_blocks(
    slot_count: int,
    active_indexes: tuple[int, ...],
    config: DeyePowerStateConfig,
) -> tuple[tuple[int, int], ...]:
    if not active_indexes:
        return ()
    lead = _duration_slots(config.startup_lead_time)
    shutdown = _duration_slots(config.shutdown_delay)
    minimum_on = max(1, _duration_slots(config.minimum_on_time))
    blocks: list[tuple[int, int]] = []
    for index in active_indexes:
        start = max(0, index - lead)
        end = min(slot_count - 1, max(index + shutdown, start + minimum_on - 1))
        if blocks and start <= blocks[-1][1] + 1:
            blocks[-1] = (blocks[-1][0], max(blocks[-1][1], end))
        else:
            blocks.append((start, end))

    minimum_off = _duration_slots(config.minimum_off_time)
    merged: list[tuple[int, int]] = []
    for block in blocks:
        if merged and block[0] - merged[-1][1] - 1 < minimum_off:
            merged[-1] = (merged[-1][0], max(merged[-1][1], block[1]))
        else:
            merged.append(block)
    return tuple(merged)


def _off_windows(
    annotated: tuple[DeyePowerStateSlot, ...],
    slots: tuple[TradingSlotPlan, ...],
    active_indexes: tuple[int, ...],
) -> tuple[OffEligibleWindow, ...]:
    windows: list[OffEligibleWindow] = []
    index = 0
    while index < len(annotated):
        if annotated[index].deye_power_state is not DeyePowerState.OFF_ELIGIBLE:
            index += 1
            continue
        start = index
        while index + 1 < len(annotated) and annotated[index + 1].deye_power_state is DeyePowerState.OFF_ELIGIBLE:
            index += 1
        end = index
        on_again_by = slots[end + 1].timestamp if end + 1 < len(slots) else None
        next_active = next((item for item in active_indexes if item > end), None)
        windows.append(
            OffEligibleWindow(
                off_from=slots[start].timestamp,
                on_again_by=on_again_by,
                next_required_activity=slots[next_active].timestamp if next_active is not None else None,
                off_duration=_SLOT * (end - start + 1),
            )
        )
        index += 1
    return tuple(windows)


def _transition_counts(slots: tuple[DeyePowerStateSlot, ...]) -> tuple[int, int]:
    on_transitions = 0
    off_transitions = 0
    for previous, current in zip(slots, slots[1:], strict=False):
        previous_off = previous.deye_power_state is DeyePowerState.OFF_ELIGIBLE
        current_off = current.deye_power_state is DeyePowerState.OFF_ELIGIBLE
        on_transitions += int(previous_off and not current_off)
        off_transitions += int(not previous_off and current_off)
    return on_transitions, off_transitions


def _estimated_saved_energy(
    off_slots: int,
    on_transitions: int,
    config: DeyePowerStateConfig,
) -> float | None:
    if config.deye_on_idle_power_w is None:
        return None
    gross_kwh = config.deye_on_idle_power_w * (_SLOT.total_seconds() / 3_600) * off_slots / 1_000
    startup_kwh = (config.deye_startup_energy_wh or 0) * on_transitions / 1_000
    return max(0.0, gross_kwh - startup_kwh)
