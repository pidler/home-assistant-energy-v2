"""SolaX shadow proposal semantics."""

from dataclasses import dataclass

from ..enums import FailureReason, PhysicalRole
from ..models import CapabilitySnapshot
from .base import AdapterOperation, AdapterResult, ShadowControlProposal


@dataclass(frozen=True)
class SolaxShadowAdapter:
    capabilities: CapabilitySnapshot

    def propose_native(self) -> AdapterResult:
        return AdapterResult(
            ShadowControlProposal("SOLAX", AdapterOperation.NATIVE, PhysicalRole.NATIVE, "SELF_USE_NATIVE")
        )

    def propose_hold(self, *, command_id: str | None = None) -> AdapterResult:
        if not self.capabilities.solax_mode5_hold_verified:
            return AdapterResult(None, (FailureReason.MODE_UNVERIFIED,))
        return AdapterResult(
            ShadowControlProposal(
                "SOLAX",
                AdapterOperation.HOLD,
                PhysicalRole.HOLD,
                "MODE5_ZERO_BATTERY_POWER",
                command_id=command_id,
            )
        )

    def propose_start(self) -> AdapterResult:
        return AdapterResult(None, (FailureReason.UNSUPPORTED_INTENT,))

    def propose_stop(self, *, command_id: str | None = None) -> AdapterResult:
        return AdapterResult(
            ShadowControlProposal(
                "SOLAX",
                AdapterOperation.STOP,
                PhysicalRole.NATIVE,
                "RETURN_TO_SAFE_NATIVE",
                command_id=command_id,
            )
        )

    def propose_charge(
        self,
        target_w: float,
        *,
        economic_requested_target_w: float | None = None,
        command_id: str | None = None,
    ) -> AdapterResult:
        return self._dispatch(
            AdapterOperation.CHARGE,
            PhysicalRole.CHARGE,
            target_w,
            economic_requested_target_w,
            command_id,
        )

    def propose_discharge(
        self,
        target_w: float,
        *,
        economic_requested_target_w: float | None = None,
        command_id: str | None = None,
    ) -> AdapterResult:
        return self._dispatch(
            AdapterOperation.DISCHARGE,
            PhysicalRole.DISCHARGE,
            target_w,
            economic_requested_target_w,
            command_id,
        )

    def _dispatch(
        self,
        operation: AdapterOperation,
        role: PhysicalRole,
        target_w: float,
        economic_requested_target_w: float | None,
        command_id: str | None,
    ) -> AdapterResult:
        if not (self.capabilities.solax_mode1_dispatch_verified and self.capabilities.solax_timeout_verified):
            return AdapterResult(None, (FailureReason.MODE_UNVERIFIED,))
        return AdapterResult(
            ShadowControlProposal(
                "SOLAX",
                operation,
                role,
                "MODE1_BATTERY_CONTROL",
                economic_requested_target_w=(
                    target_w if economic_requested_target_w is None else economic_requested_target_w
                ),
                effective_executor_target_w=target_w,
                acceptance_min_w=0.0,
                acceptance_max_w=target_w,
                command_id=command_id,
            )
        )
