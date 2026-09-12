from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from apps.energy_v2.trading.models import (
    BatteryParameters,
    BatteryRole,
    CheckpointType,
    PlannerConfig,
    PlannerInput,
    TradingSlotInput,
)
from apps.energy_v2.trading.planner import plan_trading_schedule


def battery(
    name: str,
    role: BatteryRole,
    *,
    charge_w: float = 4_000,
    discharge_w: float = 4_000,
) -> BatteryParameters:
    return BatteryParameters(
        name=name,
        capacity_kwh=10,
        minimum_soc_pct=10,
        terminal_reserve_soc_pct=10,
        max_charge_power_w=charge_w,
        max_discharge_power_w=discharge_w,
        role=role,
    )


def slots(
    start: datetime,
    sell_prices: list[float],
    *,
    pv: list[float] | None = None,
    load: list[float] | None = None,
) -> tuple[TradingSlotInput, ...]:
    pv = pv or [0.0] * len(sell_prices)
    load = load or [0.0] * len(sell_prices)
    return tuple(
        TradingSlotInput(
            timestamp=start + timedelta(minutes=15 * index),
            buy_price_czk_per_kwh=max(12.0, price + 1),
            sell_price_czk_per_kwh=price,
            pv_forecast_kwh=pv[index],
            load_forecast_kwh=load[index],
        )
        for index, price in enumerate(sell_prices)
    )


def priced_then_guard_slots(
    start: datetime,
    *,
    priced_count: int,
    total_count: int,
    sell_price: float = 1.0,
    load_kwh: float = 0.0,
    pv_by_index: dict[int, float] | None = None,
) -> tuple[TradingSlotInput, ...]:
    pv_by_index = pv_by_index or {}
    return tuple(
        TradingSlotInput(
            timestamp=start + timedelta(minutes=15 * index),
            buy_price_czk_per_kwh=max(12.0, sell_price + 1.0) if index < priced_count else None,
            sell_price_czk_per_kwh=sell_price if index < priced_count else None,
            pv_forecast_kwh=pv_by_index.get(index, 0.0),
            load_forecast_kwh=load_kwh,
        )
        for index in range(total_count)
    )


def morning_config() -> PlannerConfig:
    return PlannerConfig(
        solax_morning_trading_start_local_time=time(6, 0),
        solax_morning_trading_end_local_time=time(8, 30),
        solax_recovery_deadline_local_time=time(11, 0),
        solax_morning_conditional_floor_pct=15,
        solax_recovery_target_soc_pct=30,
    )


def morning_input(
    *,
    pv_recovery_kwh: float,
    load_recovery_kwh: float = 0.1,
    charge_w: float = 4_000,
    morning_price: float = 10,
    initial_soc: float = 30,
    morning_load_kwh: float = 0.0,
) -> PlannerInput:
    start = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
    prices = [morning_price] * 10 + [0.5] * 10
    pv = [0.0] * 10 + [pv_recovery_kwh] * 10
    load = [morning_load_kwh] * 10 + [load_recovery_kwh] * 10
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY, charge_w=charge_w)
    return PlannerInput(slots(start, prices, pv=pv, load=load), (solax,), {"SolaX": initial_soc})


def only_assessment(result):
    assert len(result.morning_recovery) == 1
    return next(iter(result.morning_recovery.values()))


def test_deye_trading_battery_can_reach_ten_percent() -> None:
    deye = battery("DEYE", BatteryRole.TRADING_BATTERY, discharge_w=10_000)
    data = PlannerInput(
        slots(datetime(2026, 9, 14, 18, 0, tzinfo=UTC), [20, 20]),
        (deye,),
        {"DEYE": 60},
    )
    result = plan_trading_schedule(data)
    assert result.terminal_soc_pct["DEYE"] == pytest.approx(10)
    assert result.terminal_reserve_target_soc_pct["DEYE"] == pytest.approx(10)
    assert "DEYE is the trading battery" in result.slots[0].reason


def test_solax_evening_checkpoint_preserves_thirty_percent() -> None:
    start = datetime(2026, 9, 14, 16, 0, tzinfo=UTC)
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY, discharge_w=10_000)
    data = PlannerInput(slots(start, [20] * 13), (solax,), {"SolaX": 60})
    result = plan_trading_schedule(data, PlannerConfig(solax_evening_checkpoint_local_time=time(19, 0)))
    checkpoint = result.checkpoints[0]
    assert checkpoint.checkpoint_type is CheckpointType.EVENING_RESERVE
    assert checkpoint.actual_soc_pct == pytest.approx(30)
    assert checkpoint.shortfall_pct == pytest.approx(0)
    assert any("evening house-reserve checkpoint" in slot.reason for slot in result.slots)


