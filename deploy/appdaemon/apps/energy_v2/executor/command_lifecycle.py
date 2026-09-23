"""Idempotent command lifecycle and observation checks."""

from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite

from .enums import CommandLifecycleState, FailureReason, PhysicalRole, PlannerIntentType
from .models import CommandObservation, CommandRecord, PhysicalResponseAcceptance, StabilityEvidence

_NEXT = {
    CommandLifecycleState.INTENT_CREATED: frozenset({CommandLifecycleState.COMMAND_ISSUED}),
    CommandLifecycleState.COMMAND_ISSUED: frozenset(),
    CommandLifecycleState.READBACK_CONFIRMED: frozenset(),
    CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED: frozenset(),
    CommandLifecycleState.STABLE: frozenset(),
    CommandLifecycleState.FAILED: frozenset(),
    CommandLifecycleState.EXPIRED: frozenset(),
}


@dataclass(frozen=True)
class LifecycleResult:
    record: CommandRecord
    reasons: tuple[FailureReason, ...] = ()


def validate_command_id(command_id: str, issued_command_ids: frozenset[str]) -> tuple[FailureReason, ...]:
    if not command_id or command_id in issued_command_ids:
        return (FailureReason.DUPLICATE_COMMAND_ID,)
    return ()


_TERMINAL = {
    CommandLifecycleState.STABLE,
    CommandLifecycleState.FAILED,
    CommandLifecycleState.EXPIRED,
}

_INTENT_ROLES = {
    PlannerIntentType.CHARGE_SOLAX: PhysicalRole.CHARGE,
    PlannerIntentType.DISCHARGE_SOLAX: PhysicalRole.DISCHARGE,
    PlannerIntentType.CHARGE_DEYE: PhysicalRole.CHARGE,
    PlannerIntentType.DISCHARGE_DEYE: PhysicalRole.DISCHARGE,
}


def advance_command_lifecycle(
    record: CommandRecord,
    proposed: CommandLifecycleState,
    now: datetime,
    current_execution_epoch: int,
) -> LifecycleResult:
    if record.lifecycle_state in _TERMINAL:
        return LifecycleResult(record)
    if record.execution_epoch != current_execution_epoch:
        return LifecycleResult(record, (FailureReason.OLD_EXECUTION_EPOCH,))
    if now.tzinfo is None or now.utcoffset() is None:
        return LifecycleResult(record, (FailureReason.MISSING_TELEMETRY,))
    if now < record.created_at:
        return LifecycleResult(record, (FailureReason.OBSERVATION_OUT_OF_ORDER,))
    if now >= record.expires_at:
        return LifecycleResult(
            replace(
                record,
                lifecycle_state=CommandLifecycleState.EXPIRED,
                failure_reason=FailureReason.COMMAND_EXPIRED,
                expired_at=now,
            ),
            (FailureReason.COMMAND_EXPIRED,),
        )
    if proposed not in _NEXT[record.lifecycle_state]:
        return LifecycleResult(record, (FailureReason.TRANSITION_FORBIDDEN,))
    return LifecycleResult(replace(record, lifecycle_state=proposed, issued_at=now))


def _failed(
    record: CommandRecord,
    reason: FailureReason,
    observation: CommandObservation,
    *,
    retain_evidence: bool = True,
) -> LifecycleResult:
    changes: dict[str, object] = {
        "lifecycle_state": CommandLifecycleState.FAILED,
        "failure_reason": reason,
    }
    if retain_evidence:
        changes.update(
            observed_readback=observation.readback_values,
            observed_response_w=observation.physical_response_w,
            last_observation_at=observation.observed_at,
        )
    failed = replace(record, **changes)
    return LifecycleResult(failed, (reason,))


def _validate_observation(
    record: CommandRecord,
    observation: CommandObservation,
    current_execution_epoch: int,
) -> LifecycleResult | None:
    if record.lifecycle_state in _TERMINAL:
        return LifecycleResult(record)
    if observation.command_id != record.command_id:
        return LifecycleResult(record, (FailureReason.COMMAND_ID_MISMATCH,))
    if (
        record.execution_epoch != current_execution_epoch
        or observation.execution_epoch != current_execution_epoch
        or observation.execution_epoch != record.execution_epoch
    ):
        return LifecycleResult(record, (FailureReason.OLD_EXECUTION_EPOCH,))
    earliest = record.issued_at or record.created_at
    if observation.observed_at < earliest or (
        record.last_observation_at is not None and observation.observed_at < record.last_observation_at
    ):
        return _failed(record, FailureReason.OBSERVATION_OUT_OF_ORDER, observation, retain_evidence=False)
    if observation.observed_at >= record.expires_at:
        return LifecycleResult(
            replace(
                record,
                lifecycle_state=CommandLifecycleState.EXPIRED,
                observed_readback=observation.readback_values,
                observed_response_w=observation.physical_response_w,
                last_observation_at=observation.observed_at,
                failure_reason=FailureReason.COMMAND_EXPIRED,
                expired_at=observation.observed_at,
            ),
            (FailureReason.COMMAND_EXPIRED,),
        )
    if not observation.telemetry_fresh:
        return _failed(record, FailureReason.STALE_TELEMETRY, observation)
    if not observation.telemetry_coherent:
        return _failed(record, FailureReason.TELEMETRY_INCOHERENT, observation)
    if not observation.device_available or observation.fault:
        return _failed(record, FailureReason.INVERTER_UNAVAILABLE, observation)
    return None


