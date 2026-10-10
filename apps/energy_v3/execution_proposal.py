"""Pure ENERGY V3.2 protocol proposal; never executes a physical command."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from .models import CurrentTarget, Decision, DecisionReason, DecisionState


class ControlledInverter(StrEnum):
    SOLAX = "SOLAX"
    DEYE = "DEYE"


class CounterpartSafeState(StrEnum):
    BOTH_NATIVE = "BOTH_NATIVE"
    DEYE_ZERO_POWER_HOLD = "DEYE_ZERO_POWER_HOLD"
    SOLAX_ZERO_BATTERY_HOLD = "SOLAX_ZERO_BATTERY_HOLD"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProposalBlocker(StrEnum):
    OWNERSHIP_NOT_EXCLUSIVE = "OWNERSHIP_NOT_EXCLUSIVE"
    SOLAX_INTERLOCK_UNVERIFIED = "SOLAX_INTERLOCK_UNVERIFIED"
    DEYE_INTERLOCK_UNVERIFIED = "DEYE_INTERLOCK_UNVERIFIED"
    IMPORT_LIMIT_GUARD_MISSING = "IMPORT_LIMIT_GUARD_MISSING"
    SOLAX_TRADE_RESERVE_GUARD_MISSING = "SOLAX_TRADE_RESERVE_GUARD_MISSING"
    CONTROLLER_DECISION_MISMATCH = "CONTROLLER_DECISION_MISMATCH"
    FUTURE_MULTI_INVERTER_REQUIREMENT = "FUTURE_MULTI_INVERTER_REQUIREMENT"


@dataclass(frozen=True)
class ExecutionReadiness:
    """Evidence not currently represented by the V3.1 controller.

    False defaults are deliberate.  A proposal is shadow data, and callers must
    supply independently verified evidence before it can be marked ready.
    """

    exclusive_ownership: bool = False
    solax_interlock_verified: bool = False
    deye_interlock_verified: bool = False
    import_limit_guard_verified: bool = False
    solax_trade_reserve_guard_verified: bool = False


@dataclass(frozen=True)
class ProtocolSetpoint:
    field: str
    value: int | float
    unit: str
    step: int | float
    expected_battery_power_w: float


@dataclass(frozen=True)
class ExecutionProposal:
    """Diagnostic translation of one controller decision into protocol terms."""

    controller_state: DecisionState
    controller_reason: DecisionReason
    solax_requested_w: float
    deye_requested_w: float
    controlled_inverter: ControlledInverter | None
    haeo_target_w: float | None
    protocol_setpoint: ProtocolSetpoint | None
    counterpart_safe_state: CounterpartSafeState
    ownership_ready: bool
    missing_safety_conditions: tuple[str, ...]
    blocked: bool
    future_multi_inverter_requirement: bool = False


def execution_proposal(
    target: CurrentTarget,
    decision: Decision,
    *,
    readiness: ExecutionReadiness | None = None,
    deye_rated_power_w: float = 12_000.0,
) -> ExecutionProposal:
    """Translate an existing controller decision without granting execution.

    HAEO uses positive discharge and negative charge.  The verified SolaX VPP
    experiment uses the inverse sign in watts.  DEYE register 1109 uses signed
    0.1-percent units, where positive discharges and negative charges.
    """

    evidence = readiness or ExecutionReadiness()
    base = {
        "controller_state": decision.state,
        "controller_reason": decision.reason,
        "solax_requested_w": target.solax_target_w,
        "deye_requested_w": target.deye_target_w,
    }

    if decision.state is DecisionState.IDLE:
        return ExecutionProposal(
            **base,
            controlled_inverter=None,
            haeo_target_w=None,
            protocol_setpoint=None,
            counterpart_safe_state=CounterpartSafeState.BOTH_NATIVE,
            ownership_ready=evidence.exclusive_ownership,
            missing_safety_conditions=(),
            blocked=False,
        )

    if decision.state is DecisionState.RETURN_TO_NORMAL:
        future = (
            decision.reason
            in {
                DecisionReason.MULTI_OWNER_UNSUPPORTED,
                DecisionReason.CROSS_TRANSFER_RISK,
            }
            and target.solax_target_w != 0
            and target.deye_target_w != 0
        )
        missing = [f"CONTROLLER:{decision.reason.value}"]
        if future:
            missing.append(ProposalBlocker.FUTURE_MULTI_INVERTER_REQUIREMENT.value)
        return ExecutionProposal(
            **base,
            controlled_inverter=None,
            haeo_target_w=None,
            protocol_setpoint=None,
            counterpart_safe_state=CounterpartSafeState.NOT_APPLICABLE,
            ownership_ready=evidence.exclusive_ownership,
            missing_safety_conditions=tuple(missing),
            blocked=True,
            future_multi_inverter_requirement=future,
        )

    owner = ControlledInverter.SOLAX if decision.state is DecisionState.CONTROL_SOLAX else ControlledInverter.DEYE
    expected_target = target.solax_target_w if owner is ControlledInverter.SOLAX else target.deye_target_w
    blockers: list[str] = []
    if decision.target_w != expected_target or expected_target == 0:
        blockers.append(ProposalBlocker.CONTROLLER_DECISION_MISMATCH.value)
    if not evidence.exclusive_ownership:
        blockers.append(ProposalBlocker.OWNERSHIP_NOT_EXCLUSIVE.value)

    if owner is ControlledInverter.SOLAX:
        counterpart = CounterpartSafeState.DEYE_ZERO_POWER_HOLD
        if not evidence.deye_interlock_verified:
            blockers.append(ProposalBlocker.DEYE_INTERLOCK_UNVERIFIED.value)
        setpoint = _solax_setpoint(expected_target)
        if expected_target > 0 and not evidence.solax_trade_reserve_guard_verified:
            blockers.append(ProposalBlocker.SOLAX_TRADE_RESERVE_GUARD_MISSING.value)
    else:
        counterpart = CounterpartSafeState.SOLAX_ZERO_BATTERY_HOLD
        if not evidence.solax_interlock_verified:
            blockers.append(ProposalBlocker.SOLAX_INTERLOCK_UNVERIFIED.value)
        setpoint = _deye_setpoint(expected_target, deye_rated_power_w)

    if expected_target < 0 and not evidence.import_limit_guard_verified:
        blockers.append(ProposalBlocker.IMPORT_LIMIT_GUARD_MISSING.value)

    return ExecutionProposal(
        **base,
        controlled_inverter=owner,
        haeo_target_w=expected_target,
        protocol_setpoint=setpoint,
        counterpart_safe_state=counterpart,
        ownership_ready=evidence.exclusive_ownership,
        missing_safety_conditions=tuple(blockers),
        blocked=bool(blockers),
    )


def _solax_setpoint(haeo_target_w: float) -> ProtocolSetpoint:
    value = float(_quantize(-haeo_target_w, 100))
    return ProtocolSetpoint(
        field="number.solax_remotecontrol_active_power",
        value=value,
        unit="W",
        step=100,
        expected_battery_power_w=-value,
    )


def _deye_setpoint(haeo_target_w: float, rated_power_w: float) -> ProtocolSetpoint:
    if rated_power_w <= 0:
        raise ValueError("DEYE rated power must be positive")
    raw = int(_quantize(haeo_target_w * 1_000 / rated_power_w, 1))
    return ProtocolSetpoint(
        field="register_1109",
        value=raw,
        unit="0.1_percent_of_rated_power",
        step=1,
        expected_battery_power_w=raw * rated_power_w / 1_000,
    )


def _quantize(value: float, step: int) -> Decimal:
    scaled = Decimal(str(value)) / Decimal(step)
    return scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal(step)
