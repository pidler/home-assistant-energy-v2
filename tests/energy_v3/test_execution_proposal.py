from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.energy_v3.controller import decide
from apps.energy_v3.execution_proposal import (
    ControlledInverter,
    CounterpartSafeState,
    ExecutionReadiness,
    ProposalBlocker,
    execution_proposal,
)
from apps.energy_v3.models import (
    CapabilityLevel,
    CurrentTarget,
    DeyeCapabilities,
    ExportAuthorization,
    SafetySnapshot,
    SolaxCapabilities,
    V3Capabilities,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
VERIFIED = CapabilityLevel.VERIFIED


def target(solax_w: float = 0.0, deye_w: float = 0.0) -> CurrentTarget:
    return CurrentTarget(NOW, NOW + timedelta(minutes=15), solax_w, deye_w)


def safety(*, solax_soc: float = 60.0, pcc_export_w: float = 0.0) -> SafetySnapshot:
    return SafetySnapshot(
        telemetry_fresh=True,
        solax_soc=solax_soc,
        deye_soc=60.0,
        pcc_export_w=pcc_export_w,
        export_authorization=ExportAuthorization(NOW, NOW + timedelta(minutes=15), 9_800.0),
        solax_available=True,
        deye_available=True,
        solax_fault=False,
        deye_fault=False,
        measured_solax_battery_power_w=0.0,
        measured_deye_battery_power_w=0.0,
        writer_conflict=False,
    )


CAPABILITIES = V3Capabilities(
    solax=SolaxCapabilities(set_power_discharge=VERIFIED, set_power_charge=VERIFIED),
    deye=DeyeCapabilities(
        enter_export=VERIFIED,
        export_power=VERIFIED,
        charge_power=VERIFIED,
    ),
)


def proposal(
    current: CurrentTarget,
    *,
    snapshot: SafetySnapshot | None = None,
    readiness: ExecutionReadiness | None = None,
):
    decision = decide(current, snapshot or safety(), now=NOW, capabilities=CAPABILITIES)
    return execution_proposal(current, decision, readiness=readiness)


COMPLETE = ExecutionReadiness(
    exclusive_ownership=True,
    solax_interlock_verified=True,
    deye_interlock_verified=True,
    import_limit_guard_verified=True,
    solax_trade_reserve_guard_verified=True,
)


def test_both_zero_requires_no_active_execution() -> None:
    result = proposal(target())

    assert result.controlled_inverter is None
    assert result.counterpart_safe_state is CounterpartSafeState.BOTH_NATIVE
    assert result.blocked is False


@pytest.mark.parametrize(
    ("current", "owner", "protocol_value", "protocol_unit", "counterpart"),
    [
        (target(solax_w=-1_200), ControlledInverter.SOLAX, 1_200.0, "W", CounterpartSafeState.DEYE_ZERO_POWER_HOLD),
        (
            target(deye_w=-1_200),
            ControlledInverter.DEYE,
            -100,
            "0.1_percent_of_rated_power",
            CounterpartSafeState.SOLAX_ZERO_BATTERY_HOLD,
        ),
        (target(solax_w=1_200), ControlledInverter.SOLAX, -1_200.0, "W", CounterpartSafeState.DEYE_ZERO_POWER_HOLD),
        (
            target(deye_w=1_200),
            ControlledInverter.DEYE,
            100,
            "0.1_percent_of_rated_power",
            CounterpartSafeState.SOLAX_ZERO_BATTERY_HOLD,
        ),
    ],
)
def test_single_owner_combinations_translate_verified_protocol_signs(
    current: CurrentTarget,
    owner: ControlledInverter,
    protocol_value: int | float,
    protocol_unit: str,
    counterpart: CounterpartSafeState,
) -> None:
    result = proposal(current, readiness=COMPLETE)

    assert result.controlled_inverter is owner
    assert result.protocol_setpoint is not None
    assert result.protocol_setpoint.value == protocol_value
    assert result.protocol_setpoint.unit == protocol_unit
    assert result.protocol_setpoint.expected_battery_power_w == pytest.approx(
        current.solax_target_w or current.deye_target_w
    )
    assert result.counterpart_safe_state is counterpart
    assert result.blocked is False


def test_two_nonzero_same_direction_is_recorded_as_future_requirement() -> None:
    result = proposal(target(1_200, 1_200), readiness=COMPLETE)

    assert result.blocked is True
    assert result.future_multi_inverter_requirement is True
    assert result.solax_requested_w == 1_200
    assert result.deye_requested_w == 1_200
    assert result.missing_safety_conditions == (
        "CONTROLLER:MULTI_OWNER_UNSUPPORTED",
        ProposalBlocker.FUTURE_MULTI_INVERTER_REQUIREMENT.value,
    )


def test_opposite_directions_are_recorded_but_never_translated() -> None:
    result = proposal(target(1_200, -1_200), readiness=COMPLETE)

    assert result.blocked is True
    assert result.future_multi_inverter_requirement is True
    assert result.protocol_setpoint is None
    assert result.missing_safety_conditions[0] == "CONTROLLER:CROSS_TRANSFER_RISK"


def test_default_evidence_blocks_control_without_exclusive_ownership_or_deye_hold() -> None:
    result = proposal(target(solax_w=1_200))

    assert result.missing_safety_conditions == (
        ProposalBlocker.OWNERSHIP_NOT_EXCLUSIVE.value,
        ProposalBlocker.DEYE_INTERLOCK_UNVERIFIED.value,
        ProposalBlocker.SOLAX_TRADE_RESERVE_GUARD_MISSING.value,
    )
    assert result.blocked is True


def test_charge_identifies_missing_import_limit_guard_without_reimplementing_it() -> None:
    readiness = ExecutionReadiness(exclusive_ownership=True, solax_interlock_verified=True)
    result = proposal(target(deye_w=-1_200), readiness=readiness)

    assert result.missing_safety_conditions == (ProposalBlocker.IMPORT_LIMIT_GUARD_MISSING.value,)


def test_solax_trade_reserve_blocker_applies_only_to_commanded_discharge() -> None:
    common = dict(exclusive_ownership=True, deye_interlock_verified=True, import_limit_guard_verified=True)

    discharge = proposal(target(solax_w=1_200), readiness=ExecutionReadiness(**common))
    charge = proposal(target(solax_w=-1_200), readiness=ExecutionReadiness(**common))
    idle = proposal(target(), readiness=ExecutionReadiness(**common))

    assert ProposalBlocker.SOLAX_TRADE_RESERVE_GUARD_MISSING.value in discharge.missing_safety_conditions
    assert ProposalBlocker.SOLAX_TRADE_RESERVE_GUARD_MISSING.value not in charge.missing_safety_conditions
    assert ProposalBlocker.SOLAX_TRADE_RESERVE_GUARD_MISSING.value not in idle.missing_safety_conditions


def test_controller_export_limit_cannot_be_bypassed_by_proposal() -> None:
    result = proposal(target(deye_w=5_000), snapshot=safety(pcc_export_w=4_801), readiness=COMPLETE)

    assert result.blocked is True
    assert result.protocol_setpoint is None
    assert result.missing_safety_conditions == ("CONTROLLER:EXPORT_TARGET_LIMIT",)


def test_controller_soc_floor_cannot_be_bypassed_by_proposal() -> None:
    result = proposal(target(solax_w=1_200), snapshot=safety(solax_soc=15), readiness=COMPLETE)

    assert result.blocked is True
    assert result.missing_safety_conditions == ("CONTROLLER:SOLAX_SOC_FLOOR",)


def test_protocol_steps_are_explicit_and_quantized() -> None:
    solax = proposal(target(solax_w=1_255), readiness=COMPLETE).protocol_setpoint
    deye = proposal(target(deye_w=1_205), readiness=COMPLETE).protocol_setpoint

    assert solax is not None and (solax.value, solax.step) == (-1_300.0, 100)
    assert deye is not None and (deye.value, deye.step) == (100, 1)


def test_invalid_deye_rated_power_is_rejected() -> None:
    current = target(deye_w=1_200)
    decision = decide(current, safety(), now=NOW, capabilities=CAPABILITIES)

    with pytest.raises(ValueError, match="rated power"):
        execution_proposal(current, decision, readiness=COMPLETE, deye_rated_power_w=0)


def test_execution_proposal_source_has_no_physical_write_api() -> None:
    source = (Path(__file__).parents[2] / "apps" / "energy_v3" / "execution_proposal.py").read_text(encoding="utf-8")

    forbidden = ("call_service", "set_state", "write_register", "select_option", "button.press", "import appdaemon")
    assert all(token not in source for token in forbidden)
