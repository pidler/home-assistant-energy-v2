"""Pure break-before-make transition planning and evidence-driven advancement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from types import MappingProxyType

from ..deye_state import DeyeOperatingState
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
from .models import CapabilitySnapshot, CommandRecord, PhysicalRoleVector, PlannerIntent
from .safety import (
    PhysicalStateSnapshot,
    ReadinessEvidence,
    SafetyConfig,
    SafetyDecision,
    combine_safety_decisions,
    evaluate_readiness,
)

_ACTIVE = {PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
_WRITER_ROLES = {PhysicalRole.HOLD, *_ACTIVE}
_READBACK_STATES = {
    CommandLifecycleState.READBACK_CONFIRMED,
    CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED,
    CommandLifecycleState.STABLE,
}
_PHYSICAL_STATES = {CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED, CommandLifecycleState.STABLE}


class ShadowCommandKind(StrEnum):
    STOP_CURRENT_OWNER = "STOP_CURRENT_OWNER"
    ARM_DESTINATION = "ARM_DESTINATION"
    START_DEYE_REQUIRED = "START_DEYE_REQUIRED"
    APPLY_TARGET = "APPLY_TARGET"


@dataclass(frozen=True)
class ShadowCommandProposal:
    kind: ShadowCommandKind
    battery: str
    role: PhysicalRole
    command_id: str | None = None
    target_w: float | None = None

    def __post_init__(self) -> None:
        if self.battery not in {"SOLAX", "DEYE"}:
            raise ValueError("battery must be SOLAX or DEYE")
        if not isinstance(self.role, PhysicalRole):
            raise ValueError("role must be PhysicalRole")
        if self.target_w is not None and (
            isinstance(self.target_w, bool) or not isfinite(self.target_w) or self.target_w <= 0
        ):
            raise ValueError("target_w must be finite and positive")
        command_required = self.kind in {ShadowCommandKind.STOP_CURRENT_OWNER, ShadowCommandKind.APPLY_TARGET} or (
            self.kind is ShadowCommandKind.ARM_DESTINATION and self.role is PhysicalRole.HOLD
        )
        if command_required and not self.command_id:
            raise ValueError("stop, hold-arm, and target proposals require command_id")
        if not command_required and self.command_id is not None:
            raise ValueError("passive proposals cannot carry command_id")
        if self.kind is ShadowCommandKind.APPLY_TARGET:
            if self.role not in _ACTIVE or self.target_w is None:
                raise ValueError("target proposal requires active role and target")
        elif self.target_w is not None:
            raise ValueError("only target proposal may carry target_w")
        if self.kind is ShadowCommandKind.ARM_DESTINATION and self.role in _ACTIVE:
            raise ValueError("ARM_DESTINATION must remain passive")


@dataclass(frozen=True)
class ExpectedCommandEvidence:
    command_id: str
    kind: ShadowCommandKind
    battery: str
    role: PhysicalRole
    target_w: float | None
    plan_id: str
    intent_id: str
    execution_epoch: int

    def __post_init__(self) -> None:
        if not self.command_id or not self.plan_id or not self.intent_id or self.execution_epoch < 0:
            raise ValueError("valid expected-command identity is required")
        if self.battery not in {"SOLAX", "DEYE"}:
            raise ValueError("battery must be SOLAX or DEYE")
        if self.kind not in {
            ShadowCommandKind.STOP_CURRENT_OWNER,
            ShadowCommandKind.ARM_DESTINATION,
            ShadowCommandKind.APPLY_TARGET,
        }:
            raise ValueError("only stop, hold-arm, or target commands can be awaited")
        if self.target_w is not None and (
            isinstance(self.target_w, bool) or not isfinite(self.target_w) or self.target_w <= 0
        ):
            raise ValueError("target_w must be finite and positive")
        if self.kind is ShadowCommandKind.APPLY_TARGET:
            if self.role not in _ACTIVE or self.target_w is None:
                raise ValueError("target expectation requires active role and target")
        elif self.kind is ShadowCommandKind.ARM_DESTINATION:
            if self.battery != "SOLAX" or self.role is not PhysicalRole.HOLD or self.target_w is not None:
                raise ValueError("hold-arm expectation must bind SolaX HOLD without target")
        elif self.target_w is not None or self.role in _ACTIVE:
            raise ValueError("stop expectation must be passive")


@dataclass(frozen=True)
class BoundCommandEvidence:
    record: CommandRecord
    kind: ShadowCommandKind
    battery: str
    role: PhysicalRole
    target_w: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.record, CommandRecord):
            raise ValueError("record must be CommandRecord")
        if self.battery not in {"SOLAX", "DEYE"}:
            raise ValueError("battery must be SOLAX or DEYE")
        if self.target_w is not None and (
            isinstance(self.target_w, bool) or not isfinite(self.target_w) or self.target_w <= 0
        ):
            raise ValueError("target_w must be finite and positive")


@dataclass(frozen=True)
class NeutralSettlingEvidence:
    neutral_since: datetime
    latest_sample_at: datetime
    solax_source_timestamp: datetime
    deye_source_timestamp: datetime
    currently_neutral: bool
    threshold_w: float
    required_dwell_s: float

    def __post_init__(self) -> None:
        for name in ("neutral_since", "latest_sample_at", "solax_source_timestamp", "deye_source_timestamp"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if not isinstance(self.currently_neutral, bool):
            raise ValueError("currently_neutral must be bool")
        for name in ("threshold_w", "required_dwell_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.neutral_since > min(self.solax_source_timestamp, self.deye_source_timestamp):
            raise ValueError("neutral_since cannot follow source evidence")
        if self.latest_sample_at != max(self.solax_source_timestamp, self.deye_source_timestamp):
            raise ValueError("latest_sample_at must be newest source sample")


@dataclass(frozen=True)
class TransitionConfig:
    phase_timeouts_s: Mapping[TransitionPhase, float]

    def __post_init__(self) -> None:
        expected = set(TransitionPhase) - {TransitionPhase.COMPLETE}
        if set(self.phase_timeouts_s) != expected:
            raise ValueError("phase_timeouts_s must cover every nonterminal phase exactly")
        values: dict[TransitionPhase, float] = {}
        for phase, timeout in self.phase_timeouts_s.items():
            if not isinstance(phase, TransitionPhase):
                raise ValueError("phase timeout keys must be TransitionPhase")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, int | float)
                or not isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("phase timeouts must be finite and positive")
            values[phase] = float(timeout)
        object.__setattr__(self, "phase_timeouts_s", MappingProxyType(values))


def _writer_owners(roles: PhysicalRoleVector) -> frozenset[str]:
    owners: set[str] = set()
    if roles.solax in _WRITER_ROLES:
        owners.add("SOLAX")
    if roles.deye in _WRITER_ROLES:
        owners.add("DEYE")
    return frozenset(owners)


def _canonical_phases(full_break: bool, active_target: bool) -> tuple[TransitionPhase, ...]:
    phases = [TransitionPhase.FREEZE_INTENT]
    if full_break:
        phases.extend(
            (TransitionPhase.STOP_CURRENT_OWNER, TransitionPhase.WAIT_CURRENT_ACK, TransitionPhase.WAIT_NEUTRAL)
        )
    if active_target:
        phases.extend(
            (
                TransitionPhase.VERIFY_PCC,
                TransitionPhase.ARM_DESTINATION,
                TransitionPhase.WAIT_DESTINATION_READY,
                TransitionPhase.ISSUE_TARGET,
                TransitionPhase.WAIT_TARGET_ACK,
                TransitionPhase.WAIT_PHYSICAL_RESPONSE,
                TransitionPhase.WAIT_STABLE,
            )
        )
    phases.append(TransitionPhase.COMPLETE)
    return tuple(phases)


@dataclass(frozen=True)
class TransitionPlan:
    transition_id: str
    plan_id: str
    intent_id: str
    execution_epoch: int
    intent: PlannerIntent
    current_state: ExecutionState
    current_roles: PhysicalRoleVector
    desired_roles: PhysicalRoleVector
    phases: tuple[TransitionPhase, ...]
    created_at: datetime
    deadline: datetime
    full_break_required: bool
    reduced_retarget: bool = False

    def __post_init__(self) -> None:
        if not self.transition_id or not self.plan_id or not self.intent_id or self.execution_epoch < 0:
            raise ValueError("valid transition identity is required")
        if self.plan_id != self.intent.plan_id or self.intent_id != self.intent.intent_id:
            raise ValueError("transition identity must match intent")
        for name in ("created_at", "deadline"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.deadline <= self.created_at or self.deadline != self.intent.deadline:
            raise ValueError("transition deadline must match live intent")
        phases = tuple(self.phases)
        expected_break = bool(_writer_owners(self.current_roles))
        active_target = self.desired_roles.solax in _ACTIVE or self.desired_roles.deye in _ACTIVE
        if phases != _canonical_phases(expected_break, active_target):
            raise ValueError("transition phases must use canonical sequence")
        if self.full_break_required is not expected_break or self.reduced_retarget:
            raise ValueError("transition break flags are inconsistent")
        object.__setattr__(self, "phases", phases)
        if not isinstance(self.full_break_required, bool) or not isinstance(self.reduced_retarget, bool):
            raise ValueError("transition flags must be bool")


@dataclass(frozen=True)
class TransitionStepEvidence:
    phase: TransitionPhase
    completed_at: datetime
    transition_id: str
    plan_id: str
    intent_id: str
    execution_epoch: int
    proposal: ExpectedCommandEvidence | None = None
    command: BoundCommandEvidence | None = None
    pcc_action: SafetyAction | None = None
    pcc_reasons: tuple[FailureReason, ...] = ()
    pcc_source_timestamp: datetime | None = None
    effective_target_w: float | None = None
    readiness: ReadinessEvidence | None = None
    neutral_since: datetime | None = None
    settling_sample_at: datetime | None = None
    solax_settling_sample_at: datetime | None = None
    deye_settling_sample_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, TransitionPhase):
            raise ValueError("step phase must be TransitionPhase")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        if not self.transition_id or not self.plan_id or not self.intent_id or self.execution_epoch < 0:
            raise ValueError("valid step identity is required")
        reasons = tuple(self.pcc_reasons)
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("PCC reasons must contain FailureReason values")
        object.__setattr__(self, "pcc_reasons", reasons)
        for name in (
            "pcc_source_timestamp",
            "neutral_since",
            "settling_sample_at",
            "solax_settling_sample_at",
            "deye_settling_sample_at",
        ):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        settling_sources = (self.solax_settling_sample_at, self.deye_settling_sample_at)
        if self.settling_sample_at is None:
            if any(timestamp is not None for timestamp in settling_sources):
                raise ValueError("settling source timestamps require aggregate sample time")
        elif any(timestamp is None for timestamp in settling_sources):
            raise ValueError("settling sample requires both source timestamps")
        elif self.settling_sample_at != max(settling_sources):
            raise ValueError("aggregate settling time must match latest source timestamp")
        if self.effective_target_w is not None and (
            isinstance(self.effective_target_w, bool)
            or not isfinite(self.effective_target_w)
            or self.effective_target_w <= 0
        ):
            raise ValueError("step effective target must be finite and positive")


@dataclass(frozen=True)
class TransitionProgress:
    plan: TransitionPlan
    phase_index: int
    completed_phases: tuple[TransitionPhase, ...]
    completed_steps: tuple[TransitionStepEvidence, ...]
    phase_entered_at: datetime
    phase_deadline: datetime
    physical_roles: PhysicalRoleVector
    writer_owners: frozenset[str]
    expected_command: ExpectedCommandEvidence | None
    requested_target_w: float | None
    effective_target_w: float | None
    pcc_verified_at: datetime | None = None
    pcc_valid_until: datetime | None = None
    pcc_action: SafetyAction | None = None
    pcc_reasons: tuple[FailureReason, ...] = ()
    readiness_verified_at: datetime | None = None
    readiness_valid_until: datetime | None = None
    neutral_since: datetime | None = None
    neutral_last_sample_at: datetime | None = None
    neutral_last_solax_sample_at: datetime | None = None
    neutral_last_deye_sample_at: datetime | None = None
    settling_currently_neutral: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TransitionPlan):
            raise ValueError("plan must be TransitionPlan")
        if isinstance(self.phase_index, bool) or not isinstance(self.phase_index, int):
            raise ValueError("phase_index must be integer")
        if not 0 <= self.phase_index < len(self.plan.phases):
            raise ValueError("phase_index outside canonical sequence")
        completed = tuple(self.completed_phases)
        if completed != self.plan.phases[: self.phase_index]:
            raise ValueError("completed phases do not match canonical progress")
        object.__setattr__(self, "completed_phases", completed)
        steps = tuple(self.completed_steps)
        if len(steps) != self.phase_index or tuple(step.phase for step in steps) != completed:
            raise ValueError("completed steps must prove the canonical completed prefix")
        if not all(isinstance(step, TransitionStepEvidence) for step in steps):
            raise ValueError("completed steps must contain TransitionStepEvidence values")
        object.__setattr__(self, "completed_steps", steps)
        for name in (
            "phase_entered_at",
            "phase_deadline",
            "pcc_verified_at",
            "pcc_valid_until",
            "readiness_verified_at",
            "readiness_valid_until",
            "neutral_since",
            "neutral_last_sample_at",
            "neutral_last_solax_sample_at",
            "neutral_last_deye_sample_at",
        ):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.phase_deadline <= self.phase_entered_at:
            raise ValueError("phase deadline must follow entry")
        if self.phase_entered_at < self.plan.created_at or self.phase_deadline > self.plan.deadline:
            raise ValueError("progress timestamps must stay within transition lifetime")
        previous_completed_at = self.plan.created_at
        for step in steps:
            if (
                step.transition_id != self.plan.transition_id
                or step.plan_id != self.plan.plan_id
                or step.intent_id != self.plan.intent_id
                or step.execution_epoch != self.plan.execution_epoch
            ):
                raise ValueError("completed step identity must match transition")
            if not previous_completed_at <= step.completed_at <= self.phase_entered_at:
                raise ValueError("completed step chronology is invalid")
            previous_completed_at = step.completed_at
        owners = frozenset(self.writer_owners)
        if not owners <= {"SOLAX", "DEYE"}:
            raise ValueError("writer owners must be SOLAX/DEYE")
        object.__setattr__(self, "writer_owners", owners)
        pcc_reasons = tuple(self.pcc_reasons)
        if not all(isinstance(reason, FailureReason) for reason in pcc_reasons):
            raise ValueError("PCC reasons must contain FailureReason values")
        object.__setattr__(self, "pcc_reasons", pcc_reasons)
        if self.pcc_action is not None and self.pcc_action not in {SafetyAction.NONE, SafetyAction.CLAMP}:
            raise ValueError("persisted PCC action must be NONE or CLAMP")
        if self.requested_target_w != self.plan.intent.target_w:
            raise ValueError("requested target must match frozen intent")
        if self.effective_target_w is not None:
            if (
                isinstance(self.effective_target_w, bool)
                or not isfinite(self.effective_target_w)
                or self.effective_target_w <= 0
                or self.requested_target_w is None
                or self.effective_target_w > self.requested_target_w
            ):
                raise ValueError("effective target must be positive and bounded by request")
        phase = self.current_phase
        stop_wait = phase is TransitionPhase.WAIT_CURRENT_ACK
        arm_wait = phase is TransitionPhase.ARM_DESTINATION
        target_wait = phase in {
            TransitionPhase.WAIT_TARGET_ACK,
            TransitionPhase.WAIT_PHYSICAL_RESPONSE,
            TransitionPhase.WAIT_STABLE,
            TransitionPhase.COMPLETE,
        }
        if stop_wait and (
            self.expected_command is None or self.expected_command.kind is not ShadowCommandKind.STOP_CURRENT_OWNER
        ):
            raise ValueError("stop wait requires exact stop command")
        if arm_wait and self.expected_command is not None:
            if self.expected_command.kind is not ShadowCommandKind.ARM_DESTINATION:
                raise ValueError("ARM wait requires exact hold-arm command")
        if target_wait and (self.plan.desired_roles.solax in _ACTIVE or self.plan.desired_roles.deye in _ACTIVE):
            if self.expected_command is None or self.expected_command.kind is not ShadowCommandKind.APPLY_TARGET:
                raise ValueError("target lifecycle requires exact target command")
        if not stop_wait and not arm_wait and not target_wait and self.expected_command is not None:
            raise ValueError("unexpected command outside command wait")
        if self.expected_command is not None:
            expected = self.expected_command
            if (
                expected.plan_id != self.plan.plan_id
                or expected.intent_id != self.plan.intent_id
                or expected.execution_epoch != self.plan.execution_epoch
            ):
                raise ValueError("expected command identity must match transition")
            if expected.kind is ShadowCommandKind.STOP_CURRENT_OWNER:
                stop_owner = _stop_owner(self)
                if stop_owner is None or expected.battery != stop_owner[0]:
                    raise ValueError("stop command must address the current writer owner")
            elif expected.kind is ShadowCommandKind.ARM_DESTINATION:
                owner = _owner(self.plan.desired_roles)
                if (
                    owner is None
                    or owner[0] != "DEYE"
                    or (expected.battery, expected.role)
                    != (
                        "SOLAX",
                        PhysicalRole.HOLD,
                    )
                ):
                    raise ValueError("hold-arm command must protect a DEYE destination")
            else:
                owner = _owner(self.plan.desired_roles)
                if owner is None or (expected.battery, expected.role) != owner:
                    raise ValueError("target command must match desired owner and role")
                if expected.target_w != self.effective_target_w:
                    raise ValueError("target command must match effective bounded target")
        for verified_at, valid_until, name in (
            (self.pcc_verified_at, self.pcc_valid_until, "PCC"),
            (self.readiness_verified_at, self.readiness_valid_until, "readiness"),
        ):
            if (verified_at is None) is not (valid_until is None):
                raise ValueError(f"{name} verification timestamps must be paired")
            if verified_at is not None and valid_until is not None and valid_until <= verified_at:
                raise ValueError(f"{name} validity must follow verification")
        if (self.pcc_verified_at is None) is not (self.pcc_action is None):
            raise ValueError("PCC result must be stored with verification evidence")
        if self.neutral_since is not None and self.neutral_last_sample_at is None:
            raise ValueError("neutral_since requires a retained settling sample")
        settling_sources = (self.neutral_last_solax_sample_at, self.neutral_last_deye_sample_at)
        if self.neutral_last_sample_at is None:
            if any(timestamp is not None for timestamp in settling_sources):
                raise ValueError("settling source timestamps require aggregate sample time")
        elif any(timestamp is None for timestamp in settling_sources):
            raise ValueError("settling sample requires both retained source timestamps")
        elif self.neutral_last_sample_at != max(settling_sources):
            raise ValueError("aggregate settling time must match latest retained source timestamp")
        if self.settling_currently_neutral not in {None, True, False}:
            raise ValueError("settling_currently_neutral must be bool or None")
        if self.settling_currently_neutral is None and (
            self.neutral_since is not None or self.neutral_last_sample_at is not None
        ):
            raise ValueError("settling timestamps require an observed settling state")
        if self.settling_currently_neutral is True and self.neutral_since is None:
            raise ValueError("continuous neutral state requires neutral_since")
        if self.settling_currently_neutral is False:
            if self.neutral_since is not None:
                raise ValueError("non-neutral state must reset neutral_since")
            if self.neutral_last_sample_at is None:
                raise ValueError("non-neutral state requires a retained settling sample")
        if (
            self.neutral_since is not None
            and self.neutral_last_sample_at is not None
            and self.neutral_last_sample_at < self.neutral_since
        ):
            raise ValueError("neutral sample cannot precede continuous settling")
        if TransitionPhase.VERIFY_PCC in self.plan.phases:
            verify_index = self.plan.phases.index(TransitionPhase.VERIFY_PCC)
            if self.phase_index > verify_index and (
                self.pcc_verified_at is None or self.pcc_valid_until is None or self.effective_target_w is None
            ):
                raise ValueError("post-PCC progress requires bounded target evidence")
        if TransitionPhase.WAIT_DESTINATION_READY in self.plan.phases:
            ready_index = self.plan.phases.index(TransitionPhase.WAIT_DESTINATION_READY)
            if self.phase_index > ready_index and (
                self.readiness_verified_at is None or self.readiness_valid_until is None
            ):
                raise ValueError("post-readiness progress requires freshness evidence")
        if self.plan.full_break_required:
            neutral_index = self.plan.phases.index(TransitionPhase.WAIT_NEUTRAL)
            if self.phase_index > neutral_index and (self.neutral_since is None or self.neutral_last_sample_at is None):
                raise ValueError("post-neutral progress requires settling evidence")
        self._validate_completed_step_proofs()

    def _validate_completed_step_proofs(self) -> None:
        stop_proposal: ExpectedCommandEvidence | None = None
        target_proposal: ExpectedCommandEvidence | None = None
        for step in self.completed_steps:
            if step.phase is TransitionPhase.STOP_CURRENT_OWNER:
                if step.proposal is None or step.proposal.kind is not ShadowCommandKind.STOP_CURRENT_OWNER:
                    raise ValueError("STOP step requires retained stop proposal")
                stop_proposal = step.proposal
            elif step.phase is TransitionPhase.WAIT_CURRENT_ACK:
                if stop_proposal is None or not _bound_matches_expected(stop_proposal, step.command, self.plan):
                    raise ValueError("stop acknowledgement must match retained stop proposal")
                assert step.command is not None
                if step.command.record.lifecycle_state not in _READBACK_STATES:
                    raise ValueError("stop acknowledgement lacks readback proof")
            elif step.phase is TransitionPhase.WAIT_NEUTRAL:
                if (
                    step.neutral_since is None
                    or step.settling_sample_at is None
                    or step.solax_settling_sample_at is None
                    or step.deye_settling_sample_at is None
                ):
                    raise ValueError("neutral step requires retained continuous settling proof")
            elif step.phase is TransitionPhase.VERIFY_PCC:
                if (
                    step.pcc_action not in {SafetyAction.NONE, SafetyAction.CLAMP}
                    or step.pcc_source_timestamp is None
                    or step.effective_target_w is None
                ):
                    raise ValueError("PCC step requires retained bounded safety proof")
            elif step.phase is TransitionPhase.WAIT_DESTINATION_READY:
                if step.readiness is None:
                    raise ValueError("readiness step requires retained source evidence")
            elif step.phase is TransitionPhase.ISSUE_TARGET:
                if step.proposal is None or step.proposal.kind is not ShadowCommandKind.APPLY_TARGET:
                    raise ValueError("target issue step requires retained target proposal")
                target_proposal = step.proposal
            elif step.phase in {
                TransitionPhase.WAIT_TARGET_ACK,
                TransitionPhase.WAIT_PHYSICAL_RESPONSE,
                TransitionPhase.WAIT_STABLE,
            }:
                if target_proposal is None or not _bound_matches_expected(target_proposal, step.command, self.plan):
                    raise ValueError("target lifecycle proof must match retained target proposal")
                assert step.command is not None
                required_states = {
                    TransitionPhase.WAIT_TARGET_ACK: _READBACK_STATES,
                    TransitionPhase.WAIT_PHYSICAL_RESPONSE: _PHYSICAL_STATES,
                    TransitionPhase.WAIT_STABLE: {CommandLifecycleState.STABLE},
                }[step.phase]
                if step.command.record.lifecycle_state not in required_states:
                    raise ValueError("target lifecycle step lacks required retained evidence")

    @property
    def current_phase(self) -> TransitionPhase:
        return self.plan.phases[self.phase_index]


@dataclass(frozen=True)
class TransitionPlanningResult:
    plan: TransitionPlan | None
    progress: TransitionProgress | None
    decision: ExecutorDecision
    action: SafetyAction
    reasons: tuple[FailureReason, ...] = ()

    def __post_init__(self) -> None:
        reasons = tuple(self.reasons)
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("reasons must contain FailureReason values")
        object.__setattr__(self, "reasons", reasons)


@dataclass(frozen=True)
class TransitionAdvanceResult:
    progress: TransitionProgress
    phase: TransitionPhase
    phase_started_at: datetime
    decision: ExecutorDecision
    action: SafetyAction
    advanced: bool
    completed: bool
    proposals: tuple[ShadowCommandProposal, ...] = ()
    outcome_state: ExecutionState | None = None
    reasons: tuple[FailureReason, ...] = ()

    def __post_init__(self) -> None:
        if self.phase is not self.progress.current_phase or self.phase_started_at != self.progress.phase_entered_at:
            raise ValueError("result phase must match immutable progress")
        proposals = tuple(self.proposals)
        reasons = tuple(self.reasons)
        if not all(isinstance(proposal, ShadowCommandProposal) for proposal in proposals):
            raise ValueError("proposals must contain ShadowCommandProposal values")
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("reasons must contain FailureReason values")
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "reasons", reasons)


def _desired_roles(intent: PlannerIntent) -> PhysicalRoleVector:
    roles = {
        PlannerIntentType.NORMAL: PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF),
        PlannerIntentType.CHARGE_SOLAX: PhysicalRoleVector(PhysicalRole.CHARGE, PhysicalRole.OFF),
        PlannerIntentType.DISCHARGE_SOLAX: PhysicalRoleVector(PhysicalRole.DISCHARGE, PhysicalRole.OFF),
        PlannerIntentType.CHARGE_DEYE: PhysicalRoleVector(PhysicalRole.HOLD, PhysicalRole.CHARGE),
        PlannerIntentType.DISCHARGE_DEYE: PhysicalRoleVector(PhysicalRole.HOLD, PhysicalRole.DISCHARGE),
    }
    return roles[intent.intent_type]


def _owner(roles: PhysicalRoleVector) -> tuple[str, PhysicalRole] | None:
    if roles.solax in _ACTIVE:
        return "SOLAX", roles.solax
    if roles.deye in _ACTIVE:
        return "DEYE", roles.deye
    return None


def _stop_owner(progress: TransitionProgress) -> tuple[str, PhysicalRole] | None:
    active_owner = _owner(progress.physical_roles)
    if active_owner is not None:
        return active_owner
    if len(progress.writer_owners) == 1:
        battery = next(iter(progress.writer_owners))
        role = progress.physical_roles.solax if battery == "SOLAX" else progress.physical_roles.deye
        return battery, role
    return None


def _capability_reasons(
    desired: PhysicalRoleVector,
    capabilities: CapabilitySnapshot,
    deye_state: DeyeOperatingState,
) -> tuple[FailureReason, ...]:
    reasons: list[FailureReason] = []
    if desired.solax is PhysicalRole.HOLD and not capabilities.solax_mode5_hold_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if desired.solax in _ACTIVE and not (
        capabilities.solax_mode1_dispatch_verified and capabilities.solax_timeout_verified
    ):
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if desired.deye is PhysicalRole.HOLD and not capabilities.deye_hold_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if desired.deye in _ACTIVE:
        if not capabilities.deye_bounded_export_verified or not capabilities.solax_mode5_hold_verified:
            reasons.append(FailureReason.MODE_UNVERIFIED)
        if deye_state is DeyeOperatingState.INTENTIONAL_OFF and not capabilities.deye_start_verified:
            reasons.append(FailureReason.MODE_UNVERIFIED)
        if deye_state in {DeyeOperatingState.UNEXPECTED_FAULT, DeyeOperatingState.UNAVAILABLE}:
            reasons.append(FailureReason.DEYE_NOT_READY)
    return tuple(dict.fromkeys(reasons))


def plan_transition(
    *,
    transition_id: str,
    current_state: ExecutionState,
    current_roles: PhysicalRoleVector,
    intent: PlannerIntent,
    current_execution_epoch: int,
    capabilities: CapabilitySnapshot,
    deye_state: DeyeOperatingState,
    config: TransitionConfig,
    now: datetime,
) -> TransitionPlanningResult:
    """Plan a shadow sequence. Pass 2 deliberately does not support direct retarget."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not transition_id or current_execution_epoch < 0:
        raise ValueError("transition_id and nonnegative execution epoch are required")
    if now >= intent.deadline:
        return TransitionPlanningResult(
            None, None, ExecutorDecision.REJECTED, SafetyAction.DEFER, (FailureReason.COMMAND_EXPIRED,)
        )
    desired = _desired_roles(intent)
    capability_reasons = _capability_reasons(desired, capabilities, deye_state)
    if capability_reasons:
        return TransitionPlanningResult(None, None, ExecutorDecision.REJECTED, SafetyAction.DEFER, capability_reasons)
    # Direct retarget semantics are not yet proven, so even same-direction retargets use a full break.
    full_break = bool(_writer_owners(current_roles))
    active_target = desired.solax in _ACTIVE or desired.deye in _ACTIVE
    phases = _canonical_phases(full_break, active_target)
    plan = TransitionPlan(
        transition_id=transition_id,
        plan_id=intent.plan_id,
        intent_id=intent.intent_id,
        execution_epoch=current_execution_epoch,
        intent=intent,
        current_state=current_state,
        current_roles=current_roles,
        desired_roles=desired,
        phases=phases,
        created_at=now,
        deadline=intent.deadline,
        full_break_required=full_break,
        reduced_retarget=False,
    )
    progress = TransitionProgress(
        plan=plan,
        phase_index=0,
        completed_phases=(),
        completed_steps=(),
        phase_entered_at=now,
        phase_deadline=min(
            intent.deadline,
            now + timedelta(seconds=config.phase_timeouts_s[TransitionPhase.FREEZE_INTENT]),
        ),
        physical_roles=current_roles,
        writer_owners=_writer_owners(current_roles),
        expected_command=None,
        requested_target_w=intent.target_w,
        effective_target_w=intent.target_w,
    )
    return TransitionPlanningResult(plan, progress, ExecutorDecision.ACCEPTED, SafetyAction.NONE)


