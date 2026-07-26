from __future__ import annotations

from datetime import datetime, timedelta

from apps.energy_v2.flow import (
    FlowDebouncer,
    FlowSnapshot,
    FlowThresholds,
    SignConventions,
    assess_flow_snapshot,
    derive_flow_snapshot,
    deye_battery_charging_power_w,
    deye_battery_discharging_power_w,
    grid_export_power_w,
    grid_import_power_w,
    solax_battery_charging_power_w,
    solax_battery_discharging_power_w,
    summarize_flow,
)
from apps.energy_v2.models import FlowState
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


def test_deye_sign_convention_discharging_positive() -> None:
    signs = SignConventions(deye_battery_discharging_positive=True)
    assert deye_battery_discharging_power_w(900.0, signs) == 900.0
    assert deye_battery_charging_power_w(900.0, signs) == 0.0
    assert deye_battery_discharging_power_w(-400.0, signs) == 0.0
    assert deye_battery_charging_power_w(-400.0, signs) == 400.0


def test_grid_import_export_transform() -> None:
    signs = SignConventions(deye_grid_import_positive=True)
    assert grid_import_power_w(600.0, signs) == 600.0
    assert grid_export_power_w(600.0, signs) == 0.0
    assert grid_import_power_w(-300.0, signs) == 0.0
    assert grid_export_power_w(-300.0, signs) == 300.0


def test_zero_power_is_normal() -> None:
    assessment = assess_flow_snapshot(flow())
    assert assessment.state is FlowState.NORMAL
    assert not assessment.warnings
    assert not assessment.violations


def test_derive_flow_snapshot_uses_explicit_signs() -> None:
    telemetry = snapshot(
        solax_battery_power_w=-500.0,
        deye_battery_power_w=-700.0,
        deye_grid_power_w=-1000.0,
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
    telemetry = snapshot(solax_pv_power_w=1000.0, solax_house_load_w=-5000.0, deye_battery_power_w=-800.0)
    result = derive_flow_snapshot(telemetry, SignConventions())

    assert result.house_load_w == 0.0
    assert assess_flow_snapshot(result).state is not FlowState.LIKELY_PV_SURPLUS_CHARGE


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
