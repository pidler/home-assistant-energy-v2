"""DEYE supervisory shadow proposal semantics."""

from dataclasses import dataclass

from ...deye_state import DeyeOperatingState
from ..enums import FailureReason, PhysicalRole
from ..models import CapabilitySnapshot
from .base import AdapterOperation, AdapterResult, ShadowControlProposal


@dataclass(frozen=True)
class DeyeShadowAdapter:
    capabilities: CapabilitySnapshot
    operating_state: DeyeOperatingState
    configured_ceiling_w: float | None = None

    def propose_native(self) -> AdapterResult:
        return AdapterResult(
            ShadowControlProposal("DEYE", AdapterOperation.NATIVE, PhysicalRole.OFF, "NON_OWNING_NATIVE")
        )

    def propose_hold(self, *, command_id: str | None = None) -> AdapterResult:
        if not self.capabilities.deye_hold_verified:
            return AdapterResult(None, (FailureReason.MODE_UNVERIFIED,))
        return AdapterResult(
            ShadowControlProposal(
                "DEYE",
                AdapterOperation.HOLD,
                PhysicalRole.HOLD,
                "VERIFIED_SUPERVISORY_HOLD",
                requires_ready=True,
                command_id=command_id,
            )
        )

    def propose_start(self) -> AdapterResult:
        if self.operating_state is not DeyeOperatingState.INTENTIONAL_OFF:
            return AdapterResult(None, (FailureReason.DEYE_NOT_READY,))
        if not self.capabilities.deye_start_verified:
            return AdapterResult(None, (FailureReason.MODE_UNVERIFIED,))
        return AdapterResult(
            ShadowControlProposal(
                "DEYE",
                AdapterOperation.START,
                PhysicalRole.OFF,
                "SUPERVISORY_START_REQUIRED",
                requires_ready=True,
                startup_required=True,
                solax_hold_required=True,
            )
        )

    def propose_stop(self, *, command_id: str | None = None) -> AdapterResult:
        return AdapterResult(
            ShadowControlProposal(
                "DEYE",
                AdapterOperation.STOP,
                PhysicalRole.OFF,
                "RETURN_TO_NON_OWNING_OFF",
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
        if self.operating_state is not DeyeOperatingState.READY:
            return AdapterResult(None, (FailureReason.DEYE_NOT_READY,))
        if not self.capabilities.deye_bounded_export_verified:
            return AdapterResult(None, (FailureReason.MODE_UNVERIFIED,))
        if self.configured_ceiling_w is None or self.configured_ceiling_w <= 0:
            return AdapterResult(None, (FailureReason.MISSING_TELEMETRY,))
        applied_ceiling_w = min(target_w, self.configured_ceiling_w)
        return AdapterResult(
            ShadowControlProposal(
                "DEYE",
                operation,
                role,
                "SUPERVISORY_POLICY_CEILING",
                economic_requested_target_w=(
                    target_w if economic_requested_target_w is None else economic_requested_target_w
                ),
                effective_executor_target_w=target_w,
                configured_max_ceiling_w=self.configured_ceiling_w,
                applied_supervisory_ceiling_w=applied_ceiling_w,
                acceptance_min_w=0.0,
                acceptance_max_w=applied_ceiling_w,
                requires_ready=True,
                exact_power_guaranteed=False,
                command_id=command_id,
            )
        )
