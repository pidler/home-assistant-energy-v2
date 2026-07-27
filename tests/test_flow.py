from __future__ import annotations

from datetime import datetime, timedelta

from apps.energy_v2.flow import (
    FlowDebouncer,
    FlowSnapshot,
    FlowThresholds,
    RollingExportAverage,
    RollingExportAverageTracker,
    SignConventions,
    SystemParameters,
    assess_export_limit,
    assess_flow_snapshot,
    derive_flow_snapshot,
    deye_battery_charging_power_w,
    deye_battery_discharging_power_w,
    grid_export_power_w,
    grid_import_power_w,
    solax_battery_charging_power_w,
    solax_battery_discharging_power_w,
    summarize_flow,
    validate_flow_thresholds,
    validate_system_parameters,
)
from apps.energy_v2.models import ExportLimitState, FlowState
from tests.test_planner import snapshot


def flow(**overrides: float) -> FlowSnapshot:
    data = dict(
        grid_import_w=0.0,
        grid_export_w=0.0,
        solax_battery_charging_w=0.0,
        solax_battery_discharging_w=0.0,
        deye_battery_charging_w=0.0,
        deye_battery_discharging_w=0.0,
        pv_power_w=0.0,
        house_load_w=0.0,
    )
    data.update(overrides)
    return FlowSnapshot(**data)


def test_solax_sign_convention_charging_positive() -> None:
    signs = SignConventions(solax_battery_charging_positive=True)
    assert solax_battery_charging_power_w(700.0, signs) == 700.0
    assert solax_battery_discharging_power_w(700.0, signs) == 0.0
    assert solax_battery_charging_power_w(-500.0, signs) == 0.0
    assert solax_battery_discharging_power_w(-500.0, signs) == 500.0


def test_deye_sign_convention_charging_positive() -> None:
    signs = SignConventions(deye_battery_charging_positive=True)
    assert deye_battery_charging_power_w(900.0, signs) == 900.0
    assert deye_battery_discharging_power_w(900.0, signs) == 0.0
    assert deye_battery_charging_power_w(-400.0, signs) == 0.0
    assert deye_battery_discharging_power_w(-400.0, signs) == 400.0


def test_grid_import_export_transform() -> None:
    signs = SignConventions(grid_export_positive=True)
    assert grid_export_power_w(600.0, signs) == 600.0
    assert grid_import_power_w(600.0, signs) == 0.0
    assert grid_export_power_w(-300.0, signs) == 0.0
    assert grid_import_power_w(-300.0, signs) == 300.0


def test_zero_power_is_normal() -> None:
    assessment = assess_flow_snapshot(flow())
    assert assessment.state is FlowState.NORMAL
    assert not assessment.warnings
    assert not assessment.violations


def test_derive_flow_snapshot_uses_explicit_signs() -> None:
    telemetry = snapshot(
        solax_battery_power_w=-500.0,
        deye_battery_power_w=700.0,
        solax_measured_power_w=1000.0,
        deye_grid_power_w=-9999.0,
        solax_pv_power_w=3000.0,
        solax_house_load_w=1200.0,
    )

    result = derive_flow_snapshot(telemetry, SignConventions())

    assert result.solax_battery_discharging_w == 500.0
    assert result.deye_battery_charging_w == 700.0
    assert result.grid_export_w == 1000.0
    assert result.pv_power_w == 3000.0
    assert result.house_load_w == 1200.0


def test_short_import_transient_has_no_violation() -> None:
    monitor = FlowDebouncer(FlowThresholds(warning_persistence_s=5.0, violation_persistence_s=10.0))
    now = datetime(2026, 7, 26, 12, 0, 0)

    assessment = monitor.assess(flow(grid_import_w=700.0), now)

    assert assessment.state is FlowState.GRID_IMPORT
    assert not assessment.warnings
    assert "Grid import violation" not in "; ".join(assessment.violations)
    assert assessment.transients


def test_long_import_has_violation() -> None:
    monitor = FlowDebouncer(FlowThresholds(warning_persistence_s=5.0, violation_persistence_s=10.0))
    now = datetime(2026, 7, 26, 12, 0, 0)
    monitor.assess(flow(grid_import_w=700.0), now)

    assessment = monitor.assess(flow(grid_import_w=700.0), now + timedelta(seconds=11))

    assert assessment.state is FlowState.GRID_IMPORT
    assert "Grid import violation" in "; ".join(assessment.violations)


