"""Passive, computation-only trading planner.

Nothing in this package publishes commands or calls Home Assistant services.
"""

from .inputs import assemble_slots
from .models import (
    BatteryParameters,
    ForecastQuality,
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
    "PlanComparison",
    "PlannerConfig",
    "PlannerInput",
    "PlannerResult",
    "TradingAction",
    "TradingSlotInput",
    "TradingSlotPlan",
    "assemble_slots",
    "compare_plans",
    "plan_trading_schedule",
]
