"""Passive, computation-only trading planner.

Nothing in this package publishes commands or calls Home Assistant services.
"""

from .inputs import assemble_slots, calculate_whole_site_load_w
from .models import (
    BatteryParameters,
    ForecastQuality,
    PhysicalLimitStatus,
    PlanComparison,
    PlannerConfig,
    PlannerInput,
    PlannerResult,
    TradingAction,
    TradingSlotInput,
    TradingSlotPlan,
)
from .planner import compare_plans, plan_trading_schedule

__all__ = [
    "BatteryParameters",
    "ForecastQuality",
    "PhysicalLimitStatus",
    "PlanComparison",
    "PlannerConfig",
    "PlannerInput",
    "PlannerResult",
    "TradingAction",
    "TradingSlotInput",
    "TradingSlotPlan",
    "assemble_slots",
    "calculate_whole_site_load_w",
    "compare_plans",
    "plan_trading_schedule",
]
