from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v3 import (
    CapabilityLevel,
    CurrentTarget,
    DecisionReason,
    DecisionState,
    DeyeCapabilities,
    SafetyConfig,
    SafetySnapshot,
    SolaxCapabilities,
    V3Capabilities,
    decide,
)

NOW = datetime(2026, 9, 30, 12, 5, tzinfo=UTC)


def target(*, solax_w: float = 0.0, deye_w: float = 0.0, **changes: object) -> CurrentTarget:
    values: dict[str, object] = {
        "timestamp": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(minutes=10),
        "solax_target_w": solax_w,
        "deye_target_w": deye_w,
    }
    values.update(changes)
    return CurrentTarget(**values)  # type: ignore[arg-type]


def safe(**changes: object) -> SafetySnapshot:
    values: dict[str, object] = {
        "telemetry_fresh": True,
        "solax_soc": 60.0,
        "deye_soc": 60.0,
        "pcc_export_w": 0.0,
        "rolling_export_w": 0.0,
        "solax_available": True,
        "deye_available": True,
        "solax_fault": False,
        "deye_fault": False,
        "measured_solax_battery_power_w": 0.0,
        "measured_deye_battery_power_w": 0.0,
        "writer_conflict": False,
    }
    values.update(changes)
    return SafetySnapshot(**values)  # type: ignore[arg-type]


def verified(
    *,
    solax_discharge: bool = False,
    solax_charge: bool = False,
    deye_export: bool = False,
    deye_charge: bool = False,
    deye_enter_export: bool = True,
) -> V3Capabilities:
    level = CapabilityLevel.VERIFIED
    unsupported = CapabilityLevel.UNSUPPORTED
    return V3Capabilities(
        solax=SolaxCapabilities(
            set_power_discharge=level if solax_discharge else unsupported,
            set_power_charge=level if solax_charge else unsupported,
        ),
        deye=DeyeCapabilities(
            enter_export=level if deye_enter_export else unsupported,
            export_power=level if deye_export else unsupported,
            charge_power=level if deye_charge else unsupported,
        ),
    )


def run(
    current: CurrentTarget,
    snapshot: SafetySnapshot | None = None,
    *,
    now: datetime = NOW,
    config: SafetyConfig | None = None,
    capabilities: V3Capabilities | None = None,
):
    return decide(current, snapshot or safe(), now=now, config=config, capabilities=capabilities)


def test_valid_target_is_accepted_for_verified_capability() -> None:
    result = run(target(solax_w=2_000), capabilities=verified(solax_discharge=True))

    assert result.state is DecisionState.CONTROL_SOLAX
    assert result.reason is DecisionReason.CONTROL_ALLOWED
    assert result.target_w == 2_000


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_until": NOW},
        {"valid_until": NOW - timedelta(seconds=1)},
    ],
)
def test_expired_target_returns_to_normal(changes: dict[str, object]) -> None:
    result = run(target(solax_w=1_000, **changes), capabilities=verified(solax_discharge=True))

    assert (result.state, result.reason) == (DecisionState.RETURN_TO_NORMAL, DecisionReason.TARGET_NOT_CURRENT)


def test_future_dated_target_returns_to_normal() -> None:
    result = run(
        target(timestamp=NOW + timedelta(seconds=1), valid_until=NOW + timedelta(minutes=15)),
    )

    assert result.reason is DecisionReason.TARGET_NOT_CURRENT


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_target_returns_to_normal(value: float) -> None:
    result = run(target(solax_w=value))

    assert result.reason is DecisionReason.TARGET_POWER_INVALID


def test_timezone_naive_target_or_now_returns_to_normal() -> None:
    naive = NOW.replace(tzinfo=None)

    assert run(target(timestamp=naive)).reason is DecisionReason.TARGET_TIMESTAMP_INVALID
    assert run(target(), now=naive).reason is DecisionReason.TARGET_TIMESTAMP_INVALID


def test_target_above_configured_power_limit_returns_to_normal() -> None:
    result = run(target(deye_w=12_001), capabilities=verified(deye_export=True))

    assert result.reason is DecisionReason.TARGET_POWER_LIMIT


