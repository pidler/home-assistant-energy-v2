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

__all__ = [
    "CapabilitySnapshot",
    "CommandLifecycleState",
    "CommandObservation",
    "CommandRecord",
    "ExecutionContext",
    "ExecutionState",
    "ExecutorDecision",
    "FailureReason",
    "PhysicalRole",
    "PhysicalResponseAcceptance",
    "PhysicalRoleVector",
    "PlannerIntent",
    "PlannerIntentType",
    "SafetyAction",
    "StabilityEvidence",
    "TransitionPhase",
]