def test_long_import_event_counts_once_with_repeated_5s_ticks() -> None:
    monitor = FlowDebouncer(FlowThresholds(warning_persistence_s=5.0, violation_persistence_s=10.0))
    start = datetime(2026, 7, 26, 12, 0, 0)

    for seconds in range(0, 600, 5):
        monitor.assess(flow(grid_import_w=700.0), start + timedelta(seconds=seconds))

    assert monitor.persistent_import_count == 1


def test_separate_import_events_count_twice() -> None:
    monitor = FlowDebouncer(FlowThresholds(warning_persistence_s=5.0, violation_persistence_s=10.0))
    start = datetime(2026, 7, 26, 12, 0, 0)

    monitor.assess(flow(grid_import_w=700.0), start)
    monitor.assess(flow(grid_import_w=700.0), start + timedelta(seconds=10))
    monitor.assess(flow(), start + timedelta(seconds=15))
    monitor.assess(flow(grid_import_w=700.0), start + timedelta(seconds=20))
    monitor.assess(flow(grid_import_w=700.0), start + timedelta(seconds=30))

    assert monitor.persistent_import_count == 2


def test_long_cross_charge_event_counts_once() -> None:
    monitor = FlowDebouncer(FlowThresholds(warning_persistence_s=5.0, violation_persistence_s=10.0))
    start = datetime(2026, 7, 26, 12, 0, 0)
    cross = flow(deye_battery_discharging_w=800.0, grid_export_w=900.0, solax_battery_charging_w=700.0)

    for seconds in range(0, 600, 5):
        monitor.assess(cross, start + timedelta(seconds=seconds))

    assert monitor.cross_charging_count == 1


def test_solax_discharge_and_deye_charge_detected() -> None:
    assessment = assess_flow_snapshot(flow(solax_battery_discharging_w=600.0, deye_battery_charging_w=700.0))
    assert assessment.state is FlowState.SOLAX_TO_DEYE


def test_deye_discharge_and_solax_charge_detected() -> None:
    assessment = assess_flow_snapshot(flow(deye_battery_discharging_w=700.0, solax_battery_charging_w=600.0))
    assert assessment.state is FlowState.DEYE_TO_SOLAX


def test_export_while_other_battery_charges_detected() -> None:
    assessment = assess_flow_snapshot(
        flow(deye_battery_discharging_w=800.0, grid_export_w=900.0, solax_battery_charging_w=700.0)
    )
    assert assessment.state is FlowState.CROSS_CHARGING
    assert assessment.violations == ("DEYE exports while SolaX battery charges",)


def test_likely_deye_charging_from_pv_surplus() -> None:
    assessment = assess_flow_snapshot(flow(deye_battery_charging_w=900.0, pv_power_w=4000.0, house_load_w=1200.0))
    assert assessment.state is FlowState.LIKELY_PV_SURPLUS_CHARGE


def test_negative_house_load_does_not_increase_surplus() -> None:
    telemetry = snapshot(solax_pv_power_w=1000.0, solax_house_load_w=-5000.0, deye_battery_power_w=800.0)
    result = derive_flow_snapshot(telemetry, SignConventions())

    assert result.house_load_w == 0.0
    assert assess_flow_snapshot(result).state is not FlowState.LIKELY_PV_SURPLUS_CHARGE


def test_grid_flow_uses_solax_measured_power_not_control_sensors() -> None:
    export = derive_flow_snapshot(snapshot(solax_measured_power_w=5000.0, deye_grid_power_w=-1000.0), SignConventions())
    import_ = derive_flow_snapshot(
        snapshot(solax_measured_power_w=-5000.0, deye_grid_power_w=1000.0),
        SignConventions(),
    )
    zero = derive_flow_snapshot(snapshot(solax_measured_power_w=0.0), SignConventions())

    assert export.grid_export_w == 5000.0
    assert export.grid_import_w == 0.0
    assert import_.grid_import_w == 5000.0
    assert import_.grid_export_w == 0.0
    assert zero.grid_import_w == 0.0
    assert zero.grid_export_w == 0.0


