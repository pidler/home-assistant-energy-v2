from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import ExportLimitState, FlowState, TelemetrySnapshot


@dataclass(frozen=True)
class SystemParameters:
    solax_rated_power_w: float = 12_000.0
    solax_battery_capacity_kwh: float = 24.0
    solax_min_soc_pct: float = 10.0
    deye_rated_power_w: float = 12_000.0
    deye_battery_capacity_kwh: float = 32.0
    deye_min_soc_pct: float = 10.0
    target_export_limit_w: float = 9_800.0
    legal_export_average_limit_w: float = 10_000.0
    export_average_warning_w: float = 9_800.0
    export_average_window_s: float = 900.0
    export_sample_max_age_s: float = 120.0


@dataclass(frozen=True)
class SignConventions:
    solax_battery_charging_positive: bool = True
    deye_battery_charging_positive: bool = True
    grid_export_positive: bool = True


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
    telemetry_stale_timeout_s: float = 30.0


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


@dataclass(frozen=True)
class RollingExportAverage:
    average_w: float
    covered_duration_s: float
    window_complete: bool
    stale: bool = False
    last_sample_age_s: float | None = None


@dataclass(frozen=True)
class ExportLimitAssessment:
    state: ExportLimitState
    instant_export_w: float
    rolling_average: RollingExportAverage
    warnings: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportSample:
    timestamp: datetime
    export_w: float


@dataclass
class RollingExportAverageTracker:
    window_s: float = 900.0
    max_sample_age_s: float = 120.0
    samples: list[ExportSample] = field(default_factory=list)

    def add_sample(self, now: datetime, export_w: float) -> RollingExportAverage:
        if not math.isfinite(export_w):
            return self.average(now)
        self.samples.append(ExportSample(now, max(export_w, 0.0)))
        self.samples.sort(key=lambda sample: sample.timestamp)
        self._drop_samples_before(now - timedelta(seconds=self.window_s))
        return self.average(now)

    def average(self, now: datetime) -> RollingExportAverage:
        if not self.samples:
            return RollingExportAverage(0.0, 0.0, False, stale=True)
        last_sample_age_s = max((now - self.samples[-1].timestamp).total_seconds(), 0.0)
        stale = last_sample_age_s > self.max_sample_age_s
        window_start = now - timedelta(seconds=self.window_s)
        first_timestamp = max(self.samples[0].timestamp, window_start)
        covered_duration_s = max((now - first_timestamp).total_seconds(), 0.0)
        if covered_duration_s <= 0:
            return RollingExportAverage(0.0, 0.0, False, stale=stale, last_sample_age_s=last_sample_age_s)

        energy_ws = 0.0
        relevant_samples = [sample for sample in self.samples if sample.timestamp >= window_start]
        previous_samples = [sample for sample in self.samples if sample.timestamp < window_start]
        if previous_samples:
            relevant_samples.insert(0, ExportSample(window_start, previous_samples[-1].export_w))

        for index, sample in enumerate(relevant_samples):
            segment_start = max(sample.timestamp, window_start)
            if index + 1 < len(relevant_samples):
                segment_end = min(relevant_samples[index + 1].timestamp, now)
            else:
                segment_end = now
            duration_s = max((segment_end - segment_start).total_seconds(), 0.0)
            energy_ws += sample.export_w * duration_s

        return RollingExportAverage(
            average_w=energy_ws / covered_duration_s,
            covered_duration_s=covered_duration_s,
            window_complete=covered_duration_s >= self.window_s,
            stale=stale,
            last_sample_age_s=last_sample_age_s,
        )

    def _drop_samples_before(self, cutoff: datetime) -> None:
        while len(self.samples) > 1 and self.samples[1].timestamp <= cutoff:
            self.samples.pop(0)


