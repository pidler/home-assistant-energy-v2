from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v2.trading.models import (
    BatteryParameters,
    ForecastQuality,
    PlannerConfig,
    PlannerInput,
    TradingAction,
    TradingSlotInput,
)
from apps.energy_v2.trading.planner import compare_plans, plan_trading_schedule
from apps.energy_v2.trading.renderer import render_json, render_text

START = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)


def battery(
    name: str = "DEYE",
    *,
    capacity: float = 10,
    reserve: float = 20,
    charge_w: float = 4_000,
    discharge_w: float = 4_000,
) -> BatteryParameters:
    return BatteryParameters(
        name=name,
        capacity_kwh=capacity,
        minimum_soc_pct=10,
        maximum_soc_pct=100,
        terminal_reserve_soc_pct=reserve,
        max_charge_power_w=charge_w,
        max_discharge_power_w=discharge_w,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
    )


def planning_input(
    prices: list[float],
    *,
    pv: list[float] | None = None,
    load: list[float] | None = None,
    batteries: tuple[BatteryParameters, ...] | None = None,
    initial_soc: dict[str, float] | None = None,
) -> PlannerInput:
    pv = pv or [0.0] * len(prices)
    load = load or [0.0] * len(prices)
    batteries = batteries or (battery(),)
    initial_soc = initial_soc or {item.name: 50.0 for item in batteries}
    return PlannerInput(
        slots=tuple(
            TradingSlotInput(
                timestamp=START + timedelta(minutes=15 * index),
                buy_price_czk_per_kwh=max(8.0, price + 1.0),
                sell_price_czk_per_kwh=price,
                pv_forecast_kwh=pv[index],
                load_forecast_kwh=load[index],
            )
            for index, price in enumerate(prices)
        ),
        batteries=batteries,
        initial_soc_pct=initial_soc,
        generated_at=START,
        load_forecast_quality=ForecastQuality.SIMPLE_BASELINE,
    )


def test_a_evening_peak_exports_at_high_price() -> None:
    result = plan_trading_schedule(planning_input([1, 5, 1]))
    assert result.slots[1].planned_grid_export_kwh > 0
    assert result.slots[1].action is TradingAction.EXPORT
    assert result.slots[0].planned_grid_export_kwh == pytest.approx(0)


def test_b_tomorrow_better_weak_pv_reserves_energy() -> None:
    before = plan_trading_schedule(planning_input([4, 1]))
    after = plan_trading_schedule(planning_input([4, 1, 6, 6, 6, 6, 1]))
    assert before.slots[0].planned_grid_export_kwh > 0
    assert after.slots[0].planned_grid_export_kwh == pytest.approx(0)
    assert sum(slot.planned_grid_export_kwh for slot in after.slots[2:6]) > 0


def test_c_strong_pv_allows_deeper_discharge_before_refill() -> None:
    weak = plan_trading_schedule(planning_input([4, 1, 6, 6, 1]))
    sunny = plan_trading_schedule(planning_input([4, 1, 6, 6, 1], pv=[0, 0, 0, 0, 5]))
    weak_export = sum(slot.planned_grid_export_kwh for slot in weak.slots[:4])
    sunny_export = sum(slot.planned_grid_export_kwh for slot in sunny.slots[:4])
    assert sunny_export > weak_export


def test_d_low_midday_price_charges_from_pv() -> None:
    result = plan_trading_schedule(planning_input([0.1, 6], pv=[0.5, 0]))
    assert result.slots[0].action is TradingAction.CHARGE_FROM_PV
    assert result.slots[0].batteries["DEYE"].charge_from_pv_kwh > 0


def test_e_full_battery_exports_pv_surplus() -> None:
    result = plan_trading_schedule(planning_input([3], pv=[3], initial_soc={"DEYE": 100}))
    assert result.slots[0].planned_grid_export_kwh > 0
    assert result.slots[0].batteries["DEYE"].charge_from_pv_kwh == pytest.approx(0)


def test_f_soc_floor_blocks_discharge() -> None:
    item = battery(reserve=10)
    result = plan_trading_schedule(planning_input([100], batteries=(item,), initial_soc={"DEYE": 10}))
    assert result.slots[0].planned_grid_export_kwh == pytest.approx(0)
    assert result.terminal_soc_pct["DEYE"] == pytest.approx(10)


def test_g_export_cap_is_hard_constraint() -> None:
    item = battery(discharge_w=30_000)
    result = plan_trading_schedule(planning_input([100], pv=[10], batteries=(item,), initial_soc={"DEYE": 100}))
    assert result.slots[0].planned_grid_export_kwh <= 9.8 * 0.25 + 1e-9
    assert result.slots[0].planned_grid_export_kwh == pytest.approx(2.45)


