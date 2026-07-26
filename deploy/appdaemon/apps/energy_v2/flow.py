from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import FlowState, TelemetrySnapshot


@dataclass(frozen=True)
class SignConventions:
    solax_battery_charging_positive: bool = True
    deye_battery_discharging_positive: bool = True
    deye_grid_import_positive: bool = True


@dataclass(frozen=True)
class FlowThresholds:
    grid_import_warning_w: float = 200.0
    grid_import_violation_w: float = 500.0
    battery_flow_warning_w: float = 300.0
    battery_flow_violation_w: float = 500.0
    minimum_deye_charge_w: float = 300.0
    minimum_export_w: float = 300.0
    pv_surplus_reserve_w: float = 500.0
    warning_persistence_s: float = 5.0
    violation_persistence_s: float = 10.0


@dataclass(frozen=True)
class FlowSnapshot:
    grid_import_w: float
    grid_export_w: float
    solax_battery_charging_w: float
    solax_battery_discharging_w: float
    deye_battery_charging_w: float
    deye_battery_discharging_w: float
    pv_power_w: float
    house_load_w: float


@dataclass(frozen=True)
class FlowAssessment:
    state: FlowState
    warnings: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    transients: tuple[str, ...] = ()


@dataclass
class FlowDebouncer:
    thresholds: FlowThresholds = field(default_factory=FlowThresholds)
    grid_import_warning_since: datetime | None = None
    grid_import_violation_since: datetime | None = None
    solax_to_deye_since: datetime | None = None
    deye_to_solax_since: datetime | None = None
    cross_charging_since: datetime | None = None
    transient_import_count: int = 0
    persistent_import_count: int = 0
    solax_to_deye_count: int = 0
    deye_to_solax_count: int = 0
    state_started_at: datetime | None = None
    state_durations_s: dict[FlowState, float] = field(default_factory=dict)
    last_state: FlowState = FlowState.UNKNOWN

    def assess(self, snapshot: FlowSnapshot, now: datetime) -> FlowAssessment:
        instant = assess_flow_snapshot(snapshot, self.thresholds)
        self._track_state_duration(instant.state, now)

        warnings: list[str] = []
        violations: list[str] = []
        transients: list[str] = list(instant.transients)

        if snapshot.grid_import_w > self.thresholds.grid_import_warning_w:
            self.grid_import_warning_since = self.grid_import_warning_since or now
            if self._elapsed(self.grid_import_warning_since, now) >= self.thresholds.warning_persistence_s:
                warnings.append(f"Grid import is persistent: {snapshot.grid_import_w:.0f} W")
                self.persistent_import_count += 1
            else:
                transients.append(f"Transient grid import: {snapshot.grid_import_w:.0f} W")
                self.transient_import_count += 1
        else:
            self.grid_import_warning_since = None

        if snapshot.grid_import_w > self.thresholds.grid_import_violation_w:
            self.grid_import_violation_since = self.grid_import_violation_since or now
            if self._elapsed(self.grid_import_violation_since, now) >= self.thresholds.violation_persistence_s:
                violations.append(f"Grid import violation: {snapshot.grid_import_w:.0f} W")
        else:
            self.grid_import_violation_since = None

        if instant.state is FlowState.SOLAX_TO_DEYE:
            self.solax_to_deye_since = self.solax_to_deye_since or now
            if self._elapsed(self.solax_to_deye_since, now) >= self.thresholds.violation_persistence_s:
                violations.append("SolaX battery appears to discharge while DEYE charges")
                self.solax_to_deye_count += 1
            else:
                transients.append("Transient SolaX to DEYE battery flow")
        else:
            self.solax_to_deye_since = None

        if instant.state is FlowState.DEYE_TO_SOLAX:
            self.deye_to_solax_since = self.deye_to_solax_since or now
            if self._elapsed(self.deye_to_solax_since, now) >= self.thresholds.violation_persistence_s:
                violations.append("DEYE battery appears to discharge while SolaX charges")
                self.deye_to_solax_count += 1
            else:
                transients.append("Transient DEYE to SolaX battery flow")
        else:
            self.deye_to_solax_since = None

        if instant.state is FlowState.CROSS_CHARGING:
            self.cross_charging_since = self.cross_charging_since or now
            if self._elapsed(self.cross_charging_since, now) >= self.thresholds.violation_persistence_s:
                violations.extend(instant.violations)
            else:
                transients.append("Transient export while the other battery charges")
        else:
            self.cross_charging_since = None

        return FlowAssessment(
            state=instant.state,
            warnings=tuple(dict.fromkeys(warnings)),
            violations=tuple(dict.fromkeys(violations)),
            transients=tuple(dict.fromkeys(transients)),
        )

    def _track_state_duration(self, state: FlowState, now: datetime) -> None:
        if self.state_started_at is None:
            self.state_started_at = now
            self.last_state = state
            return
        if state is self.last_state:
            return
        duration = max((now - self.state_started_at).total_seconds(), 0.0)
        self.state_durations_s[self.last_state] = self.state_durations_s.get(self.last_state, 0.0) + duration
        self.state_started_at = now
        self.last_state = state

    @staticmethod
    def _elapsed(start: datetime, now: datetime) -> float:
        return max((now - start).total_seconds(), 0.0)


