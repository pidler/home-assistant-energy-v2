from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import (
    BatteryParameters,
    BatteryRole,
    CheckpointType,
    MorningRecoveryAssessment,
    PlannerConfig,
    PlannerInput,
    SocCheckpoint,
)


@dataclass(frozen=True)
class ResolvedSocPolicy:
    checkpoints: tuple[SocCheckpoint, ...]
    morning_recovery: dict[str, MorningRecoveryAssessment]
    state_floors_pct: dict[str, tuple[float, ...]]
    recovery_export_blocked_indexes: dict[str, frozenset[int]]


def resolve_soc_policy(
    data: PlannerInput,
    config: PlannerConfig,
    *,
    morning_exception_enabled: bool,
) -> ResolvedSocPolicy:
    """Resolve role policy for one deterministic LP candidate.

    The normal candidate blocks SolaX trading export during the morning window
    while allowing house-load discharge down to the physical floor. The exception candidate permits
    15% inside the configured trading window and adds a hard 30% recovery
    checkpoint. Complete LP feasibility decides whether recovery is possible.
    """

    batteries = {battery.name: battery for battery in data.batteries}
    state_timestamps = tuple(slot.timestamp for slot in data.slots) + (
        data.slots[-1].timestamp + timedelta(hours=config.slot_hours),
    )
    floors = {name: [battery.minimum_soc_pct] * len(state_timestamps) for name, battery in batteries.items()}
    checkpoints = list(data.soc_checkpoints)
    recovery: dict[str, MorningRecoveryAssessment] = {}
    blocked: dict[str, set[int]] = {name: set() for name in batteries}

    house_batteries = [battery for battery in data.batteries if battery.role is BatteryRole.HOUSE_RESERVE_BATTERY]
    if len(house_batteries) > 1:
        raise ValueError("Phase 5A supports one HOUSE_RESERVE_BATTERY")
    if house_batteries:
        house = house_batteries[0]
        checkpoints.extend(_evening_checkpoints(data, config, house, state_timestamps))
        for assessment in _morning_assessments(data, config, house, state_timestamps):
            key = f"{house.name}:{assessment.trading_window_start.date().isoformat()}"
            recovery[key] = assessment
            _apply_morning_floor(
                floors[house.name],
                state_timestamps,
                assessment,
                morning_exception_enabled=morning_exception_enabled,
            )
            blocked[house.name].update(
                index
                for index, slot in enumerate(data.slots)
                if (
                    assessment.trading_window_end <= slot.timestamp < assessment.recovery_deadline
                    or (
                        not morning_exception_enabled
                        and assessment.trading_window_start <= slot.timestamp < assessment.trading_window_end
                    )
                )
            )
            if morning_exception_enabled:
                checkpoints.append(
                    SocCheckpoint(
                        battery_name=house.name,
                        timestamp=assessment.recovery_deadline,
                        minimum_soc_pct=assessment.recovery_target_pct,
                        checkpoint_type=CheckpointType.RECOVERY_TARGET,
                        reason="Restore SolaX house reserve after conditional morning trading",
                        hard=True,
                    )
                )

    checkpoints.sort(key=lambda item: (item.timestamp, item.battery_name, item.checkpoint_type.value))
    return ResolvedSocPolicy(
        checkpoints=tuple(checkpoints),
        morning_recovery=recovery,
        state_floors_pct={name: tuple(values) for name, values in floors.items()},
        recovery_export_blocked_indexes={name: frozenset(values) for name, values in blocked.items()},
    )


def has_configured_morning_policy(data: PlannerInput, config: PlannerConfig) -> bool:
    return bool(
        any(battery.role is BatteryRole.HOUSE_RESERVE_BATTERY for battery in data.batteries)
        and config.solax_morning_trading_start_local_time is not None
    )


def morning_exception_precheck_passes(data: PlannerInput, config: PlannerConfig) -> bool:
    """Conservative gate before solving the exception candidate.

    This gate accounts for whole-site load, efficiency, duration and per-slot
    charge power. The candidate LP then proves recovery using actual allocations.
    """

    policy = resolve_soc_policy(data, config, morning_exception_enabled=True)
    assessments = tuple(policy.morning_recovery.values())
    return bool(assessments) and all(
        assessment.maximum_storable_recovery_kwh + 1e-9 >= assessment.required_recovery_kwh
        for assessment in assessments
    )


