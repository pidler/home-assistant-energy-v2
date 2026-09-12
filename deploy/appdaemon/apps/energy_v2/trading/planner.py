from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pulp

from .explanation import ExplanationContext, explain_slot, price_percentile
from .models import (
    BatterySlotPlan,
    ImportantDecision,
    PlanComparison,
    PlannerConfig,
    PlannerInput,
    PlannerResult,
    SocCheckpointResult,
    TradingAction,
    TradingSlotPlan,
)
from .objective import continuation_price, terminal_reserve_shortfall_costs, terminal_values
from .policy import has_configured_morning_policy, morning_exception_precheck_passes, resolve_soc_policy

_EPSILON_KWH = 1e-5


def plan_trading_schedule(data: PlannerInput, config: PlannerConfig | None = None) -> PlannerResult:
    """Solve a passive 15-minute summer schedule as a deterministic linear program.

    Battery power is positive for charging and negative for discharging. The
    function is pure computation: it has no Home Assistant or Phase 4 imports.
    """

    config = config or PlannerConfig()
    data.validate()
    config.validate()
    normal = _solve_trading_candidate(data, config, morning_exception_enabled=False)
    if not has_configured_morning_policy(data, config):
        return _decorate_policy_reasons(normal)
    if not morning_exception_precheck_passes(data, config):
        return _decorate_policy_reasons(_mark_morning_candidate(normal, candidate_feasible=False, selected=False))
    try:
        exception = _solve_trading_candidate(data, config, morning_exception_enabled=True)
    except RuntimeError as error:
        if "Infeasible" not in str(error):
            raise
        return _decorate_policy_reasons(_mark_morning_candidate(normal, candidate_feasible=False, selected=False))
    if exception.objective_value_czk > normal.objective_value_czk + 1e-6:
        return _decorate_policy_reasons(_mark_morning_candidate(exception, candidate_feasible=True, selected=True))
    return _decorate_policy_reasons(_mark_morning_candidate(normal, candidate_feasible=True, selected=False))