@dataclass
class FlowDebouncer:
    thresholds: FlowThresholds = field(default_factory=FlowThresholds)
    grid_import_warning_since: datetime | None = None
    grid_import_violation_since: datetime | None = None
    solax_to_deye_since: datetime | None = None
    deye_to_solax_since: datetime | None = None
    cross_charging_since: datetime | None = None
    grid_import_warning_confirmed: bool = False
    grid_import_violation_confirmed: bool = False
    solax_to_deye_confirmed: bool = False
    deye_to_solax_confirmed: bool = False
    cross_charging_confirmed: bool = False
    transient_import_count: int = 0
    persistent_import_count: int = 0
    solax_to_deye_count: int = 0
    deye_to_solax_count: int = 0
    cross_charging_count: int = 0
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
                if not self.grid_import_warning_confirmed:
                    self.persistent_import_count += 1
                    self.grid_import_warning_confirmed = True
            else:
                transients.append(f"Transient grid import: {snapshot.grid_import_w:.0f} W")
                self.transient_import_count += 1
        else:
            self.grid_import_warning_since = None
            self.grid_import_warning_confirmed = False

        if snapshot.grid_import_w > self.thresholds.grid_import_violation_w:
            self.grid_import_violation_since = self.grid_import_violation_since or now
            if self._elapsed(self.grid_import_violation_since, now) >= self.thresholds.violation_persistence_s:
                violations.append(f"Grid import violation: {snapshot.grid_import_w:.0f} W")
                self.grid_import_violation_confirmed = True
        else:
            self.grid_import_violation_since = None
            self.grid_import_violation_confirmed = False

        if instant.state is FlowState.SOLAX_TO_DEYE:
            self.solax_to_deye_since = self.solax_to_deye_since or now
            if self._elapsed(self.solax_to_deye_since, now) >= self.thresholds.violation_persistence_s:
                violations.append("SolaX battery appears to discharge while DEYE charges")
                if not self.solax_to_deye_confirmed:
                    self.solax_to_deye_count += 1
                    self.solax_to_deye_confirmed = True
            else:
                transients.append("Transient SolaX to DEYE battery flow")
        else:
            self.solax_to_deye_since = None
            self.solax_to_deye_confirmed = False

        if instant.state is FlowState.DEYE_TO_SOLAX:
            self.deye_to_solax_since = self.deye_to_solax_since or now
            if self._elapsed(self.deye_to_solax_since, now) >= self.thresholds.violation_persistence_s:
                violations.append("DEYE battery appears to discharge while SolaX charges")
                if not self.deye_to_solax_confirmed:
                    self.deye_to_solax_count += 1
                    self.deye_to_solax_confirmed = True
            else:
                transients.append("Transient DEYE to SolaX battery flow")
        else:
            self.deye_to_solax_since = None
            self.deye_to_solax_confirmed = False

        if instant.state is FlowState.CROSS_CHARGING:
            self.cross_charging_since = self.cross_charging_since or now
            if self._elapsed(self.cross_charging_since, now) >= self.thresholds.violation_persistence_s:
                violations.extend(instant.violations)
                if not self.cross_charging_confirmed:
                    self.cross_charging_count += 1
                    self.cross_charging_confirmed = True
            else:
                transients.append("Transient export while the other battery charges")
        else:
            self.cross_charging_since = None
            self.cross_charging_confirmed = False

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
    return max(raw_power_w if signs.deye_battery_charging_positive else -raw_power_w, 0.0)


def deye_battery_discharging_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(-raw_power_w if signs.deye_battery_charging_positive else raw_power_w, 0.0)


def grid_import_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(-raw_power_w if signs.grid_export_positive else raw_power_w, 0.0)


def grid_export_power_w(raw_power_w: float, signs: SignConventions) -> float:
    return max(raw_power_w if signs.grid_export_positive else -raw_power_w, 0.0)


