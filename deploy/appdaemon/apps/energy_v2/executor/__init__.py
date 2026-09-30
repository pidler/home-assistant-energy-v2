"""Pure Phase 5B execution-domain foundation."""

from .enums import (
    CommandLifecycleState,
    ExecutionState,
    ExecutorDecision,
    FailureReason,
    PhysicalRole,
    PlannerIntentType,
    SafetyAction,
    TransitionPhase,
)
from .haeo_plan import BatteryEnergyBudget, CurrentSlotPlan, HaeoPlanAdapter, PlanAdaptation
from .models import (
    CapabilitySnapshot,
    CommandObservation,
    CommandRecord,
    ExecutionContext,
    PhysicalResponseAcceptance,
    PhysicalRoleVector,
    PlannerIntent,
    StabilityEvidence,
)
from .slot_executor import EnergyAccount, SlotExecutionSession, SlotExecutorDecision, SlotSafetyStatus

__all__ = [
    "CapabilitySnapshot",
    "BatteryEnergyBudget",
    "CommandLifecycleState",
    "CommandObservation",
    "CommandRecord",
    "CurrentSlotPlan",
    "EnergyAccount",
    "ExecutionContext",
    "ExecutionState",
    "ExecutorDecision",
    "FailureReason",
    "HaeoPlanAdapter",
    "PhysicalRole",
    "PhysicalResponseAcceptance",
    "PhysicalRoleVector",
    "PlannerIntent",
    "PlannerIntentType",
    "PlanAdaptation",
    "SafetyAction",
    "SlotExecutionSession",
    "SlotExecutorDecision",
    "SlotSafetyStatus",
    "StabilityEvidence",
    "TransitionPhase",
]
