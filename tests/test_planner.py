from __future__ import annotations

from datetime import datetime

from apps.energy_v2.flow import FlowAssessment
from apps.energy_v2.models import FlowState, Mode, Strategy, TelemetrySnapshot
from apps.energy_v2.planner import plan_shadow_mode


def snapshot(**overrides: object) -> TelemetrySnapshot:
    data = dict(
        timestamp=datetime.now(),
        solax_soc_pct=96.0,
        solax_battery_power_w=0.0,
        solax_pv_power_w=5000.0,
        solax_house_load_w=1000.0,
        solax_grid_import_w=0.0,
        solax_grid_export_w=0.0,
        deye_soc_pct=50.0,
        deye_battery_power_w=0.0,
        deye_battery_state="idle",
        deye_grid_power_w=0.0,
        deye_external_power_w=0.0,
        deye_device_state="Normal",
        deye_connected=True,
        buy_price=2.0,
        sell_price=3.5,
        future_sell_rank=5.0,
        deye_grid_charging_enabled=False,
        deye_export_enabled=False,
    )
    data.update(overrides)
    return TelemetrySnapshot(**data)


def decide(**kwargs: object):
    defaults = dict(
        snapshot=snapshot(),
        telemetry_valid=True,
        export_enabled=False,
        deye_ledger_kwh=0.0,
        solax_ledger_kwh=0.0,
        deye_min_soc_pct=15.0,
        deye_max_soc_pct=90.0,
        solax_min_soc_pct=30.0,
        solax_pv_charge_start_soc_pct=95.0,
        pv_reserve_w=1000.0,
        minimum_sell_price=3.0,
        maximum_future_rank=12,
    )
    defaults.update(kwargs)
    return plan_shadow_mode(**defaults)


def test_invalid_telemetry_fault() -> None:
    assert decide(telemetry_valid=False).mode is Mode.FAULT


def test_zero_ledgers_no_export() -> None:
    assert decide(export_enabled=True).mode is Mode.PV_CHARGE_DEYE


def test_deye_ledger_high_price_export_deye() -> None:
    decision = decide(export_enabled=True, deye_ledger_kwh=1.0)
    assert decision.mode is Mode.EXPORT_DEYE


def test_solax_ledger_without_deye_ledger_export_solax() -> None:
    decision = decide(export_enabled=True, solax_ledger_kwh=1.0)
    assert decision.mode is Mode.EXPORT_SOLAX


def test_high_pv_surplus_pv_charge_deye() -> None:
    decision = decide(export_enabled=False, deye_ledger_kwh=0.0, solax_ledger_kwh=0.0)
    assert decision.mode is Mode.PV_CHARGE_DEYE


def test_negative_house_load_does_not_increase_surplus() -> None:
    low_pv = snapshot(solax_pv_power_w=1200.0, solax_house_load_w=-9000.0)
    decision = decide(snapshot=low_pv)
    assert decision.mode is Mode.IDLE


def test_grid_charging_enabled_blocks_pv_charge() -> None:
    decision = decide(snapshot=snapshot(deye_grid_charging_enabled=True))
    assert decision.mode is Mode.IDLE


def test_normal_idle() -> None:
    idle_snapshot = snapshot(solax_pv_power_w=1000.0, solax_house_load_w=800.0)
    assert decide(snapshot=idle_snapshot).mode is Mode.IDLE


def test_flow_violation_fault_recommendation() -> None:
    decision = decide(flow_assessment=FlowAssessment(FlowState.SOLAX_TO_DEYE, violations=("cycling",)))
    assert decision.mode is Mode.FAULT
    assert decision.flow_violation is True
    assert decision.flow_state is FlowState.SOLAX_TO_DEYE


def test_unimplemented_strategy_is_disabled() -> None:
    decision = decide(strategy=Strategy.WINTER_GRID_OPTIMIZATION)
    assert decision.mode is Mode.DISABLED
    assert "not implemented" in decision.reason
