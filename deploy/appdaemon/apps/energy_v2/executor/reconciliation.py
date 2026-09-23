"""Pure restart and reconnect reconciliation for the shadow executor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..deye_state import DeyeOperatingState
from .enums import ExecutionState, FailureReason, PhysicalRole, SafetyAction
from .models import CapabilitySnapshot, CommandRecord, PhysicalRoleVector, PlannerIntent
from .safety import (
    PhysicalStateSnapshot,
    SafetyConfig,
    SafetyDecision,
    combine_safety_decisions,
    evaluate_telemetry_coherence,
    evaluate_writer_conflicts,
)

_OUTCOMES = {
    ExecutionState.NORMAL,
    ExecutionState.PRECHECK,
    ExecutionState.ROLLBACK,
    ExecutionState.SAFE_STOP,
}


@dataclass(frozen=True)
class PersistedExecutorMetadata:
    execution_epoch: int
    active_plan_id: str | None = None
    active_intent_id: str | None = None
    active_command_id: str | None = None
    commands: tuple[CommandRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.execution_epoch < 0:
            raise ValueError("persisted execution_epoch must be nonnegative")
        commands = tuple(self.commands)
        if not all(isinstance(command, CommandRecord) for command in commands):
            raise ValueError("commands must contain CommandRecord values")
        object.__setattr__(self, "commands", commands)


@dataclass(frozen=True)
class ReconciliationResult:
    outcome_state: ExecutionState
    rollback_required: bool
    invalidated_command_ids: tuple[str, ...]
    reasons: tuple[FailureReason, ...] = ()
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome_state not in _OUTCOMES:
            raise ValueError("reconciliation outcome must be non-active")
        if not isinstance(self.rollback_required, bool):
            raise ValueError("rollback_required must be bool")
        reasons = tuple(self.reasons)
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("reasons must contain FailureReason values")
        invalidated = tuple(sorted(set(self.invalidated_command_ids)))
        if not all(isinstance(item, str) and item for item in invalidated):
            raise ValueError("invalidated command IDs must be nonempty strings")
        object.__setattr__(self, "invalidated_command_ids", invalidated)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "details", tuple(sorted(set(self.details))))


def _result(
    state: ExecutionState,
    *,
    invalidated: tuple[str, ...],
    reasons: tuple[FailureReason, ...] = (),
    details: tuple[str, ...] = (),
) -> ReconciliationResult:
    return ReconciliationResult(
        state,
        state is ExecutionState.ROLLBACK,
        invalidated,
        tuple(dict.fromkeys(reasons)),
        details,
    )


def _fresh_timestamp(value: datetime | None, *, now: datetime, max_age_s: float) -> bool:
    return value is not None and 0 <= (now - value).total_seconds() <= max_age_s


def evaluate_reconciliation(
    *,
    current_execution_epoch: int,
    physical: PhysicalStateSnapshot,
    capabilities: CapabilitySnapshot,
    persisted: PersistedExecutorMetadata | None,
    safety_decisions: tuple[SafetyDecision, ...],
    config: SafetyConfig,
    now: datetime,
    execution_enabled: bool,
    fresh_intent: PlannerIntent | None,
) -> ReconciliationResult:
    """Classify restart state without ever replaying an old command."""
    if current_execution_epoch < 0:
        raise ValueError("current_execution_epoch must be nonnegative")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(execution_enabled, bool):
        raise ValueError("execution_enabled must be bool")
    if not isinstance(capabilities, CapabilitySnapshot) or not isinstance(config, SafetyConfig):
        raise ValueError("capabilities and config must use executor models")
    invalidated = tuple(
        sorted(
            command.command_id
            for command in (persisted.commands if persisted else ())
            if command.execution_epoch != current_execution_epoch
        )
    )
    telemetry = evaluate_telemetry_coherence(
        {
            "deye_battery_power": physical.deye_battery_power,
            "pcc": physical.pcc,
            "solax_battery_power": physical.solax_battery_power,
        },
        state=ExecutionState.RECONCILING,
        max_age_s=config.pcc_max_age_s,
        now=now,
    )
    conflicts = evaluate_writer_conflicts(
        physical.writer_conflicts,
        state=ExecutionState.RECONCILING,
        config=config,
    )
    combined = combine_safety_decisions((*safety_decisions, telemetry, conflicts))
    unsafe_reasons: list[FailureReason] = []
    if combined.action in {SafetyAction.DEFER, SafetyAction.SAFE_STOP}:
        unsafe_reasons.extend(combined.reasons)
    if physical.writer_conflicts:
        unsafe_reasons.append(FailureReason.WRITER_CONFLICT)
    if not physical.solax_available or physical.solax_fault:
        unsafe_reasons.append(FailureReason.INVERTER_UNAVAILABLE)
    if physical.deye_state in {DeyeOperatingState.UNEXPECTED_FAULT, DeyeOperatingState.UNAVAILABLE}:
        unsafe_reasons.append(FailureReason.INVERTER_UNAVAILABLE)
    if physical.reconnect_detected and not physical.continuity_certain:
        unsafe_reasons.append(FailureReason.RESTART_RECONCILIATION)
    if not _fresh_timestamp(
        physical.solax_control_source_timestamp,
        now=now,
        max_age_s=config.control_state_max_age_s,
    ) or not _fresh_timestamp(
        physical.deye_control_source_timestamp,
        now=now,
        max_age_s=config.control_state_max_age_s,
    ):
        unsafe_reasons.append(FailureReason.STALE_TELEMETRY)
    if combined.action in {SafetyAction.DEFER, SafetyAction.SAFE_STOP} or unsafe_reasons:
        return _result(
            ExecutionState.SAFE_STOP,
            invalidated=invalidated,
            reasons=tuple(dict.fromkeys(unsafe_reasons or (FailureReason.RECONCILIATION_FAILED,))),
            details=combined.details,
        )
    if combined.action is SafetyAction.ROLLBACK:
        if physical.rollback_safe:
            return _result(
                ExecutionState.ROLLBACK,
                invalidated=invalidated,
                reasons=combined.reasons or (FailureReason.RECONCILIATION_REQUIRED,),
                details=combined.details,
            )
        return _result(
            ExecutionState.SAFE_STOP,
            invalidated=invalidated,
            reasons=combined.reasons + (FailureReason.RECONCILIATION_FAILED,),
            details=combined.details,
        )

    active_solax = physical.solax_remote_control_active
    active_deye = physical.ownership is not None and physical.ownership.deye in {
        PhysicalRole.CHARGE,
        PhysicalRole.DISCHARGE,
        PhysicalRole.HOLD,
    }
    if active_solax or active_deye:
        attributable = bool(
            persisted
            and persisted.active_command_id
            and any(
                command.command_id == persisted.active_command_id and command.execution_epoch != current_execution_epoch
                for command in persisted.commands
            )
        )
        if attributable and physical.rollback_safe:
            return _result(
                ExecutionState.ROLLBACK,
                invalidated=invalidated,
                reasons=(FailureReason.RESTART_RECONCILIATION,),
                details=("old active control requires rollback",),
            )
        return _result(
            ExecutionState.SAFE_STOP,
            invalidated=invalidated,
            reasons=(FailureReason.RECONCILIATION_FAILED,),
            details=("active control ownership is unattributable or rollback is unsafe",),
        )

    safe_roles = physical.ownership == PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF)
    safe_native = (
        physical.solax_native_mode_confirmed
        and not physical.solax_remote_control_active
        and physical.deye_non_owning_confirmed
        and safe_roles
    )
    battery_values = (
        physical.solax_battery_power.value,
        physical.deye_battery_power.value,
    )
    neutral = all(
        value is not None and not isinstance(value, bool) and abs(float(value)) <= config.neutral_power_threshold_w
        for value in battery_values
    )
    safe_native = safe_native and neutral
    if not safe_native:
        return _result(
            ExecutionState.SAFE_STOP,
            invalidated=invalidated,
            reasons=(FailureReason.RECONCILIATION_FAILED,),
            details=("safe native ownership cannot be proven",),
        )
    if execution_enabled and fresh_intent is not None and now < fresh_intent.deadline:
        return _result(ExecutionState.PRECHECK, invalidated=invalidated)
    # With no fresh intent there is nothing to precheck; remain in verified native NORMAL.
    return _result(ExecutionState.NORMAL, invalidated=invalidated)
