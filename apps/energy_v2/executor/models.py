"""Immutable inputs and records for execution-domain decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType

from .enums import CommandLifecycleState, ExecutionState, FailureReason, PhysicalRole, PlannerIntentType


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _finite_nonnegative(value: float | None, name: str) -> None:
    if value is not None and (not isfinite(value) or value < 0):
        raise ValueError(f"{name} must be finite and nonnegative")


def _finite(value: float | None, name: str) -> None:
    if value is not None and not isfinite(value):
        raise ValueError(f"{name} must be finite")


def _strict_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("mapping keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("mapping numbers must be finite")
    if isinstance(value, (str, int, float, bool, datetime, Enum)) or value is None:
        return value
    raise TypeError(f"mutable or unsupported mapping value: {type(value).__name__}")


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = _freeze(value)
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True)
class CapabilitySnapshot:
    solax_mode1_dispatch_verified: bool
    solax_mode5_hold_verified: bool
    solax_timeout_verified: bool
    deye_start_verified: bool
    deye_hold_verified: bool
    deye_bounded_export_verified: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        flags = (
            self.solax_mode1_dispatch_verified,
            self.solax_mode5_hold_verified,
            self.solax_timeout_verified,
            self.deye_start_verified,
            self.deye_hold_verified,
            self.deye_bounded_export_verified,
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("capability flags must be bool")
        _aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class PlannerIntent:
    plan_id: str
    intent_id: str
    slot_start: datetime
    slot_end: datetime
    intent_type: PlannerIntentType
    target_w: float | None
    deadline: datetime
    hypothetical: bool
    created_at: datetime
    reason_code: str
    reason: str
    confidence: str

    def __post_init__(self) -> None:
        if not self.plan_id or not self.intent_id:
            raise ValueError("plan_id and intent_id are required")
        if not isinstance(self.intent_type, PlannerIntentType):
            raise ValueError("intent_type must be PlannerIntentType")
        if not isinstance(self.hypothetical, bool):
            raise ValueError("hypothetical must be bool")
        for name in ("slot_start", "slot_end", "deadline", "created_at"):
            _aware(getattr(self, name), name)
        if self.slot_end <= self.slot_start:
            raise ValueError("slot_end must be after slot_start")
        if not self.slot_start <= self.deadline <= self.slot_end:
            raise ValueError("deadline must be within the slot")
        _finite_nonnegative(self.target_w, "target_w")
        if self.intent_type is PlannerIntentType.NORMAL:
            if self.target_w is not None:
                raise ValueError("NORMAL intent has no target_w")
        elif self.target_w is None or self.target_w <= 0:
            raise ValueError("active intent requires positive target_w")


@dataclass(frozen=True)
class PhysicalRoleVector:
    solax_role: PhysicalRole
    deye_role: PhysicalRole

    @property
    def solax(self) -> PhysicalRole:
        return self.solax_role

    @property
    def deye(self) -> PhysicalRole:
        return self.deye_role


@dataclass(frozen=True)
class ExecutionContext:
    execution_epoch: int
    state: ExecutionState
    state_entered_at: datetime
    now: datetime
    active_plan_id: str | None = None
    active_intent_id: str | None = None
    active_command_id: str | None = None
    intent: PlannerIntent | None = None
    last_failure: FailureReason | None = None
    command_expires_at: datetime | None = None
    enabled: bool = False
    reset_requested: bool = False
    writer_conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.execution_epoch < 0:
            raise ValueError("execution_epoch must be nonnegative")
        if not isinstance(self.state, ExecutionState):
            raise ValueError("state must be ExecutionState")
        _strict_bool(self.enabled, "enabled")
        _strict_bool(self.reset_requested, "reset_requested")
        for name in ("state_entered_at", "now", "command_expires_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        conflicts = tuple(self.writer_conflicts)
        if not all(isinstance(conflict, str) for conflict in conflicts):
            raise ValueError("writer_conflicts must contain strings")
        object.__setattr__(self, "writer_conflicts", conflicts)


@dataclass(frozen=True)
class CommandRecord:
    command_id: str
    execution_epoch: int
    plan_id: str
    intent_id: str
    intent_type: PlannerIntentType
    created_at: datetime
    expires_at: datetime
    lifecycle_state: CommandLifecycleState = CommandLifecycleState.INTENT_CREATED
    requested_target_w: float | None = None
    clamped_target_w: float | None = None
    expected_readback: Mapping[str, object] = field(default_factory=dict)
    observed_readback: Mapping[str, object] = field(default_factory=dict)
    observed_response_w: float | None = None
    last_observation_at: datetime | None = None
    failure_reason: FailureReason | None = None
    issued_at: datetime | None = None
    readback_confirmed_at: datetime | None = None
    physical_response_confirmed_at: datetime | None = None
    stable_confirmed_at: datetime | None = None
    observed_response_role: PhysicalRole | None = None
    stability_sample_count: int | None = None
    stability_duration_s: float | None = None
    expired_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.command_id or not self.plan_id or not self.intent_id:
            raise ValueError("command_id, plan_id and intent_id are required")
        if not isinstance(self.intent_type, PlannerIntentType):
            raise ValueError("intent_type must be PlannerIntentType")
        if not isinstance(self.lifecycle_state, CommandLifecycleState):
            raise ValueError("lifecycle_state must be CommandLifecycleState")
        if self.execution_epoch < 0:
            raise ValueError("execution_epoch must be nonnegative")
        _aware(self.created_at, "created_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        for name in (
            "issued_at",
            "readback_confirmed_at",
            "physical_response_confirmed_at",
            "stable_confirmed_at",
            "last_observation_at",
            "expired_at",
        ):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        if self.issued_at is not None and self.issued_at < self.created_at:
            raise ValueError("issued_at cannot precede created_at")
        if self.issued_at is not None and self.issued_at >= self.expires_at:
            raise ValueError("issued_at must precede expires_at")
        if self.last_observation_at is not None and self.last_observation_at < (self.issued_at or self.created_at):
            raise ValueError("last_observation_at cannot precede command chronology")
        _finite_nonnegative(self.requested_target_w, "requested_target_w")
        _finite_nonnegative(self.clamped_target_w, "clamped_target_w")
        if self.intent_type is PlannerIntentType.NORMAL:
            if self.requested_target_w is not None or self.clamped_target_w is not None:
                raise ValueError("NORMAL command cannot carry an active target")
        elif self.requested_target_w is None or self.requested_target_w <= 0:
            raise ValueError("active command requires positive requested_target_w")
        if self.clamped_target_w is not None and self.clamped_target_w <= 0:
            raise ValueError("clamped_target_w must be positive when present")
        _finite(self.observed_response_w, "observed_response_w")
        _finite_nonnegative(self.stability_duration_s, "stability_duration_s")
        if self.observed_response_role is not None and not isinstance(self.observed_response_role, PhysicalRole):
            raise ValueError("observed_response_role must be PhysicalRole")
        if self.stability_sample_count is not None and (
            isinstance(self.stability_sample_count, bool)
            or not isinstance(self.stability_sample_count, int)
            or self.stability_sample_count < 1
        ):
            raise ValueError("stability_sample_count must be a positive integer")
        expected = _frozen_mapping(self.expected_readback)
        observed = _frozen_mapping(self.observed_readback)
        object.__setattr__(self, "expected_readback", expected)
        object.__setattr__(self, "observed_readback", observed)
        self._validate_lifecycle_proof(expected, observed)

    def _validate_lifecycle_proof(
        self,
        expected: Mapping[str, object],
        observed: Mapping[str, object],
    ) -> None:
        issued_states = {
            CommandLifecycleState.COMMAND_ISSUED,
            CommandLifecycleState.READBACK_CONFIRMED,
            CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED,
            CommandLifecycleState.STABLE,
        }
        readback_states = {
            CommandLifecycleState.READBACK_CONFIRMED,
            CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED,
            CommandLifecycleState.STABLE,
        }
        physical_states = {CommandLifecycleState.PHYSICAL_RESPONSE_CONFIRMED, CommandLifecycleState.STABLE}
        if self.lifecycle_state in issued_states and self.issued_at is None:
            raise ValueError("issued lifecycle state requires issued_at")
        if self.lifecycle_state in readback_states:
            if not expected or not observed or self.readback_confirmed_at is None:
                raise ValueError("readback-confirmed lifecycle state requires readback proof")
            if any(key not in observed or observed[key] != value for key, value in expected.items()):
                raise ValueError("confirmed readback does not match expected readback")
        if self.lifecycle_state in physical_states:
            expected_role = {
                PlannerIntentType.CHARGE_SOLAX: PhysicalRole.CHARGE,
                PlannerIntentType.DISCHARGE_SOLAX: PhysicalRole.DISCHARGE,
                PlannerIntentType.CHARGE_DEYE: PhysicalRole.CHARGE,
                PlannerIntentType.DISCHARGE_DEYE: PhysicalRole.DISCHARGE,
            }.get(self.intent_type)
            if (
                self.observed_response_w is None
                or self.observed_response_role is not expected_role
                or self.physical_response_confirmed_at is None
            ):
                raise ValueError("physical-response lifecycle state requires response proof")
        if self.lifecycle_state is CommandLifecycleState.STABLE:
            if (
                self.stable_confirmed_at is None
                or self.stability_sample_count is None
                or self.stability_duration_s is None
            ):
                raise ValueError("stable lifecycle state requires stability proof")
        if self.lifecycle_state is CommandLifecycleState.FAILED and self.failure_reason is None:
            raise ValueError("failed lifecycle state requires failure_reason")
        if self.lifecycle_state is CommandLifecycleState.EXPIRED:
            if self.expired_at is None or self.expired_at < self.expires_at:
                raise ValueError("expired lifecycle state requires consistent expired_at")
            if self.failure_reason is not FailureReason.COMMAND_EXPIRED:
                raise ValueError("expired lifecycle state requires COMMAND_EXPIRED")
        ordered = (
            ("issued_at", self.issued_at),
            ("readback_confirmed_at", self.readback_confirmed_at),
            ("physical_response_confirmed_at", self.physical_response_confirmed_at),
            ("stable_confirmed_at", self.stable_confirmed_at),
        )
        previous_name, previous = "created_at", self.created_at
        for name, value in ordered:
            if value is None:
                continue
            if value < previous:
                raise ValueError(f"{name} cannot precede {previous_name}")
            if value >= self.expires_at:
                raise ValueError(f"{name} must precede expires_at")
            previous_name, previous = name, value
        if self.last_observation_at is not None and self.last_observation_at < previous:
            raise ValueError("last_observation_at cannot precede accepted evidence")


@dataclass(frozen=True)
class CommandObservation:
    command_id: str
    execution_epoch: int
    observed_at: datetime
    readback_values: Mapping[str, object] = field(default_factory=dict)
    physical_response_w: float | None = None
    physical_response_role: PhysicalRole | None = None
    pcc_power_w: float | None = None
    telemetry_fresh: bool = False
    telemetry_coherent: bool = False
    device_available: bool = False
    fault: str | None = None

    def __post_init__(self) -> None:
        if not self.command_id:
            raise ValueError("command_id is required")
        if self.execution_epoch < 0:
            raise ValueError("execution_epoch must be nonnegative")
        _aware(self.observed_at, "observed_at")
        _strict_bool(self.telemetry_fresh, "telemetry_fresh")
        _strict_bool(self.telemetry_coherent, "telemetry_coherent")
        _strict_bool(self.device_available, "device_available")
        _finite(self.physical_response_w, "physical_response_w")
        _finite(self.pcc_power_w, "pcc_power_w")
        if self.physical_response_role is not None and not isinstance(self.physical_response_role, PhysicalRole):
            raise ValueError("physical_response_role must be PhysicalRole")
        object.__setattr__(self, "readback_values", _frozen_mapping(self.readback_values))


@dataclass(frozen=True)
class PhysicalResponseAcceptance:
    expected_role: PhysicalRole
    minimum_magnitude_w: float
    maximum_magnitude_w: float
    allow_zero: bool = False

    def __post_init__(self) -> None:
        if self.expected_role not in {PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}:
            raise ValueError("expected_role must be CHARGE or DISCHARGE")
        _finite_nonnegative(self.minimum_magnitude_w, "minimum_magnitude_w")
        _finite_nonnegative(self.maximum_magnitude_w, "maximum_magnitude_w")
        if self.maximum_magnitude_w < self.minimum_magnitude_w:
            raise ValueError("maximum_magnitude_w must be at least minimum_magnitude_w")
        if not isinstance(self.allow_zero, bool):
            raise ValueError("allow_zero must be bool")


@dataclass(frozen=True)
class StabilityEvidence:
    command_id: str
    execution_epoch: int
    observed_at: datetime
    stable: bool
    sample_count: int
    stable_duration_s: float

    def __post_init__(self) -> None:
        if not self.command_id or self.execution_epoch < 0:
            raise ValueError("valid command identity is required")
        _aware(self.observed_at, "observed_at")
        _strict_bool(self.stable, "stable")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 1:
            raise ValueError("sample_count must be positive")
        _finite_nonnegative(self.stable_duration_s, "stable_duration_s")
