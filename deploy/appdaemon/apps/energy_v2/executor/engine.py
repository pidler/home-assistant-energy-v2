"""Deterministic orchestration for the pure Phase 5B shadow executor."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite

from ..deye_state import DeyeOperatingState
from .adapters import (
    AdapterOperation,
    DeyeShadowAdapter,
    ShadowControlProposal,
    SolaxShadowAdapter,
)
from .diagnostics import ExecutorDiagnostics, build_diagnostics
from .enums import ExecutionState, ExecutorDecision, FailureReason, PhysicalRole, SafetyAction, TransitionPhase
from .invariants import roles_for_intent
from .models import CapabilitySnapshot, CommandRecord, ExecutionContext, PhysicalRoleVector, PlannerIntent
from .planner_contract import validate_planner_intent
from .reconciliation import PersistedExecutorMetadata, ReconciliationResult, evaluate_reconciliation
from .safety import (
    PhysicalStateSnapshot,
    ReadinessEvidence,
    RollingExportEvidence,
    SafetyConfig,
    SafetyDecision,
    combine_safety_decisions,
    evaluate_command_expiry,
    evaluate_cross_flow,
    evaluate_pcc_safety,
    evaluate_readiness,
    evaluate_telemetry_coherence,
    evaluate_writer_conflicts,
)
from .state_machine import TransitionGuardFacts, validate_guarded_transition
from .transition import (
    BoundCommandEvidence,
    NeutralSettlingEvidence,
    ShadowCommandKind,
    ShadowCommandProposal,
    TransitionConfig,
    TransitionProgress,
    advance_transition,
    plan_transition,
)


@dataclass(frozen=True)
class ExecutorInputs:
    context: ExecutionContext
    intent: PlannerIntent | None
    physical: PhysicalStateSnapshot
    capabilities: CapabilitySnapshot
    safety_config: SafetyConfig
    transition_config: TransitionConfig
    current_roles: PhysicalRoleVector
    now: datetime
    transition_id: str
    transition_progress: TransitionProgress | None = None
    command_record: CommandRecord | None = None
    persisted: PersistedExecutorMetadata | None = None
    reconciliation_required: bool = False
    rolling_export: RollingExportEvidence | None = None
    readiness_evidence: ReadinessEvidence | None = None
    settling_evidence: NeutralSettlingEvidence | None = None
    additional_safety: tuple[SafetyDecision, ...] = ()
    contradictions: tuple[str, ...] = ()
    cross_flow_violation_duration_s: float = 0.0
    proposal_command_id: str | None = None
    deye_dispatch_ceiling_w: float | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() is None or self.context.now != self.now:
            raise ValueError("now must be timezone-aware and match execution context")
        if not self.transition_id:
            raise ValueError("caller-supplied transition_id is required")
        if not isinstance(self.reconciliation_required, bool):
            raise ValueError("reconciliation_required must be bool")
        if (
            isinstance(self.cross_flow_violation_duration_s, bool)
            or not isfinite(self.cross_flow_violation_duration_s)
            or self.cross_flow_violation_duration_s < 0
        ):
            raise ValueError("cross-flow duration must be finite and nonnegative")
        if self.deye_dispatch_ceiling_w is not None and (
            isinstance(self.deye_dispatch_ceiling_w, bool)
            or not isfinite(self.deye_dispatch_ceiling_w)
            or self.deye_dispatch_ceiling_w <= 0
        ):
            raise ValueError("DEYE ceiling must be finite and positive")
        safety = tuple(self.additional_safety)
        contradictions = tuple(self.contradictions)
        if not all(isinstance(item, SafetyDecision) for item in safety):
            raise ValueError("additional safety values must be SafetyDecision")
        if not all(isinstance(item, str) and item for item in contradictions):
            raise ValueError("contradictions must contain nonempty strings")
        if self.transition_progress is not None:
            if self.current_roles != self.transition_progress.physical_roles:
                raise ValueError("current roles must match retained transition progress")
        object.__setattr__(self, "additional_safety", safety)
        object.__setattr__(self, "contradictions", contradictions)


@dataclass(frozen=True)
class ExecutorResult:
    state: ExecutionState
    roles: PhysicalRoleVector
    decision: ExecutorDecision
    safety_action: SafetyAction
    reasons: tuple[FailureReason, ...]
    transition_progress: TransitionProgress | None
    proposal: ShadowControlProposal | None
    reconciliation: ReconciliationResult | None
    diagnostics: ExecutorDiagnostics

    def __post_init__(self) -> None:
        reasons = tuple(self.reasons)
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("result reasons must use FailureReason")
        if self.proposal is not None and self.safety_action in {
            SafetyAction.DEFER,
            SafetyAction.ROLLBACK,
            SafetyAction.SAFE_STOP,
        }:
            raise ValueError("deferred, rollback, and safe-stop results cannot carry a proposal")
        if self.proposal is not None and not self.proposal.shadow_only:
            raise ValueError("executor may expose only shadow proposals")
        object.__setattr__(self, "reasons", reasons)


def _outcome_state(action: SafetyAction, fallback: ExecutionState) -> ExecutionState:
    if action is SafetyAction.SAFE_STOP:
        return ExecutionState.SAFE_STOP
    if action is SafetyAction.ROLLBACK:
        return ExecutionState.ROLLBACK
    return fallback


def _decision_for_action(action: SafetyAction) -> ExecutorDecision:
    return {
        SafetyAction.NONE: ExecutorDecision.ACCEPTED,
        SafetyAction.CLAMP: ExecutorDecision.CLAMPED,
        SafetyAction.DEFER: ExecutorDecision.DEFERRED,
        SafetyAction.ROLLBACK: ExecutorDecision.ABORTED,
        SafetyAction.SAFE_STOP: ExecutorDecision.ABORTED,
    }[action]


def _result(
    inputs: ExecutorInputs,
    *,
    state: ExecutionState,
    roles: PhysicalRoleVector,
    decision: ExecutorDecision,
    action: SafetyAction,
    reasons: tuple[FailureReason, ...] = (),
    transition: TransitionProgress | None = None,
    proposal: ShadowControlProposal | None = None,
    reconciliation: ReconciliationResult | None = None,
    pcc_safety: SafetyDecision | None = None,
    cross_flow: SafetyDecision | None = None,
    readiness: SafetyDecision | None = None,
) -> ExecutorResult:
    valid_intent = inputs.intent if isinstance(inputs.intent, PlannerIntent) else None
    intent_type = valid_intent.intent_type if valid_intent is not None else None
    diagnostics = build_diagnostics(
        state=state,
        roles=(roles.solax, roles.deye),
        intent_type=intent_type,
        requested_target_w=valid_intent.target_w if valid_intent is not None else None,
        decision=decision,
        reasons=reasons,
        action=action,
        execution_epoch=inputs.context.execution_epoch,
        now=inputs.now,
        transition=transition,
        proposal=proposal,
        reconciliation=reconciliation,
        pcc_safety=pcc_safety,
        cross_flow=cross_flow,
        readiness=readiness,
        writer_conflicts=tuple(sorted(set((*inputs.context.writer_conflicts, *inputs.physical.writer_conflicts)))),
        last_failure=inputs.context.last_failure,
    )
    return ExecutorResult(state, roles, decision, action, reasons, transition, proposal, reconciliation, diagnostics)


def _bound_command(progress: TransitionProgress, command: CommandRecord | None) -> BoundCommandEvidence | None:
    expected = progress.expected_command
    if command is None or expected is None:
        return None
    return BoundCommandEvidence(command, expected.kind, expected.battery, expected.role, expected.target_w)


def _has_writer(roles: PhysicalRoleVector | None) -> bool:
    if roles is None:
        return False
    writer_roles = {PhysicalRole.HOLD, PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
    return roles.solax in writer_roles or roles.deye in writer_roles


def _control_timestamp_fresh(timestamp: datetime | None, inputs: ExecutorInputs) -> bool:
    if timestamp is None:
        return False
    age_s = (inputs.now - timestamp).total_seconds()
    return 0 <= age_s <= inputs.safety_config.control_state_max_age_s


def _control_state_fresh(inputs: ExecutorInputs) -> bool:
    return _control_timestamp_fresh(
        inputs.physical.solax_control_source_timestamp,
        inputs,
    ) and _control_timestamp_fresh(
        inputs.physical.deye_control_source_timestamp,
        inputs,
    )


def _safe_native_proven(inputs: ExecutorInputs) -> bool:
    native = PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF)
    telemetry = evaluate_telemetry_coherence(
        {
            "deye_battery_power": inputs.physical.deye_battery_power,
            "pcc": inputs.physical.pcc,
            "solax_battery_power": inputs.physical.solax_battery_power,
        },
        state=ExecutionState.NORMAL,
        max_age_s=inputs.safety_config.pcc_max_age_s,
        now=inputs.now,
    )
    neutral_values = (
        inputs.physical.solax_battery_power.value,
        inputs.physical.deye_battery_power.value,
    )
    conflicts = (*inputs.context.writer_conflicts, *inputs.physical.writer_conflicts)
    pcc = evaluate_pcc_safety(
        inputs.physical.pcc,
        rolling=inputs.rolling_export,
        requested_target_w=None,
        state=ExecutionState.NORMAL,
        physical_ownership=inputs.physical.ownership,
        config=inputs.safety_config,
        now=inputs.now,
    )
    return (
        inputs.context.state in {ExecutionState.NORMAL, ExecutionState.PRECHECK}
        and inputs.current_roles == native
        and inputs.physical.ownership == native
        and inputs.physical.solax_available
        and not inputs.physical.solax_fault
        and inputs.physical.solax_native_mode_confirmed
        and not inputs.physical.solax_remote_control_active
        and inputs.physical.deye_state in {DeyeOperatingState.INTENTIONAL_OFF, DeyeOperatingState.READY}
        and inputs.physical.deye_non_owning_confirmed
        and inputs.physical.continuity_certain
        and inputs.physical.rollback_safe
        and _control_state_fresh(inputs)
        and not conflicts
        and not inputs.contradictions
        and all(decision.action is SafetyAction.NONE for decision in inputs.additional_safety)
        and telemetry.action is SafetyAction.NONE
        and pcc.action is SafetyAction.NONE
        and all(
            value is not None and abs(value) <= inputs.safety_config.neutral_power_threshold_w
            for value in neutral_values
        )
    )


def _ownership_contradiction(inputs: ExecutorInputs) -> bool:
    ownership = inputs.physical.ownership
    if ownership is None or ownership != inputs.current_roles or not _control_state_fresh(inputs):
        return True
    writer_roles = {PhysicalRole.HOLD, PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
    solax_owned = ownership.solax in writer_roles
    deye_owned = ownership.deye in writer_roles
    return (
        inputs.physical.solax_remote_control_active is not solax_owned
        or inputs.physical.deye_non_owning_confirmed is deye_owned
    )


def _progress_context_reasons(inputs: ExecutorInputs, progress: TransitionProgress) -> tuple[FailureReason, ...]:
    context = inputs.context
    reasons: list[FailureReason] = []
    if progress.plan.execution_epoch != context.execution_epoch:
        reasons.append(FailureReason.OLD_EXECUTION_EPOCH)
    allowed_states = {
        ExecutionState.TRANSITION,
        ExecutionState.SOLAX_HOLD,
        ExecutionState.DEYE_START_REQUIRED,
        ExecutionState.DEYE_STARTING,
    }
    if progress.current_phase is TransitionPhase.COMPLETE:
        allowed_states.update({ExecutionState.SOLAX_DISPATCH, ExecutionState.DEYE_DISPATCH})
    if context.state not in allowed_states:
        reasons.append(FailureReason.RECONCILIATION_REQUIRED)
    if context.active_plan_id is not None and context.active_plan_id != progress.plan.plan_id:
        reasons.append(FailureReason.INVALID_INTENT)
    if context.active_intent_id is not None and context.active_intent_id != progress.plan.intent_id:
        reasons.append(FailureReason.INVALID_INTENT)
    if context.intent is not None and context.intent != progress.plan.intent:
        reasons.append(FailureReason.INVALID_INTENT)
    if context.active_command_id is not None:
        known_command_ids = {
            step.command.record.command_id for step in progress.completed_steps if step.command is not None
        }
        if progress.expected_command is not None:
            known_command_ids.add(progress.expected_command.command_id)
        if context.active_command_id not in known_command_ids:
            reasons.append(FailureReason.COMMAND_ID_MISMATCH)
    return tuple(dict.fromkeys(reasons))


def _readiness_safety(
    inputs: ExecutorInputs,
    desired_roles: PhysicalRoleVector,
    progress: TransitionProgress | None,
) -> SafetyDecision:
    writer_required = _has_writer(desired_roles)
    if not writer_required:
        return SafetyDecision(SafetyAction.NONE)
    evidence = inputs.readiness_evidence
    if evidence is None:
        return SafetyDecision(SafetyAction.DEFER, (FailureReason.MISSING_TELEMETRY,))
    evaluated = evaluate_readiness(
        desired_roles,
        capabilities=inputs.capabilities,
        evidence=evidence,
        max_age_s=inputs.safety_config.readiness_max_age_s,
        now=inputs.now,
    )
    reasons = list(evaluated.reasons)
    startup_phases = {
        None,
        TransitionPhase.FREEZE_INTENT,
        TransitionPhase.VERIFY_PCC,
        TransitionPhase.ARM_DESTINATION,
        TransitionPhase.WAIT_DESTINATION_READY,
    }
    phase = progress.current_phase if progress is not None else None
    startup_state = evidence.deye_state in {DeyeOperatingState.INTENTIONAL_OFF, DeyeOperatingState.STARTING}
    deye_target = desired_roles.deye in {PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
    if deye_target and startup_state and phase in startup_phases:
        reasons = [reason for reason in reasons if reason is not FailureReason.DEYE_NOT_READY]
    if evidence.deye_state is not inputs.physical.deye_state:
        reasons.append(FailureReason.TELEMETRY_INCOHERENT)
    if (
        evidence.solax_available != inputs.physical.solax_available
        or evidence.solax_fault != inputs.physical.solax_fault
    ):
        reasons.append(FailureReason.TELEMETRY_INCOHERENT)
    reasons = list(dict.fromkeys(reasons))
    return SafetyDecision(SafetyAction.DEFER, tuple(reasons)) if reasons else SafetyDecision(SafetyAction.NONE)


def _progress_failure(
    inputs: ExecutorInputs,
    progress: TransitionProgress,
    reasons: tuple[FailureReason, ...],
) -> ExecutorResult:
    return _result(
        inputs,
        state=ExecutionState.ROLLBACK,
        roles=progress.physical_roles,
        decision=ExecutorDecision.ABORTED,
        action=SafetyAction.ROLLBACK,
        reasons=reasons,
        transition=progress,
    )


def _adapt_proposal(
    inputs: ExecutorInputs,
    raw: tuple[ShadowCommandProposal, ...],
) -> tuple[ShadowControlProposal | None, tuple[FailureReason, ...]]:
    if not raw:
        return None, ()
    selected = next((item for item in raw if item.kind is ShadowCommandKind.START_DEYE_REQUIRED), raw[0])
    if selected.battery == "SOLAX":
        adapter = SolaxShadowAdapter(inputs.capabilities)
        if selected.kind is ShadowCommandKind.STOP_CURRENT_OWNER:
            result = adapter.propose_stop(command_id=selected.command_id)
        elif selected.role is PhysicalRole.NATIVE:
            result = adapter.propose_native()
        elif selected.role is PhysicalRole.HOLD:
            result = adapter.propose_hold(command_id=selected.command_id)
        elif selected.role is PhysicalRole.CHARGE:
            assert selected.target_w is not None
            result = adapter.propose_charge(
                selected.target_w,
                economic_requested_target_w=(
                    inputs.intent.target_w if isinstance(inputs.intent, PlannerIntent) else None
                ),
                command_id=selected.command_id,
            )
        else:
            assert selected.target_w is not None
            result = adapter.propose_discharge(
                selected.target_w,
                economic_requested_target_w=(
                    inputs.intent.target_w if isinstance(inputs.intent, PlannerIntent) else None
                ),
                command_id=selected.command_id,
            )
    else:
        adapter = DeyeShadowAdapter(
            inputs.capabilities,
            inputs.physical.deye_state,
            inputs.deye_dispatch_ceiling_w,
        )
        if selected.kind is ShadowCommandKind.START_DEYE_REQUIRED:
            result = adapter.propose_start()
        elif selected.kind is ShadowCommandKind.STOP_CURRENT_OWNER:
            result = adapter.propose_stop(command_id=selected.command_id)
        elif selected.role is PhysicalRole.HOLD:
            result = adapter.propose_hold(command_id=selected.command_id)
        elif selected.role is PhysicalRole.CHARGE:
            assert selected.target_w is not None
            result = adapter.propose_charge(
                selected.target_w,
                economic_requested_target_w=(
                    inputs.intent.target_w if isinstance(inputs.intent, PlannerIntent) else None
                ),
                command_id=selected.command_id,
            )
        else:
            assert selected.target_w is not None
            result = adapter.propose_discharge(
                selected.target_w,
                economic_requested_target_w=(
                    inputs.intent.target_w if isinstance(inputs.intent, PlannerIntent) else None
                ),
                command_id=selected.command_id,
            )
    return result.proposal, result.reasons


def evaluate_executor(inputs: ExecutorInputs) -> ExecutorResult:
    """Evaluate exactly one deterministic shadow-executor step."""
    context = inputs.context
    native = PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF)
    if (
        inputs.reconciliation_required
        or context.state is ExecutionState.RECONCILING
        or inputs.physical.reconnect_detected
    ):
        reconciliation = evaluate_reconciliation(
            current_execution_epoch=context.execution_epoch,
            physical=inputs.physical,
            capabilities=inputs.capabilities,
            persisted=inputs.persisted,
            safety_decisions=inputs.additional_safety,
            config=inputs.safety_config,
            now=inputs.now,
            execution_enabled=context.enabled,
            fresh_intent=inputs.intent if isinstance(inputs.intent, PlannerIntent) else None,
        )
        action = (
            SafetyAction.SAFE_STOP
            if reconciliation.outcome_state is ExecutionState.SAFE_STOP
            else SafetyAction.ROLLBACK
            if reconciliation.outcome_state is ExecutionState.ROLLBACK
            else SafetyAction.DEFER
        )
        return _result(
            inputs,
            state=reconciliation.outcome_state,
            roles=(
                native
                if reconciliation.outcome_state in {ExecutionState.NORMAL, ExecutionState.PRECHECK}
                else inputs.current_roles
            ),
            decision=_decision_for_action(action),
            action=action,
            reasons=reconciliation.reasons,
            reconciliation=reconciliation,
        )

    progress = inputs.transition_progress
    if progress is not None:
        context_reasons = _progress_context_reasons(inputs, progress)
        if context_reasons:
            return _progress_failure(inputs, progress, context_reasons)
        if not context.enabled:
            return _progress_failure(inputs, progress, (FailureReason.DISABLED,))
        if inputs.intent is None or not isinstance(inputs.intent, PlannerIntent):
            return _progress_failure(inputs, progress, (FailureReason.INVALID_INTENT,))
        intent_reasons = validate_planner_intent(inputs.intent, inputs.now)
        if intent_reasons:
            return _progress_failure(inputs, progress, intent_reasons)
        if inputs.intent != progress.plan.intent:
            return _progress_failure(inputs, progress, (FailureReason.INVALID_INTENT,))
        if inputs.command_record is not None and inputs.command_record.execution_epoch != context.execution_epoch:
            return _progress_failure(inputs, progress, (FailureReason.OLD_EXECUTION_EPOCH,))
    elif not context.enabled:
        return _result(
            inputs,
            state=ExecutionState.DISABLED,
            roles=inputs.current_roles,
            decision=ExecutorDecision.REJECTED,
            action=SafetyAction.DEFER,
            reasons=(FailureReason.DISABLED,),
        )

    ownership_mismatch = _ownership_contradiction(inputs)
    if ownership_mismatch:
        reasons = (FailureReason.RECONCILIATION_REQUIRED,)
        if progress is not None:
            return _progress_failure(inputs, progress, reasons)
        action = SafetyAction.ROLLBACK if _has_writer(inputs.physical.ownership) else SafetyAction.SAFE_STOP
        return _result(
            inputs,
            state=ExecutionState.ROLLBACK if action is SafetyAction.ROLLBACK else ExecutionState.SAFE_STOP,
            roles=inputs.current_roles,
            decision=ExecutorDecision.ABORTED,
            action=action,
            reasons=reasons,
        )

    if inputs.intent is None:
        if not _safe_native_proven(inputs):
            action = SafetyAction.ROLLBACK if _has_writer(inputs.current_roles) else SafetyAction.SAFE_STOP
            return _result(
                inputs,
                state=ExecutionState.ROLLBACK if action is SafetyAction.ROLLBACK else ExecutionState.SAFE_STOP,
                roles=inputs.current_roles,
                decision=ExecutorDecision.ABORTED,
                action=action,
                reasons=(FailureReason.RECONCILIATION_REQUIRED,),
            )
        return _result(
            inputs,
            state=ExecutionState.NORMAL,
            roles=native,
            decision=ExecutorDecision.ACCEPTED,
            action=SafetyAction.NONE,
        )

    if not isinstance(inputs.intent, PlannerIntent):
        return _result(
            inputs,
            state=ExecutionState.PRECHECK,
            roles=inputs.current_roles,
            decision=ExecutorDecision.REJECTED,
            action=SafetyAction.DEFER,
            reasons=(FailureReason.INVALID_INTENT,),
        )

    intent_reasons = validate_planner_intent(inputs.intent, inputs.now)
    derived = roles_for_intent(inputs.intent)
    if intent_reasons or derived.roles is None:
        reasons = tuple(dict.fromkeys((*intent_reasons, *derived.reasons))) or (FailureReason.INVALID_INTENT,)
        return _result(
            inputs,
            state=ExecutionState.PRECHECK,
            roles=inputs.current_roles,
            decision=ExecutorDecision.REJECTED,
            action=SafetyAction.DEFER,
            reasons=reasons,
        )
    desired_roles = derived.roles

    safety_state = ExecutionState.TRANSITION if progress is not None else context.state
    ownership = progress.physical_roles if progress is not None else inputs.current_roles
    telemetry = evaluate_telemetry_coherence(
        {
            "deye_battery_power": inputs.physical.deye_battery_power,
            "pcc": inputs.physical.pcc,
            "solax_battery_power": inputs.physical.solax_battery_power,
        },
        contradictions=inputs.contradictions,
        state=safety_state,
        max_age_s=inputs.safety_config.pcc_max_age_s,
        now=inputs.now,
    )
    conflicts = evaluate_writer_conflicts(
        tuple(sorted(set((*context.writer_conflicts, *inputs.physical.writer_conflicts)))),
        state=safety_state,
        config=inputs.safety_config,
        physical_ownership=ownership,
    )
    pcc = evaluate_pcc_safety(
        inputs.physical.pcc,
        rolling=inputs.rolling_export,
        requested_target_w=inputs.intent.target_w,
        state=safety_state,
        physical_ownership=ownership,
        config=inputs.safety_config,
        now=inputs.now,
    )
    cross_flow = evaluate_cross_flow(
        inputs.physical.solax_battery_power,
        inputs.physical.deye_battery_power,
        state=safety_state,
        expected_roles=progress.physical_roles if progress is not None else None,
        violation_duration_s=inputs.cross_flow_violation_duration_s,
        config=inputs.safety_config,
        now=inputs.now,
    )
    command_expiry = (
        evaluate_command_expiry(inputs.command_record, state=safety_state, now=inputs.now)
        if inputs.command_record is not None
        else SafetyDecision(SafetyAction.NONE)
    )
    readiness = _readiness_safety(inputs, desired_roles, progress)
    combined = combine_safety_decisions(
        (*inputs.additional_safety, telemetry, conflicts, pcc, cross_flow, command_expiry, readiness)
    )

    if progress is None:
        if combined.action in {SafetyAction.DEFER, SafetyAction.ROLLBACK, SafetyAction.SAFE_STOP}:
            return _result(
                inputs,
                state=_outcome_state(combined.action, ExecutionState.PRECHECK),
                roles=inputs.current_roles,
                decision=_decision_for_action(combined.action),
                action=combined.action,
                reasons=combined.reasons,
                pcc_safety=pcc,
                cross_flow=cross_flow,
                readiness=readiness,
            )
        if desired_roles == native and inputs.current_roles == native:
            return _result(
                inputs,
                state=ExecutionState.NORMAL,
                roles=native,
                decision=ExecutorDecision.ACCEPTED,
                action=SafetyAction.NONE,
                pcc_safety=pcc,
                cross_flow=cross_flow,
                readiness=readiness,
            )
        guards = TransitionGuardFacts(
            execution_enabled=True,
            reconciliation_complete=True,
            capabilities=inputs.capabilities,
            deye_ready=inputs.physical.deye_state is DeyeOperatingState.READY,
        )
        guard_reasons = validate_guarded_transition(context.state, ExecutionState.TRANSITION, context, guards)
        if guard_reasons:
            return _result(
                inputs,
                state=ExecutionState.PRECHECK,
                roles=inputs.current_roles,
                decision=ExecutorDecision.REJECTED,
                action=SafetyAction.DEFER,
                reasons=guard_reasons,
                pcc_safety=pcc,
                cross_flow=cross_flow,
                readiness=readiness,
            )
        planned = plan_transition(
            transition_id=inputs.transition_id,
            current_state=context.state,
            current_roles=inputs.current_roles,
            intent=inputs.intent,
            current_execution_epoch=context.execution_epoch,
            capabilities=inputs.capabilities,
            deye_state=inputs.physical.deye_state,
            config=inputs.transition_config,
            now=inputs.now,
        )
        if planned.progress is None:
            return _result(
                inputs,
                state=ExecutionState.PRECHECK,
                roles=inputs.current_roles,
                decision=planned.decision,
                action=planned.action,
                reasons=planned.reasons,
                pcc_safety=pcc,
                cross_flow=cross_flow,
                readiness=readiness,
            )
        planned_progress = planned.progress
        if combined.action is SafetyAction.CLAMP:
            assert combined.clamped_target_w is not None
            assert planned_progress.effective_target_w is not None
            planned_progress = replace(
                planned_progress,
                effective_target_w=min(planned_progress.effective_target_w, combined.clamped_target_w),
            )
        return _result(
            inputs,
            state=ExecutionState.TRANSITION,
            roles=inputs.current_roles,
            decision=ExecutorDecision.CLAMPED if combined.action is SafetyAction.CLAMP else planned.decision,
            action=combined.action,
            reasons=combined.reasons,
            transition=planned_progress,
            pcc_safety=pcc,
            cross_flow=cross_flow,
            readiness=readiness,
        )

    advanced = advance_transition(
        progress,
        physical=inputs.physical,
        command_evidence=_bound_command(progress, inputs.command_record),
        safety_decisions=(*inputs.additional_safety, telemetry, conflicts, command_expiry, readiness),
        pcc_safety=pcc,
        cross_flow_safety=cross_flow,
        readiness_evidence=inputs.readiness_evidence,
        settling_evidence=inputs.settling_evidence,
        capabilities=inputs.capabilities,
        config=inputs.transition_config,
        safety_config=inputs.safety_config,
        now=inputs.now,
        execution_enabled=context.enabled,
        proposal_command_id=inputs.proposal_command_id,
        writer_conflicts=context.writer_conflicts,
    )
    proposal, adapter_reasons = _adapt_proposal(inputs, advanced.proposals)
    if adapter_reasons:
        return _result(
            inputs,
            state=ExecutionState.ROLLBACK if progress.writer_owners else ExecutionState.PRECHECK,
            roles=progress.physical_roles,
            decision=ExecutorDecision.REJECTED,
            action=SafetyAction.ROLLBACK if progress.writer_owners else SafetyAction.DEFER,
            reasons=adapter_reasons,
            transition=progress,
            pcc_safety=pcc,
            cross_flow=cross_flow,
            readiness=readiness,
        )
    state = advanced.outcome_state or ExecutionState.TRANSITION
    if proposal is not None and proposal.operation is AdapterOperation.START:
        state = ExecutionState.DEYE_START_REQUIRED
    return _result(
        inputs,
        state=state,
        roles=advanced.progress.physical_roles,
        decision=advanced.decision,
        action=advanced.action,
        reasons=advanced.reasons,
        transition=advanced.progress,
        proposal=proposal,
        pcc_safety=pcc,
        cross_flow=cross_flow,
        readiness=readiness,
    )
