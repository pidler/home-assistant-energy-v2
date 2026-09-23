"""Closed execution-state graph with explicit reset-only recovery."""

from dataclasses import dataclass

from .enums import ExecutionState, FailureReason, PlannerIntentType
from .models import CapabilitySnapshot, ExecutionContext, PlannerIntent

_ALLOWED: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.DISABLED: frozenset({ExecutionState.RECONCILING}),
    ExecutionState.RECONCILING: frozenset(
        {
            ExecutionState.DISABLED,
            ExecutionState.NORMAL,
            ExecutionState.PRECHECK,
            ExecutionState.ROLLBACK,
            ExecutionState.SAFE_STOP,
        }
    ),
    ExecutionState.NORMAL: frozenset(
        {
            ExecutionState.DISABLED,
            ExecutionState.PRECHECK,
            ExecutionState.TRANSITION,
            ExecutionState.ROLLBACK,
            ExecutionState.SAFE_STOP,
        }
    ),
    ExecutionState.PRECHECK: frozenset(
        {
            ExecutionState.DISABLED,
            ExecutionState.NORMAL,
            ExecutionState.TRANSITION,
            ExecutionState.DEYE_START_REQUIRED,
            ExecutionState.SAFE_STOP,
        }
    ),
    ExecutionState.TRANSITION: frozenset(
        {
            ExecutionState.NORMAL,
            ExecutionState.SOLAX_HOLD,
            ExecutionState.SOLAX_DISPATCH,
            ExecutionState.DEYE_START_REQUIRED,
            ExecutionState.DEYE_STARTING,
            ExecutionState.DEYE_HOLD,
            ExecutionState.DEYE_DISPATCH,
            ExecutionState.ROLLBACK,
            ExecutionState.SAFE_STOP,
        }
    ),
    ExecutionState.SOLAX_HOLD: frozenset(
        {ExecutionState.TRANSITION, ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}
    ),
    ExecutionState.SOLAX_DISPATCH: frozenset(
        {ExecutionState.TRANSITION, ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}
    ),
    ExecutionState.DEYE_START_REQUIRED: frozenset(
        {ExecutionState.DEYE_STARTING, ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}
    ),
    ExecutionState.DEYE_STARTING: frozenset(
        {ExecutionState.TRANSITION, ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}
    ),
    ExecutionState.DEYE_HOLD: frozenset({ExecutionState.TRANSITION, ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}),
    ExecutionState.DEYE_DISPATCH: frozenset(
        {ExecutionState.TRANSITION, ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}
    ),
    ExecutionState.ROLLBACK: frozenset({ExecutionState.DISABLED, ExecutionState.NORMAL, ExecutionState.SAFE_STOP}),
    ExecutionState.SAFE_STOP: frozenset(),
}

# These states assume authority to write an inverter control surface. DEYE_START_REQUIRED
# remains passive; DEYE_STARTING is the first state that assumes startup ownership.
_WRITER_OWNING_STATES = frozenset(
    {
        ExecutionState.SOLAX_HOLD,
        ExecutionState.SOLAX_DISPATCH,
        ExecutionState.DEYE_STARTING,
        ExecutionState.DEYE_HOLD,
        ExecutionState.DEYE_DISPATCH,
    }
)


def allowed_transitions(state: ExecutionState, context: ExecutionContext) -> frozenset[ExecutionState]:
    if state is ExecutionState.SAFE_STOP:
        return frozenset({ExecutionState.RECONCILING}) if context.reset_requested else frozenset()
    return _ALLOWED[state]


def validate_structural_transition(
    current: ExecutionState, proposed: ExecutionState, context: ExecutionContext
) -> tuple[FailureReason, ...]:
    if proposed not in allowed_transitions(current, context):
        return (FailureReason.TRANSITION_FORBIDDEN,)
    return ()


@dataclass(frozen=True)
class TransitionGuardFacts:
    execution_enabled: bool
    reconciliation_complete: bool
    capabilities: CapabilitySnapshot
    deye_ready: bool

    def __post_init__(self) -> None:
        flags = (self.execution_enabled, self.reconciliation_complete, self.deye_ready)
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("transition guard flags must be bool")


def validate_guarded_transition(
    current: ExecutionState,
    proposed: ExecutionState,
    context: ExecutionContext,
    guards: TransitionGuardFacts,
) -> tuple[FailureReason, ...]:
    structural = validate_structural_transition(current, proposed, context)
    if structural:
        return structural
    if proposed in _WRITER_OWNING_STATES and context.writer_conflicts:
        return (FailureReason.WRITER_CONFLICT,)
    if proposed in _WRITER_OWNING_STATES and not (context.enabled and guards.execution_enabled):
        return (FailureReason.RECONCILIATION_REQUIRED,)
    if current is ExecutionState.RECONCILING and proposed in {ExecutionState.NORMAL, ExecutionState.PRECHECK}:
        if not guards.reconciliation_complete:
            return (FailureReason.RECONCILIATION_REQUIRED,)
    if proposed is ExecutionState.SOLAX_HOLD and not guards.capabilities.solax_mode5_hold_verified:
        return (FailureReason.MODE_UNVERIFIED,)
    if proposed is ExecutionState.DEYE_HOLD and not guards.capabilities.deye_hold_verified:
        return (FailureReason.MODE_UNVERIFIED,)
    if proposed is ExecutionState.SOLAX_DISPATCH:
        if not (guards.capabilities.solax_mode1_dispatch_verified and guards.capabilities.solax_timeout_verified):
            return (FailureReason.MODE_UNVERIFIED,)
    if proposed is ExecutionState.DEYE_STARTING and not guards.capabilities.deye_start_verified:
        return (FailureReason.MODE_UNVERIFIED,)
    if proposed is ExecutionState.DEYE_DISPATCH:
        if not guards.deye_ready:
            return (FailureReason.DEYE_NOT_READY,)
        if not (guards.capabilities.deye_bounded_export_verified and guards.capabilities.solax_mode5_hold_verified):
            return (FailureReason.MODE_UNVERIFIED,)
    return ()


def validate_intent_transition(
    intent: PlannerIntent,
    current: ExecutionState,
    proposed: ExecutionState,
    context: ExecutionContext,
    guards: TransitionGuardFacts,
) -> tuple[FailureReason, ...]:
    targets = {
        PlannerIntentType.NORMAL: {ExecutionState.NORMAL},
        PlannerIntentType.CHARGE_SOLAX: {ExecutionState.SOLAX_DISPATCH},
        PlannerIntentType.DISCHARGE_SOLAX: {ExecutionState.SOLAX_DISPATCH},
        PlannerIntentType.CHARGE_DEYE: {ExecutionState.DEYE_START_REQUIRED, ExecutionState.DEYE_DISPATCH},
        PlannerIntentType.DISCHARGE_DEYE: {ExecutionState.DEYE_START_REQUIRED, ExecutionState.DEYE_DISPATCH},
    }
    if not isinstance(intent.intent_type, PlannerIntentType) or proposed not in targets[intent.intent_type]:
        return (FailureReason.INVALID_INTENT,)
    return validate_guarded_transition(current, proposed, context, guards)
