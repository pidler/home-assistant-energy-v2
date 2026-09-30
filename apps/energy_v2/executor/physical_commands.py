"""Explicit, gated physical command sequences for only proven installation behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import StrEnum


class CapabilityLevel(StrEnum):
    VERIFIED = "VERIFIED"
    EXPERIMENTAL = "EXPERIMENTAL"
    UNAVAILABLE = "UNAVAILABLE"


class ControlAuthority(StrEnum):
    LEGACY = "LEGACY"
    ENERGY_V2_EXECUTOR = "ENERGY_V2_EXECUTOR"


@dataclass(frozen=True)
class ServiceCall:
    service: str
    entity_id: str
    data: dict[str, object]

    def __post_init__(self) -> None:
        if self.service not in {"select/select_option", "switch/turn_on", "switch/turn_off"}:
            raise ValueError("physical service is outside the allowlist")
        if self.entity_id.split(".", 1)[0] not in {"select", "switch"}:
            raise ValueError("physical target domain is outside the allowlist")


@dataclass(frozen=True)
class PhysicalCommandPlan:
    name: str
    capability: CapabilityLevel
    calls: tuple[ServiceCall, ...]
    reason: str = ""
    restores_known_normal: bool = False

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.capability, CapabilityLevel):
            raise ValueError("named capability is required")
        if self.capability is not CapabilityLevel.VERIFIED and self.calls:
            raise ValueError("unverified operations cannot contain physical calls")
        if self.restores_known_normal and self.capability is not CapabilityLevel.VERIFIED:
            raise ValueError("restore must be verified")


@dataclass(frozen=True)
class PhysicalExecutionGates:
    static_writer_enabled: bool = False
    physical_execution_enabled: bool = False
    authority: ControlAuthority = ControlAuthority.LEGACY
    energy_v2_enabled: bool = False
    safe_to_enable: bool = False
    export_enabled: bool = False
    service_mode: bool = False
    telemetry_fresh: bool = False
    plan_fresh: bool = False
    export_limit_clear: bool = False
    soc_floor_clear: bool = False
    cross_transfer_clear: bool = False
    writer_conflicts_clear: bool = False

    def __post_init__(self) -> None:
        flags = (
            self.static_writer_enabled,
            self.physical_execution_enabled,
            self.energy_v2_enabled,
            self.safe_to_enable,
            self.export_enabled,
            self.service_mode,
            self.telemetry_fresh,
            self.plan_fresh,
            self.export_limit_clear,
            self.soc_floor_clear,
            self.cross_transfer_clear,
            self.writer_conflicts_clear,
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("physical gate flags must be strict bool")
        if not isinstance(self.authority, ControlAuthority):
            raise ValueError("authority must use ControlAuthority")

    def active_allowed(self) -> bool:
        return all(
            (
                self.static_writer_enabled,
                self.physical_execution_enabled,
                self.authority is ControlAuthority.ENERGY_V2_EXECUTOR,
                self.energy_v2_enabled,
                self.safe_to_enable,
                self.export_enabled,
                not self.service_mode,
                self.telemetry_fresh,
                self.plan_fresh,
                self.export_limit_clear,
                self.soc_floor_clear,
                self.cross_transfer_clear,
                self.writer_conflicts_clear,
            )
        )

    def owned_restore_allowed(self, *, executor_owns_control: bool) -> bool:
        # Fault, stale telemetry, cross-flow, or user disable must be able to unwind
        # only a session that this executor previously placed under control.
        return self.static_writer_enabled and executor_owns_control


class ServiceCaller(Protocol):
    def call_service(self, service: str, **kwargs: object) -> object: ...


class SolaxCommandAdapter:
    """Only physically proven normal and hold operations carry calls."""

    def __init__(self, *, vpp_experimental_enabled: bool = False) -> None:
        self.vpp_experimental_enabled = vpp_experimental_enabled

    def normal(self) -> PhysicalCommandPlan:
        return PhysicalCommandPlan(
            "SOLAX_NORMAL",
            CapabilityLevel.VERIFIED,
            (_select("select.solax_charger_use_mode", "Self Use Mode"),),
            restores_known_normal=True,
        )

    def hold(self) -> PhysicalCommandPlan:
        return PhysicalCommandPlan(
            "SOLAX_HOLD",
            CapabilityLevel.VERIFIED,
            (
                _select("select.solax_charger_use_mode", "Manual Mode"),
                _select("select.solax_manual_mode_select", "Stop Charge and Discharge"),
            ),
        )

    def export(self, target_w: float) -> PhysicalCommandPlan:
        return self._vpp("SOLAX_EXPORT_VPP", target_w)

    def charge(self, target_w: float) -> PhysicalCommandPlan:
        return self._vpp("SOLAX_CHARGE_VPP", target_w)

    def _vpp(self, name: str, target_w: float) -> PhysicalCommandPlan:
        if target_w <= 0:
            raise ValueError("VPP target must be positive")
        reason = "Mode 8 VPP is not physically verified"
        if not self.vpp_experimental_enabled:
            reason += "; dedicated experimental capability is disabled"
        return PhysicalCommandPlan(name, CapabilityLevel.EXPERIMENTAL, (), reason)


class DeyeCommandAdapter:
    """Preserves the user's proven DEYE normal/export sequences exactly."""

    def normal(self) -> PhysicalCommandPlan:
        return PhysicalCommandPlan(
            "DEYE_NORMAL",
            CapabilityLevel.VERIFIED,
            (
                _select("select.deye_ac_coupling", "Grid"),
                _switch("switch.deye_export_surplus", False),
                _select("select.deye_time_of_use", "Disabled"),
                _select("select.deye_work_mode", "Zero Export To CT"),
            ),
            restores_known_normal=True,
        )

    def export(self) -> PhysicalCommandPlan:
        return PhysicalCommandPlan(
            "DEYE_EXPORT",
            CapabilityLevel.VERIFIED,
            (
                _select("select.deye_ac_coupling", "Disabled"),
                _switch("switch.deye_export_surplus", True),
                _select("select.deye_time_of_use", "Enabled"),
                _select("select.deye_work_mode", "Export First"),
            ),
        )

    def hold(self) -> PhysicalCommandPlan:
        return PhysicalCommandPlan(
            "DEYE_HOLD",
            CapabilityLevel.UNAVAILABLE,
            (),
            "No physically verified DEYE hold sequence",
        )

    def charge(self, target_w: float) -> PhysicalCommandPlan:
        if target_w <= 0:
            raise ValueError("charge target must be positive")
        return PhysicalCommandPlan(
            "DEYE_CHARGE",
            CapabilityLevel.UNAVAILABLE,
            (),
            "No physically verified DEYE charge sequence",
        )