def _failure(
    progress: TransitionProgress,
    *,
    action: SafetyAction,
    reason: FailureReason,
) -> TransitionAdvanceResult:
    outcome = ExecutionState.SAFE_STOP if action is SafetyAction.SAFE_STOP else ExecutionState.ROLLBACK
    return TransitionAdvanceResult(
        progress,
        progress.current_phase,
        progress.phase_entered_at,
        ExecutorDecision.ABORTED,
        action,
        False,
        False,
        outcome_state=outcome,
        reasons=(reason,),
    )


def _next_progress(
    progress: TransitionProgress,
    now: datetime,
    config: TransitionConfig,
    step: TransitionStepEvidence,
    **changes: object,
) -> TransitionProgress:
    next_index = progress.phase_index + 1
    if next_index >= len(progress.plan.phases):
        raise ValueError("cannot advance beyond COMPLETE")
    next_phase = progress.plan.phases[next_index]
    deadline = progress.plan.deadline
    if next_phase is not TransitionPhase.COMPLETE:
        deadline = min(deadline, now + timedelta(seconds=config.phase_timeouts_s[next_phase]))
    values: dict[str, object] = {
        "phase_index": next_index,
        "completed_phases": progress.plan.phases[:next_index],
        "completed_steps": (*progress.completed_steps, step),
        "phase_entered_at": now,
        "phase_deadline": deadline,
    }
    values.update(changes)
    return replace(progress, **values)