def derive_flow_snapshot(telemetry: TelemetrySnapshot, signs: SignConventions) -> FlowSnapshot:
    solax_battery_power = telemetry.solax_battery_power_w or 0.0
    deye_battery_power = telemetry.deye_battery_power_w or 0.0
    measured_grid_power = telemetry.solax_measured_power_w or 0.0
    return FlowSnapshot(
        grid_import_w=grid_import_power_w(measured_grid_power, signs),
        grid_export_w=grid_export_power_w(measured_grid_power, signs),
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


def assess_export_limit(
    instant_grid_export_w: float,
    rolling_average: RollingExportAverage,
    parameters: SystemParameters | None = None,
) -> ExportLimitAssessment:
    parameters = parameters or SystemParameters()
    instant_grid_export_w = max(instant_grid_export_w, 0.0)
    if rolling_average.stale:
        return ExportLimitAssessment(
            ExportLimitState.UNKNOWN,
            instant_grid_export_w,
            rolling_average,
            warnings=("Export telemetry is missing or stale",),
        )

    warnings: list[str] = []
    violations: list[str] = []
    if not rolling_average.window_complete:
        warnings.append(
            f"Export average is partial: {rolling_average.covered_duration_s:.0f}/"
            f"{parameters.export_average_window_s:.0f} s"
        )

    if rolling_average.average_w > parameters.legal_export_average_limit_w:
        violations.append(
            f"15-minute average export exceeds limit: {rolling_average.average_w:.0f} W "
            f"> {parameters.legal_export_average_limit_w:.0f} W"
        )
        return ExportLimitAssessment(
            ExportLimitState.EXPORT_AVERAGE_LIMIT_VIOLATION,
            instant_grid_export_w,
            rolling_average,
            tuple(warnings),
            tuple(violations),
        )

    if rolling_average.average_w >= parameters.export_average_warning_w:
        warnings.append(f"15-minute average export near limit: {rolling_average.average_w:.0f} W")
        return ExportLimitAssessment(
            ExportLimitState.EXPORT_AVERAGE_NEAR_LIMIT,
            instant_grid_export_w,
            rolling_average,
            tuple(warnings),
        )

    if instant_grid_export_w > parameters.target_export_limit_w:
        warnings.append(
            f"Instant export above operational target: {instant_grid_export_w:.0f} W "
            f"> {parameters.target_export_limit_w:.0f} W"
        )
        return ExportLimitAssessment(
            ExportLimitState.EXPORT_INSTANT_ABOVE_TARGET,
            instant_grid_export_w,
            rolling_average,
            tuple(warnings),
        )

    return ExportLimitAssessment(
        ExportLimitState.EXPORT_WITHIN_TARGET,
        instant_grid_export_w,
        rolling_average,
        tuple(warnings),
    )


def merge_export_limit_assessment(
    flow_assessment: FlowAssessment,
    export_assessment: ExportLimitAssessment,
) -> FlowAssessment:
    return FlowAssessment(
        state=flow_assessment.state,
        warnings=tuple(dict.fromkeys(flow_assessment.warnings + export_assessment.warnings)),
        violations=tuple(dict.fromkeys(flow_assessment.violations + export_assessment.violations)),
        transients=flow_assessment.transients,
    )


def summarize_export_limit(assessment: ExportLimitAssessment, max_len: int = 255) -> str:
    rolling = assessment.rolling_average
    complete = "complete" if rolling.window_complete else "partial"
    stale = ", stale" if rolling.stale else ""
    age = "" if rolling.last_sample_age_s is None else f", age={rolling.last_sample_age_s:.0f} s"
    text = (
        f"{assessment.state.value}: instant={assessment.instant_export_w:.0f} W, "
        f"avg15={rolling.average_w:.0f} W, covered={rolling.covered_duration_s:.0f} s "
        f"({complete}{stale}{age})"
    )
    return text[:max_len]


def validate_system_parameters(parameters: SystemParameters) -> tuple[str, ...]:
    reasons: list[str] = []
    for name, value in parameters.__dict__.items():
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            reasons.append(f"{name} must be a finite number")
    positive_fields = (
        "solax_rated_power_w",
        "solax_battery_capacity_kwh",
        "deye_rated_power_w",
        "deye_battery_capacity_kwh",
        "target_export_limit_w",
        "legal_export_average_limit_w",
        "export_average_warning_w",
        "export_average_window_s",
        "export_sample_max_age_s",
    )
    for name in positive_fields:
        if getattr(parameters, name) <= 0:
            reasons.append(f"{name} must be > 0")
    for name in ("solax_min_soc_pct", "deye_min_soc_pct"):
        value = getattr(parameters, name)
        if value < 0 or value > 100:
            reasons.append(f"{name} must be between 0 and 100")
    if parameters.target_export_limit_w > parameters.legal_export_average_limit_w:
        reasons.append("target_export_limit_w must be <= legal_export_average_limit_w")
    if parameters.export_average_warning_w > parameters.legal_export_average_limit_w:
        reasons.append("export_average_warning_w must be <= legal_export_average_limit_w")
    return tuple(reasons)


def validate_flow_thresholds(thresholds: FlowThresholds) -> tuple[str, ...]:
    reasons: list[str] = []
    for name, value in thresholds.__dict__.items():
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            reasons.append(f"{name} must be a finite number")
    non_negative_fields = (
        "grid_import_warning_w",
        "grid_import_violation_w",
        "battery_flow_warning_w",
        "battery_flow_violation_w",
        "minimum_deye_charge_w",
        "minimum_export_w",
        "pv_surplus_reserve_w",
    )
    for name in non_negative_fields:
        if getattr(thresholds, name) < 0:
            reasons.append(f"{name} must be >= 0")
    for name in ("warning_persistence_s", "violation_persistence_s", "telemetry_stale_timeout_s"):
        if getattr(thresholds, name) <= 0:
            reasons.append(f"{name} must be > 0")
    if thresholds.grid_import_violation_w < thresholds.grid_import_warning_w:
        reasons.append("grid_import_violation_w must be >= grid_import_warning_w")
    if thresholds.battery_flow_violation_w < thresholds.battery_flow_warning_w:
        reasons.append("battery_flow_violation_w must be >= battery_flow_warning_w")
    if thresholds.violation_persistence_s < thresholds.warning_persistence_s:
        reasons.append("violation_persistence_s must be >= warning_persistence_s")
    return tuple(reasons)


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