def confirm_readback(
    record: CommandRecord,
    observation: CommandObservation,
    current_execution_epoch: int,
) -> LifecycleResult:
    invalid = _validate_observation(record, observation, current_execution_epoch)
    if invalid is not None:
        return invalid
    if record.lifecycle_state is not CommandLifecycleState.COMMAND_ISSUED:
        return LifecycleResult(record, (FailureReason.TRANSITION_FORBIDDEN,))
    if (
        not record.expected_readback
        or not observation.readback_values
        or any(key not in observation.readback_values for key in record.expected_readback)
    ):
        return _failed(record, FailureReason.READBACK_EVIDENCE_MISSING, observation)
    if any(observation.readback_values[key] != value for key, value in record.expected_readback.items()):
        return _failed(record, FailureReason.READBACK_MISMATCH, observation)
    confirmed = replace(
        record,
        lifecycle_state=CommandLifecycleState.READBACK_CONFIRMED,
        observed_readback=observation.readback_values,
        last_observation_at=observation.observed_at,
        readback_confirmed_at=observation.observed_at,
        failure_reason=None,
    )
    return LifecycleResult(confirmed)


def confirm_physical_response(
    record: CommandRecord,
    observation: CommandObservation,
    current_execution_epoch: int,
    acceptance: PhysicalResponseAcceptance,
) -> LifecycleResult:
    invalid = _validate_observation(record, observation, current_execution_epoch)
    if invalid is not None:
        return invalid
    if record.lifecycle_state is not CommandLifecycleState.READBACK_CONFIRMED:
        return LifecycleResult(record, (FailureReason.TRANSITION_FORBIDDEN,))
    response = observation.physical_response_w
    target = record.clamped_target_w if record.clamped_target_w is not None else record.requested_target_w
    magnitude = abs(response) if response is not None and isfinite(response) else None
    zero_forbidden = target is not None and target > 0 and not acceptance.allow_zero
    if (
        response is None
        or magnitude is None
        or _INTENT_ROLES.get(record.intent_type) is not acceptance.expected_role
        or observation.physical_response_role is not acceptance.expected_role
        or (zero_forbidden and magnitude == 0)
        or not acceptance.minimum_magnitude_w <= magnitude <= acceptance.maximum_magnitude_w
    ):
        return _failed(record, FailureReason.PHYSICAL_RESPONSE_MISMATCH, observation)
    confirmed = replace(
        record,
        lifecycle_state=CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED,
        observed_readback=observation.readback_values,
        observed_response_w=response,
        observed_response_role=observation.physical_response_role,
        last_observation_at=observation.observed_at,
        physical_response_confirmed_at=observation.observed_at,
        failure_reason=None,
    )
    return LifecycleResult(confirmed)


def confirm_stability(
    record: CommandRecord,
    evidence: StabilityEvidence,
    current_execution_epoch: int,
) -> LifecycleResult:
    if record.lifecycle_state in _TERMINAL:
        return LifecycleResult(record)
    if evidence.command_id != record.command_id:
        return LifecycleResult(record, (FailureReason.COMMAND_ID_MISMATCH,))
    if (
        record.execution_epoch != current_execution_epoch
        or evidence.execution_epoch != current_execution_epoch
        or evidence.execution_epoch != record.execution_epoch
    ):
        return LifecycleResult(record, (FailureReason.OLD_EXECUTION_EPOCH,))
    if record.lifecycle_state is not CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED:
        return LifecycleResult(record, (FailureReason.TRANSITION_FORBIDDEN,))
    if evidence.observed_at < (record.last_observation_at or record.created_at):
        return LifecycleResult(record, (FailureReason.OBSERVATION_OUT_OF_ORDER,))
    if evidence.observed_at >= record.expires_at:
        return LifecycleResult(
            replace(
                record,
                lifecycle_state=CommandLifecycleState.EXPIRED,
                last_observation_at=evidence.observed_at,
                failure_reason=FailureReason.COMMAND_EXPIRED,
                expired_at=evidence.observed_at,
            ),
            (FailureReason.COMMAND_EXPIRED,),
        )
    if not evidence.stable:
        return LifecycleResult(record, (FailureReason.STABILITY_NOT_CONFIRMED,))
    return LifecycleResult(
        replace(
            record,
            lifecycle_state=CommandLifecycleState.STABLE,
            last_observation_at=evidence.observed_at,
            stable_confirmed_at=evidence.observed_at,
            stability_sample_count=evidence.sample_count,
            stability_duration_s=evidence.stable_duration_s,
            failure_reason=None,
        )
    )


def evaluate_command_observation(
    record: CommandRecord,
    observation: CommandObservation,
    current_execution_epoch: int,
    acceptance: PhysicalResponseAcceptance | None = None,
) -> LifecycleResult:
    if record.lifecycle_state in _TERMINAL:
        return LifecycleResult(record)
    if record.lifecycle_state is CommandLifecycleState.COMMAND_ISSUED:
        return confirm_readback(record, observation, current_execution_epoch)
    if record.lifecycle_state is CommandLifecycleState.READBACK_CONFIRMED and acceptance is not None:
        return confirm_physical_response(record, observation, current_execution_epoch, acceptance)
    return LifecycleResult(record, (FailureReason.TRANSITION_FORBIDDEN,))
