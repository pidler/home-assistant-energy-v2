"""Stateless ENERGY V3 target validation, safety, and arbitration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import (
    CapabilityLevel,
    CurrentTarget,
    Decision,
    DecisionReason,
    DecisionState,
    ExportAuthorization,
    SafetyConfig,
    SafetySnapshot,
    V3Capabilities,
    _is_finite_number,
)


def decide(
    target: CurrentTarget,
    safety: SafetySnapshot,
    *,
    now: datetime,
    config: SafetyConfig | None = None,
    capabilities: V3Capabilities | None = None,
) -> Decision:
    """Return one deterministic, side-effect-free control decision."""

    config = config or SafetyConfig()
    capabilities = capabilities or V3Capabilities()
    normalized, failure = _validate_target(target, now, config)
    if failure is not None:
        return _normal(failure)
    assert normalized is not None

    safety_failure = _evaluate_safety(normalized, safety, now, config)
    if safety_failure is not None:
        return _normal(safety_failure)

    solax_active = normalized.solax_target_w != 0
    deye_active = normalized.deye_target_w != 0
    if not solax_active and not deye_active:
        return Decision(DecisionState.IDLE, DecisionReason.TARGET_ZERO)
    if solax_active and deye_active:
        if normalized.solax_target_w * normalized.deye_target_w < 0:
            return _normal(DecisionReason.CROSS_TRANSFER_RISK)
        return _normal(DecisionReason.MULTI_OWNER_UNSUPPORTED)
    if solax_active:
        required = (
            capabilities.solax.set_power_discharge
            if normalized.solax_target_w > 0
            else capabilities.solax.set_power_charge
        )
        if required is not CapabilityLevel.VERIFIED:
            return _normal(DecisionReason.CAPABILITY_UNSUPPORTED)
        return Decision(DecisionState.CONTROL_SOLAX, DecisionReason.CONTROL_ALLOWED, normalized.solax_target_w)

    required = capabilities.deye.export_power if normalized.deye_target_w > 0 else capabilities.deye.charge_power
    if required is not CapabilityLevel.VERIFIED:
        return _normal(DecisionReason.CAPABILITY_UNSUPPORTED)
    if normalized.deye_target_w > 0 and capabilities.deye.enter_export is not CapabilityLevel.VERIFIED:
        return _normal(DecisionReason.CAPABILITY_UNSUPPORTED)
    return Decision(DecisionState.CONTROL_DEYE, DecisionReason.CONTROL_ALLOWED, normalized.deye_target_w)


def _validate_target(
    target: CurrentTarget,
    now: datetime,
    config: SafetyConfig,
) -> tuple[CurrentTarget | None, DecisionReason | None]:
    timestamps = (target.timestamp, target.valid_until, now)
    if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
        return None, DecisionReason.TARGET_TIMESTAMP_INVALID
    if target.valid_until <= target.timestamp or not target.timestamp <= now < target.valid_until:
        return None, DecisionReason.TARGET_NOT_CURRENT
    if not all(_is_finite_number(value) for value in (target.solax_target_w, target.deye_target_w)):
        return None, DecisionReason.TARGET_POWER_INVALID
    if (
        abs(target.solax_target_w) > config.solax_max_abs_power_w
        or abs(target.deye_target_w) > config.deye_max_abs_power_w
    ):
        return None, DecisionReason.TARGET_POWER_LIMIT
    return (
        replace(
            target,
            solax_target_w=_deadband(target.solax_target_w, config.zero_deadband_w),
            deye_target_w=_deadband(target.deye_target_w, config.zero_deadband_w),
        ),
        None,
    )


def _evaluate_safety(
    target: CurrentTarget,
    safety: SafetySnapshot,
    now: datetime,
    config: SafetyConfig,
) -> DecisionReason | None:
    if not safety.telemetry_fresh:
        return DecisionReason.TELEMETRY_STALE
    required_numeric = (
        safety.pcc_export_w,
        safety.measured_solax_battery_power_w,
        safety.measured_deye_battery_power_w,
    )
    if any(value is None or not _is_finite_number(value) for value in required_numeric):
        return DecisionReason.TELEMETRY_MISSING
    if safety.writer_conflict:
        return DecisionReason.WRITER_CONFLICT
    if not safety.solax_available or not safety.deye_available:
        return DecisionReason.INVERTER_UNAVAILABLE
    if safety.solax_fault is None or safety.deye_fault is None:
        return DecisionReason.TELEMETRY_MISSING
    if safety.solax_fault or safety.deye_fault:
        return DecisionReason.INVERTER_FAULT

    protected_soc_threshold = config.protected_soc_floor_pct + config.discharge_guard_margin_pct
    if target.solax_target_w > 0:
        if not _is_valid_soc(safety.solax_soc):
            return DecisionReason.TELEMETRY_MISSING
        if safety.solax_soc <= protected_soc_threshold:
            return DecisionReason.SOLAX_SOC_FLOOR
    if target.deye_target_w > 0:
        if not _is_valid_soc(safety.deye_soc):
            return DecisionReason.TELEMETRY_MISSING
        if safety.deye_soc <= protected_soc_threshold:
            return DecisionReason.DEYE_SOC_FLOOR

    assert safety.pcc_export_w is not None
    assert safety.measured_solax_battery_power_w is not None
    assert safety.measured_deye_battery_power_w is not None
    solax_w = safety.measured_solax_battery_power_w
    deye_w = safety.measured_deye_battery_power_w

    # Current PCC already contains measured battery flow. Project only the
    # additional discharge needed to reach the requested absolute power target.
    additional_discharge_w = _additional_discharge(target.solax_target_w, solax_w) + _additional_discharge(
        target.deye_target_w,
        deye_w,
    )
    projected_pcc_export_w = max(safety.pcc_export_w, 0.0) + additional_discharge_w
    if projected_pcc_export_w > config.operational_export_target_w:
        return DecisionReason.EXPORT_TARGET_LIMIT
    if target.solax_target_w > 0 or target.deye_target_w > 0:
        authorization_failure = _validate_export_authorization(safety.export_authorization, now)
        if authorization_failure is not None:
            return authorization_failure
        assert safety.export_authorization is not None
        if projected_pcc_export_w > safety.export_authorization.max_pcc_export_w:
            return DecisionReason.EXPORT_AUTHORIZATION_LIMIT

    tolerance = config.cross_transfer_tolerance_w
    cross_transfer = (solax_w > tolerance and deye_w < -tolerance) or (deye_w > tolerance and solax_w < -tolerance)
    return DecisionReason.CROSS_TRANSFER_RISK if cross_transfer else None


def _deadband(value: float, deadband_w: float) -> float:
    return 0.0 if abs(value) <= deadband_w else value


def _additional_discharge(target_w: float, measured_battery_power_w: float) -> float:
    """Estimate added grid contribution for a positive discharge target."""

    return max(target_w + measured_battery_power_w, 0.0) if target_w > 0 else 0.0


def _is_valid_soc(value: object) -> bool:
    return _is_finite_number(value) and 0 <= value <= 100


def _validate_export_authorization(
    authorization: ExportAuthorization | None,
    now: datetime,
) -> DecisionReason | None:
    if authorization is None:
        return DecisionReason.EXPORT_AUTHORIZATION_MISSING
    timestamps = (authorization.timestamp, authorization.valid_until)
    if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
        return DecisionReason.EXPORT_AUTHORIZATION_INVALID
    if authorization.valid_until <= authorization.timestamp:
        return DecisionReason.EXPORT_AUTHORIZATION_INVALID
    if not _is_finite_number(authorization.max_pcc_export_w) or authorization.max_pcc_export_w < 0:
        return DecisionReason.EXPORT_AUTHORIZATION_INVALID
    if not authorization.timestamp <= now < authorization.valid_until:
        return DecisionReason.EXPORT_AUTHORIZATION_NOT_CURRENT
    return None


def _normal(reason: DecisionReason) -> Decision:
    return Decision(DecisionState.RETURN_TO_NORMAL, reason)
