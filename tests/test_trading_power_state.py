from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v2.trading.models import BatterySlotPlan, TradingAction, TradingSlotPlan
from apps.energy_v2.trading.power_state import (
    AssumptionStatus,
    DeyePowerState,
    DeyePowerStateConfig,
    DeyeTelemetryExpectation,
    build_deye_power_state_schedule,
)

START = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)


def trading_plan(activity: list[tuple[float, float, float]]) -> tuple[TradingSlotPlan, ...]:
    result = []
    for index, (charge, load, export) in enumerate(activity):
        result.append(
            TradingSlotPlan(
                timestamp=START + timedelta(minutes=15 * index),
                buy_price_czk_per_kwh=8.0,
                sell_price_czk_per_kwh=5.0,
                pv_forecast_kwh=charge,
                load_forecast_kwh=load,
                planned_grid_import_kwh=0.0,
                planned_grid_export_kwh=export,
                batteries={
                    "DEYE": BatterySlotPlan(
                        charge_from_pv_kwh=charge,
                        discharge_to_load_kwh=load,
                        discharge_to_export_kwh=export,
                        planned_power_w=(charge - load - export) * 4_000,
                        projected_soc_pct=50.0,
                        active_soc_floor_pct=10.0,
                        trading_export_blocked=False,
                        overnight_trading_export_blocked=False,
                    )
                },
                action=TradingAction.HOLD,
                reason="TEST",
                guard_only=False,
            )
        )
    return tuple(result)


ACTIVE = (0.1, 0.0, 0.0)
IDLE = (0.0, 0.0, 0.0)


def test_continuous_activity_is_never_off_eligible() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([ACTIVE] * 8))

    assert all(item.deye_power_state is DeyePowerState.ON_REQUIRED for item in schedule.slots)
    assert schedule.total_off_eligible_duration == timedelta(0)


def test_long_idle_gap_becomes_off_eligible_and_reports_window() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([ACTIVE, IDLE, IDLE, IDLE, IDLE, ACTIVE]))

    assert [item.deye_power_state for item in schedule.slots] == [
        DeyePowerState.ON_REQUIRED,
        DeyePowerState.READY,
        DeyePowerState.OFF_ELIGIBLE,
        DeyePowerState.OFF_ELIGIBLE,
        DeyePowerState.STARTING,
        DeyePowerState.ON_REQUIRED,
    ]
    assert schedule.off_windows[0].off_from == START + timedelta(minutes=30)
    assert schedule.off_windows[0].on_again_by == START + timedelta(minutes=60)
    assert schedule.off_windows[0].next_required_activity == START + timedelta(minutes=75)
    assert schedule.off_windows[0].off_duration == timedelta(minutes=30)


def test_next_activity_inside_startup_lead_is_on_required() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([IDLE, IDLE, ACTIVE]))

    assert schedule.slots[1].deye_power_state is DeyePowerState.STARTING
    assert schedule.slots[1].deye_required is True


def test_short_idle_gap_below_minimum_off_time_remains_on() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([ACTIVE, IDLE, IDLE, ACTIVE]))

    assert all(item.deye_power_state is not DeyePowerState.OFF_ELIGIBLE for item in schedule.slots)


def test_shutdown_delay_keeps_deye_ready_after_activity() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([ACTIVE, IDLE, IDLE, IDLE]))

    assert schedule.slots[1].deye_power_state is DeyePowerState.READY
    assert schedule.slots[2].deye_power_state is DeyePowerState.OFF_ELIGIBLE


def test_multiple_bursts_do_not_create_rapid_power_cycles() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([ACTIVE, IDLE, IDLE, ACTIVE, IDLE, IDLE, ACTIVE]))

    assert schedule.number_of_on_transitions == 0
    assert schedule.number_of_off_transitions == 0
    assert schedule.total_off_eligible_duration == timedelta(0)


def test_configured_idle_power_calculates_estimated_saved_energy() -> None:
    config = DeyePowerStateConfig(
        deye_on_idle_power_w=100.0,
        idle_power_status=AssumptionStatus.CONFIGURED,
    )
    schedule = build_deye_power_state_schedule(trading_plan([IDLE] * 4), config)

    assert schedule.estimated_idle_energy_saved_kwh == pytest.approx(0.1)


def test_unknown_idle_power_does_not_invent_savings() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([IDLE] * 4))

    assert schedule.estimated_idle_energy_saved_kwh is None
    assert schedule.idle_power_status is AssumptionStatus.UNKNOWN


def test_expected_off_is_only_telemetry_context_without_physical_behavior() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([IDLE] * 4))

    assert all(item.telemetry_expectation is DeyeTelemetryExpectation.DEYE_EXPECTED_OFF for item in schedule.slots)
    assert all(item.deye_power_state is DeyePowerState.OFF_ELIGIBLE for item in schedule.slots)
    assert all(not item.overlay_conflict for item in schedule.slots)


def test_real_data_like_long_gap_starts_before_next_pv_charge() -> None:
    schedule = build_deye_power_state_schedule(trading_plan([ACTIVE, *([IDLE] * 48), ACTIVE]))

    assert schedule.longest_off_window == timedelta(hours=11, minutes=30)
    assert schedule.slots[-2].deye_power_state is DeyePowerState.STARTING
    assert schedule.slots[-1].deye_power_state is DeyePowerState.ON_REQUIRED


def test_activity_threshold_is_explicit_and_strict() -> None:
    config = DeyePowerStateConfig(activity_threshold_kwh_per_slot=0.01)
    schedule = build_deye_power_state_schedule(trading_plan([(0.01, 0.0, 0.0)]), config)

    assert schedule.slots[0].deye_power_state is DeyePowerState.OFF_ELIGIBLE


def test_invalid_idle_power_status_is_rejected() -> None:
    config = DeyePowerStateConfig(deye_on_idle_power_w=100.0)

    with pytest.raises(ValueError, match="explicit non-UNKNOWN"):
        build_deye_power_state_schedule(trading_plan([IDLE]), config)