def _step_evidence(progress: TransitionProgress, now: datetime, **proof: object) -> TransitionStepEvidence:
    return TransitionStepEvidence(
        phase=progress.current_phase,
        completed_at=now,
        transition_id=progress.plan.transition_id,
        plan_id=progress.plan.plan_id,
        intent_id=progress.plan.intent_id,
        execution_epoch=progress.plan.execution_epoch,
        **proof,
    )


def _advanced(progress: TransitionProgress) -> TransitionAdvanceResult:
    completed = progress.current_phase is TransitionPhase.COMPLETE
    return TransitionAdvanceResult(
        progress,
        progress.current_phase,
        progress.phase_entered_at,
        ExecutorDecision.ACCEPTED,
        SafetyAction.NONE,
        True,
        completed,
        outcome_state=_desired_execution_state(progress.plan.desired_roles) if completed else None,
    )


def _desired_execution_state(roles: PhysicalRoleVector) -> ExecutionState:
    if roles.solax in _ACTIVE:
        return ExecutionState.SOLAX_DISPATCH
    if roles.deye in _ACTIVE:
        return ExecutionState.DEYE_DISPATCH
    return ExecutionState.NORMAL


def _bound_matches_expected(
    expected: ExpectedCommandEvidence,
    evidence: BoundCommandEvidence | None,
    plan: TransitionPlan,
) -> bool:
    if evidence is None:
        return False
    command = evidence.record
    if (
        command.command_id != expected.command_id
        or command.execution_epoch != expected.execution_epoch
        or command.plan_id != expected.plan_id
        or command.intent_id != expected.intent_id
        or evidence.kind is not expected.kind
        or evidence.battery != expected.battery
        or evidence.role is not expected.role
        or evidence.target_w != expected.target_w
    ):
        return False
    if expected.kind is ShadowCommandKind.STOP_CURRENT_OWNER:
        return command.intent_type is PlannerIntentType.NORMAL and command.requested_target_w is None
    if expected.kind is ShadowCommandKind.ARM_DESTINATION:
        effective = command.clamped_target_w if command.clamped_target_w is not None else command.requested_target_w
        return command.intent_type is plan.intent.intent_type and effective == plan.intent.target_w
    effective = command.clamped_target_w if command.clamped_target_w is not None else command.requested_target_w
    return command.intent_type is plan.intent.intent_type and effective == expected.target_w


