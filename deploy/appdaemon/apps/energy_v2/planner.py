from __future__ import annotations

from .flow import FlowAssessment
from .models import Mode, PlannerDecision, Strategy, TelemetrySnapshot


def plan_shadow_mode(
    snapshot: TelemetrySnapshot,
    *,
    telemetry_valid: bool,
    export_enabled: bool,
    deye_ledger_kwh: float,
    solax_ledger_kwh: float,
    deye_min_soc_pct: float,
    deye_max_soc_pct: float,
    solax_min_soc_pct: float,
    solax_pv_charge_start_soc_pct: float,
    pv_reserve_w: float,
    minimum_sell_price: float,
    maximum_future_rank: int,
    strategy: Strategy = Strategy.SUMMER_NO_GRID_CHARGE,
    flow_assessment: FlowAssessment | None = None,
) -> PlannerDecision:
    if strategy is not Strategy.SUMMER_NO_GRID_CHARGE:
        return PlannerDecision(
            Mode.DISABLED,
            f"Strategy {strategy.value} is not implemented in phase 2",
            "high",
            flow_assessment.state if flow_assessment else None,
            bool(flow_assessment and flow_assessment.warnings),
            bool(flow_assessment and flow_assessment.violations),
        )

    if not telemetry_valid:
        return PlannerDecision(Mode.FAULT, "Telemetry is not valid", "high")

    if flow_assessment and flow_assessment.violations:
        return PlannerDecision(
            Mode.FAULT,
            "Flow violation detected",
            "high",
            flow_assessment.state,
            bool(flow_assessment.warnings),
            True,
        )

    sell_price = snapshot.sell_price
    future_rank = snapshot.future_sell_rank
    deye_soc = snapshot.deye_soc_pct
    solax_soc = snapshot.solax_soc_pct

    price_window_ok = (
        sell_price is not None
        and future_rank is not None
        and sell_price >= minimum_sell_price
        and future_rank <= maximum_future_rank
    )

    if (
        export_enabled
        and deye_ledger_kwh > 0.1
        and deye_soc is not None
        and deye_soc > deye_min_soc_pct
        and price_window_ok
    ):
        return PlannerDecision(
            Mode.EXPORT_DEYE,
            "DEYE FV ledger and sell price allow export",
            "medium",
            flow_assessment.state if flow_assessment else None,
            bool(flow_assessment and flow_assessment.warnings),
            bool(flow_assessment and flow_assessment.violations),
        )

    if (
        export_enabled
        and solax_ledger_kwh > 0.1
        and solax_soc is not None
        and solax_soc > solax_min_soc_pct
        and price_window_ok
    ):
        return PlannerDecision(
            Mode.EXPORT_SOLAX,
            "SolaX FV ledger and sell price allow export",
            "medium",
            flow_assessment.state if flow_assessment else None,
            bool(flow_assessment and flow_assessment.warnings),
            bool(flow_assessment and flow_assessment.violations),
        )

    if (
        deye_soc is not None
        and solax_soc is not None
        and snapshot.solax_pv_power_w is not None
        and snapshot.solax_house_load_w is not None
        and snapshot.deye_grid_charging_enabled is False
    ):
        pv_surplus_w = snapshot.solax_pv_power_w - max(snapshot.solax_house_load_w, 0.0)
        if (
            deye_soc < deye_max_soc_pct
            and solax_soc > solax_pv_charge_start_soc_pct
            and pv_surplus_w >= pv_reserve_w + 500
        ):
            return PlannerDecision(
                Mode.PV_CHARGE_DEYE,
                "Conservative PV surplus allows DEYE charge recommendation",
                "low",
                flow_assessment.state if flow_assessment else None,
                bool(flow_assessment and flow_assessment.warnings),
                bool(flow_assessment and flow_assessment.violations),
            )

    return PlannerDecision(
        Mode.IDLE,
        "No safe profitable shadow action",
        "medium",
        flow_assessment.state if flow_assessment else None,
        bool(flow_assessment and flow_assessment.warnings),
        bool(flow_assessment and flow_assessment.violations),
    )
