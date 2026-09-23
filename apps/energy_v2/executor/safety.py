"""Pure, structured safety evaluation for the shadow executor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from ..deye_state import DeyeOperatingState
from .enums import CommandLifecycleState, ExecutionState, FailureReason, PhysicalRole, SafetyAction
from .models import CapabilitySnapshot, CommandRecord, PhysicalRoleVector
from .state_machine import _WRITER_OWNING_STATES

_ACTION_STRENGTH = {
    SafetyAction.NONE: 0,
    SafetyAction.CLAMP: 1,
    SafetyAction.DEFER: 2,
    SafetyAction.ROLLBACK: 3,
    SafetyAction.SAFE_STOP: 4,
}
_REASON_ORDER = {reason: index for index, reason in enumerate(FailureReason)}
_SOLAX_STATES = {ExecutionState.SOLAX_HOLD, ExecutionState.SOLAX_DISPATCH}
_DEYE_STATES = {ExecutionState.DEYE_STARTING, ExecutionState.DEYE_HOLD, ExecutionState.DEYE_DISPATCH}


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _strict_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")


def _finite_nonnegative(value: float, name: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be {qualifier}")


@dataclass(frozen=True)
class SafetyConfig:
    """Caller-owned policy; only the approved export limits have defaults."""

    pcc_max_age_s: float
    pcc_plausible_abs_w: float
    neutral_power_threshold_w: float
    neutral_dwell_s: float
    cross_flow_threshold_w: float
    cross_flow_dwell_s: float
    non_owner_threshold_w: float
    non_owner_dwell_s: float
    implausible_pcc_action: SafetyAction
    persistent_cross_flow_action: SafetyAction
    owned_writer_conflict_action: SafetyAction
    readiness_max_age_s: float
    control_state_max_age_s: float
    operational_export_target_w: float = 9800.0
    contractual_export_limit_w: float = 10000.0
    rolling_window_s: float = 900.0

    def __post_init__(self) -> None:
        positive = (
            "pcc_max_age_s",
            "pcc_plausible_abs_w",
            "neutral_power_threshold_w",
            "neutral_dwell_s",
            "cross_flow_threshold_w",
            "cross_flow_dwell_s",
            "non_owner_threshold_w",
            "non_owner_dwell_s",
            "readiness_max_age_s",
            "control_state_max_age_s",
            "operational_export_target_w",
            "contractual_export_limit_w",
            "rolling_window_s",
        )
        for name in positive:
            _finite_nonnegative(getattr(self, name), name, positive=True)
        if self.operational_export_target_w > self.contractual_export_limit_w:
            raise ValueError("operational export target must not exceed contractual limit")
        if self.implausible_pcc_action not in {
            SafetyAction.DEFER,
            SafetyAction.ROLLBACK,
            SafetyAction.SAFE_STOP,
        }:
            raise ValueError("implausible_pcc_action must fail closed")
        for name in ("persistent_cross_flow_action", "owned_writer_conflict_action"):
            if getattr(self, name) not in {SafetyAction.ROLLBACK, SafetyAction.SAFE_STOP}:
                raise ValueError(f"{name} must be ROLLBACK or SAFE_STOP")


@dataclass(frozen=True)
class NumericTelemetry:
    """A value plus observation evidence; receipt time is never freshness proof."""

    value: float | None
    source_timestamp: datetime | None = None
    report_timestamp: datetime | None = None
    receipt_timestamp: datetime | None = None
    coherent: bool = True

    def __post_init__(self) -> None:
        for name in ("source_timestamp", "report_timestamp", "receipt_timestamp"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        _strict_bool(self.coherent, "coherent")

    @property
    def freshness_timestamp(self) -> datetime | None:
        return self.source_timestamp if self.source_timestamp is not None else self.report_timestamp


@dataclass(frozen=True)
class RollingExportEvidence:
    average_w: float | None
    covered_duration_s: float
    window_complete: bool
    latest_sample_at: datetime | None

    def __post_init__(self) -> None:
        _finite_nonnegative(self.covered_duration_s, "covered_duration_s")
        _strict_bool(self.window_complete, "window_complete")
        if self.latest_sample_at is not None:
            _aware(self.latest_sample_at, "latest_sample_at")


@dataclass(frozen=True)
class PhysicalStateSnapshot:
    observed_at: datetime
    pcc: NumericTelemetry
    solax_battery_power: NumericTelemetry
    deye_battery_power: NumericTelemetry
    solax_available: bool
    solax_fault: bool
    solax_native_mode_confirmed: bool
    solax_remote_control_active: bool
    deye_state: DeyeOperatingState
    deye_non_owning_confirmed: bool
    ownership: PhysicalRoleVector | None
    solax_control_source_timestamp: datetime | None = None
    deye_control_source_timestamp: datetime | None = None
    writer_conflicts: tuple[str, ...] = ()
    continuity_certain: bool = True
    rollback_safe: bool = True
    reconnect_detected: bool = False

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if not all(
            isinstance(sample, NumericTelemetry)
            for sample in (self.pcc, self.solax_battery_power, self.deye_battery_power)
        ):
            raise ValueError("physical telemetry must use NumericTelemetry")
        flags = (
            self.solax_available,
            self.solax_fault,
            self.solax_native_mode_confirmed,
            self.solax_remote_control_active,
            self.deye_non_owning_confirmed,
            self.continuity_certain,
            self.rollback_safe,
            self.reconnect_detected,
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("physical-state flags must be bool")
        if not isinstance(self.deye_state, DeyeOperatingState):
            raise ValueError("deye_state must be DeyeOperatingState")
        if self.ownership is not None and not isinstance(self.ownership, PhysicalRoleVector):
            raise ValueError("ownership must be PhysicalRoleVector")
        for name in ("solax_control_source_timestamp", "deye_control_source_timestamp"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        raw_conflicts = tuple(self.writer_conflicts)
        if not all(isinstance(item, str) and item for item in raw_conflicts):
            raise ValueError("writer_conflicts must contain nonempty strings")
        conflicts = tuple(sorted(set(raw_conflicts)))
        object.__setattr__(self, "writer_conflicts", conflicts)


@dataclass(frozen=True)
class ReadinessEvidence:
    deye_state: DeyeOperatingState
    deye_source_timestamp: datetime | None
    solax_available: bool
    solax_fault: bool
    solax_source_timestamp: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.deye_state, DeyeOperatingState):
            raise ValueError("deye_state must be DeyeOperatingState")
        _strict_bool(self.solax_available, "solax_available")
        _strict_bool(self.solax_fault, "solax_fault")
        for name in ("deye_source_timestamp", "solax_source_timestamp"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)


@dataclass(frozen=True)
class SafetyDecision:
    action: SafetyAction
    reasons: tuple[FailureReason, ...] = ()
    clamped_target_w: float | None = None
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, SafetyAction):
            raise ValueError("action must be SafetyAction")
        reasons = tuple(self.reasons)
        details = tuple(self.details)
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("reasons must contain FailureReason values")
        if self.clamped_target_w is not None:
            _finite_nonnegative(self.clamped_target_w, "clamped_target_w")
        if not all(isinstance(detail, str) for detail in details):
            raise ValueError("details must contain strings")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "details", details)


def _decision(
    action: SafetyAction,
    *reasons: FailureReason,
    clamped_target_w: float | None = None,
    details: tuple[str, ...] = (),
) -> SafetyDecision:
    ordered_reasons = tuple(sorted(set(reasons), key=_REASON_ORDER.__getitem__))
    return SafetyDecision(action, ordered_reasons, clamped_target_w, tuple(sorted(set(details))))


def combine_safety_decisions(decisions: tuple[SafetyDecision, ...] | list[SafetyDecision]) -> SafetyDecision:
    """Combine without depending on caller iteration order."""
    if not decisions:
        return SafetyDecision(SafetyAction.NONE)
    strongest = max((decision.action for decision in decisions), key=_ACTION_STRENGTH.__getitem__)
    reasons = tuple(
        sorted(
            {reason for decision in decisions for reason in decision.reasons},
            key=_REASON_ORDER.__getitem__,
        )
    )
    details = tuple(sorted({detail for decision in decisions for detail in decision.details}))
    clamp_values = [
        decision.clamped_target_w
        for decision in decisions
        if decision.action is SafetyAction.CLAMP and decision.clamped_target_w is not None
    ]
    clamped = min(clamp_values) if strongest is SafetyAction.CLAMP and clamp_values else None
    return SafetyDecision(strongest, reasons, clamped, details)


def _has_writer_ownership(ownership: PhysicalRoleVector | None) -> bool:
    if ownership is None:
        return False
    writer_roles = {PhysicalRole.HOLD, PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
    return ownership.solax in writer_roles or ownership.deye in writer_roles


def _ownership_failure_action(
    state: ExecutionState,
    ownership: PhysicalRoleVector | None = None,
) -> SafetyAction:
    if state in _WRITER_OWNING_STATES or _has_writer_ownership(ownership):
        return SafetyAction.ROLLBACK
    return SafetyAction.DEFER


def _telemetry_failure(
    sample: NumericTelemetry | None,
    *,
    now: datetime,
    max_age_s: float,
    state: ExecutionState,
    ownership: PhysicalRoleVector | None = None,
    stale_reason: FailureReason = FailureReason.STALE_TELEMETRY,
) -> SafetyDecision:
    action = _ownership_failure_action(state, ownership)
    if sample is None or sample.value is None or sample.freshness_timestamp is None:
        return _decision(action, FailureReason.MISSING_TELEMETRY)
    try:
        finite = not isinstance(sample.value, bool) and isfinite(sample.value)
    except TypeError:
        finite = False
    if not finite:
        return _decision(action, FailureReason.TELEMETRY_INCOHERENT, details=("nonfinite telemetry",))
    if not sample.coherent:
        return _decision(action, FailureReason.TELEMETRY_INCOHERENT)
    age_s = (now - sample.freshness_timestamp).total_seconds()
    if age_s < 0 or age_s > max_age_s:
        return _decision(action, stale_reason, details=(f"age_s={age_s:.3f}",))
    return SafetyDecision(SafetyAction.NONE)


def evaluate_pcc_safety(
    pcc: NumericTelemetry | None,
    *,
    rolling: RollingExportEvidence | None,
    requested_target_w: float | None,
    state: ExecutionState,
    physical_ownership: PhysicalRoleVector | None = None,
    config: SafetyConfig,
    now: datetime,
) -> SafetyDecision:
    """Evaluate positive-export PCC feedback and the rolling contractual limit."""
    _aware(now, "now")
    base = _telemetry_failure(
        pcc,
        now=now,
        max_age_s=config.pcc_max_age_s,
        state=state,
        ownership=physical_ownership,
        stale_reason=FailureReason.PCC_STALE,
    )
    if base.action is not SafetyAction.NONE:
        return base
    assert pcc is not None and pcc.value is not None
    pcc_w = float(pcc.value)
    if abs(pcc_w) > config.pcc_plausible_abs_w:
        configured = _decision(config.implausible_pcc_action, FailureReason.PCC_IMPLAUSIBLE)
        if _ownership_failure_action(state, physical_ownership) is SafetyAction.ROLLBACK:
            return combine_safety_decisions(
                (configured, _decision(SafetyAction.ROLLBACK, FailureReason.PCC_IMPLAUSIBLE))
            )
        return configured
    rolling_action = _ownership_failure_action(state, physical_ownership)
    if rolling is None or rolling.average_w is None or rolling.latest_sample_at is None:
        return _decision(rolling_action, FailureReason.MISSING_TELEMETRY, details=("rolling export unavailable",))
    try:
        rolling_finite = not isinstance(rolling.average_w, bool) and isfinite(rolling.average_w)
    except TypeError:
        rolling_finite = False
    rolling_age_s = (now - rolling.latest_sample_at).total_seconds()
    if not rolling_finite or rolling_age_s < 0 or rolling_age_s > config.pcc_max_age_s:
        return _decision(rolling_action, FailureReason.PCC_STALE, details=("rolling export stale or invalid",))
    if not rolling.window_complete or rolling.covered_duration_s < config.rolling_window_s:
        return _decision(
            rolling_action,
            FailureReason.MISSING_TELEMETRY,
            details=("contractual rolling window incomplete",),
        )
    if rolling.average_w > config.contractual_export_limit_w:
        return _decision(rolling_action, FailureReason.PCC_ROLLING_LIMIT)
    if requested_target_w is None:
        return SafetyDecision(SafetyAction.NONE)
    _finite_nonnegative(requested_target_w, "requested_target_w")
    safe_target = min(requested_target_w, max(config.operational_export_target_w - max(pcc_w, 0.0), 0.0))
    if safe_target <= 0 and requested_target_w > 0:
        return _decision(_ownership_failure_action(state, physical_ownership), FailureReason.PCC_EXPORT_LIMIT)
    if safe_target < requested_target_w:
        return _decision(
            SafetyAction.CLAMP,
            FailureReason.PCC_EXPORT_LIMIT,
            clamped_target_w=safe_target,
            details=(f"pcc_export_w={max(pcc_w, 0.0):.3f}",),
        )
    return SafetyDecision(SafetyAction.NONE)


def evaluate_cross_flow(
    solax_power: NumericTelemetry | None,
    deye_power: NumericTelemetry | None,
    *,
    state: ExecutionState,
    expected_roles: PhysicalRoleVector | None = None,
    violation_duration_s: float,
    config: SafetyConfig,
    now: datetime,
) -> SafetyDecision:
    """Use normalized battery power: positive charge, negative discharge."""
    _aware(now, "now")
    _finite_nonnegative(violation_duration_s, "violation_duration_s")
    telemetry = combine_safety_decisions(
        [
            _telemetry_failure(
                sample,
                now=now,
                max_age_s=config.pcc_max_age_s,
                state=state,
                ownership=expected_roles,
            )
            for sample in (solax_power, deye_power)
        ]
    )
    if telemetry.action is not SafetyAction.NONE:
        return telemetry
    assert solax_power is not None and solax_power.value is not None
    assert deye_power is not None and deye_power.value is not None
    solax_w, deye_w = float(solax_power.value), float(deye_power.value)
    opposing = (solax_w > config.cross_flow_threshold_w and deye_w < -config.cross_flow_threshold_w) or (
        deye_w > config.cross_flow_threshold_w and solax_w < -config.cross_flow_threshold_w
    )
    violations: list[str] = []
    if expected_roles is not None:
        for battery, value, role in (
            ("solax", solax_w, expected_roles.solax),
            ("deye", deye_w, expected_roles.deye),
        ):
            if role is PhysicalRole.CHARGE and value < -config.neutral_power_threshold_w:
                violations.append(f"{battery} reversed from expected charge")
            elif role is PhysicalRole.DISCHARGE and value > config.neutral_power_threshold_w:
                violations.append(f"{battery} reversed from expected discharge")
            elif role in {PhysicalRole.HOLD, PhysicalRole.OFF} and abs(value) > config.neutral_power_threshold_w:
                violations.append(f"{battery} moved while expected {role.value.lower()}")
    non_owner = False
    required_dwell_s = config.cross_flow_dwell_s
    if state in _SOLAX_STATES:
        non_owner = abs(deye_w) > config.non_owner_threshold_w
        required_dwell_s = min(required_dwell_s, config.non_owner_dwell_s) if opposing else config.non_owner_dwell_s
    elif state in _DEYE_STATES:
        non_owner = abs(solax_w) > config.non_owner_threshold_w
        required_dwell_s = min(required_dwell_s, config.non_owner_dwell_s) if opposing else config.non_owner_dwell_s
    if not opposing and not non_owner and not violations:
        return SafetyDecision(SafetyAction.NONE)
    details = (
        f"deye_power_w={deye_w:.3f}",
        f"solax_power_w={solax_w:.3f}",
        *sorted(violations),
    )
    ownership_action = _ownership_failure_action(state, expected_roles)
    if ownership_action is SafetyAction.DEFER or violation_duration_s < required_dwell_s:
        return _decision(SafetyAction.DEFER, FailureReason.CROSS_BATTERY_FLOW, details=details)
    return _decision(config.persistent_cross_flow_action, FailureReason.CROSS_BATTERY_FLOW, details=details)


def evaluate_writer_conflicts(
    conflict_ids: tuple[str, ...] | list[str],
    *,
    state: ExecutionState,
    config: SafetyConfig,
    physical_ownership: PhysicalRoleVector | None = None,
) -> SafetyDecision:
    raw_conflicts = tuple(conflict_ids)
    if not all(isinstance(item, str) and item for item in raw_conflicts):
        raise ValueError("conflict IDs must be nonempty strings")
    conflicts = tuple(sorted(set(raw_conflicts)))
    if not conflicts or state in {ExecutionState.ROLLBACK, ExecutionState.SAFE_STOP}:
        return SafetyDecision(SafetyAction.NONE)
    action = (
        config.owned_writer_conflict_action
        if _ownership_failure_action(state, physical_ownership) is SafetyAction.ROLLBACK
        else SafetyAction.DEFER
    )
    return _decision(action, FailureReason.WRITER_CONFLICT, details=conflicts)


def evaluate_readiness(
    desired: PhysicalRoleVector,
    *,
    capabilities: CapabilitySnapshot,
    evidence: ReadinessEvidence,
    max_age_s: float,
    now: datetime,
) -> SafetyDecision:
    _aware(now, "now")
    _finite_nonnegative(max_age_s, "max_age_s", positive=True)
    reasons: list[FailureReason] = []
    capability_age_s = (now - capabilities.observed_at).total_seconds()
    if not 0 <= capability_age_s <= max_age_s:
        reasons.append(FailureReason.STALE_TELEMETRY)
    solax_required = desired.solax in {PhysicalRole.HOLD, PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
    deye_required = desired.deye in {PhysicalRole.HOLD, PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}
    if solax_required:
        if evidence.solax_source_timestamp is None:
            reasons.append(FailureReason.MISSING_TELEMETRY)
        elif not 0 <= (now - evidence.solax_source_timestamp).total_seconds() <= max_age_s:
            reasons.append(FailureReason.STALE_TELEMETRY)
        if not evidence.solax_available or evidence.solax_fault:
            reasons.append(FailureReason.INVERTER_UNAVAILABLE)
    if deye_required:
        if evidence.deye_source_timestamp is None:
            reasons.append(FailureReason.MISSING_TELEMETRY)
        elif not 0 <= (now - evidence.deye_source_timestamp).total_seconds() <= max_age_s:
            reasons.append(FailureReason.STALE_TELEMETRY)
    if desired.solax is PhysicalRole.HOLD and not capabilities.solax_mode5_hold_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if desired.solax in {PhysicalRole.CHARGE, PhysicalRole.DISCHARGE} and not (
        capabilities.solax_mode1_dispatch_verified and capabilities.solax_timeout_verified
    ):
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if desired.deye is PhysicalRole.HOLD:
        if not capabilities.deye_hold_verified:
            reasons.append(FailureReason.MODE_UNVERIFIED)
        if evidence.deye_state is not DeyeOperatingState.READY:
            reasons.append(FailureReason.DEYE_NOT_READY)
    if desired.deye in {PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}:
        if evidence.deye_state is not DeyeOperatingState.READY:
            reasons.append(FailureReason.DEYE_NOT_READY)
        if not capabilities.deye_bounded_export_verified:
            reasons.append(FailureReason.MODE_UNVERIFIED)
        if desired.solax is PhysicalRole.HOLD and not capabilities.solax_mode5_hold_verified:
            reasons.append(FailureReason.MODE_UNVERIFIED)
    return _decision(SafetyAction.DEFER, *reasons) if reasons else SafetyDecision(SafetyAction.NONE)


def evaluate_command_expiry(
    command: CommandRecord,
    *,
    state: ExecutionState,
    now: datetime,
) -> SafetyDecision:
    _aware(now, "now")
    if command.lifecycle_state is CommandLifecycleState.EXPIRED or now >= command.expires_at:
        return _decision(_ownership_failure_action(state), FailureReason.COMMAND_EXPIRED)
    return SafetyDecision(SafetyAction.NONE)


def evaluate_telemetry_coherence(
    required: dict[str, NumericTelemetry | None],
    *,
    contradictions: tuple[str, ...] = (),
    state: ExecutionState,
    max_age_s: float,
    now: datetime,
) -> SafetyDecision:
    _aware(now, "now")
    _finite_nonnegative(max_age_s, "max_age_s", positive=True)
    if not all(isinstance(name, str) and name for name in required):
        raise ValueError("required telemetry names must be nonempty strings")
    if not all(isinstance(item, str) and item for item in contradictions):
        raise ValueError("contradictions must contain nonempty strings")
    decisions = [
        _telemetry_failure(sample, now=now, max_age_s=max_age_s, state=state) for _, sample in sorted(required.items())
    ]
    if contradictions:
        decisions.append(
            _decision(
                _ownership_failure_action(state),
                FailureReason.TELEMETRY_INCOHERENT,
                details=tuple(sorted(set(contradictions))),
            )
        )
    return combine_safety_decisions(decisions)