def test_deye_normalized_battery_power_is_not_flipped_twice() -> None:
    discharge = derive_flow_snapshot(
        snapshot(deye_battery_power_raw_w=1000.0, deye_battery_power_w=-1000.0),
        SignConventions(),
    )
    charge = derive_flow_snapshot(
        snapshot(deye_battery_power_raw_w=-1000.0, deye_battery_power_w=1000.0),
        SignConventions(),
    )

    assert discharge.deye_battery_discharging_w == 1000.0
    assert discharge.deye_battery_charging_w == 0.0
    assert charge.deye_battery_charging_w == 1000.0
    assert charge.deye_battery_discharging_w == 0.0


def test_diagnostic_summary_is_limited() -> None:
    text = summarize_flow(
        flow(
            grid_import_w=123456.0,
            grid_export_w=123456.0,
            solax_battery_charging_w=123456.0,
            solax_battery_discharging_w=123456.0,
            deye_battery_charging_w=123456.0,
            deye_battery_discharging_w=123456.0,
            pv_power_w=123456.0,
            house_load_w=123456.0,
        ),
        assess_flow_snapshot(flow()),
        max_len=80,
    )
    assert len(text) == 80


def test_constant_export_9000_w_for_15_minutes_is_within_target() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 9000.0)
    average = tracker.add_sample(start + timedelta(seconds=900), 9000.0)
    assessment = assess_export_limit(9000.0, average)

    assert average.average_w == 9000.0
    assert average.window_complete
    assert assessment.state is ExportLimitState.EXPORT_WITHIN_TARGET
    assert not assessment.violations


def test_constant_export_9800_w_for_15_minutes_is_near_limit_warning() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 9800.0)
    average = tracker.add_sample(start + timedelta(seconds=900), 9800.0)
    assessment = assess_export_limit(9800.0, average)

    assert average.average_w == 9800.0
    assert assessment.state is ExportLimitState.EXPORT_AVERAGE_NEAR_LIMIT
    assert assessment.warnings
    assert not assessment.violations


def test_constant_export_10000_w_for_15_minutes_is_near_limit_not_violation() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 10000.0)
    average = tracker.add_sample(start + timedelta(seconds=900), 10000.0)
    assessment = assess_export_limit(10000.0, average)

    assert average.average_w == 10000.0
    assert assessment.state is ExportLimitState.EXPORT_AVERAGE_NEAR_LIMIT
    assert not assessment.violations


def test_constant_export_above_10000_w_for_15_minutes_is_violation() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 10050.0)
    average = tracker.add_sample(start + timedelta(seconds=900), 10050.0)
    assessment = assess_export_limit(10050.0, average)

    assert assessment.state is ExportLimitState.EXPORT_AVERAGE_LIMIT_VIOLATION
    assert assessment.violations


def test_short_spike_above_10000_w_does_not_violate_average_limit() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 9000.0)
    tracker.add_sample(start + timedelta(seconds=840), 11000.0)
    average = tracker.add_sample(start + timedelta(seconds=900), 9000.0)
    assessment = assess_export_limit(11000.0, average)

    assert round(average.average_w, 3) == round(((9000.0 * 840.0) + (11000.0 * 60.0)) / 900.0, 3)
    assert average.average_w < 10000.0
    assert assessment.state is ExportLimitState.EXPORT_INSTANT_ABOVE_TARGET
    assert not assessment.violations


def test_irregular_sample_intervals_are_time_weighted() -> None:
    tracker = RollingExportAverageTracker(window_s=300.0)
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 1000.0)
    tracker.add_sample(start + timedelta(seconds=60), 7000.0)
    average = tracker.add_sample(start + timedelta(seconds=300), 7000.0)

    assert average.average_w == ((1000.0 * 60.0) + (7000.0 * 240.0)) / 300.0


def test_old_samples_are_removed_from_sliding_window() -> None:
    tracker = RollingExportAverageTracker(window_s=300.0)
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 11000.0)
    tracker.add_sample(start + timedelta(seconds=300), 1000.0)
    average = tracker.add_sample(start + timedelta(seconds=600), 1000.0)

    assert average.average_w == 1000.0
    assert len(tracker.samples) == 2


def test_restart_incomplete_window_is_partial_not_definitive() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 9800.0)
    average = tracker.add_sample(start + timedelta(seconds=60), 9800.0)
    assessment = assess_export_limit(9800.0, average)

    assert average.covered_duration_s == 60.0
    assert not average.window_complete
    assert any("partial" in warning for warning in assessment.warnings)