def test_strong_pv_selects_morning_exception_and_recovers() -> None:
    result = plan_trading_schedule(morning_input(pv_recovery_kwh=0.5, initial_soc=22), morning_config())
    assessment = only_assessment(result)
    morning_min = min(slot.batteries["SolaX"].projected_soc_pct for slot in result.slots[:10])
    assert assessment.candidate_feasible
    assert assessment.selected
    assert morning_min == pytest.approx(15)
    assert assessment.expected_recovery_soc_pct >= 30
    assert assessment.expected_recovery_time is not None
    assert assessment.morning_start_soc_pct == pytest.approx(22)
    export_reasons = [slot.reason for slot in result.slots[:10] if slot.batteries["SolaX"].discharge_to_export_kwh > 0]
    assert any("entered morning at 22.0%" in reason for reason in export_reasons)
    assert any("conditional 15% trading floor is active" in reason for reason in export_reasons)


def test_weak_pv_keeps_normal_morning_floor() -> None:
    result = plan_trading_schedule(morning_input(pv_recovery_kwh=0.1, initial_soc=22), morning_config())
    assessment = only_assessment(result)
    assert not assessment.candidate_feasible
    assert not assessment.selected
    assert min(slot.batteries["SolaX"].projected_soc_pct for slot in result.slots[:10]) == pytest.approx(22)
    assert all(slot.batteries["SolaX"].discharge_to_export_kwh == pytest.approx(0) for slot in result.slots[:10])
    assert "cannot restore SolaX to 30%" in result.slots[0].reason


def test_normal_candidate_allows_morning_house_load_discharge() -> None:
    result = plan_trading_schedule(
        morning_input(
            pv_recovery_kwh=0,
            load_recovery_kwh=0,
            initial_soc=22,
            morning_load_kwh=0.1,
        ),
        morning_config(),
    )
    assert sum(slot.batteries["SolaX"].discharge_to_load_kwh for slot in result.slots[:10]) > 0
    assert sum(slot.batteries["SolaX"].discharge_to_export_kwh for slot in result.slots[:10]) == pytest.approx(0)
    assert min(slot.batteries["SolaX"].projected_soc_pct for slot in result.slots[:10]) < 22
    assert "remaining SOC is available for house operation" in result.slots[0].reason


def test_evening_reserve_is_available_for_overnight_house_load() -> None:
    start = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY)
    data = PlannerInput(slots(start, [0] * 4, load=[0.2] * 4), (solax,), {"SolaX": 30})
    result = plan_trading_schedule(data, PlannerConfig(solax_evening_checkpoint_local_time=time(18, 0)))
    assert result.checkpoints[0].actual_soc_pct == pytest.approx(30)
    assert result.terminal_soc_pct["SolaX"] < 30
    assert "using the 30% evening reserve" in result.slots[0].reason


def test_evening_reserve_blocks_trading_export_and_projects_guard_horizon() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY, discharge_w=10_000)
    data = PlannerInput(
        priced_then_guard_slots(start, priced_count=12, total_count=36, sell_price=100, load_kwh=0.05),
        (solax,),
        {"SolaX": 30},
    )
    config = PlannerConfig(
        solax_evening_checkpoint_local_time=time(21, 0),
        solax_morning_trading_start_local_time=time(6, 0),
        solax_morning_trading_end_local_time=time(8, 30),
        solax_recovery_deadline_local_time=time(11, 0),
    )

    result = plan_trading_schedule(data, config)

    assert result.economic_horizon_end == datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    assert result.horizon_end == datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    assert all(slot.batteries["SolaX"].discharge_to_export_kwh == pytest.approx(0) for slot in result.slots)
    assert sum(slot.batteries["SolaX"].discharge_to_load_kwh for slot in result.slots) > 0
    assert result.slots[11].batteries["SolaX"].projected_soc_pct < 30
    assert result.slots[-1].batteries["SolaX"].projected_soc_pct < result.slots[11].batteries["SolaX"].projected_soc_pct
    assert "evening reserve is dedicated to overnight house operation" in result.slots[4].reason
    assert "GUARD ONLY" in result.slots[12].reason


