"""Bounded immutable diagnostics for the pure shadow executor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from .adapters.base import ShadowControlProposal
from .enums import ExecutionState, ExecutorDecision, FailureReason, PhysicalRole, PlannerIntentType, SafetyAction
from .reconciliation import ReconciliationResult
from .safety import SafetyDecision
from .transition import TransitionProgress

_MAX_REASONS = 12
_MAX_CONFLICTS = 8
_MAX_TEXT = 160


def _bounded(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:_MAX_TEXT]


def _summary(decision: SafetyDecision | None) -> str | None:
    if decision is None:
        return None
    reasons = ",".join(reason.value for reason in decision.reasons[:_MAX_REASONS]) or "NONE"
    target = "" if decision.clamped_target_w is None else f":{decision.clamped_target_w:.3f}W"
    return _bounded(f"{decision.action.value}:{reasons}{target}")


def _proposal_summary(proposal: ShadowControlProposal | None) -> str | None:
    if proposal is None:
        return None
    target = "" if proposal.effective_executor_target_w is None else f":{proposal.effective_executor_target_w:.3f}W"
    ceiling = (
        ""
        if proposal.applied_supervisory_ceiling_w is None
        else f":CEILING={proposal.applied_supervisory_ceiling_w:.3f}W"
    )
    return _bounded(f"{proposal.battery}:{proposal.operation.value}:{proposal.control_contract}{target}{ceiling}")


@dataclass(frozen=True)
class ExecutorDiagnostics:
    execution_state: str
    transition_phase: str | None
    solax_role: str
    deye_role: str
    planner_intent: str | None
    requested_target_w: float | None
    effective_target_w: float | None
    configured_max_ceiling_w: float | None
    applied_supervisory_ceiling_w: float | None
    executor_decision: str
    reason_codes: tuple[str, ...]
    safety_action: str
    plan_id: str | None
    intent_id: str | None
    command_id: str | None
    execution_epoch: int
    proposal_summary: str | None
    reconciliation_summary: str | None
    pcc_safety_summary: str | None
    cross_flow_summary: str | None
    readiness_summary: str | None
    writer_conflicts: tuple[str, ...]
    last_failure_reason: str | None
    shadow_only: bool
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        if self.execution_epoch < 0 or self.shadow_only is not True:
            raise ValueError("diagnostics must use a nonnegative epoch and remain shadow-only")
        text_fields = (
            "execution_state",
            "transition_phase",
            "solax_role",
            "deye_role",
            "planner_intent",
            "executor_decision",
            "safety_action",
            "plan_id",
            "intent_id",
            "command_id",
            "proposal_summary",
            "reconciliation_summary",
            "pcc_safety_summary",
            "cross_flow_summary",
            "readiness_summary",
            "last_failure_reason",
        )
        if not all(getattr(self, name) is None or isinstance(getattr(self, name), str) for name in text_fields):
            raise ValueError("diagnostic text fields must be strings or None")
        raw_reasons = tuple(self.reason_codes)
        raw_conflicts = tuple(self.writer_conflicts)
        if not all(isinstance(value, str) for value in (*raw_reasons, *raw_conflicts)):
            raise ValueError("diagnostic reason and conflict values must be strings")
        reasons = tuple(_bounded(value) for value in raw_reasons[:_MAX_REASONS])
        conflicts = tuple(_bounded(value) for value in sorted(set(raw_conflicts)))[:_MAX_CONFLICTS]
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "writer_conflicts", conflicts)
        for name in text_fields:
            object.__setattr__(self, name, _bounded(getattr(self, name)))

    def as_payload(self) -> MappingProxyType:
        return MappingProxyType(
            {
                "execution_state": self.execution_state,
                "transition_phase": self.transition_phase,
                "solax_role": self.solax_role,
                "deye_role": self.deye_role,
                "planner_intent": self.planner_intent,
                "requested_target_w": self.requested_target_w,
                "effective_target_w": self.effective_target_w,
                "configured_max_ceiling_w": self.configured_max_ceiling_w,
                "applied_supervisory_ceiling_w": self.applied_supervisory_ceiling_w,
                "executor_decision": self.executor_decision,
                "reason_codes": self.reason_codes,
                "safety_action": self.safety_action,
                "plan_id": self.plan_id,
                "intent_id": self.intent_id,
                "command_id": self.command_id,
                "execution_epoch": self.execution_epoch,
                "proposal_summary": self.proposal_summary,
                "reconciliation_summary": self.reconciliation_summary,
                "pcc_safety_summary": self.pcc_safety_summary,
                "cross_flow_summary": self.cross_flow_summary,
                "readiness_summary": self.readiness_summary,
                "writer_conflicts": self.writer_conflicts,
                "last_failure_reason": self.last_failure_reason,
                "shadow_only": self.shadow_only,
                "updated_at": self.updated_at.isoformat(),
            }
        )


def build_diagnostics(
    *,
    state: ExecutionState,
    roles: tuple[PhysicalRole, PhysicalRole],
    intent_type: PlannerIntentType | None,
    requested_target_w: float | None,
    decision: ExecutorDecision,
    reasons: tuple[FailureReason, ...],
    action: SafetyAction,
    execution_epoch: int,
    now: datetime,
    transition: TransitionProgress | None = None,
    proposal: ShadowControlProposal | None = None,
    reconciliation: ReconciliationResult | None = None,
    pcc_safety: SafetyDecision | None = None,
    cross_flow: SafetyDecision | None = None,
    readiness: SafetyDecision | None = None,
    writer_conflicts: tuple[str, ...] = (),
    last_failure: FailureReason | None = None,
) -> ExecutorDiagnostics:
    plan = transition.plan if transition is not None else None
    reconciliation_summary = None
    if reconciliation is not None:
        reason_text = ",".join(reason.value for reason in reconciliation.reasons[:_MAX_REASONS]) or "NONE"
        reconciliation_summary = f"{reconciliation.outcome_state.value}:{reason_text}"
    command_id = None
    effective_target_w = requested_target_w
    if transition is not None:
        effective_target_w = transition.effective_target_w
        if transition.expected_command is not None:
            command_id = transition.expected_command.command_id
    return ExecutorDiagnostics(
        execution_state=state.value,
        transition_phase=transition.current_phase.value if transition is not None else None,
        solax_role=roles[0].value,
        deye_role=roles[1].value,
        planner_intent=intent_type.value if intent_type is not None else None,
        requested_target_w=requested_target_w,
        effective_target_w=effective_target_w,
        configured_max_ceiling_w=proposal.configured_max_ceiling_w if proposal is not None else None,
        applied_supervisory_ceiling_w=(proposal.applied_supervisory_ceiling_w if proposal is not None else None),
        executor_decision=decision.value,
        reason_codes=tuple(reason.value for reason in reasons),
        safety_action=action.value,
        plan_id=plan.plan_id if plan is not None else None,
        intent_id=plan.intent_id if plan is not None else None,
        command_id=command_id,
        execution_epoch=execution_epoch,
        proposal_summary=_proposal_summary(proposal),
        reconciliation_summary=reconciliation_summary,
        pcc_safety_summary=_summary(pcc_safety),
        cross_flow_summary=_summary(cross_flow),
        readiness_summary=_summary(readiness),
        writer_conflicts=writer_conflicts,
        last_failure_reason=last_failure.value if last_failure is not None else None,
        shadow_only=True,
        updated_at=now,
    )