def test_h_negative_buy_price_still_does_not_grid_charge() -> None:
    data = planning_input([1], load=[1], initial_soc={"DEYE": 20})
    negative_price_slot = TradingSlotInput(START, -5, -6, 0, 1)
    data = PlannerInput((negative_price_slot,), data.batteries, data.initial_soc_pct)
    result = plan_trading_schedule(data)
    assert result.expected_import_kwh == pytest.approx(1)
    assert result.slots[0].batteries["DEYE"].charge_from_pv_kwh == pytest.approx(0)
    assert result.terminal_soc_pct["DEYE"] == pytest.approx(20)


def test_all_negative_sell_prices_remain_bounded() -> None:
    result = plan_trading_schedule(planning_input([-5, -4]))
    assert result.expected_export_kwh == pytest.approx(0)
    assert result.terminal_value_czk_per_kwh["DEYE"] == pytest.approx(1)


def test_invalid_tariff_spread_is_rejected_instead_of_allowing_grid_arbitrage() -> None:
    data = planning_input([1])
    invalid_slot = TradingSlotInput(START, 0, 1, 0, 0)
    invalid = PlannerInput((invalid_slot,), data.batteries, data.initial_soc_pct)
    with pytest.raises(ValueError, match="buy price"):
        plan_trading_schedule(invalid)


def test_i_terminal_reserve_prevents_horizon_edge_dump() -> None:
    result = plan_trading_schedule(planning_input([1, 100]))
    assert result.slots[-1].planned_grid_export_kwh > 0
    assert result.terminal_soc_pct["DEYE"] >= 20
    assert result.terminal_soc_pct["DEYE"] < 50
    assert result.terminal_reserve_target_kwh["DEYE"] == pytest.approx(2)


def test_terminal_value_changes_horizon_edge_decision() -> None:
    item = battery(reserve=10, discharge_w=20_000)
    data = planning_input([100], batteries=(item,), initial_soc={"DEYE": 50})
    without_value = plan_trading_schedule(data, PlannerConfig(terminal_value_factor=0))
    with_value = plan_trading_schedule(data, PlannerConfig(terminal_value_factor=1))
    assert with_value.terminal_soc_pct["DEYE"] > without_value.terminal_soc_pct["DEYE"]
    assert with_value.terminal_value_czk_per_kwh["DEYE"] == pytest.approx(95)


def test_past_peak_does_not_inflate_terminal_continuation_value() -> None:
    result = plan_trading_schedule(planning_input([10] + [2] * 12))
    assert result.terminal_continuation_price_czk_per_kwh == pytest.approx(2)
    assert result.terminal_value_czk_per_kwh["DEYE"] == pytest.approx(2 * 0.95 * 0.60)


def test_explicit_terminal_continuation_price_is_supported() -> None:
    result = plan_trading_schedule(
        planning_input([10, 1]),
        PlannerConfig(terminal_continuation_price_czk_per_kwh=3),
    )
    assert result.terminal_continuation_price_czk_per_kwh == pytest.approx(3)
    assert result.terminal_value_method == "EXPLICIT_CONTINUATION_PRICE"


def test_soft_terminal_reserve_remains_feasible_below_target() -> None:
    result = plan_trading_schedule(planning_input([5], initial_soc={"DEYE": 11}))
    assert result.terminal_soc_pct["DEYE"] == pytest.approx(11)
    assert result.terminal_reserve_shortfall_kwh["DEYE"] == pytest.approx(0.9)
    assert "reserve protected" not in result.slots[0].reason
    assert "shortfall 0.90 kWh" in result.slots[0].reason


def test_j_d_plus_one_replan_changes_today_decision() -> None:
    today_only = plan_trading_schedule(planning_input([4, 1]))
    with_d_plus_one = plan_trading_schedule(planning_input([4, 1, 6, 6, 6, 6, 1]))
    assert today_only.slots[0].action is TradingAction.EXPORT
    assert with_d_plus_one.slots[0].action is TradingAction.HOLD
    assert "solved plan" in with_d_plus_one.slots[0].reason.lower()
    assert "later exports" in with_d_plus_one.slots[0].reason


def test_higher_future_price_without_planned_export_is_not_claimed_as_reserved() -> None:
    result = plan_trading_schedule(planning_input([1, 10], initial_soc={"DEYE": 10}))
    assert result.slots[0].action is TradingAction.HOLD
    assert "held for" not in result.slots[0].reason.lower()
    assert "no battery export" in result.slots[0].reason.lower()


def test_actual_future_battery_export_can_explain_hold_for_later_price() -> None:
    item = battery(reserve=10)
    result = plan_trading_schedule(planning_input([1, 8], batteries=(item,), initial_soc={"DEYE": 50}))
    assert result.slots[0].action is TradingAction.HOLD
    assert result.slots[1].batteries["DEYE"].discharge_to_export_kwh > 0
    assert "later exports" in result.slots[0].reason