def test_high_price_after_checkpoint_cannot_unlock_solax_but_deye_trades() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY, discharge_w=10_000)
    deye = battery("DEYE", BatteryRole.TRADING_BATTERY, discharge_w=10_000)
    prices = [1.0, 1.0, 1.0, 1.0, 100.0, 1.0]
    data = PlannerInput(slots(start, prices), (deye, solax), {"DEYE": 60, "SolaX": 30})
    config = PlannerConfig(
        solax_evening_checkpoint_local_time=time(21, 0),
        solax_morning_trading_start_local_time=time(6, 0),
        solax_morning_trading_end_local_time=time(8, 30),
        solax_recovery_deadline_local_time=time(11, 0),
    )

    result = plan_trading_schedule(data, config)

    peak = result.slots[4]
    assert peak.batteries["SolaX"].discharge_to_export_kwh == pytest.approx(0)
    assert peak.batteries["DEYE"].discharge_to_export_kwh > 0


def test_morning_window_releases_overnight_block_for_recovery_candidate() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
    total_count = 56
    prices = [0.0] * total_count
    pv = [0.0] * total_count
    morning_start_index = 36
    morning_end_index = 46
    recovery_deadline_index = 56
    for index in range(morning_start_index, morning_end_index):
        prices[index] = 10.0
    for index in range(morning_end_index, recovery_deadline_index):
        pv[index] = 0.5
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY, charge_w=4_000, discharge_w=10_000)
    data = PlannerInput(slots(start, prices, pv=pv), (solax,), {"SolaX": 30})
    config = PlannerConfig(
        solax_evening_checkpoint_local_time=time(21, 0),
        solax_morning_trading_start_local_time=time(6, 0),
        solax_morning_trading_end_local_time=time(8, 30),
        solax_recovery_deadline_local_time=time(11, 0),
    )

    result = plan_trading_schedule(data, config)

    assert all(
        slot.batteries["SolaX"].discharge_to_export_kwh == pytest.approx(0)
        for slot in result.slots[:morning_start_index]
    )
    assert (
        sum(
            slot.batteries["SolaX"].discharge_to_export_kwh
            for slot in result.slots[morning_start_index:morning_end_index]
        )
        > 0
    )
    assert only_assessment(result).selected


def test_charge_power_limit_blocks_morning_exception() -> None:
    result = plan_trading_schedule(morning_input(pv_recovery_kwh=1.0, charge_w=500), morning_config())
    assessment = only_assessment(result)
    assert assessment.forecast_pv_surplus_for_recovery_kwh > assessment.required_recovery_kwh
    assert assessment.maximum_storable_recovery_kwh < assessment.required_recovery_kwh
    assert not assessment.candidate_feasible
    assert not assessment.selected


def test_house_load_consumes_recovery_pv() -> None:
    result = plan_trading_schedule(
        morning_input(pv_recovery_kwh=0.3, load_recovery_kwh=0.2),
        morning_config(),
    )
    assessment = only_assessment(result)
    assert not assessment.candidate_feasible
    assert not assessment.selected


def test_unreachable_evening_checkpoint_reports_explicit_shortfall() -> None:
    start = datetime(2026, 9, 14, 16, 0, tzinfo=UTC)
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY)
    data = PlannerInput(slots(start, [1] * 5), (solax,), {"SolaX": 15})
    result = plan_trading_schedule(data, PlannerConfig(solax_evening_checkpoint_local_time=time(17, 0)))
    checkpoint = result.checkpoints[0]
    assert not checkpoint.hard
    assert checkpoint.actual_soc_pct == pytest.approx(15)
    assert checkpoint.shortfall_pct == pytest.approx(15)
    assert "EVENING_RESERVE_SHORTFALL" in checkpoint.reason


def test_non_quarter_checkpoint_maps_to_first_following_slot() -> None:
    start = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)
    solax = battery("SolaX", BatteryRole.HOUSE_RESERVE_BATTERY)
    data = PlannerInput(slots(start, [2] * 4), (solax,), {"SolaX": 40})
    result = plan_trading_schedule(data, PlannerConfig(solax_evening_checkpoint_local_time=time(18, 7)))
    assert result.checkpoints[0].timestamp == datetime(2026, 9, 14, 18, 15, tzinfo=UTC)


def test_feasible_exception_is_selected_only_when_economically_better() -> None:
    profitable = plan_trading_schedule(morning_input(pv_recovery_kwh=0.5, morning_price=10), morning_config())
    not_profitable = plan_trading_schedule(morning_input(pv_recovery_kwh=0.5, morning_price=0), morning_config())
    assert only_assessment(profitable).selected
    assert only_assessment(not_profitable).candidate_feasible
    assert not only_assessment(not_profitable).selected


def test_battery_role_and_active_floor_are_exposed() -> None:
    result = plan_trading_schedule(morning_input(pv_recovery_kwh=0.5), morning_config())
    assert result.battery_role["SolaX"] is BatteryRole.HOUSE_RESERVE_BATTERY
    assert min(slot.batteries["SolaX"].active_soc_floor_pct for slot in result.slots[:10]) == pytest.approx(15)
