"""Read-only interpretation of DEYE feedback; never issues startup/shutdown commands."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .models import StrEnum
from .observations import communication_time, observation_time, transition_time


class DeyeOperatingState(StrEnum):
    INTENTIONAL_OFF = "INTENTIONAL_OFF"
    STOPPING = "STOPPING"
    STARTING = "STARTING"
    READY = "READY"
    UNEXPECTED_FAULT = "UNEXPECTED_FAULT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DeyeStateConfig:
    startup_window_s: float = 300.0
    ready_confirmation_s: float = 10.0
    shutdown_settle_s: float = 20.0
    zero_tolerance_w: float = 10.0
    feedback_max_age_s: float = 30.0
    maximum_skew_s: float = 20.0

    def __post_init__(self) -> None:
        for key, value in vars(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"deye_state.{key} must be finite and greater than zero")
        if self.ready_confirmation_s >= self.startup_window_s:
            raise ValueError("DEYE ready confirmation must be shorter than startup window")


@dataclass(frozen=True)
class DeyeAssessment:
    state: DeyeOperatingState
    reason: str
    observed_at: datetime | None = None

    @property
    def dispatch_ready(self) -> bool:
        return self.state is DeyeOperatingState.READY


class DeyeStateAdapter:
    def __init__(self, config: DeyeStateConfig | None = None) -> None:
        self.config = config or DeyeStateConfig()
        self._transition: tuple[str, datetime] | None = None
        self._zero_since: datetime | None = None
        self._normal_since: datetime | None = None
        self._was_ready = False
        self._last_observed: datetime | None = None
        self._last_switch: tuple[str, datetime] | None = None

    def evaluate(self, feedback: Mapping[str, object], now: datetime) -> DeyeAssessment:
        def result(state: DeyeOperatingState, reason: str, at: datetime | None = None) -> DeyeAssessment:
            return DeyeAssessment(state, reason, at)

        stamps = {
            key: observation_time(feedback.get(key), now) for key in ("switch", "connection", "state", "fault", "power")
        }
        stamps["connection"] = communication_time(feedback.get("connection"), now)
        if any(at is None or (now - at).total_seconds() > self.config.feedback_max_age_s for at in stamps.values()):
            self._zero_since = self._normal_since = None
            return result(DeyeOperatingState.UNAVAILABLE, "Missing, stale or invalid DEYE feedback timestamps")
        times = [at for at in stamps.values() if at is not None]
        observed = min(times)
        if (max(times) - observed).total_seconds() > self.config.maximum_skew_s:
            self._zero_since = self._normal_since = None
            return result(DeyeOperatingState.UNAVAILABLE, "DEYE feedback is incoherent", observed)
        if self._last_observed is not None and (
            observed < self._last_observed
            or (observed - self._last_observed).total_seconds() > self.config.feedback_max_age_s
        ):
            self._zero_since = self._normal_since = None
        self._last_observed = observed
        values = {key: raw.get("state") if isinstance(raw, dict) else None for key, raw in feedback.items()}
        switch = values.get("switch")
        changed = transition_time(feedback.get("switch"), now)
        if switch not in ("on", "off") or values.get("connection") != "on":
            self._zero_since = self._normal_since = None
            return result(DeyeOperatingState.UNAVAILABLE, "Switch/connection feedback is unavailable", observed)
        previous_switch = self._last_switch
        switch_observed = stamps["switch"]
        assert switch_observed is not None
        self._last_switch = (switch, switch_observed)
        if changed is None:
            if previous_switch is not None and previous_switch[0] != switch and switch_observed > previous_switch[1]:
                # The edge occurred between two reports. Use the earlier bound so
                # polling latency cannot extend the startup allowance.
                changed = previous_switch[1]
            elif self._transition is not None and self._transition[0] == switch:
                changed = self._transition[1]
            else:
                self._zero_since = self._normal_since = None
                return result(DeyeOperatingState.UNAVAILABLE, "Switch transition time is unverified", observed)
        # A real transition, not a process restart or reconnect, defines startup time.
        transition = (switch, changed)
        if transition != self._transition:
            self._transition = transition
            self._zero_since = self._normal_since = None
            self._was_ready = False
            power_changed = transition_time(feedback.get("power"), now)
            state_changed = transition_time(feedback.get("state"), now)
            fault_changed = transition_time(feedback.get("fault"), now)
            if power_changed is not None:
                self._zero_since = max(changed, power_changed)
            if state_changed is not None and fault_changed is not None:
                self._normal_since = max(changed, state_changed, fault_changed)
        try:
            power = float(values.get("power"))
        except (ValueError, TypeError):
            power = math.nan
        state, fault = values.get("state"), values.get("fault")
        if (
            not math.isfinite(power)
            or state not in ("Standby", "Self-test", "Normal", "Alarm", "Fault")
            or fault in (None, "", "unknown", "unavailable")
        ):
            self._zero_since = self._normal_since = None
            return result(DeyeOperatingState.UNAVAILABLE, "Invalid DEYE power/device feedback", observed)
        if switch == "off":
            self._normal_since = None
            if fault not in ("OK", "Tz_Integ_Fault failure"):
                self._zero_since = None
                return result(DeyeOperatingState.UNEXPECTED_FAULT, f"Unrecognized fault while OFF: {fault}", observed)
            if abs(power) > self.config.zero_tolerance_w:
                self._zero_since = None
                return result(DeyeOperatingState.STOPPING, "OFF output has not settled near zero", observed)
            self._zero_since = self._zero_since or observed
            power_changed = transition_time(feedback.get("power"), now)
            if power_changed is not None:
                # A newer zero entry can reveal an excursion between polling ticks.
                self._zero_since = max(self._zero_since, power_changed)
            if (observed - self._zero_since).total_seconds() < self.config.shutdown_settle_s:
                return result(DeyeOperatingState.STOPPING, "Confirming measured near-zero output after OFF", observed)
            return result(
                DeyeOperatingState.INTENTIONAL_OFF, "Fresh OFF feedback and settled measured output", observed
            )
        self._zero_since = None
        normal = state == "Normal" and fault == "OK"
        if normal:
            self._normal_since = self._normal_since or observed
            for key in ("state", "fault"):
                entered = transition_time(feedback.get(key), now)
                if entered is not None:
                    self._normal_since = max(self._normal_since, entered)
            if (observed - self._normal_since).total_seconds() >= self.config.ready_confirmation_s:
                self._was_ready = True
                return result(DeyeOperatingState.READY, "Normal state and coherent feedback confirmed", observed)
            return result(DeyeOperatingState.STARTING, "Confirming Normal readiness", observed)
        self._normal_since = None
        elapsed = (now - changed).total_seconds()
        if self._was_ready or elapsed >= self.config.startup_window_s:
            return result(
                DeyeOperatingState.UNEXPECTED_FAULT, f"Not ready outside startup allowance: {state}; {fault}", observed
            )
        return result(DeyeOperatingState.STARTING, f"Bounded startup in progress: {state}; {fault}", observed)
