"""Pure, shadow-only DEYE surplus-charge state machine.

This module deliberately has no AppDaemon dependency and no service-call capability.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from math import floor, isfinite

from .models import ChargeShadowState


@dataclass(frozen=True)
class ChargeControllerParameters:
    start_solax_soc_pct: float = 90.0
    stop_solax_soc_pct: float = 80.0
    minimum_pv_power_w: float = 1000.0
    start_confirmation_s: float = 30.0
    maximum_deye_charge_current_a: float = 240.0
    solax_priority_deye_current_a: float = 0.0
    solax_discharge_tolerance_w: float = 300.0
    actual_deye_charge_threshold_w: float = 300.0
    transfer_confirmation_s: float = 10.0
    transfer_average_window_s: float = 10.0
    safe_power_reserve_w: float = 300.0
    minimum_safe_deye_charge_power_w: float = 300.0
    return_confirmation_s: float = 30.0
    conflict_attempt_limit: int = 3
    conflict_window_s: float = 60.0
    fault_recovery_s: float = 60.0


def validate_charge_controller_parameters(p: ChargeControllerParameters) -> tuple[str, ...]:
    errors: list[str] = []
    if not 0 <= p.stop_solax_soc_pct < p.start_solax_soc_pct <= 100:
        errors.append("start_solax_soc_pct must be greater than stop_solax_soc_pct in 0..100")
    for name in (
        "minimum_pv_power_w",
        "start_confirmation_s",
        "maximum_deye_charge_current_a",
        "solax_priority_deye_current_a",
        "solax_discharge_tolerance_w",
        "actual_deye_charge_threshold_w",
        "transfer_confirmation_s",
        "transfer_average_window_s",
        "safe_power_reserve_w",
        "minimum_safe_deye_charge_power_w",
        "return_confirmation_s",
        "conflict_window_s",
        "fault_recovery_s",
    ):
        value = getattr(p, name)
        if not isfinite(value) or value < 0 or (name.endswith("_s") and value == 0):
            errors.append(f"{name} must be finite and positive")
    if not 0 <= p.maximum_deye_charge_current_a <= 240:
        errors.append("maximum_deye_charge_current_a must be in 0..240")
    if not 0 <= p.solax_priority_deye_current_a <= p.maximum_deye_charge_current_a:
        errors.append("solax_priority_deye_current_a is outside permitted current range")
    if p.conflict_attempt_limit < 1:
        errors.append("conflict_attempt_limit must be at least one")
    return tuple(errors)


@dataclass(frozen=True)
class ChargeTelemetry:
    timestamp: datetime
    solax_soc_pct: float | None
    solax_pv_power_w: float | None
    solax_battery_power_w: float | None  # + charge / - discharge
    deye_battery_power_w: float | None  # normalized: + charge / - discharge
    deye_battery_voltage_v: float | None


@dataclass(frozen=True)
class ChargeShadowDecision:
    state: ChargeShadowState
    recommended_current_a: float
    reason: str
    block_reason: str
    seconds_to_transition: float | None
    average_solax_discharge_w: float | None = None
    average_deye_charge_w: float | None = None
    average_deye_voltage_v: float | None = None
    safe_power_w: float | None = None
    calculated_current_a: float | None = None


class TimeWeightedChargeWindow:
    """Last-value-held, time-weighted window robust to irregular ticks."""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self.samples: deque[tuple[datetime, float, float, float]] = deque()

    def add(self, at: datetime, solax_discharge_w: float, deye_charge_w: float, voltage_v: float) -> None:
        if not all(isfinite(v) for v in (solax_discharge_w, deye_charge_w, voltage_v)):
            return
        self.samples.append((at, solax_discharge_w, deye_charge_w, voltage_v))
        cutoff = at.timestamp() - self.window_s * 2
        while len(self.samples) > 1 and self.samples[1][0].timestamp() < cutoff:
            self.samples.popleft()

    def averages(self, now: datetime) -> tuple[float | None, float | None, float | None]:
        if len(self.samples) < 2:
            return (None, None, None)
        start = now.timestamp() - self.window_s
        weighted = [0.0, 0.0, 0.0]
        covered = 0.0
        values = list(self.samples)
        for index, (at, discharge, charge, voltage) in enumerate(values):
            end = values[index + 1][0].timestamp() if index + 1 < len(values) else now.timestamp()
            begin = max(at.timestamp(), start)
            duration = max(0.0, end - begin)
            if duration:
                covered += duration
                for target, value in enumerate((discharge, charge, voltage)):
                    weighted[target] += value * duration
        if covered <= 0:
            return (None, None, None)
        return tuple(value / covered for value in weighted)  # type: ignore[return-value]


class DeyeChargeShadowController:
    def __init__(self, parameters: ChargeControllerParameters | None = None) -> None:
        self.parameters = parameters or ChargeControllerParameters()
        self.state = ChargeShadowState.DISABLED
        self.state_since: datetime | None = None
        self.window = TimeWeightedChargeWindow(self.parameters.transfer_average_window_s)
        self.limited_current_a = 0.0

    def evaluate(self, telemetry: ChargeTelemetry, *, enabled: bool) -> ChargeShadowDecision:
        p = self.parameters
        errors = validate_charge_controller_parameters(p)
        if not enabled:
            return self._transition(
                ChargeShadowState.DISABLED, telemetry.timestamp, 0, "Shadow charge controller is disabled"
            )
        if errors or not self._telemetry_valid(telemetry):
            return self._transition(
                ChargeShadowState.FAULT,
                telemetry.timestamp,
                0,
                "; ".join(errors) or "Required charge telemetry is invalid",
            )
        assert telemetry.solax_soc_pct is not None
        assert telemetry.solax_pv_power_w is not None
        assert telemetry.solax_battery_power_w is not None
        assert telemetry.deye_battery_power_w is not None
        assert telemetry.deye_battery_voltage_v is not None
        discharge = max(-telemetry.solax_battery_power_w, 0.0)
        deye_charge = max(telemetry.deye_battery_power_w, 0.0)
        self.window.add(telemetry.timestamp, discharge, deye_charge, telemetry.deye_battery_voltage_v)
        averages = self.window.averages(telemetry.timestamp)
        eligible = telemetry.solax_soc_pct > p.start_solax_soc_pct and telemetry.solax_pv_power_w > p.minimum_pv_power_w
        transfer = discharge > p.solax_discharge_tolerance_w and deye_charge > p.actual_deye_charge_threshold_w

        if telemetry.solax_soc_pct < p.stop_solax_soc_pct:
            return self._transition(
                ChargeShadowState.SOLAX_PRIORITY,
                telemetry.timestamp,
                p.solax_priority_deye_current_a,
                "SolaX SOC is below stop threshold",
            )
        if self.state is ChargeShadowState.FAULT and self._elapsed(telemetry.timestamp) < p.fault_recovery_s:
            return self._decision(
                0, "Waiting for fault recovery", p.fault_recovery_s - self._elapsed(telemetry.timestamp)
            )
        if self.state in {
            ChargeShadowState.DISABLED,
            ChargeShadowState.WAITING_FOR_SOLAX,
            ChargeShadowState.SOLAX_PRIORITY,
            ChargeShadowState.FAULT,
        }:
            if not eligible:
                return self._transition(
                    ChargeShadowState.WAITING_FOR_SOLAX, telemetry.timestamp, 0, self._waiting_reason(telemetry)
                )
            return self._transition(
                ChargeShadowState.START_CONFIRMATION, telemetry.timestamp, 0, "Waiting for start confirmation"
            )
        if self.state is ChargeShadowState.START_CONFIRMATION:
            if not eligible:
                return self._transition(
                    ChargeShadowState.WAITING_FOR_SOLAX, telemetry.timestamp, 0, self._waiting_reason(telemetry)
                )
            if self._elapsed(telemetry.timestamp) >= p.start_confirmation_s:
                return self._transition(
                    ChargeShadowState.CHARGE_DEYE_FULL,
                    telemetry.timestamp,
                    p.maximum_deye_charge_current_a,
                    "Start conditions confirmed",
                )
            return self._decision(
                0, "Waiting for start confirmation", p.start_confirmation_s - self._elapsed(telemetry.timestamp)
            )
        if self.state is ChargeShadowState.CHARGE_DEYE_FULL:
            if transfer:
                return self._transition(
                    ChargeShadowState.TRANSFER_CONFIRMATION,
                    telemetry.timestamp,
                    p.maximum_deye_charge_current_a,
                    "Possible SolaX to DEYE transfer",
                )
            return self._decision(
                p.maximum_deye_charge_current_a, "Full DEYE charge recommended; low PV alone does not reduce current"
            )
        if self.state is ChargeShadowState.TRANSFER_CONFIRMATION:
            if not transfer:
                return self._transition(
                    ChargeShadowState.CHARGE_DEYE_FULL,
                    telemetry.timestamp,
                    p.maximum_deye_charge_current_a,
                    "Transfer condition cleared",
                )
            if self._elapsed(telemetry.timestamp) < p.transfer_confirmation_s:
                return self._decision(
                    p.maximum_deye_charge_current_a,
                    "Confirming possible transfer",
                    p.transfer_confirmation_s - self._elapsed(telemetry.timestamp),
                )
            return self._limited(telemetry.timestamp, averages)
        if self.state is ChargeShadowState.CHARGE_DEYE_LIMITED:
            return self._transition(
                ChargeShadowState.RETURN_CONFIRMATION,
                telemetry.timestamp,
                self.limited_current_a,
                "Limited current retained while testing return",
            )
        if self.state is ChargeShadowState.RETURN_CONFIRMATION:
            if transfer or telemetry.solax_pv_power_w <= p.minimum_pv_power_w:
                return self._decision(self.limited_current_a, "Return confirmation reset by transfer or low PV")
            if self._elapsed(telemetry.timestamp) >= p.return_confirmation_s:
                return self._transition(
                    ChargeShadowState.CHARGE_DEYE_FULL,
                    telemetry.timestamp,
                    p.maximum_deye_charge_current_a,
                    "Return conditions confirmed",
                )
            return self._decision(
                self.limited_current_a,
                "Waiting for return confirmation",
                p.return_confirmation_s - self._elapsed(telemetry.timestamp),
            )
        return self._transition(ChargeShadowState.FAULT, telemetry.timestamp, 0, "Unhandled state")

    def _limited(
        self, now: datetime, averages: tuple[float | None, float | None, float | None]
    ) -> ChargeShadowDecision:
        discharge, charge, voltage = averages
        if None in averages or voltage is None or voltage <= 0:
            return self._transition(
                ChargeShadowState.FAULT, now, 0, "Insufficient valid rolling samples for safe limit"
            )
        safe_power = charge - discharge - self.parameters.safe_power_reserve_w
        raw_current = safe_power / voltage
        current = 0.0 if safe_power < self.parameters.minimum_safe_deye_charge_power_w else float(floor(raw_current))
        current = min(max(current, 0.0), self.parameters.maximum_deye_charge_current_a)
        self.limited_current_a = current
        self._transition(ChargeShadowState.CHARGE_DEYE_LIMITED, now, current, "Confirmed SolaX to DEYE transfer")
        return ChargeShadowDecision(
            self.state,
            current,
            "Confirmed transfer; current derived from actual charge power",
            "",
            None,
            discharge,
            charge,
            voltage,
            safe_power,
            raw_current,
        )

    def _telemetry_valid(self, t: ChargeTelemetry) -> bool:
        values = (
            t.solax_soc_pct,
            t.solax_pv_power_w,
            t.solax_battery_power_w,
            t.deye_battery_power_w,
            t.deye_battery_voltage_v,
        )
        return (
            all(v is not None and isfinite(v) for v in values)
            and 0 <= (t.solax_soc_pct or -1) <= 100
            and (t.deye_battery_voltage_v or 0) > 0
        )

    def _waiting_reason(self, t: ChargeTelemetry) -> str:
        if (t.solax_soc_pct or 0) <= self.parameters.start_solax_soc_pct:
            return "Waiting for SolaX SOC above start threshold"
        return "Waiting for PV power above minimum threshold"

    def _elapsed(self, now: datetime) -> float:
        return max(0.0, (now - (self.state_since or now)).total_seconds())

    def _transition(self, state: ChargeShadowState, now: datetime, current: float, reason: str) -> ChargeShadowDecision:
        if state is not self.state:
            self.state = state
            self.state_since = now
        return self._decision(current, reason)

    def _decision(self, current: float, reason: str, seconds: float | None = None) -> ChargeShadowDecision:
        return ChargeShadowDecision(self.state, current, reason, "", max(0.0, seconds) if seconds is not None else None)