def test_missing_samples_are_unknown_not_safe() -> None:
    average = RollingExportAverageTracker().average(datetime(2026, 7, 27, 12, 0, 0))
    assessment = assess_export_limit(0.0, average)

    assert average.stale
    assert assessment.state is ExportLimitState.UNKNOWN
    assert assessment.warnings


def test_stale_telemetry_is_unknown_not_safe() -> None:
    tracker = RollingExportAverageTracker(max_sample_age_s=10.0)
    start = datetime(2026, 7, 27, 12, 0, 0)
    tracker.add_sample(start, 9000.0)
    average = tracker.average(start + timedelta(seconds=11))
    assessment = assess_export_limit(9000.0, average)

    assert average.stale
    assert assessment.state is ExportLimitState.UNKNOWN
    assert average.last_sample_age_s == 11.0


def test_exact_average_limit_crossing_uses_greater_than_10000() -> None:
    at_limit = assess_export_limit(
        10000.0,
        RollingExportAverage(average_w=10000.0, covered_duration_s=900.0, window_complete=True),
    )
    above_limit = assess_export_limit(
        10001.0,
        RollingExportAverage(average_w=10000.1, covered_duration_s=900.0, window_complete=True),
    )

    assert at_limit.state is not ExportLimitState.EXPORT_AVERAGE_LIMIT_VIOLATION
    assert above_limit.state is ExportLimitState.EXPORT_AVERAGE_LIMIT_VIOLATION


def test_system_parameter_defaults_are_confirmed_values() -> None:
    params = SystemParameters()

    assert params.solax_rated_power_w == 12_000.0
    assert params.solax_battery_capacity_kwh == 24.0
    assert params.solax_min_soc_pct == 10.0
    assert params.deye_rated_power_w == 12_000.0
    assert params.deye_battery_capacity_kwh == 32.0
    assert params.deye_min_soc_pct == 10.0
    assert params.target_export_limit_w == 9_800.0
    assert params.legal_export_average_limit_w == 10_000.0
    assert params.export_average_window_s == 900.0


def test_rolling_tracker_ignores_nan_and_inf_samples() -> None:
    tracker = RollingExportAverageTracker()
    start = datetime(2026, 7, 27, 12, 0, 0)

    tracker.add_sample(start, float("nan"))
    tracker.add_sample(start, float("inf"))

    assert tracker.average(start).stale


def test_rolling_tracker_handles_duplicate_timestamp() -> None:
    tracker = RollingExportAverageTracker(window_s=300.0)
    start = datetime(2026, 7, 27, 12, 0, 0)

    tracker.add_sample(start, 1000.0)
    tracker.add_sample(start, 5000.0)
    average = tracker.add_sample(start + timedelta(seconds=300), 5000.0)

    assert average.average_w == 5000.0


def test_rolling_tracker_sorts_timestamp_going_backwards() -> None:
    tracker = RollingExportAverageTracker(window_s=300.0)
    start = datetime(2026, 7, 27, 12, 0, 0)

    tracker.add_sample(start + timedelta(seconds=60), 7000.0)
    tracker.add_sample(start, 1000.0)
    average = tracker.add_sample(start + timedelta(seconds=300), 7000.0)

    assert average.average_w == ((1000.0 * 60.0) + (7000.0 * 240.0)) / 300.0


def test_rolling_tracker_clamps_negative_export_to_zero() -> None:
    tracker = RollingExportAverageTracker(window_s=300.0)
    start = datetime(2026, 7, 27, 12, 0, 0)

    tracker.add_sample(start, -5000.0)
    average = tracker.add_sample(start + timedelta(seconds=300), -5000.0)

    assert average.average_w == 0.0


def test_invalid_system_parameters_are_reported() -> None:
    assert validate_system_parameters(SystemParameters(target_export_limit_w=11_000.0))
    assert validate_system_parameters(SystemParameters(export_average_window_s=0.0))
    assert validate_system_parameters(SystemParameters(legal_export_average_limit_w=float("inf")))


def test_invalid_flow_thresholds_are_reported() -> None:
    assert validate_flow_thresholds(FlowThresholds(grid_import_warning_w=600.0, grid_import_violation_w=500.0))
    assert validate_flow_thresholds(FlowThresholds(warning_persistence_s=10.0, violation_persistence_s=5.0))
    assert validate_flow_thresholds(FlowThresholds(telemetry_stale_timeout_s=0.0))
    assert validate_flow_thresholds(FlowThresholds(minimum_export_w=float("nan")))