def test_zero_deadband_normalizes_both_targets_to_idle() -> None:
    result = run(target(solax_w=50, deye_w=-49), config=SafetyConfig(zero_deadband_w=50))

    assert (result.state, result.reason, result.target_w) == (
        DecisionState.IDLE,
        DecisionReason.TARGET_ZERO,
        None,
    )


def test_both_zero_is_idle() -> None:
    assert run(target()).state is DecisionState.IDLE


@pytest.mark.parametrize(
    ("current", "caps", "expected_state", "expected_target"),
    [
        (target(solax_w=2_000), verified(solax_discharge=True), DecisionState.CONTROL_SOLAX, 2_000),
        (target(solax_w=-2_000), verified(solax_charge=True), DecisionState.CONTROL_SOLAX, -2_000),
        (target(deye_w=2_000), verified(deye_export=True), DecisionState.CONTROL_DEYE, 2_000),
        (target(deye_w=-2_000), verified(deye_charge=True), DecisionState.CONTROL_DEYE, -2_000),
    ],
)
def test_single_verified_owner_is_selected(
    current: CurrentTarget,
    caps: V3Capabilities,
    expected_state: DecisionState,
    expected_target: float,
) -> None:
    result = run(current, capabilities=caps)

    assert (result.state, result.reason, result.target_w) == (
        expected_state,
        DecisionReason.CONTROL_ALLOWED,
        expected_target,
    )


@pytest.mark.parametrize("solax_w,deye_w", [(1_000, -1_000), (-1_000, 1_000)])
def test_opposite_directions_fail_closed(solax_w: float, deye_w: float) -> None:
    result = run(target(solax_w=solax_w, deye_w=deye_w))

    assert (result.state, result.reason) == (
        DecisionState.RETURN_TO_NORMAL,
        DecisionReason.CROSS_TRANSFER_RISK,
    )


@pytest.mark.parametrize("solax_w,deye_w", [(1_000, 1_000), (-1_000, -1_000)])
def test_same_direction_multiple_owners_are_unsupported(solax_w: float, deye_w: float) -> None:
    result = run(target(solax_w=solax_w, deye_w=deye_w))

    assert result.reason is DecisionReason.MULTI_OWNER_UNSUPPORTED


@pytest.mark.parametrize(
    "current",
    [target(solax_w=1_000), target(solax_w=-1_000), target(deye_w=1_000), target(deye_w=-1_000)],
)
def test_default_capabilities_do_not_allow_power_control(current: CurrentTarget) -> None:
    result = run(current)

    assert (result.state, result.reason) == (
        DecisionState.RETURN_TO_NORMAL,
        DecisionReason.CAPABILITY_UNSUPPORTED,
    )


def test_deye_bounded_export_also_requires_verified_export_mode_entry() -> None:
    result = run(target(deye_w=1_000), capabilities=verified(deye_export=True, deye_enter_export=False))

    assert result.reason is DecisionReason.CAPABILITY_UNSUPPORTED


def test_stale_telemetry_returns_to_normal() -> None:
    assert run(target(), safe(telemetry_fresh=False)).reason is DecisionReason.TELEMETRY_STALE


@pytest.mark.parametrize(
    "field",
    [
        "solax_soc",
        "deye_soc",
        "pcc_export_w",
        "rolling_export_w",
        "measured_solax_battery_power_w",
        "measured_deye_battery_power_w",
    ],
)
def test_missing_telemetry_returns_to_normal(field: str) -> None:
    assert run(target(), replace(safe(), **{field: None})).reason is DecisionReason.TELEMETRY_MISSING


def test_solax_soc_at_floor_blocks_discharge() -> None:
    result = run(
        target(solax_w=1_000),
        safe(solax_soc=10),
        capabilities=verified(solax_discharge=True),
    )

    assert result.reason is DecisionReason.SOLAX_SOC_FLOOR


def test_deye_soc_at_floor_blocks_discharge() -> None:
    result = run(target(deye_w=1_000), safe(deye_soc=10), capabilities=verified(deye_export=True))

    assert result.reason is DecisionReason.DEYE_SOC_FLOOR


def test_soc_floor_does_not_block_verified_charge() -> None:
    result = run(target(deye_w=-1_000), safe(deye_soc=5), capabilities=verified(deye_charge=True))

    assert result.state is DecisionState.CONTROL_DEYE