def _command_matches(progress: TransitionProgress, evidence: BoundCommandEvidence | None) -> bool:
    expected = progress.expected_command
    return expected is not None and _bound_matches_expected(expected, evidence, progress.plan)


def _expected(progress: TransitionProgress, proposal: ShadowCommandProposal) -> ExpectedCommandEvidence:
    assert proposal.command_id is not None
    return ExpectedCommandEvidence(
        command_id=proposal.command_id,
        kind=proposal.kind,
        battery=proposal.battery,
        role=proposal.role,
        target_w=proposal.target_w,
        plan_id=progress.plan.plan_id,
        intent_id=progress.plan.intent_id,
        execution_epoch=progress.plan.execution_epoch,
    )


def _proposals(
    progress: TransitionProgress,
    phase: TransitionPhase,
    readiness: ReadinessEvidence | None,
    command_id: str | None,
    target_w: float | None,
) -> tuple[ShadowCommandProposal, ...]:
    plan = progress.plan
    desired_owner = _owner(plan.desired_roles)
    if phase is TransitionPhase.STOP_CURRENT_OWNER:
        owner = _stop_owner(progress)
        if owner is None or not command_id:
            return ()
        battery = owner[0]
        neutral_role = PhysicalRole.NATIVE if battery == "SOLAX" else PhysicalRole.OFF
        return (ShadowCommandProposal(ShadowCommandKind.STOP_CURRENT_OWNER, battery, neutral_role, command_id),)
    if phase is TransitionPhase.ARM_DESTINATION:
        if desired_owner and desired_owner[0] == "DEYE":
            if progress.expected_command is None:
                if not command_id:
                    return ()
                return (
                    ShadowCommandProposal(
                        ShadowCommandKind.ARM_DESTINATION,
                        "SOLAX",
                        PhysicalRole.HOLD,
                        command_id,
                    ),
                )
            if readiness is not None and readiness.deye_state is DeyeOperatingState.INTENTIONAL_OFF:
                return (ShadowCommandProposal(ShadowCommandKind.START_DEYE_REQUIRED, "DEYE", PhysicalRole.OFF),)
            return ()
        if desired_owner:
            return (ShadowCommandProposal(ShadowCommandKind.ARM_DESTINATION, "SOLAX", PhysicalRole.NATIVE),)
        return ()
    if phase is TransitionPhase.ISSUE_TARGET and desired_owner and command_id and target_w is not None:
        return (
            ShadowCommandProposal(
                ShadowCommandKind.APPLY_TARGET,
                desired_owner[0],
                desired_owner[1],
                command_id,
                target_w,
            ),
        )
    return ()