def _evening_checkpoints(
    data: PlannerInput,
    config: PlannerConfig,
    battery: BatteryParameters,
    state_timestamps: tuple[datetime, ...],
) -> list[SocCheckpoint]:
    checkpoint_time = config.solax_evening_checkpoint_local_time
    if checkpoint_time is None:
        return []
    checkpoints: list[SocCheckpoint] = []
    for day in _days(state_timestamps):
        timestamp = _first_state_at_or_after(day, checkpoint_time, state_timestamps)
        if timestamp is None:
            continue
        target = config.solax_evening_reserve_soc_pct
        reachable = _optimistic_soc_at(data, config, battery, timestamp) + 1e-9 >= target
        checkpoints.append(
            SocCheckpoint(
                battery_name=battery.name,
                timestamp=timestamp,
                minimum_soc_pct=target,
                checkpoint_type=CheckpointType.EVENING_RESERVE,
                reason="SolaX evening house-reserve checkpoint",
                hard=reachable,
            )
        )
    return checkpoints


def _morning_assessments(
    data: PlannerInput,
    config: PlannerConfig,
    battery: BatteryParameters,
    state_timestamps: tuple[datetime, ...],
) -> list[MorningRecoveryAssessment]:
    start_time = config.solax_morning_trading_start_local_time
    end_time = config.solax_morning_trading_end_local_time
    deadline_time = config.solax_recovery_deadline_local_time
    if start_time is None or end_time is None or deadline_time is None:
        return []
    assessments: list[MorningRecoveryAssessment] = []
    for day in _days(state_timestamps):
        start = _first_state_at_or_after(day, start_time, state_timestamps)
        end = _first_state_at_or_after(day, end_time, state_timestamps)
        deadline = _first_state_at_or_after(day, deadline_time, state_timestamps)
        if start is None or end is None or deadline is None or not start < end < deadline:
            continue
        recovery_slots = [slot for slot in data.slots if end <= slot.timestamp < deadline]
        gross_surplus = sum(max(slot.pv_forecast_kwh - slot.load_forecast_kwh, 0.0) for slot in recovery_slots)
        charge_cap_kwh = battery.max_charge_power_w / 1000 * config.slot_hours
        maximum_storable = sum(
            min(max(slot.pv_forecast_kwh - slot.load_forecast_kwh, 0.0), charge_cap_kwh) * battery.charge_efficiency
            for slot in recovery_slots
        )
        required = (
            battery.capacity_kwh
            * (config.solax_recovery_target_soc_pct - config.solax_morning_conditional_floor_pct)
            / 100
        )
        assessments.append(
            MorningRecoveryAssessment(
                battery_name=battery.name,
                trading_window_start=start,
                trading_window_end=end,
                recovery_deadline=deadline,
                conditional_floor_pct=config.solax_morning_conditional_floor_pct,
                recovery_target_pct=config.solax_recovery_target_soc_pct,
                forecast_pv_surplus_for_recovery_kwh=gross_surplus,
                maximum_storable_recovery_kwh=maximum_storable,
                required_recovery_kwh=required,
                candidate_feasible=False,
                selected=False,
                morning_start_soc_pct=0.0,
                minimum_projected_soc_pct=0.0,
                expected_recovery_soc_pct=0.0,
                expected_recovery_time=None,
            )
        )
    return assessments


def _apply_morning_floor(
    floors: list[float],
    state_timestamps: tuple[datetime, ...],
    assessment: MorningRecoveryAssessment,
    *,
    morning_exception_enabled: bool,
) -> None:
    if not morning_exception_enabled:
        return
    for index, timestamp in enumerate(state_timestamps):
        if assessment.trading_window_start <= timestamp <= assessment.trading_window_end:
            floors[index] = max(floors[index], assessment.conditional_floor_pct)


def _first_state_at_or_after(
    day: date,
    configured_time: time,
    state_timestamps: tuple[datetime, ...],
) -> datetime | None:
    candidates = [
        timestamp
        for timestamp in state_timestamps
        if timestamp.date() == day and timestamp.timetz().replace(tzinfo=None) >= configured_time
    ]
    return min(candidates, default=None)


def _optimistic_soc_at(
    data: PlannerInput,
    config: PlannerConfig,
    battery: BatteryParameters,
    timestamp: datetime,
) -> float:
    charge_cap_kwh = battery.max_charge_power_w / 1000 * config.slot_hours
    stored = battery.capacity_kwh * data.initial_soc_pct[battery.name] / 100
    for slot in data.slots:
        if slot.timestamp >= timestamp:
            break
        usable_surplus = max(slot.pv_forecast_kwh - slot.load_forecast_kwh, 0.0)
        stored += min(usable_surplus, charge_cap_kwh) * battery.charge_efficiency
    return min(100.0, 100 * stored / battery.capacity_kwh)


def _days(state_timestamps: tuple[datetime, ...]) -> tuple[date, ...]:
    return tuple(sorted({timestamp.date() for timestamp in state_timestamps}))