def _solve_trading_candidate(
    data: PlannerInput,
    config: PlannerConfig,
    *,
    morning_exception_enabled: bool,
) -> PlannerResult:
    batteries = {battery.name: battery for battery in data.batteries}
    names = tuple(batteries)
    policy = resolve_soc_policy(data, config, morning_exception_enabled=morning_exception_enabled)
    slot_indexes = range(len(data.slots))
    economic_indexes = tuple(index for index, slot in enumerate(data.slots) if not slot.is_guard_only)
    guard_indexes = tuple(index for index, slot in enumerate(data.slots) if slot.is_guard_only)
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
    checkpoint_shortfall: dict[int, pulp.LpVariable] = {}

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
        for state_index, floor_pct in enumerate(policy.state_floors_pct[name]):
            problem += stored[name][state_index] >= battery.capacity_kwh * floor_pct / 100
        for index in policy.recovery_export_blocked_indexes[name]:
            problem += discharge_export[name][index] == 0

    state_timestamps = tuple(slot.timestamp for slot in data.slots) + (
        data.slots[-1].timestamp + timedelta(hours=config.slot_hours),
    )
    state_indexes = {timestamp: index for index, timestamp in enumerate(state_timestamps)}
    for checkpoint_index, checkpoint in enumerate(policy.checkpoints):
        state_index = state_indexes.get(checkpoint.timestamp)
        if state_index is None:
            raise ValueError(f"SOC checkpoint is outside the planning horizon: {checkpoint.timestamp.isoformat()}")
        battery = batteries[checkpoint.battery_name]
        target_kwh = battery.capacity_kwh * checkpoint.minimum_soc_pct / 100
        if checkpoint.hard:
            problem += stored[checkpoint.battery_name][state_index] >= target_kwh
        else:
            variable = pulp.LpVariable(f"checkpoint_shortfall_{checkpoint_index}", lowBound=0)
            checkpoint_shortfall[checkpoint_index] = variable
            problem += variable >= target_kwh - stored[checkpoint.battery_name][state_index]

    export_cap_kwh = config.site_export_limit_w / 1000 * config.slot_hours
    import_cap_kwh = config.site_import_limit_w / 1000 * config.slot_hours
    for index, slot in enumerate(data.slots):
        available_pv_surplus = max(slot.pv_forecast_kwh - slot.load_forecast_kwh, 0.0)
        problem += (
            pv_to_load[index] + pv_export[index] + pulp.lpSum(charge[name][index] for name in names)
            <= slot.pv_forecast_kwh
        )
        problem += pulp.lpSum(charge[name][index] for name in names) <= available_pv_surplus
        problem += (
            pv_to_load[index] + grid_import[index] + pulp.lpSum(discharge_load[name][index] for name in names)
            == slot.load_forecast_kwh
        )
        problem += pv_export[index] + pulp.lpSum(discharge_export[name][index] for name in names) <= export_cap_kwh
        problem += grid_import[index] <= import_cap_kwh
        if slot.is_guard_only:
            problem += pv_export[index] == 0
            for name in names:
                problem += discharge_export[name][index] == 0

    continuation_values = terminal_values(data.slots, data.batteries, config)
    reserve_shortfall_costs = terminal_reserve_shortfall_costs(data.slots, data.batteries, config)
    revenue = pulp.lpSum(
        data.slots[index].sell_price_czk_per_kwh
        * (pv_export[index] + pulp.lpSum(discharge_export[name][index] for name in names))
        for index in economic_indexes
    )
    import_cost = pulp.lpSum(data.slots[index].buy_price_czk_per_kwh * grid_import[index] for index in economic_indexes)
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
    operational_shortfall_cost = config.operational_checkpoint_shortfall_penalty_czk_per_kwh * pulp.lpSum(
        checkpoint_shortfall.values()
    )
    guard_grid_import_cost = config.guard_grid_import_penalty_czk_per_kwh * pulp.lpSum(
        grid_import[index] for index in guard_indexes
    )
    guard_trading_battery_load_tie_break = config.guard_trading_battery_load_tie_break_czk_per_kwh * pulp.lpSum(
        discharge_load[name][index]
        for name in names
        if batteries[name].role.value == "TRADING_BATTERY"
        for index in guard_indexes
    )
    problem += (
        revenue
        - import_cost
        - cycling_cost
        - solax_tie_break
        + terminal_value
        - reserve_shortfall_cost
        - operational_shortfall_cost
        - guard_grid_import_cost
        - guard_trading_battery_load_tie_break
    )

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False, threads=1))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"trading planner did not find an optimal solution: {pulp.LpStatus[status]}")

    prices = tuple(_known_price(slot.sell_price_czk_per_kwh) for slot in data.slots if not slot.is_guard_only)
    terminal_stored = {name: _value(stored[name][len(data.slots)]) for name in names}
    terminal_soc = {name: 100 * terminal_stored[name] / battery.capacity_kwh for name, battery in batteries.items()}
    terminal_shortfall = {name: _value(reserve_shortfall[name]) for name in names}
    reserve_targets = {
        name: battery.capacity_kwh * battery.terminal_reserve_soc_pct / 100 for name, battery in batteries.items()
    }
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
                active_soc_floor_pct=policy.state_floors_pct[name][index + 1],
                trading_export_blocked=index in policy.recovery_export_blocked_indexes[name] or slot.is_guard_only,
                overnight_trading_export_blocked=index in policy.overnight_export_blocked_indexes[name],
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
                reason="",
                guard_only=slot.is_guard_only,
            )
        )

    for index, planned in enumerate(planned_slots):
        future_indexes = [
            future_index
            for future_index in range(index + 1, len(planned_slots))
            if sum(battery.discharge_to_export_kwh for battery in planned_slots[future_index].batteries.values())
            > _EPSILON_KWH
        ]
        future_export_index = (
            max(future_indexes, key=lambda item: _known_price(planned_slots[item].sell_price_czk_per_kwh))
            if future_indexes
            else None
        )
        future_export_kwh = sum(
            sum(battery.discharge_to_export_kwh for battery in planned_slots[item].batteries.values())
            for item in future_indexes
        )
        current_stored = {
            name: (
                batteries[name].capacity_kwh * data.initial_soc_pct[name] / 100
                if index == 0
                else batteries[name].capacity_kwh * planned_slots[index - 1].batteries[name].projected_soc_pct / 100
            )
            for name in names
        }
        context = ExplanationContext(
            current_soc_pct={name: 100 * current_stored[name] / batteries[name].capacity_kwh for name in names},
            current_stored_kwh=current_stored,
            terminal_soc_pct=terminal_soc,
            terminal_reserve_target_kwh=reserve_targets,
            terminal_reserve_shortfall_kwh=terminal_shortfall,
            future_planned_battery_export_kwh=future_export_kwh,
            future_export_price_czk_per_kwh=(
                _known_price(planned_slots[future_export_index].sell_price_czk_per_kwh)
                if future_export_index is not None
                else None
            ),
            pv_before_future_export_kwh=(
                sum(data.slots[item].pv_forecast_kwh for item in range(index + 1, future_export_index + 1))
                if future_export_index is not None
                else 0.0
            ),
        )
        battery_export_kwh = sum(battery.discharge_to_export_kwh for battery in planned.batteries.values())
        planned_slots[index] = replace(
            planned,
            reason=explain_slot(
                data.slots[index],
                action=planned.action,
                battery_export_kwh=battery_export_kwh,
                price_percentile=(
                    price_percentile(prices, _known_price(planned.sell_price_czk_per_kwh))
                    if not planned.guard_only
                    else 0.0
                ),
                context=context,
            ),
        )

    revenue_value = sum(
        slot.planned_grid_export_kwh * _known_price(slot.sell_price_czk_per_kwh)
        for slot in planned_slots
        if not slot.guard_only
    )
    import_cost_value = sum(
        slot.planned_grid_import_kwh * _known_price(slot.buy_price_czk_per_kwh)
        for slot in planned_slots
        if not slot.guard_only
    )
    decisions = _important_decisions(planned_slots)
    generated_at = data.generated_at or datetime.now(UTC)
    checkpoint_results = tuple(
        _checkpoint_result(
            checkpoint,
            checkpoint_index=index,
            state_index=state_indexes[checkpoint.timestamp],
            batteries=batteries,
            stored=stored,
            checkpoint_shortfall=checkpoint_shortfall,
        )
        for index, checkpoint in enumerate(policy.checkpoints)
    )
    recovery_results = {
        key: _resolved_recovery(
            assessment,
            state_timestamps,
            stored[assessment.battery_name],
            batteries[assessment.battery_name],
        )
        for key, assessment in policy.morning_recovery.items()
    }
    return PlannerResult(
        generated_at=generated_at,
        horizon_start=data.slots[0].timestamp,
        horizon_end=data.slots[-1].timestamp + timedelta(hours=config.slot_hours),
        economic_horizon_end=data.slots[economic_indexes[-1]].timestamp + timedelta(hours=config.slot_hours),
        slots=tuple(planned_slots),
        initial_soc_pct=dict(data.initial_soc_pct),
        terminal_soc_pct=terminal_soc,
        terminal_stored_kwh=terminal_stored,
        minimum_physical_soc_pct={name: battery.minimum_soc_pct for name, battery in batteries.items()},
        terminal_reserve_target_soc_pct={name: battery.terminal_reserve_soc_pct for name, battery in batteries.items()},
        terminal_value_czk_per_kwh=continuation_values,
        terminal_continuation_price_czk_per_kwh=continuation_price(data.slots, config),
        terminal_value_method=(
            "EXPLICIT_CONTINUATION_PRICE"
            if config.terminal_continuation_price_czk_per_kwh is not None
            else f"MEDIAN_FINAL_{config.terminal_price_lookback_hours:g}_HOURS"
        ),
        terminal_reserve_target_kwh=reserve_targets,
        terminal_reserve_shortfall_kwh=terminal_shortfall,
        max_charge_power_w={name: battery.max_charge_power_w for name, battery in batteries.items()},
        max_discharge_power_w={name: battery.max_discharge_power_w for name, battery in batteries.items()},
        power_limit_status={name: battery.power_limit_status for name, battery in batteries.items()},
        battery_role={name: battery.role for name, battery in batteries.items()},
        checkpoints=checkpoint_results,
        morning_recovery=recovery_results,
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
    if (
        optimizer.terminal_value_method != manual.terminal_value_method
        or optimizer.terminal_value_czk_per_kwh != manual.terminal_value_czk_per_kwh
        or optimizer.terminal_stored_kwh.keys() != manual.terminal_stored_kwh.keys()
    ):
        raise ValueError("plans must use the same terminal valuation policy")
    optimizer_terminal_kwh = sum(optimizer.terminal_stored_kwh.values())
    manual_terminal_kwh = sum(manual.terminal_stored_kwh.values())
    optimizer_terminal_value = sum(
        optimizer.terminal_stored_kwh[name] * optimizer.terminal_value_czk_per_kwh[name]
        for name in optimizer.terminal_stored_kwh
    )
    manual_terminal_value = sum(
        manual.terminal_stored_kwh[name] * optimizer.terminal_value_czk_per_kwh[name]
        for name in manual.terminal_stored_kwh
    )
    optimizer_comparable = optimizer.expected_net_grid_value_czk + optimizer_terminal_value
    manual_comparable = manual.expected_net_grid_value_czk + manual_terminal_value
    return PlanComparison(
        optimizer_grid_cashflow_czk=optimizer.expected_net_grid_value_czk,
        manual_grid_cashflow_czk=manual.expected_net_grid_value_czk,
        grid_cashflow_difference_czk=optimizer.expected_net_grid_value_czk - manual.expected_net_grid_value_czk,
        optimizer_terminal_stored_kwh=optimizer_terminal_kwh,
        manual_terminal_stored_kwh=manual_terminal_kwh,
        terminal_stored_energy_difference_kwh=optimizer_terminal_kwh - manual_terminal_kwh,
        optimizer_terminal_value_adjustment_czk=optimizer_terminal_value,
        manual_terminal_value_adjustment_czk=manual_terminal_value,
        terminal_value_adjustment_difference_czk=optimizer_terminal_value - manual_terminal_value,
        optimizer_comparable_value_czk=optimizer_comparable,
        manual_comparable_value_czk=manual_comparable,
        comparable_value_difference_czk=optimizer_comparable - manual_comparable,
        optimizer_terminal_soc_pct=optimizer.terminal_soc_pct,
        manual_terminal_soc_pct=manual.terminal_soc_pct,
    )


def _mark_morning_candidate(
    result: PlannerResult,
    *,
    candidate_feasible: bool,
    selected: bool,
) -> PlannerResult:
    return replace(
        result,
        morning_recovery={
            key: replace(assessment, candidate_feasible=candidate_feasible, selected=selected)
            for key, assessment in result.morning_recovery.items()
        },
    )


def _decorate_policy_reasons(result: PlannerResult) -> PlannerResult:
    slots: list[TradingSlotPlan] = []
    for slot in result.slots:
        notes: list[str] = []
        for name, battery_plan in slot.batteries.items():
            if battery_plan.discharge_to_export_kwh <= _EPSILON_KWH:
                continue
            if result.battery_role[name].value == "TRADING_BATTERY":
                notes.append(
                    f"EXPORT {name}: {name} is the trading battery; projected SOC "
                    f"{battery_plan.projected_soc_pct:.1f}% remains at or above "
                    f"{battery_plan.active_soc_floor_pct:.1f}%."
                )
            else:
                assessment = _assessment_for_slot(result, name, slot.timestamp)
                if assessment is not None and assessment.selected:
                    recovered = (
                        assessment.expected_recovery_time.strftime("%H:%M")
                        if assessment.expected_recovery_time is not None
                        else assessment.recovery_deadline.strftime("%H:%M")
                    )
                    notes.append(
                        f"EXPORT {name}: entered morning at {assessment.morning_start_soc_pct:.1f}% after house "
                        f"operation; sell price {_known_price(slot.sell_price_czk_per_kwh):.2f} CZK/kWh; conditional "
                        f"{assessment.conditional_floor_pct:.0f}% trading floor is active and export reduces the "
                        f"projected minimum SOC to {assessment.minimum_projected_soc_pct:.1f}%; forecast recovery "
                        f"to {assessment.recovery_target_pct:.0f}% by {recovered}; "
                        f"forecast PV surplus available for recovery "
                        f"{assessment.forecast_pv_surplus_for_recovery_kwh:.2f} kWh."
                    )
        for name, role in result.battery_role.items():
            if role.value != "HOUSE_RESERVE_BATTERY":
                continue
            assessment = _assessment_for_slot(result, name, slot.timestamp)
            if slot.batteries[name].overnight_trading_export_blocked:
                notes.append(
                    f"HOLD {name} TRADING: the evening reserve is dedicated to overnight house operation; "
                    "trading export is blocked until the morning trading window."
                )
            if (
                assessment is not None
                and not assessment.selected
                and (slot.batteries[name].discharge_to_export_kwh <= _EPSILON_KWH)
            ):
                if assessment.candidate_feasible:
                    detail = "the normal non-trading candidate has equal or higher solved economic value"
                else:
                    detail = (
                        "the recovery candidate cannot restore SolaX to 30% by the configured deadline after "
                        "whole-site load, efficiency and charge-power limits"
                    )
                notes.append(
                    f"HOLD {name} TRADING: morning export is disabled and remaining SOC is available for house "
                    f"operation; conditional 15% trading floor is not selected because {detail}."
                )
            checkpoint = _next_binding_evening_checkpoint(result, name, slot.timestamp)
            if checkpoint is not None and slot.batteries[name].discharge_to_export_kwh <= _EPSILON_KWH:
                notes.append(
                    f"HOLD {name}: {checkpoint.target_soc_pct:.0f}% evening house-reserve checkpoint limits "
                    "further discharge."
                )
            previous_checkpoint = _previous_evening_checkpoint(result, name, slot.timestamp)
            if previous_checkpoint is not None and slot.batteries[name].discharge_to_load_kwh > _EPSILON_KWH:
                notes.append(
                    f"{name} is using the {previous_checkpoint.target_soc_pct:.0f}% evening reserve to cover "
                    "forecast overnight house load."
                )
        reason = slot.reason if not notes else f"{slot.reason} {' '.join(notes)}"
        slots.append(replace(slot, reason=reason))
    return replace(result, slots=tuple(slots), important_decisions=_important_decisions(slots))


def _assessment_for_slot(result: PlannerResult, battery_name: str, timestamp: datetime):
    return next(
        (
            assessment
            for assessment in result.morning_recovery.values()
            if assessment.battery_name == battery_name
            and assessment.trading_window_start <= timestamp < assessment.trading_window_end
        ),
        None,
    )


def _next_binding_evening_checkpoint(result: PlannerResult, battery_name: str, timestamp: datetime):
    return next(
        (
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.battery_name == battery_name
            and checkpoint.checkpoint_type.value == "EVENING_RESERVE"
            and timestamp < checkpoint.timestamp
            and abs(checkpoint.actual_soc_pct - checkpoint.target_soc_pct) <= 1e-4
        ),
        None,
    )


def _previous_evening_checkpoint(result: PlannerResult, battery_name: str, timestamp: datetime):
    matches = [
        checkpoint
        for checkpoint in result.checkpoints
        if checkpoint.battery_name == battery_name
        and checkpoint.checkpoint_type.value == "EVENING_RESERVE"
        and checkpoint.timestamp <= timestamp
    ]
    return max(matches, key=lambda item: item.timestamp, default=None)


def _checkpoint_result(
    checkpoint,
    *,
    checkpoint_index: int,
    state_index: int,
    batteries,
    stored,
    checkpoint_shortfall,
) -> SocCheckpointResult:
    battery = batteries[checkpoint.battery_name]
    actual_soc = 100 * _value(stored[checkpoint.battery_name][state_index]) / battery.capacity_kwh
    if checkpoint_index in checkpoint_shortfall:
        shortfall_pct = 100 * _value(checkpoint_shortfall[checkpoint_index]) / battery.capacity_kwh
    else:
        shortfall_pct = max(checkpoint.minimum_soc_pct - actual_soc, 0.0)
    reason = checkpoint.reason
    if shortfall_pct > 1e-6:
        reason = f"{checkpoint.checkpoint_type.value}_SHORTFALL: {reason} misses target by {shortfall_pct:.1f}%"
    return SocCheckpointResult(
        battery_name=checkpoint.battery_name,
        timestamp=checkpoint.timestamp,
        checkpoint_type=checkpoint.checkpoint_type,
        target_soc_pct=checkpoint.minimum_soc_pct,
        actual_soc_pct=actual_soc,
        shortfall_pct=shortfall_pct,
        hard=checkpoint.hard,
        reason=reason,
    )


def _resolved_recovery(assessment, state_timestamps, stored, battery):
    start_index = state_timestamps.index(assessment.trading_window_start)
    end_index = state_timestamps.index(assessment.trading_window_end)
    deadline_index = state_timestamps.index(assessment.recovery_deadline)
    morning_start_soc = 100 * _value(stored[start_index]) / battery.capacity_kwh
    minimum_projected_soc = min(
        100 * _value(stored[index]) / battery.capacity_kwh for index in range(start_index, end_index + 1)
    )
    expected_soc = 100 * _value(stored[deadline_index]) / battery.capacity_kwh
    recovered_at = None
    for index, timestamp in enumerate(state_timestamps):
        if not assessment.trading_window_end <= timestamp <= assessment.recovery_deadline:
            continue
        soc = 100 * _value(stored[index]) / battery.capacity_kwh
        if soc + 1e-6 >= assessment.recovery_target_pct:
            recovered_at = timestamp
            break
    return replace(
        assessment,
        morning_start_soc_pct=morning_start_soc,
        minimum_projected_soc_pct=minimum_projected_soc,
        expected_recovery_soc_pct=expected_soc,
        expected_recovery_time=recovered_at,
    )


def _important_decisions(slots: list[TradingSlotPlan]) -> tuple[ImportantDecision, ...]:
    decisions: list[ImportantDecision] = []
    previous: TradingAction | None = None
    for slot in slots:
        if slot.action != previous:
            decisions.append(ImportantDecision(slot.timestamp, slot.action, slot.reason))
            previous = slot.action
    return tuple(decisions)


def _known_price(value: float | None) -> float:
    if value is None:
        raise RuntimeError("guard-only slot unexpectedly entered the economic objective")
    return value


def _value(expression: pulp.LpAffineExpression | pulp.LpVariable) -> float:
    value = pulp.value(expression)
    if value is None:
        raise RuntimeError("solver returned a variable without a value")
    return float(value)
