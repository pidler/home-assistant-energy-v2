"""Immutable inputs and outputs for the pure ENERGY V3 decision function."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite


def _is_finite_number(value: object) -> bool:
    """Return whether value is a finite real number, excluding booleans."""

    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


class CapabilityLevel(StrEnum):
    VERIFIED = "VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"


class DecisionState(StrEnum):
    IDLE = "IDLE"
    CONTROL_SOLAX = "CONTROL_SOLAX"
    CONTROL_DEYE = "CONTROL_DEYE"
    RETURN_TO_NORMAL = "RETURN_TO_NORMAL"


class DecisionReason(StrEnum):
    TARGET_ZERO = "TARGET_ZERO"
    TARGET_TIMESTAMP_INVALID = "TARGET_TIMESTAMP_INVALID"
    TARGET_NOT_CURRENT = "TARGET_NOT_CURRENT"
    TARGET_POWER_INVALID = "TARGET_POWER_INVALID"
    TARGET_POWER_LIMIT = "TARGET_POWER_LIMIT"
    TELEMETRY_STALE = "TELEMETRY_STALE"
    TELEMETRY_MISSING = "TELEMETRY_MISSING"
    WRITER_CONFLICT = "WRITER_CONFLICT"
    INVERTER_UNAVAILABLE = "INVERTER_UNAVAILABLE"
    INVERTER_FAULT = "INVERTER_FAULT"
    SOLAX_SOC_FLOOR = "SOLAX_SOC_FLOOR"
    DEYE_SOC_FLOOR = "DEYE_SOC_FLOOR"
    EXPORT_TARGET_LIMIT = "EXPORT_TARGET_LIMIT"
    EXPORT_AUTHORIZATION_MISSING = "EXPORT_AUTHORIZATION_MISSING"
    EXPORT_AUTHORIZATION_INVALID = "EXPORT_AUTHORIZATION_INVALID"
    EXPORT_AUTHORIZATION_NOT_CURRENT = "EXPORT_AUTHORIZATION_NOT_CURRENT"
    EXPORT_AUTHORIZATION_LIMIT = "EXPORT_AUTHORIZATION_LIMIT"
    CROSS_TRANSFER_RISK = "CROSS_TRANSFER_RISK"
    MULTI_OWNER_UNSUPPORTED = "MULTI_OWNER_UNSUPPORTED"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    CONTROL_ALLOWED = "CONTROL_ALLOWED"


@dataclass(frozen=True)
class CurrentTarget:
    """Current HAEO target; positive power discharges and negative power charges."""

    timestamp: datetime
    valid_until: datetime
    solax_target_w: float
    deye_target_w: float


@dataclass(frozen=True)
class ExportAuthorization:
    """Time-limited PCC export ceiling from the authoritative export limiter."""

    timestamp: datetime
    valid_until: datetime
    max_pcc_export_w: float


@dataclass(frozen=True)
class SafetySnapshot:
    """Minimum evidence required for deterministic V3 arbitration.

    Battery power uses the ENERGY convention: positive charge, negative discharge.
    Grid power values are positive export magnitudes.
    """

    telemetry_fresh: bool
    solax_soc: float | None
    deye_soc: float | None
    pcc_export_w: float | None
    export_authorization: ExportAuthorization | None
    solax_available: bool
    deye_available: bool
    solax_fault: bool
    deye_fault: bool
    measured_solax_battery_power_w: float | None
    measured_deye_battery_power_w: float | None
    writer_conflict: bool


@dataclass(frozen=True)
class SafetyConfig:
    zero_deadband_w: float = 50.0
    solax_max_abs_power_w: float = 12_000.0
    deye_max_abs_power_w: float = 12_000.0
    protected_soc_floor_pct: float = 10.0
    discharge_guard_margin_pct: float = 5.0
    operational_export_target_w: float = 9_800.0
    cross_transfer_tolerance_w: float = 300.0

    def __post_init__(self) -> None:
        values = (
            self.zero_deadband_w,
            self.solax_max_abs_power_w,
            self.deye_max_abs_power_w,
            self.protected_soc_floor_pct,
            self.discharge_guard_margin_pct,
            self.operational_export_target_w,
            self.cross_transfer_tolerance_w,
        )
        if not all(_is_finite_number(value) and value >= 0 for value in values):
            raise ValueError("safety configuration values must be finite and nonnegative")
        if self.solax_max_abs_power_w <= 0 or self.deye_max_abs_power_w <= 0:
            raise ValueError("inverter power limits must be positive")
        if not 0 <= self.protected_soc_floor_pct <= 100:
            raise ValueError("protected SOC floor must be between 0 and 100")
        if self.protected_soc_floor_pct + self.discharge_guard_margin_pct > 100:
            raise ValueError("protected SOC floor plus discharge guard margin must not exceed 100")


@dataclass(frozen=True)
class SolaxCapabilities:
    normal: CapabilityLevel = CapabilityLevel.VERIFIED
    hold: CapabilityLevel = CapabilityLevel.VERIFIED
    set_power_discharge: CapabilityLevel = CapabilityLevel.UNSUPPORTED
    set_power_charge: CapabilityLevel = CapabilityLevel.UNSUPPORTED


@dataclass(frozen=True)
class DeyeCapabilities:
    normal: CapabilityLevel = CapabilityLevel.VERIFIED
    enter_export: CapabilityLevel = CapabilityLevel.VERIFIED
    export_power: CapabilityLevel = CapabilityLevel.UNSUPPORTED
    charge_power: CapabilityLevel = CapabilityLevel.UNSUPPORTED
    hold: CapabilityLevel = CapabilityLevel.UNSUPPORTED


@dataclass(frozen=True)
class V3Capabilities:
    solax: SolaxCapabilities = SolaxCapabilities()
    deye: DeyeCapabilities = DeyeCapabilities()


@dataclass(frozen=True)
class Decision:
    state: DecisionState
    reason: DecisionReason
    target_w: float | None = None

    def __post_init__(self) -> None:
        if self.state in {DecisionState.CONTROL_SOLAX, DecisionState.CONTROL_DEYE}:
            if self.target_w is None or not _is_finite_number(self.target_w) or self.target_w == 0:
                raise ValueError("control decisions require a finite nonzero target")
        elif self.target_w is not None:
            raise ValueError("non-control decisions cannot carry a target")