def deye_export_sequence(solax: SolaxCommandAdapter, deye: DeyeCommandAdapter) -> PhysicalCommandPlan:
    hold = solax.hold()
    export = deye.export()
    return PhysicalCommandPlan(
        "ENTER_DEYE_EXPORT",
        CapabilityLevel.VERIFIED,
        (*hold.calls, *export.calls),
        "Physically proven break-before-make sequence: SolaX hold before DEYE export",
    )


def known_normal_restore(solax: SolaxCommandAdapter, deye: DeyeCommandAdapter) -> PhysicalCommandPlan:
    normal_solax = solax.normal()
    normal_deye = deye.normal()
    return PhysicalCommandPlan(
        "KNOWN_NORMAL_RESTORE",
        CapabilityLevel.VERIFIED,
        (*normal_solax.calls, *normal_deye.calls),
        "User-proven NORMAL / RESTORE sequence",
        restores_known_normal=True,
    )


class PhysicalCommandWriter:
    """The sole physical I/O boundary; default gates make writes impossible."""

    def __init__(self, caller: ServiceCaller) -> None:
        self.caller = caller
        self.last_error: str | None = None

    def execute(
        self,
        plan: PhysicalCommandPlan,
        gates: PhysicalExecutionGates,
        *,
        executor_owns_control: bool = False,
    ) -> bool:
        if plan.capability is not CapabilityLevel.VERIFIED:
            return False
        allowed = (
            gates.owned_restore_allowed(executor_owns_control=executor_owns_control)
            if plan.restores_known_normal and executor_owns_control
            else gates.active_allowed()
        )
        if not allowed:
            return False
        self.last_error = None
        try:
            for call in plan.calls:
                self.caller.call_service(call.service, entity_id=call.entity_id, **call.data)
        except Exception as error:  # AppDaemon service adapters are an external I/O boundary.
            self.last_error = f"{type(error).__name__}:{error}"[:240]
            return False
        return True


def _select(entity_id: str, option: str) -> ServiceCall:
    return ServiceCall("select/select_option", entity_id, {"option": option})


def _switch(entity_id: str, enabled: bool) -> ServiceCall:
    return ServiceCall("switch/turn_on" if enabled else "switch/turn_off", entity_id, {})