def test_deye_is_preferred_only_as_tie_break() -> None:
    batteries = (battery("DEYE", discharge_w=12_000), battery("SolaX"))
    result = plan_trading_schedule(planning_input([10], batteries=batteries, initial_soc={"DEYE": 50, "SolaX": 30}))
    assert result.slots[0].batteries["DEYE"].discharge_to_export_kwh > 0
    assert result.slots[0].batteries["SolaX"].discharge_to_export_kwh == pytest.approx(0)


def test_power_energy_conversion_uses_quarter_hour() -> None:
    item = battery(discharge_w=4_000)
    result = plan_trading_schedule(planning_input([100], batteries=(item,), initial_soc={"DEYE": 100}))
    battery_plan = result.slots[0].batteries["DEYE"]
    assert battery_plan.discharge_to_export_kwh == pytest.approx(1)
    assert battery_plan.planned_power_w == pytest.approx(-4_000)


def test_input_validation_rejects_grid_charging() -> None:
    with pytest.raises(ValueError, match="grid charging"):
        plan_trading_schedule(planning_input([1]), PlannerConfig(grid_charging_allowed=True))


def test_input_validation_rejects_non_finite_values() -> None:
    data = planning_input([float("nan")])
    with pytest.raises(ValueError, match="finite"):
        plan_trading_schedule(data)


def test_input_validation_rejects_non_quarter_hour_cadence() -> None:
    data = planning_input([1, 2])
    bad_slot = TradingSlotInput(START + timedelta(minutes=20), 8, 2, 0, 0)
    bad_data = PlannerInput((data.slots[0], bad_slot), data.batteries, data.initial_soc_pct)
    with pytest.raises(ValueError, match="15-minute cadence"):
        plan_trading_schedule(bad_data)


def test_renderer_contains_summary_table_reasons_and_json() -> None:
    result = plan_trading_schedule(planning_input([1, 5]))
    text = render_text(result)
    rendered_json = render_json(result)
    assert "Expected net grid value" in text
    assert "IMPORTANT DECISIONS" in text
    assert "HEURISTIC SOFT RESERVE" in text
    assert "physical minimum 10.0%" in text
    assert "MODEL_ASSUMPTION" in text
    assert "DEYE SOC" in text
    assert '"expected_export_kwh"' in rendered_json


def test_manual_plan_comparison_data_model() -> None:
    optimizer = plan_trading_schedule(planning_input([2] * 12))
    manual = replace(
        optimizer,
        expected_net_grid_value_czk=optimizer.expected_net_grid_value_czk + 1,
        terminal_stored_kwh={"DEYE": 1},
        terminal_soc_pct={"DEYE": 10},
    )
    comparison = compare_plans(optimizer, manual)
    assert comparison.grid_cashflow_difference_czk == pytest.approx(-1)
    assert comparison.terminal_stored_energy_difference_kwh > 0
    assert comparison.terminal_value_adjustment_difference_czk > 1
    assert comparison.comparable_value_difference_czk > 0
    assert comparison.optimizer_terminal_soc_pct == optimizer.terminal_soc_pct


def test_plan_comparison_requires_same_terminal_valuation_policy() -> None:
    optimizer = plan_trading_schedule(planning_input([2] * 12))
    manual = replace(optimizer, terminal_value_czk_per_kwh={"DEYE": 99})
    with pytest.raises(ValueError, match="same terminal valuation"):
        compare_plans(optimizer, manual)


def test_tomorrow_peak_inside_horizon_is_scheduled_not_terminal_artifact() -> None:
    prices = [3] * 8 + [9] * 4 + [1] * 12
    result = plan_trading_schedule(planning_input(prices, initial_soc={"DEYE": 60}))
    assert sum(slot.batteries["DEYE"].discharge_to_export_kwh for slot in result.slots[8:12]) > 0
    assert result.terminal_continuation_price_czk_per_kwh == pytest.approx(1)


def test_weak_horizon_edge_keeps_only_soft_target_not_old_peak_value() -> None:
    result = plan_trading_schedule(planning_input([10] + [1] * 16, initial_soc={"DEYE": 80}))
    assert result.slots[0].batteries["DEYE"].discharge_to_export_kwh > 0
    assert result.terminal_value_czk_per_kwh["DEYE"] == pytest.approx(1)
    assert sum(slot.planned_grid_export_kwh for slot in result.slots[1:]) == pytest.approx(0)
    assert result.terminal_soc_pct["DEYE"] < 80