def solax_battery_charging_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(raw_power_w if signs.solax_battery_charging_positive else -raw_power_w, 0.0)


def solax_battery_discharging_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(-raw_power_w if signs.solax_battery_charging_positive else raw_power_w, 0.0)


def deye_battery_charging_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(-raw_power_w if signs.deye_battery_discharging_positive else raw_power_w, 0.0)


def deye_battery_discharging_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(raw_power_w if signs.deye_battery_discharging_positive else -raw_power_w, 0.0)


def grid_import_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(raw_power_w if signs.deye_grid_import_positive else -raw_power_w, 0.0)


def grid_export_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(-raw_power_w if signs.deye_grid_import_positive else raw_power_w, 0.0)


def derive_flow_snapshot(telemetry: TelemetrySnapshot, signs: SignConventions) -> FlowSnapshot:
    solax_battery_power = telemetry.solax_battery_power_w or 0.0
    deye_battery_power = telemetry.deye_battery_power_w or 0.0
    deye_grid_power = telemetry.deye_grid_power_w or 0.0
    return FlowSnapshot(
        grid_import_w=grid_import_power_w(deye_grid_power, signs),
        grid_export_w=grid_export_power_w(deye_grid_power, signs),
        solax_battery_charging_w=solax_battery_charging_power_w(solax_battery_power, signs),
        solax_battery_discharging_w=solax_battery_discharging_power_w(solax_battery_power, signs),
        deye_battery_charging_w=deye_battery_charging_power_w(deye_battery_power, signs),
        deye_battery_discharging_w=deye_battery_discharging_power_w(deye_battery_power, signs),
        pv_power_w=max(telemetry.solax_pv_power_w or 0.0, 0.0),
        house_load_w=max(telemetry.solax_house_load_w or 0.0, 0.0),
    )


