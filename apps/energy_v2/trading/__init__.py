"""Passive, computation-only trading planner.

Nothing in this package publishes commands or calls Home Assistant services.
"""

from .inputs import assemble_slots, calculate_whole_site_load_w
from .models import (
    BatteryFloorRecovery,
    BatteryParameters,
    BatteryRole,
    CheckpointType,
    ForecastQuality,
    PhysicalLimitStatus,
    PlanComparison,
    PlannerConfig,
    PlannerInput,
    PlannerResult,
    SocCheckpoint,
    SocCheckpointResult,
    TradingAction,
    TradingSlotInput,
    TradingSlotPlan,
)
from .planner import compare_plans, plan_trading_schedule
from .power_state import (
    AssumptionStatus,
    DeyePowerState,
    DeyePowerStateConfig,
    DeyePowerStateSchedule,
    DeyePowerStateSlot,
    DeyeTelemetryExpectation,
    OffEligibleWindow,
    build_deye_power_state_schedule,
)

__all__ = [
    "BatteryFloorRecovery",
    "BatteryParameters",
    "BatteryRole",
    "AssumptionStatus",
    "CheckpointType",
    "ForecastQuality",
    "DeyePowerState",
    "DeyePowerStateConfig",
    "DeyePowerStateSchedule",
    "DeyePowerStateSlot",
    "DeyeTelemetryExpectation",
    "PhysicalLimitStatus",
    "PlanComparison",
    "OffEligibleWindow",
    "PlannerConfig",
    "PlannerInput",
    "PlannerResult",
    "SocCheckpoint",
    "SocCheckpointResult",
    "TradingAction",
    "TradingSlotInput",
    "TradingSlotPlan",
    "assemble_slots",
    "build_deye_power_state_schedule",
    "calculate_whole_site_load_w",
    "compare_plans",
    "plan_trading_schedule",
]
