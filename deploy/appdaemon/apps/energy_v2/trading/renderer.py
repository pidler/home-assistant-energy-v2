from __future__ import annotations

import json
from dataclasses import asdict

from .models import PlannerResult


def render_text(result: PlannerResult) -> str:
    names = tuple(result.initial_soc_pct)
    lines = [
        f"Planning horizon: {result.horizon_start.isoformat()} -> {result.horizon_end.isoformat()}",
        f"Expected export: {result.expected_export_kwh:.3f} kWh",
        f"Expected import: {result.expected_import_kwh:.3f} kWh",
        f"Expected revenue: {result.expected_revenue_czk:.2f} CZK",
        f"Expected import cost: {result.expected_import_cost_czk:.2f} CZK",
        f"Expected net grid value: {result.expected_net_grid_value_czk:.2f} CZK",
        f"Economic objective including terminal value/penalties: {result.objective_value_czk:.2f} CZK",
        f"Load forecast quality: {result.load_forecast_quality.value}",
        f"Terminal continuation estimate: {result.terminal_continuation_price_czk_per_kwh:.2f} CZK/kWh "
        f"({result.terminal_value_method})",
        "Terminal reserve classification: HEURISTIC SOFT RESERVE (not a physical minimum or guaranteed result)",
    ]
    for name in names:
        lines.append(
            f"{name} SOC: {result.initial_soc_pct[name]:.1f}% -> actual terminal "
            f"{result.terminal_soc_pct[name]:.1f}%; physical minimum {result.minimum_physical_soc_pct[name]:.1f}%; "
            f"HEURISTIC SOFT RESERVE target {result.terminal_reserve_target_soc_pct[name]:.1f}% / "
            f"{result.terminal_reserve_target_kwh[name]:.2f} kWh; actual stored "
            f"{result.terminal_stored_kwh[name]:.2f} kWh valued at "
            f"{result.terminal_value_czk_per_kwh[name]:.2f} CZK/kWh; "
            f"actual reserve shortfall {result.terminal_reserve_shortfall_kwh[name]:.2f} kWh; "
            f"charge/discharge limits {result.max_charge_power_w[name]:.0f}/"
            f"{result.max_discharge_power_w[name]:.0f} W ({result.power_limit_status[name].value}); "
            f"role {result.battery_role[name].value}"
        )
    for checkpoint in result.checkpoints:
        lines.append(
            f"SOC CHECKPOINT {checkpoint.checkpoint_type.value} {checkpoint.battery_name} at "
            f"{checkpoint.timestamp.isoformat()}: target {checkpoint.target_soc_pct:.1f}%, projected "
            f"{checkpoint.actual_soc_pct:.1f}%, shortfall {checkpoint.shortfall_pct:.1f}%, "
            f"{'HARD' if checkpoint.hard else 'OPERATIONAL SHORTFALL'}; {checkpoint.reason}"
        )
    for assessment in result.morning_recovery.values():
        lines.append(
            f"MORNING POLICY {assessment.battery_name}: candidate feasible={assessment.candidate_feasible}, "
            f"selected={assessment.selected}, floor {assessment.conditional_floor_pct:.1f}%, recovery target "
            f"{assessment.recovery_target_pct:.1f}% by {assessment.recovery_deadline.isoformat()}, morning start "
            f"{assessment.morning_start_soc_pct:.1f}%, projected minimum "
            f"{assessment.minimum_projected_soc_pct:.1f}%, projected recovery "
            f"{assessment.expected_recovery_soc_pct:.1f}%, PV surplus "
            f"{assessment.forecast_pv_surplus_for_recovery_kwh:.2f} kWh, charge-limited storable "
            f"{assessment.maximum_storable_recovery_kwh:.2f} kWh"
        )
    header = [
        "TIME",
        "SELL",
        "BUY",
        "PV kWh",
        "LOAD kWh",
        "IMPORT",
        "EXPORT",
        *(f"{name} SOC" for name in names),
        *(f"{name} FLOOR" for name in names),
        *(f"{name} W" for name in names),
        "ACTION",
        "REASON",
    ]
    lines.extend(("", " | ".join(header), "-" * 180))
    for slot in result.slots:
        row = [
            slot.timestamp.isoformat(),
            f"{slot.sell_price_czk_per_kwh:.2f}",
            f"{slot.buy_price_czk_per_kwh:.2f}",
            f"{slot.pv_forecast_kwh:.3f}",
            f"{slot.load_forecast_kwh:.3f}",
            f"{slot.planned_grid_import_kwh:.3f}",
            f"{slot.planned_grid_export_kwh:.3f}",
            *(f"{slot.batteries[name].projected_soc_pct:.1f}%" for name in names),
            *(f"{slot.batteries[name].active_soc_floor_pct:.1f}%" for name in names),
            *(f"{slot.batteries[name].planned_power_w:.0f}" for name in names),
            slot.action.value,
            slot.reason,
        ]
        lines.append(" | ".join(row))
    lines.extend(("", "IMPORTANT DECISIONS"))
    for decision in result.important_decisions:
        lines.append(f"{decision.timestamp.isoformat()} {decision.action.value}: {decision.reason}")
    return "\n".join(lines)


def render_json(result: PlannerResult) -> str:
    return json.dumps(asdict(result), default=_json_default, ensure_ascii=False, indent=2, sort_keys=True)


def _json_default(value: object) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    if hasattr(value, "value"):
        return str(value.value)  # type: ignore[union-attr]
    raise TypeError(f"cannot serialize {type(value).__name__}")