def assess_flow_snapshot(snapshot: FlowSnapshot, thresholds: FlowThresholds | None = None) -> FlowAssessment:
    thresholds = thresholds or FlowThresholds()
    warnings: list[str] = []
    violations: list[str] = []
    transients: list[str] = []

    if snapshot.grid_import_w > thresholds.grid_import_warning_w:
        warnings.append(f"Grid import detected: {snapshot.grid_import_w:.0f} W")
    if snapshot.grid_import_w > thresholds.grid_import_violation_w:
        transients.append(f"Grid import above violation threshold: {snapshot.grid_import_w:.0f} W")

    if _cross_charging_from_deye_export(snapshot, thresholds):
        violations.append("DEYE exports while SolaX battery charges")
        return FlowAssessment(FlowState.CROSS_CHARGING, tuple(warnings), tuple(violations), tuple(transients))

    if _cross_charging_from_solax_export(snapshot, thresholds):
        violations.append("SolaX exports while DEYE battery charges")
        return FlowAssessment(FlowState.CROSS_CHARGING, tuple(warnings), tuple(violations), tuple(transients))

    if (
        snapshot.solax_battery_discharging_w > thresholds.battery_flow_warning_w
        and snapshot.deye_battery_charging_w > thresholds.battery_flow_warning_w
    ):
        return FlowAssessment(FlowState.SOLAX_TO_DEYE, tuple(warnings), (), tuple(transients))

    if (
        snapshot.deye_battery_discharging_w > thresholds.battery_flow_warning_w
        and snapshot.solax_battery_charging_w > thresholds.battery_flow_warning_w
    ):
        return FlowAssessment(FlowState.DEYE_TO_SOLAX, tuple(warnings), (), tuple(transients))

    if _likely_pv_surplus_charge(snapshot, thresholds):
        return FlowAssessment(FlowState.LIKELY_PV_SURPLUS_CHARGE, tuple(warnings), (), tuple(transients))

    if snapshot.grid_export_w > thresholds.minimum_export_w:
        return FlowAssessment(FlowState.GRID_EXPORT, tuple(warnings), (), tuple(transients))
    if snapshot.grid_import_w > thresholds.grid_import_warning_w:
        return FlowAssessment(FlowState.GRID_IMPORT, tuple(warnings), (), tuple(transients))
    return FlowAssessment(FlowState.NORMAL, tuple(warnings), (), tuple(transients))


def summarize_flow(snapshot: FlowSnapshot, assessment: FlowAssessment, max_len: int = 255) -> str:
    text = (
        f"{assessment.state.value}: grid import/export {snapshot.grid_import_w:.0f}/{snapshot.grid_export_w:.0f} W, "
        f"SolaX batt charge/discharge {snapshot.solax_battery_charging_w:.0f}/"
        f"{snapshot.solax_battery_discharging_w:.0f} W, "
        f"DEYE batt charge/discharge {snapshot.deye_battery_charging_w:.0f}/"
        f"{snapshot.deye_battery_discharging_w:.0f} W, PV/load {snapshot.pv_power_w:.0f}/{snapshot.house_load_w:.0f} W"
    )
    return text[:max_len]


def _likely_pv_surplus_charge(snapshot: FlowSnapshot, thresholds: FlowThresholds) -> bool:
    pv_surplus_w = max(snapshot.pv_power_w - snapshot.house_load_w, 0.0)
    return (
        snapshot.deye_battery_charging_w > thresholds.minimum_deye_charge_w
        and pv_surplus_w > snapshot.deye_battery_charging_w + thresholds.pv_surplus_reserve_w
        and snapshot.grid_import_w <= thresholds.grid_import_warning_w
        and snapshot.solax_battery_discharging_w <= thresholds.battery_flow_warning_w
    )


def _cross_charging_from_deye_export(snapshot: FlowSnapshot, thresholds: FlowThresholds) -> bool:
    return (
        snapshot.deye_battery_discharging_w > thresholds.battery_flow_violation_w
        and snapshot.grid_export_w > thresholds.minimum_export_w
        and snapshot.solax_battery_charging_w > thresholds.battery_flow_violation_w
    )


def _cross_charging_from_solax_export(snapshot: FlowSnapshot, thresholds: FlowThresholds) -> bool:
    return (
        snapshot.solax_battery_discharging_w > thresholds.battery_flow_violation_w
        and snapshot.grid_export_w > thresholds.minimum_export_w
        and snapshot.deye_battery_charging_w > thresholds.battery_flow_violation_w
    )


def persistence_delta(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)