def _apply_clamp(progress: TransitionProgress, decision: SafetyDecision) -> TransitionProgress:
    if decision.action is not SafetyAction.CLAMP:
        return progress
    if decision.clamped_target_w is None or progress.effective_target_w is None:
        raise ValueError("CLAMP requires active bounded target")
    return replace(
        progress,
        effective_target_w=min(progress.effective_target_w, decision.clamped_target_w),
    )


def _readiness_valid_until(
    desired: PhysicalRoleVector,
    evidence: ReadinessEvidence,
    capabilities: CapabilitySnapshot,
    max_age_s: float,
) -> datetime:
    timestamps = [capabilities.observed_at]
    if desired.solax in _WRITER_ROLES:
        assert evidence.solax_source_timestamp is not None
        timestamps.append(evidence.solax_source_timestamp)
    if desired.deye in _WRITER_ROLES:
        assert evidence.deye_source_timestamp is not None
        timestamps.append(evidence.deye_source_timestamp)
    return min(timestamp + timedelta(seconds=max_age_s) for timestamp in timestamps)


def advance_transition(
    progress: TransitionProgress,
    *,
    physical: PhysicalStateSnapshot,
    command_evidence: BoundCommandEvidence | None,
    safety_decisions: tuple[SafetyDecision, ...],
    pcc_safety: SafetyDecision | None,
    cross_flow_safety: SafetyDecision | None,
    readiness_evidence: ReadinessEvidence | None,
    settling_evidence: NeutralSettlingEvidence | None,
    capabilities: CapabilitySnapshot,
    config: TransitionConfig,
    safety_config: SafetyConfig,
    now: datetime,
    execution_enabled: bool,
    proposal_command_id: str | None = None,
    writer_conflicts: tuple[str, ...] = (),
) -> TransitionAdvanceResult:
    """Advance only the phase owned by immutable transition progress."""
    plan = progress.plan
    current_phase = progress.current_phase
    phase_started_at = progress.phase_entered_at
    for value, name in ((phase_started_at, "phase_entered_at"), (now, "now")):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
    if now < phase_started_at:
        raise ValueError("now cannot precede phase_started_at")
    if not isinstance(execution_enabled, bool):
        raise ValueError("execution_enabled must be bool")
    expected_deadline = plan.deadline
    if current_phase is not TransitionPhase.COMPLETE:
        expected_deadline = min(
            plan.deadline,
            phase_started_at + timedelta(seconds=config.phase_timeouts_s[current_phase]),
        )
    if progress.phase_deadline != expected_deadline:
        raise ValueError("phase deadline does not match caller configuration")
    if now >= plan.deadline:
        return _failure(
            progress,
            action=SafetyAction.ROLLBACK,
            reason=FailureReason.COMMAND_EXPIRED,
        )
    if current_phase is not TransitionPhase.COMPLETE and now >= progress.phase_deadline:
        combined = combine_safety_decisions(
            tuple(decision for decision in (*safety_decisions, pcc_safety, cross_flow_safety) if decision is not None)
        )
        action = SafetyAction.SAFE_STOP if combined.action is SafetyAction.SAFE_STOP else SafetyAction.ROLLBACK
        return _failure(progress, action=action, reason=FailureReason.COMMAND_TIMEOUT)
    active_continuation_phases = {
        TransitionPhase.WAIT_DESTINATION_READY,
        TransitionPhase.ISSUE_TARGET,
        TransitionPhase.WAIT_TARGET_ACK,
        TransitionPhase.WAIT_PHYSICAL_RESPONSE,
        TransitionPhase.WAIT_STABLE,
        TransitionPhase.COMPLETE,
    }
    if not execution_enabled and current_phase in active_continuation_phases:
        return _failure(progress, action=SafetyAction.ROLLBACK, reason=FailureReason.DISABLED)
    ownership_producing = current_phase is TransitionPhase.ARM_DESTINATION and _owner(plan.desired_roles) is not None
    requires_current_pcc = bool(progress.writer_owners) or ownership_producing
    if requires_current_pcc and current_phase is not TransitionPhase.STOP_CURRENT_OWNER:
        pcc_reason: FailureReason | None = None
        if pcc_safety is None:
            pcc_reason = FailureReason.MISSING_TELEMETRY
        else:
            pcc_timestamp = physical.pcc.freshness_timestamp
            retained_pcc_expired = ownership_producing and (
                progress.pcc_valid_until is None or now >= progress.pcc_valid_until
            )
            if (
                pcc_timestamp is None
                or not 0 <= (now - pcc_timestamp).total_seconds() < safety_config.pcc_max_age_s
                or retained_pcc_expired
            ):
                pcc_reason = FailureReason.PCC_STALE
        if pcc_reason is not None:
            if progress.writer_owners:
                return _failure(progress, action=SafetyAction.ROLLBACK, reason=pcc_reason)
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(pcc_reason,),
            )
    transition_cross_flow = None if current_phase is TransitionPhase.WAIT_NEUTRAL else cross_flow_safety
    phase_safety = tuple(
        decision for decision in (*safety_decisions, pcc_safety, transition_cross_flow) if decision is not None
    )
    combined = combine_safety_decisions(phase_safety)
    if combined.action is SafetyAction.CLAMP:
        if (
            progress.expected_command is not None
            and combined.clamped_target_w is not None
            and progress.effective_target_w is not None
            and combined.clamped_target_w < progress.effective_target_w
        ):
            return _failure(
                progress,
                action=SafetyAction.ROLLBACK,
                reason=FailureReason.PCC_EXPORT_LIMIT,
            )
        progress = _apply_clamp(progress, combined)
    if combined.action in {SafetyAction.SAFE_STOP, SafetyAction.ROLLBACK}:
        reason = combined.reasons[0] if combined.reasons else FailureReason.RECONCILIATION_REQUIRED
        return _failure(progress, action=combined.action, reason=reason)
    if combined.action is SafetyAction.DEFER:
        if progress.writer_owners:
            reason = combined.reasons[0] if combined.reasons else FailureReason.RECONCILIATION_REQUIRED
            return _failure(progress, action=SafetyAction.ROLLBACK, reason=reason)
        return TransitionAdvanceResult(
            progress,
            current_phase,
            phase_started_at,
            ExecutorDecision.DEFERRED,
            SafetyAction.DEFER,
            False,
            False,
            reasons=combined.reasons,
        )
    conflicts = tuple(sorted(set((*physical.writer_conflicts, *writer_conflicts))))
    if conflicts and current_phase is not TransitionPhase.STOP_CURRENT_OWNER:
        if progress.writer_owners:
            return _failure(
                progress,
                action=safety_config.owned_writer_conflict_action,
                reason=FailureReason.WRITER_CONFLICT,
            )
        return TransitionAdvanceResult(
            progress,
            current_phase,
            phase_started_at,
            ExecutorDecision.DEFERRED,
            SafetyAction.DEFER,
            False,
            False,
            reasons=(FailureReason.WRITER_CONFLICT,),
        )
    if current_phase is TransitionPhase.COMPLETE:
        return TransitionAdvanceResult(
            progress,
            current_phase,
            phase_started_at,
            ExecutorDecision.ACCEPTED,
            SafetyAction.NONE,
            False,
            True,
            outcome_state=_desired_execution_state(plan.desired_roles),
        )
    if current_phase is TransitionPhase.FREEZE_INTENT:
        if plan.execution_epoch < 0 or plan.plan_id != plan.intent.plan_id or plan.intent_id != plan.intent.intent_id:
            return _failure(
                progress,
                action=SafetyAction.ROLLBACK,
                reason=FailureReason.INVALID_INTENT,
            )
        return _advanced(_next_progress(progress, now, config, _step_evidence(progress, now)))
    if current_phase is TransitionPhase.STOP_CURRENT_OWNER:
        proposals = _proposals(
            progress,
            current_phase,
            readiness_evidence,
            proposal_command_id,
            None,
        )
        if not proposals:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.READBACK_EVIDENCE_MISSING,),
            )
        expected = _expected(progress, proposals[0])
        next_progress = _next_progress(
            progress,
            now,
            config,
            _step_evidence(progress, now, proposal=expected),
            expected_command=expected,
        )
        return replace(_advanced(next_progress), proposals=proposals)
    if current_phase is TransitionPhase.WAIT_CURRENT_ACK:
        if not _command_matches(progress, command_evidence):
            reason = (
                FailureReason.READBACK_EVIDENCE_MISSING if command_evidence is None else FailureReason.READBACK_MISMATCH
            )
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        assert command_evidence is not None
        command = command_evidence.record
        if command.lifecycle_state not in _READBACK_STATES:
            reason = command.failure_reason or FailureReason.READBACK_EVIDENCE_MISSING
            if command.lifecycle_state in {CommandLifecycleState.FAILED, CommandLifecycleState.EXPIRED}:
                return _failure(progress, action=SafetyAction.ROLLBACK, reason=reason)
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        return _advanced(
            _next_progress(
                progress,
                now,
                config,
                _step_evidence(progress, now, command=command_evidence),
                expected_command=None,
            )
        )
    if current_phase is TransitionPhase.WAIT_NEUTRAL:
        if cross_flow_safety is None or settling_evidence is None:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.MISSING_TELEMETRY,),
            )
        samples = (physical.solax_battery_power, physical.deye_battery_power)
        values = tuple(sample.value for sample in samples)
        timestamps = tuple(sample.freshness_timestamp for sample in samples)
        evidence_shape_valid = (
            settling_evidence.threshold_w == safety_config.neutral_power_threshold_w
            and settling_evidence.required_dwell_s == safety_config.neutral_dwell_s
            and timestamps == (settling_evidence.solax_source_timestamp, settling_evidence.deye_source_timestamp)
            and settling_evidence.latest_sample_at <= now
            and all(
                0 <= (now - source_timestamp).total_seconds() <= safety_config.pcc_max_age_s
                for source_timestamp in (
                    settling_evidence.solax_source_timestamp,
                    settling_evidence.deye_source_timestamp,
                )
            )
        )
        if not evidence_shape_valid:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.STALE_TELEMETRY,),
            )
        sample_at = settling_evidence.latest_sample_at
        source_timestamps = (
            settling_evidence.solax_source_timestamp,
            settling_evidence.deye_source_timestamp,
        )
        retained_source_timestamps = (
            progress.neutral_last_solax_sample_at,
            progress.neutral_last_deye_sample_at,
        )
        if any(
            retained is not None and current < retained
            for current, retained in zip(source_timestamps, retained_source_timestamps, strict=True)
        ):
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.REJECTED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.OBSERVATION_OUT_OF_ORDER,),
            )
        observed_neutral = all(
            value is not None
            and not isinstance(value, bool)
            and isfinite(value)
            and abs(float(value)) <= safety_config.neutral_power_threshold_w
            for value in values
        )
        if settling_evidence.currently_neutral is not observed_neutral:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.REJECTED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.TELEMETRY_INCOHERENT,),
            )
        if not observed_neutral:
            reset_progress = replace(
                progress,
                neutral_since=None,
                neutral_last_sample_at=sample_at,
                neutral_last_solax_sample_at=settling_evidence.solax_source_timestamp,
                neutral_last_deye_sample_at=settling_evidence.deye_source_timestamp,
                settling_currently_neutral=False,
            )
            if cross_flow_safety.action in {
                SafetyAction.DEFER,
                SafetyAction.ROLLBACK,
                SafetyAction.SAFE_STOP,
            }:
                action = (
                    cross_flow_safety.action
                    if cross_flow_safety.action in {SafetyAction.ROLLBACK, SafetyAction.SAFE_STOP}
                    else SafetyAction.ROLLBACK
                )
                reason = cross_flow_safety.reasons[0] if cross_flow_safety.reasons else FailureReason.CROSS_BATTERY_FLOW
                return _failure(reset_progress, action=action, reason=reason)
            return TransitionAdvanceResult(
                reset_progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.CROSS_BATTERY_FLOW,),
            )
        neutral_since = (
            progress.neutral_since
            if progress.settling_currently_neutral and progress.neutral_since is not None
            else sample_at
        )
        settling_progress = replace(
            progress,
            neutral_since=neutral_since,
            neutral_last_sample_at=sample_at,
            neutral_last_solax_sample_at=settling_evidence.solax_source_timestamp,
            neutral_last_deye_sample_at=settling_evidence.deye_source_timestamp,
            settling_currently_neutral=True,
        )
        if cross_flow_safety.action is not SafetyAction.NONE:
            action = (
                cross_flow_safety.action
                if cross_flow_safety.action in {SafetyAction.ROLLBACK, SafetyAction.SAFE_STOP}
                else SafetyAction.ROLLBACK
            )
            reason = cross_flow_safety.reasons[0] if cross_flow_safety.reasons else FailureReason.CROSS_BATTERY_FLOW
            return _failure(settling_progress, action=action, reason=reason)
        if (sample_at - neutral_since).total_seconds() < safety_config.neutral_dwell_s:
            return TransitionAdvanceResult(
                settling_progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.CROSS_BATTERY_FLOW,),
            )
        next_progress = _next_progress(
            settling_progress,
            now,
            config,
            _step_evidence(
                settling_progress,
                now,
                neutral_since=neutral_since,
                settling_sample_at=sample_at,
                solax_settling_sample_at=settling_evidence.solax_source_timestamp,
                deye_settling_sample_at=settling_evidence.deye_source_timestamp,
            ),
            physical_roles=PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF),
            writer_owners=frozenset(),
        )
        return _advanced(next_progress)
    if current_phase is TransitionPhase.VERIFY_PCC:
        if pcc_safety is None:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.MISSING_TELEMETRY,),
            )
        timestamp = physical.pcc.freshness_timestamp
        if timestamp is None or not 0 <= (now - timestamp).total_seconds() <= safety_config.pcc_max_age_s:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.PCC_STALE,),
            )
        if pcc_safety.action not in {SafetyAction.NONE, SafetyAction.CLAMP}:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=pcc_safety.reasons,
            )
        pcc_valid_until = timestamp + timedelta(seconds=safety_config.pcc_max_age_s)
        if pcc_valid_until <= now:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.PCC_STALE,),
            )
        progress = _apply_clamp(progress, pcc_safety)
        next_progress = _next_progress(
            progress,
            now,
            config,
            _step_evidence(
                progress,
                now,
                pcc_action=pcc_safety.action,
                pcc_reasons=pcc_safety.reasons,
                pcc_source_timestamp=timestamp,
                effective_target_w=progress.effective_target_w,
            ),
            pcc_verified_at=now,
            pcc_valid_until=pcc_valid_until,
            pcc_action=pcc_safety.action,
            pcc_reasons=pcc_safety.reasons,
        )
        return replace(_advanced(next_progress), action=pcc_safety.action)
    if current_phase is TransitionPhase.ARM_DESTINATION:
        if not execution_enabled:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.DISABLED,),
            )
        reasons = _capability_reasons(plan.desired_roles, capabilities, physical.deye_state)
        if reasons:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.REJECTED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=reasons,
            )
        if readiness_evidence is None:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.MISSING_TELEMETRY,),
            )
        desired_owner = _owner(plan.desired_roles)
        if desired_owner and desired_owner[0] == "DEYE" and progress.expected_command is None:
            proposals = _proposals(progress, current_phase, readiness_evidence, proposal_command_id, None)
            if not proposals:
                return TransitionAdvanceResult(
                    progress,
                    current_phase,
                    phase_started_at,
                    ExecutorDecision.DEFERRED,
                    SafetyAction.DEFER,
                    False,
                    False,
                    reasons=(FailureReason.READBACK_EVIDENCE_MISSING,),
                )
            expected = _expected(progress, proposals[0])
            retained = replace(progress, expected_command=expected)
            return replace(_advanced(retained), proposals=proposals, advanced=False, completed=False)
        if desired_owner and desired_owner[0] == "DEYE":
            if not _command_matches(progress, command_evidence):
                reason = (
                    FailureReason.READBACK_EVIDENCE_MISSING
                    if command_evidence is None
                    else FailureReason.READBACK_MISMATCH
                )
                return TransitionAdvanceResult(
                    progress,
                    current_phase,
                    phase_started_at,
                    ExecutorDecision.DEFERRED,
                    SafetyAction.DEFER,
                    False,
                    False,
                    reasons=(reason,),
                )
            assert command_evidence is not None
            if command_evidence.record.lifecycle_state not in _READBACK_STATES:
                return TransitionAdvanceResult(
                    progress,
                    current_phase,
                    phase_started_at,
                    ExecutorDecision.DEFERRED,
                    SafetyAction.DEFER,
                    False,
                    False,
                    reasons=(FailureReason.READBACK_EVIDENCE_MISSING,),
                )
            proposals = _proposals(progress, current_phase, readiness_evidence, None, None)
        else:
            proposals = _proposals(progress, current_phase, readiness_evidence, None, None)
        roles = PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF)
        owners: frozenset[str] = frozenset()
        if desired_owner and desired_owner[0] == "DEYE":
            roles = PhysicalRoleVector(PhysicalRole.HOLD, PhysicalRole.OFF)
            owners = frozenset({"SOLAX", "DEYE"})
        elif desired_owner:
            owners = frozenset({"SOLAX"})
        next_progress = _next_progress(
            progress,
            now,
            config,
            _step_evidence(
                progress,
                now,
                proposal=progress.expected_command,
                command=command_evidence if progress.expected_command is not None else None,
            ),
            physical_roles=roles,
            writer_owners=owners,
            expected_command=None,
        )
        return replace(_advanced(next_progress), proposals=proposals)
    if current_phase is TransitionPhase.WAIT_DESTINATION_READY:
        if readiness_evidence is None:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.MISSING_TELEMETRY,),
            )
        readiness = evaluate_readiness(
            plan.desired_roles,
            capabilities=capabilities,
            evidence=readiness_evidence,
            max_age_s=safety_config.readiness_max_age_s,
            now=now,
        )
        if readiness.action is not SafetyAction.NONE:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=readiness.reasons,
            )
        readiness_valid_until = _readiness_valid_until(
            plan.desired_roles,
            readiness_evidence,
            capabilities,
            safety_config.readiness_max_age_s,
        )
        if readiness_valid_until <= now:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.STALE_TELEMETRY,),
            )
        next_progress = _next_progress(
            progress,
            now,
            config,
            _step_evidence(progress, now, readiness=readiness_evidence),
            readiness_verified_at=now,
            readiness_valid_until=readiness_valid_until,
        )
        return _advanced(next_progress)
    if current_phase is TransitionPhase.ISSUE_TARGET:
        target = progress.effective_target_w
        if target is None and _owner(plan.desired_roles) is not None:
            return _failure(
                progress,
                action=SafetyAction.ROLLBACK,
                reason=FailureReason.INVALID_INTENT,
            )
        if progress.pcc_valid_until is None or now >= progress.pcc_valid_until:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.PCC_STALE,),
            )
        if progress.readiness_valid_until is None or now >= progress.readiness_valid_until:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.STALE_TELEMETRY,),
            )
        proposals = _proposals(
            progress,
            current_phase,
            readiness_evidence,
            proposal_command_id,
            target,
        )
        if not proposals:
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(FailureReason.READBACK_EVIDENCE_MISSING,),
            )
        expected = _expected(progress, proposals[0])
        next_progress = _next_progress(
            progress,
            now,
            config,
            _step_evidence(progress, now, proposal=expected),
            expected_command=expected,
        )
        return replace(_advanced(next_progress), proposals=proposals)
    if current_phase is TransitionPhase.WAIT_TARGET_ACK:
        if not _command_matches(progress, command_evidence):
            reason = (
                FailureReason.READBACK_EVIDENCE_MISSING if command_evidence is None else FailureReason.READBACK_MISMATCH
            )
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        assert command_evidence is not None
        command = command_evidence.record
        if command.lifecycle_state not in _READBACK_STATES:
            reason = command.failure_reason if command.failure_reason else FailureReason.READBACK_EVIDENCE_MISSING
            if command.lifecycle_state in {CommandLifecycleState.FAILED, CommandLifecycleState.EXPIRED}:
                return _failure(progress, action=SafetyAction.ROLLBACK, reason=reason)
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        return _advanced(
            _next_progress(
                progress,
                now,
                config,
                _step_evidence(progress, now, command=command_evidence),
            )
        )
    if current_phase is TransitionPhase.WAIT_PHYSICAL_RESPONSE:
        if not _command_matches(progress, command_evidence):
            reason = (
                FailureReason.PHYSICAL_RESPONSE_TIMEOUT if command_evidence is None else FailureReason.READBACK_MISMATCH
            )
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        assert command_evidence is not None
        command = command_evidence.record
        if command.lifecycle_state not in _PHYSICAL_STATES:
            reason = command.failure_reason if command.failure_reason else FailureReason.PHYSICAL_RESPONSE_TIMEOUT
            if command.lifecycle_state in {CommandLifecycleState.FAILED, CommandLifecycleState.EXPIRED}:
                return _failure(progress, action=SafetyAction.ROLLBACK, reason=reason)
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        return _advanced(
            _next_progress(
                progress,
                now,
                config,
                _step_evidence(progress, now, command=command_evidence),
            )
        )
    if current_phase is TransitionPhase.WAIT_STABLE:
        if not _command_matches(progress, command_evidence):
            reason = (
                FailureReason.STABILITY_NOT_CONFIRMED if command_evidence is None else FailureReason.READBACK_MISMATCH
            )
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        assert command_evidence is not None
        command = command_evidence.record
        if command.lifecycle_state is not CommandLifecycleState.STABLE:
            reason = command.failure_reason if command.failure_reason else FailureReason.STABILITY_NOT_CONFIRMED
            if command.lifecycle_state in {CommandLifecycleState.FAILED, CommandLifecycleState.EXPIRED}:
                return _failure(progress, action=SafetyAction.ROLLBACK, reason=reason)
            return TransitionAdvanceResult(
                progress,
                current_phase,
                phase_started_at,
                ExecutorDecision.DEFERRED,
                SafetyAction.DEFER,
                False,
                False,
                reasons=(reason,),
            )
        next_progress = _next_progress(
            progress,
            now,
            config,
            _step_evidence(progress, now, command=command_evidence),
            physical_roles=plan.desired_roles,
            writer_owners=_writer_owners(plan.desired_roles),
        )
        return _advanced(next_progress)
    raise AssertionError(f"unhandled transition phase: {current_phase}")
