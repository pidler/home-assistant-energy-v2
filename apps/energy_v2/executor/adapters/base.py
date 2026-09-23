"""Immutable domain proposals produced without performing I/O."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from ...models import StrEnum
from ..enums import FailureReason, PhysicalRole


class AdapterOperation(StrEnum):
    NATIVE = "NATIVE"
    HOLD = "HOLD"
    START = "START"
    STOP = "STOP"
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"


@dataclass(frozen=True)
class ShadowControlProposal:
    battery: str
    operation: AdapterOperation
    role: PhysicalRole
    control_contract: str
    economic_requested_target_w: float | None = None
    effective_executor_target_w: float | None = None
    configured_max_ceiling_w: float | None = None
    applied_supervisory_ceiling_w: float | None = None
    acceptance_min_w: float | None = None
    acceptance_max_w: float | None = None
    requires_ready: bool = False
    startup_required: bool = False
    solax_hold_required: bool = False
    exact_power_guaranteed: bool = False
    command_id: str | None = None
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if self.battery not in {"SOLAX", "DEYE"}:
            raise ValueError("battery must be SOLAX or DEYE")
        if not isinstance(self.operation, AdapterOperation) or not isinstance(self.role, PhysicalRole):
            raise ValueError("operation and role must use executor enums")
        if not self.control_contract or len(self.control_contract) > 80:
            raise ValueError("bounded control_contract is required")
        for name in (
            "economic_requested_target_w",
            "effective_executor_target_w",
            "configured_max_ceiling_w",
            "applied_supervisory_ceiling_w",
            "acceptance_min_w",
            "acceptance_max_w",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.operation in {AdapterOperation.CHARGE, AdapterOperation.DISCHARGE}:
            if self.economic_requested_target_w is None or self.economic_requested_target_w <= 0:
                raise ValueError("active proposal requires a positive economic request")
            if self.effective_executor_target_w is None or self.effective_executor_target_w <= 0:
                raise ValueError("active proposal requires a positive target magnitude")
            if self.effective_executor_target_w > self.economic_requested_target_w:
                raise ValueError("effective target must not exceed economic request")
            expected = PhysicalRole.CHARGE if self.operation is AdapterOperation.CHARGE else PhysicalRole.DISCHARGE
            if self.role is not expected:
                raise ValueError("active proposal direction must match role")
        elif any(
            value is not None
            for value in (
                self.economic_requested_target_w,
                self.effective_executor_target_w,
                self.configured_max_ceiling_w,
                self.applied_supervisory_ceiling_w,
            )
        ):
            raise ValueError("passive proposal cannot carry target or ceiling power")
        ceiling_values = (self.configured_max_ceiling_w, self.applied_supervisory_ceiling_w)
        if (ceiling_values[0] is None) is not (ceiling_values[1] is None):
            raise ValueError("configured and applied ceilings must be paired")
        if self.applied_supervisory_ceiling_w is not None:
            if self.applied_supervisory_ceiling_w > self.configured_max_ceiling_w:
                raise ValueError("applied ceiling must not exceed configured maximum")
            if self.applied_supervisory_ceiling_w > self.effective_executor_target_w:
                raise ValueError("applied ceiling must not exceed effective target")
        if self.acceptance_min_w is not None and self.acceptance_max_w is not None:
            if self.acceptance_max_w < self.acceptance_min_w:
                raise ValueError("acceptance maximum must not be below minimum")
        if (
            self.acceptance_max_w is not None
            and self.effective_executor_target_w is not None
            and self.acceptance_max_w > self.effective_executor_target_w
        ):
            raise ValueError("acceptance maximum must not exceed effective target")
        flags = (
            self.requires_ready,
            self.startup_required,
            self.solax_hold_required,
            self.exact_power_guaranteed,
            self.shadow_only,
        )
        if not all(isinstance(flag, bool) for flag in flags) or not self.shadow_only:
            raise ValueError("proposal flags must be bool and shadow_only must remain true")


@dataclass(frozen=True)
class AdapterResult:
    proposal: ShadowControlProposal | None
    reasons: tuple[FailureReason, ...] = ()

    def __post_init__(self) -> None:
        reasons = tuple(self.reasons)
        if self.proposal is not None and not isinstance(self.proposal, ShadowControlProposal):
            raise ValueError("proposal must be ShadowControlProposal")
        if not all(isinstance(reason, FailureReason) for reason in reasons):
            raise ValueError("reasons must use FailureReason")
        if self.proposal is not None and reasons:
            raise ValueError("successful adapter result cannot include failure reasons")
        object.__setattr__(self, "reasons", reasons)


class ShadowProposalAdapter(Protocol):
    def propose_native(self) -> AdapterResult: ...

    def propose_hold(self, *, command_id: str | None = None) -> AdapterResult: ...

    def propose_start(self) -> AdapterResult: ...

    def propose_stop(self, *, command_id: str | None = None) -> AdapterResult: ...

    def propose_charge(
        self,
        target_w: float,
        *,
        economic_requested_target_w: float | None = None,
        command_id: str | None = None,
    ) -> AdapterResult: ...

    def propose_discharge(
        self,
        target_w: float,
        *,
        economic_requested_target_w: float | None = None,
        command_id: str | None = None,
    ) -> AdapterResult: ...
