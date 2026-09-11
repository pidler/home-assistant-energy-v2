from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pulp

from .explanation import explain_slot, price_percentile
from .models import (
    BatterySlotPlan,
    ImportantDecision,
    PlanComparison,
    PlannerConfig,
    PlannerInput,
    PlannerResult,
    TradingAction,
    TradingSlotPlan,
)
from .objective import terminal_reserve_shortfall_costs, terminal_values

_EPSILON_KWH = 1e-5


def plan_trading_schedule(data: PlannerInput, config: PlannerConfig | None = None) -> PlannerResult:
    """Solve a passive 15-minute summer schedule as a deterministic linear program.

    Battery power is positive for charging and negative for discharging. The
    function is pure computation: it has no Home Assistant or Phase 4 imports.
    """

    config = config or PlannerConfig()
    data.validate()
    config.validate()
    batteries = {battery.name: battery for battery in data.batteries}
    names = tuple(batteries)
    slot_indexes = range(len(data.slots))
    problem = pulp.LpProblem("energy_v2_phase_5a_shadow", pulp.LpMaximize)

    pv_to_load = pulp.LpVariable.dicts("pv_to_load", slot_indexes, lowBound=0)
    pv_export = pulp.LpVariable.dicts("pv_export", slot_indexes, lowBound=0)
    grid_import = pulp.LpVariable.dicts("grid_import", slot_indexes, lowBound=0)
    charge = pulp.LpVariable.dicts("pv_charge", (names, slot_indexes), lowBound=0)
    discharge_load = pulp.LpVariable.dicts("battery_to_load", (names, slot_indexes), lowBound=0)
    discharge_export = pulp.LpVariable.dicts("battery_export", (names, slot_indexes), lowBound=0)
    stored = {
        name: pulp.LpVariable.dicts(
            f"stored_{name}",
            range(len(data.slots) + 1),
            lowBound=batteries[name].capacity_kwh * batteries[name].minimum_soc_pct / 100,
            upBound=batteries[name].capacity_kwh * batteries[name].maximum_soc_pct / 100,
        )
        for name in names
    }
    reserve_shortfall = {name: pulp.LpVariable(f"terminal_reserve_shortfall_{name}", lowBound=0) for name in names}

    for name, battery in batteries.items():
        problem += stored[name][0] == battery.capacity_kwh * data.initial_soc_pct[name] / 100
        for index in slot_indexes:
            problem += charge[name][index] <= battery.max_charge_power_w / 1000 * config.slot_hours
            problem += (
                discharge_load[name][index] + discharge_export[name][index]
                <= battery.max_discharge_power_w / 1000 * config.slot_hours
            )
            problem += stored[name][index + 1] == (
                stored[name][index]
                + charge[name][index] * battery.charge_efficiency
                - (discharge_load[name][index] + discharge_export[name][index]) / battery.discharge_efficiency
            )
        terminal_reserve_kwh = battery.capacity_kwh * battery.terminal_reserve_soc_pct / 100
        problem += reserve_shortfall[name] >= terminal_reserve_kwh - stored[name][len(data.slots)]

    export_cap_kwh = config.site_export_limit_w / 1000 * config.slot_hours
    import_cap_kwh = config.site_import_limit_w / 1000 * config.slot_hours
    for index, slot in enumerate(data.slots):
        problem += (
            pv_to_load[index] + pv_export[index] + pulp.lpSum(charge[name][index] for name in names)
            <= slot.pv_forecast_kwh
        )
        problem += (
            pv_to_load[index] + grid_import[index] + pulp.lpSum(discharge_load[name][index] for name in names)
            == slot.load_forecast_kwh
        )
        problem += pv_export[index] + pulp.lpSum(discharge_export[name][index] for name in names) <= export_cap_kwh
        problem += grid_import[index] <= import_cap_kwh

    continuation_values = terminal_values(data.slots, data.batteries, config)
    reserve_shortfall_costs = terminal_reserve_shortfall_costs(data.slots, data.batteries, config)
    revenue = pulp.lpSum(
        data.slots[index].sell_price_czk_per_kwh
        * (pv_export[index] + pulp.lpSum(discharge_export[name][index] for name in names))
        for index in slot_indexes
    )
    import_cost = pulp.lpSum(data.slots[index].buy_price_czk_per_kwh * grid_import[index] for index in slot_indexes)
    cycling_cost = config.cycling_penalty_czk_per_kwh * pulp.lpSum(
        charge[name][index] + discharge_load[name][index] + discharge_export[name][index]
        for name in names
        for index in slot_indexes
    )
    solax_tie_break = config.solax_tie_break_penalty_czk_per_kwh * pulp.lpSum(
        discharge_load[name][index] + discharge_export[name][index]
        for name in names
        if name.casefold() == "solax"
        for index in slot_indexes
    )
    terminal_value = pulp.lpSum(continuation_values[name] * stored[name][len(data.slots)] for name in names)
    reserve_shortfall_cost = pulp.lpSum(reserve_shortfall_costs[name] * reserve_shortfall[name] for name in names)
    problem += revenue - import_cost - cycling_cost - solax_tie_break + terminal_value - reserve_shortfall_cost

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False, threads=1))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"trading planner did not find an optimal solution: {pulp.LpStatus[status]}")

    prices = tuple(slot.sell_price_czk_per_kwh for slot in data.slots)
    planned_slots: list[TradingSlotPlan] = []
    for index, slot in enumerate(data.slots):
        battery_plans: dict[str, BatterySlotPlan] = {}
        total_charge = 0.0
        total_battery_export = 0.0
        for name, battery in batteries.items():
            charge_kwh = _value(charge[name][index])
            to_load_kwh = _value(discharge_load[name][index])
            to_export_kwh = _value(discharge_export[name][index])
            total_charge += charge_kwh
            total_battery_export += to_export_kwh
            net_ac_kwh = charge_kwh - to_load_kwh - to_export_kwh
            battery_plans[name] = BatterySlotPlan(
                charge_from_pv_kwh=charge_kwh,
                discharge_to_load_kwh=to_load_kwh,
                discharge_to_export_kwh=to_export_kwh,
                planned_power_w=net_ac_kwh / config.slot_hours * 1000,
                projected_soc_pct=100 * _value(stored[name][index + 1]) / battery.capacity_kwh,
            )
        export_kwh = _value(pv_export[index]) + total_battery_export
        import_kwh = _value(grid_import[index])
        if export_kwh > _EPSILON_KWH:
            action = TradingAction.EXPORT
        elif total_charge > _EPSILON_KWH:
            action = TradingAction.CHARGE_FROM_PV
        elif import_kwh > _EPSILON_KWH:
            action = TradingAction.IMPORT_FOR_LOAD
        else:
            action = TradingAction.HOLD
        later_prices = prices[index + 1 :]
        future_peak = max(later_prices) if later_prices else None
        reason = explain_slot(
            slot,
            action=action,
            battery_export_kwh=total_battery_export,
            future_peak=future_peak,
            price_percentile=price_percentile(prices, slot.sell_price_czk_per_kwh),
        )
        planned_slots.append(
            TradingSlotPlan(
                timestamp=slot.timestamp,
                buy_price_czk_per_kwh=slot.buy_price_czk_per_kwh,
                sell_price_czk_per_kwh=slot.sell_price_czk_per_kwh,
                pv_forecast_kwh=slot.pv_forecast_kwh,
                load_forecast_kwh=slot.load_forecast_kwh,
                planned_grid_import_kwh=import_kwh,
                planned_grid_export_kwh=export_kwh,
                batteries=battery_plans,
                action=action,
                reason=reason,
            )
        )

    revenue_value = sum(slot.planned_grid_export_kwh * slot.sell_price_czk_per_kwh for slot in planned_slots)
    import_cost_value = sum(slot.planned_grid_import_kwh * slot.buy_price_czk_per_kwh for slot in planned_slots)
    decisions = _important_decisions(planned_slots)
    terminal_soc = {
        name: 100 * _value(stored[name][len(data.slots)]) / battery.capacity_kwh for name, battery in batteries.items()
    }
    terminal_shortfall = {name: _value(reserve_shortfall[name]) for name in names}
    reserves = {
        name: battery.capacity_kwh * battery.terminal_reserve_soc_pct / 100 for name, battery in batteries.items()
    }
    generated_at = data.generated_at or datetime.now(UTC)
    return PlannerResult(
        generated_at=generated_at,
        horizon_start=data.slots[0].timestamp,
        horizon_end=data.slots[-1].timestamp + timedelta(hours=config.slot_hours),
        slots=tuple(planned_slots),
        initial_soc_pct=dict(data.initial_soc_pct),
        terminal_soc_pct=terminal_soc,
        terminal_value_czk_per_kwh=continuation_values,
        terminal_reserved_kwh=reserves,
        terminal_reserve_shortfall_kwh=terminal_shortfall,
        expected_export_kwh=sum(slot.planned_grid_export_kwh for slot in planned_slots),
        expected_import_kwh=sum(slot.planned_grid_import_kwh for slot in planned_slots),
        expected_revenue_czk=revenue_value,
        expected_import_cost_czk=import_cost_value,
        expected_net_grid_value_czk=revenue_value - import_cost_value,
        objective_value_czk=_value(problem.objective),
        load_forecast_quality=data.load_forecast_quality,
        important_decisions=decisions,
    )


def compare_plans(optimizer: PlannerResult, manual: PlannerResult) -> PlanComparison:
    return PlanComparison(
        optimizer_net_value_czk=optimizer.expected_net_grid_value_czk,
        manual_net_value_czk=manual.expected_net_grid_value_czk,
        difference_czk=optimizer.expected_net_grid_value_czk - manual.expected_net_grid_value_czk,
        optimizer_terminal_soc_pct=optimizer.terminal_soc_pct,
        manual_terminal_soc_pct=manual.terminal_soc_pct,
    )


def _important_decisions(slots: list[TradingSlotPlan]) -> tuple[ImportantDecision, ...]:
    decisions: list[ImportantDecision] = []
    previous: TradingAction | None = None
    for slot in slots:
        if slot.action != previous:
            decisions.append(ImportantDecision(slot.timestamp, slot.action, slot.reason))
            previous = slot.action
    return tuple(decisions)


def _value(expression: pulp.LpAffineExpression | pulp.LpVariable) -> float:
    value = pulp.value(expression)
    if value is None:
        raise RuntimeError("solver returned a variable without a value")
    return float(value)