def test_projected_operational_export_limit_fails_closed() -> None:
    result = run(target(deye_w=5_000), safe(pcc_export_w=4_801), capabilities=verified(deye_export=True))

    assert result.reason is DecisionReason.EXPORT_TARGET_LIMIT


def test_projected_rolling_export_limit_fails_closed() -> None:
    result = run(
        target(deye_w=4_100),
        safe(pcc_export_w=0, rolling_export_w=5_901),
        capabilities=verified(deye_export=True),
    )

    assert result.reason is DecisionReason.ROLLING_EXPORT_LIMIT


def test_existing_measured_discharge_is_not_added_to_pcc_twice() -> None:
    result = run(
        target(deye_w=5_000),
        safe(pcc_export_w=5_000, rolling_export_w=5_000, measured_deye_battery_power_w=-5_000),
        capabilities=verified(deye_export=True),
    )

    assert result.state is DecisionState.CONTROL_DEYE


@pytest.mark.parametrize("field", ["solax_available", "deye_available"])
def test_unavailable_inverter_fails_closed(field: str) -> None:
    assert run(target(), replace(safe(), **{field: False})).reason is DecisionReason.INVERTER_UNAVAILABLE


@pytest.mark.parametrize("field", ["solax_fault", "deye_fault"])
def test_inverter_fault_fails_closed(field: str) -> None:
    assert run(target(), replace(safe(), **{field: True})).reason is DecisionReason.INVERTER_FAULT


def test_writer_conflict_fails_closed() -> None:
    assert run(target(), safe(writer_conflict=True)).reason is DecisionReason.WRITER_CONFLICT


@pytest.mark.parametrize(
    ("solax_w", "deye_w"),
    [(301, -301), (-301, 301)],
)
def test_measured_cross_transfer_fails_closed(solax_w: float, deye_w: float) -> None:
    result = run(
        target(),
        safe(measured_solax_battery_power_w=solax_w, measured_deye_battery_power_w=deye_w),
    )

    assert result.reason is DecisionReason.CROSS_TRANSFER_RISK


def test_cross_transfer_tolerance_boundary_is_not_a_violation() -> None:
    result = run(
        target(),
        safe(measured_solax_battery_power_w=300, measured_deye_battery_power_w=-300),
    )

    assert result.state is DecisionState.IDLE


def test_late_entry_keeps_the_same_power_target_without_catch_up() -> None:
    current = target(solax_w=2_000)
    caps = verified(solax_discharge=True)

    early = run(current, now=NOW - timedelta(minutes=4), capabilities=caps)
    late = run(current, now=NOW + timedelta(minutes=9, seconds=59), capabilities=caps)

    assert early == late
    assert late.target_w == 2_000


def test_restart_is_stateless_and_deterministic() -> None:
    current = target(deye_w=2_000)
    snapshot = safe()
    caps = verified(deye_export=True)

    assert run(current, snapshot, capabilities=caps) == run(current, snapshot, capabilities=caps)


def test_domain_models_contain_no_energy_accounting_fields() -> None:
    names = {field.name for model in (CurrentTarget, SafetySnapshot) for field in fields(model)}

    assert not any("energy" in name or "delivered" in name or "budget" in name for name in names)


def test_capability_defaults_match_verified_physical_knowledge() -> None:
    capabilities = V3Capabilities()

    assert capabilities.solax.normal is CapabilityLevel.VERIFIED
    assert capabilities.solax.hold is CapabilityLevel.VERIFIED
    assert capabilities.solax.set_power_discharge is CapabilityLevel.UNSUPPORTED
    assert capabilities.solax.set_power_charge is CapabilityLevel.UNSUPPORTED
    assert capabilities.deye.normal is CapabilityLevel.VERIFIED
    assert capabilities.deye.enter_export is CapabilityLevel.VERIFIED
    assert capabilities.deye.export_power is CapabilityLevel.UNSUPPORTED
    assert capabilities.deye.charge_power is CapabilityLevel.UNSUPPORTED
    assert capabilities.deye.hold is CapabilityLevel.UNSUPPORTED
