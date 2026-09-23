"""Cross-inverter role invariants for pure planning decisions."""

from dataclasses import dataclass

from .enums import FailureReason, PhysicalRole, PlannerIntentType
from .models import CapabilitySnapshot, PhysicalRoleVector, PlannerIntent

_ACTIVE = {PhysicalRole.CHARGE, PhysicalRole.DISCHARGE}


@dataclass(frozen=True)
class RoleDerivation:
    roles: PhysicalRoleVector | None
    reasons: tuple[FailureReason, ...] = ()


def validate_role_vector(
    roles: PhysicalRoleVector,
    capabilities: CapabilitySnapshot,
    *,
    deye_ready: bool,
    writer_conflicts: tuple[str, ...] = (),
) -> tuple[FailureReason, ...]:
    if not isinstance(deye_ready, bool):
        raise ValueError("deye_ready must be bool")
    reasons: list[FailureReason] = []
    if writer_conflicts:
        reasons.append(FailureReason.WRITER_CONFLICT)
    if roles.solax is PhysicalRole.OFF or roles.deye is PhysicalRole.NATIVE:
        reasons.append(FailureReason.INVALID_INTENT)
    if roles.solax is PhysicalRole.HOLD and not capabilities.solax_mode5_hold_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if roles.deye is PhysicalRole.HOLD and not capabilities.deye_hold_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if roles.solax in _ACTIVE and not capabilities.solax_mode1_dispatch_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if roles.deye in _ACTIVE and not capabilities.deye_bounded_export_verified:
        reasons.append(FailureReason.MODE_UNVERIFIED)
    if roles.solax in _ACTIVE and roles.deye not in {PhysicalRole.OFF, PhysicalRole.HOLD}:
        reasons.append(FailureReason.CROSS_BATTERY_FLOW)
    if roles.deye in _ACTIVE:
        if roles.solax is not PhysicalRole.HOLD:
            reasons.append(FailureReason.CROSS_BATTERY_FLOW)
        if not deye_ready:
            reasons.append(FailureReason.DEYE_NOT_READY)
    return tuple(dict.fromkeys(reasons))


def roles_for_intent(intent: PlannerIntent) -> RoleDerivation:
    if not isinstance(intent.intent_type, PlannerIntentType):
        return RoleDerivation(None, (FailureReason.INVALID_INTENT,))
    if intent.intent_type is PlannerIntentType.NORMAL:
        return RoleDerivation(PhysicalRoleVector(PhysicalRole.NATIVE, PhysicalRole.OFF))
    if intent.intent_type is PlannerIntentType.CHARGE_SOLAX:
        return RoleDerivation(PhysicalRoleVector(PhysicalRole.CHARGE, PhysicalRole.OFF))
    if intent.intent_type is PlannerIntentType.DISCHARGE_SOLAX:
        return RoleDerivation(PhysicalRoleVector(PhysicalRole.DISCHARGE, PhysicalRole.OFF))
    if intent.intent_type is PlannerIntentType.CHARGE_DEYE:
        return RoleDerivation(PhysicalRoleVector(PhysicalRole.HOLD, PhysicalRole.CHARGE))
    if intent.intent_type is PlannerIntentType.DISCHARGE_DEYE:
        return RoleDerivation(PhysicalRoleVector(PhysicalRole.HOLD, PhysicalRole.DISCHARGE))
    return RoleDerivation(None, (FailureReason.INVALID_INTENT,))


def validate_intent_roles(
    intent: PlannerIntent,
    capabilities: CapabilitySnapshot,
    *,
    deye_ready: bool,
    writer_conflicts: tuple[str, ...] = (),
) -> tuple[FailureReason, ...]:
    derived = roles_for_intent(intent)
    if derived.roles is None:
        return derived.reasons
    return validate_role_vector(
        derived.roles,
        capabilities,
        deye_ready=deye_ready,
        writer_conflicts=writer_conflicts,
    )