def test_sign_convention_charge_positive_discharge_negative() -> None:
    charged = plan_trading_schedule(planning_input([0.1, 6], pv=[2, 0]))
    discharged = plan_trading_schedule(planning_input([100]))
    assert charged.slots[0].batteries["DEYE"].planned_power_w > 0
    assert discharged.slots[0].batteries["DEYE"].planned_power_w < 0


def test_positive_cycle_penalty_avoids_simultaneous_charge_and_discharge() -> None:
    result = plan_trading_schedule(planning_input([5, 5], pv=[2, 2]))
    for slot in result.slots:
        battery_plan = slot.batteries["DEYE"]
        discharged = battery_plan.discharge_to_load_kwh + battery_plan.discharge_to_export_kwh
        assert not (battery_plan.charge_from_pv_kwh > 1e-6 and discharged > 1e-6)


def test_guard_only_slot_blocks_export_but_keeps_pv_charging_active() -> None:
    item = battery(reserve=10)
    slots = (
        TradingSlotInput(START, 8.0, 1.0, 0.0, 0.0),
        TradingSlotInput(START + timedelta(minutes=15), None, None, 1.0, 0.0),
    )
    data = PlannerInput(slots, (item,), {"DEYE": 10})

    result = plan_trading_schedule(data)
    guard = result.slots[1]

    assert result.economic_horizon_end == START + timedelta(minutes=15)
    assert result.horizon_end == START + timedelta(minutes=30)
    assert guard.guard_only
    assert guard.planned_grid_export_kwh == pytest.approx(0)
    assert guard.batteries["DEYE"].discharge_to_export_kwh == pytest.approx(0)
    assert guard.batteries["DEYE"].charge_from_pv_kwh > 0
    assert "price is unavailable" in guard.reason
    assert "GUARD" in render_text(result)


def test_economic_and_physical_terminal_states_are_distinct() -> None:
    item = battery(reserve=20)
    priced = TradingSlotInput(START, 8.0, 1.0, 0.0, 0.0)
    guard = tuple(
        TradingSlotInput(START + timedelta(minutes=15 * index), None, None, 0.0, 0.1) for index in range(1, 5)
    )

    result = plan_trading_schedule(PlannerInput((priced, *guard), (item,), {"DEYE": 30}))

    assert result.economic_terminal_soc_pct["DEYE"] == pytest.approx(
        result.slots[0].batteries["DEYE"].projected_soc_pct
    )
    assert result.physical_terminal_soc_pct["DEYE"] < result.economic_terminal_soc_pct["DEYE"]
    assert result.terminal_soc_pct == result.economic_terminal_soc_pct
    assert result.terminal_stored_kwh == result.economic_terminal_stored_kwh
    rendered = render_text(result)
    assert "ECONOMIC HORIZON END" in rendered
    assert "PHYSICAL GUARD END" in rendered


def test_guard_does_not_move_continuation_valuation_or_priced_decisions() -> None:
    item = battery(reserve=20)
    base = planning_input([1, 7, 2], batteries=(item,), initial_soc={"DEYE": 60})
    guard = tuple(
        TradingSlotInput(base.slots[-1].timestamp + timedelta(minutes=15 * index), None, None, 0.0, 0.0)
        for index in range(1, 9)
    )

    without_guard = plan_trading_schedule(base)
    with_guard = plan_trading_schedule(
        PlannerInput(
            (*base.slots, *guard),
            base.batteries,
            base.initial_soc_pct,
            generated_at=base.generated_at,
            load_forecast_quality=base.load_forecast_quality,
        )
    )

    assert with_guard.terminal_continuation_price_czk_per_kwh == pytest.approx(
        without_guard.terminal_continuation_price_czk_per_kwh
    )
    assert with_guard.terminal_value_czk_per_kwh == without_guard.terminal_value_czk_per_kwh
    assert with_guard.economic_terminal_stored_kwh == pytest.approx(without_guard.economic_terminal_stored_kwh)
    assert with_guard.objective_value_czk == pytest.approx(without_guard.objective_value_czk)
    for expected, actual in zip(without_guard.slots, with_guard.slots[: len(base.slots)], strict=True):
        assert actual.planned_grid_import_kwh == pytest.approx(expected.planned_grid_import_kwh)
        assert actual.planned_grid_export_kwh == pytest.approx(expected.planned_grid_export_kwh)
        assert actual.batteries["DEYE"].planned_power_w == pytest.approx(expected.batteries["DEYE"].planned_power_w)


def test_guard_only_slot_requires_both_prices_to_be_unavailable() -> None:
    item = battery(reserve=10)
    bad = TradingSlotInput(START, None, 1.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="both be available or both be unavailable"):
        plan_trading_schedule(PlannerInput((bad,), (item,), {"DEYE": 10}))
