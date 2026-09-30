from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.energy_v2.executor.enums import ExecutionState, FailureReason, PlannerIntentType
from apps.energy_v2.executor.haeo_plan import HaeoPlanAdapter
from apps.energy_v2.executor.physical_commands import (
    CapabilityLevel,
    ControlAuthority,
    DeyeCommandAdapter,
    PhysicalCommandWriter,
    PhysicalExecutionGates,
    SolaxCommandAdapter,
    deye_export_sequence,
    known_normal_restore,
)
from apps.energy_v2.executor.slot_diagnostics import helper_projection
from apps.energy_v2.executor.slot_executor import SlotExecutionSession, SlotSafetyStatus

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 10, 7, 30, tzinfo=UTC)
SLOT_START = NOW.replace(minute=0, second=0)


def payload(
    *,
    generated_at: datetime = NOW - timedelta(seconds=10),
    deye_kw: float = 4.0,
    solax_kw: float = 2.0,
    deye_priority: float = 1.0,
    solax_priority: float = 1.0,
) -> dict[str, object]:
    return {
        "plan_id": "haeo-plan-1",
        "generated_at": generated_at.isoformat(),
        "degraded_inputs": [],
        "slots": [
            {
                "timestamp": SLOT_START.isoformat(),
                "deye_target_kw": deye_kw,
                "solax_target_kw": solax_kw,
                "deye_economic_priority": deye_priority,
                "solax_economic_priority": solax_priority,
            }
        ],
    }


def current_plan(**kwargs):
    result = HaeoPlanAdapter().adapt(payload(**kwargs), NOW)
    assert result.slot is not None
    return result.slot


def test_stale_haeo_plan_is_rejected() -> None:
    result = HaeoPlanAdapter(max_plan_age_s=60).adapt(payload(generated_at=NOW - timedelta(seconds=61)), NOW)
    assert result.slot is None
    assert result.reasons == (FailureReason.HAEO_PLAN_STALE,)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "bad", None])
def test_malformed_haeo_target_is_rejected(value: object) -> None:
    raw = payload()
    raw["slots"][0]["deye_target_kw"] = value  # type: ignore[index]
    result = HaeoPlanAdapter().adapt(raw, NOW)
    assert result.slot is None
    assert result.reasons == (FailureReason.HAEO_PLAN_MALFORMED,)


def test_entering_slot_late_uses_only_remaining_energy() -> None:
    plan = current_plan(deye_kw=4.0, solax_kw=0.0)
    assert plan.budgets["DEYE"].target_energy_kwh == pytest.approx(0.5)


def test_declared_slot_energy_is_prorated_when_entering_late() -> None:
    raw = payload(deye_kw=4.0, solax_kw=0.0)
    raw["slots"][0]["deye_energy_kwh"] = 1.0  # type: ignore[index]
    plan = HaeoPlanAdapter().adapt(raw, NOW).slot
    assert plan is not None
    assert plan.budgets["DEYE"].target_energy_kwh == pytest.approx(0.5)


def test_energy_integration_uses_measured_power_and_stops_at_target() -> None:
    plan = current_plan(deye_kw=0.4, solax_kw=0.0)
    session = SlotExecutionSession()
    session.load_plan(plan, NOW)
    decision = session.decide(NOW, SlotSafetyStatus())
    assert decision.owner == "DEYE"
    session.record_power(NOW + timedelta(seconds=450), solax_power_w=0, deye_power_w=400)
    assert session.accounts["DEYE"].delivered_kwh == pytest.approx(0.05)
    assert session.decide(NOW + timedelta(seconds=450), SlotSafetyStatus()).state is ExecutionState.COMPLETE


def test_opposite_measured_flow_is_not_credited() -> None:
    plan = current_plan(deye_kw=1.0, solax_kw=0.0)
    session = SlotExecutionSession()
    session.load_plan(plan, NOW)
    session.decide(NOW, SlotSafetyStatus())
    session.record_power(NOW + timedelta(seconds=60), solax_power_w=0, deye_power_w=-1000)
    assert session.accounts["DEYE"].delivered_kwh == 0


def test_deye_is_only_equal_value_tie_breaker() -> None:
    plan = current_plan()
    session = SlotExecutionSession()
    session.load_plan(plan, NOW)
    assert session.decide(NOW, SlotSafetyStatus()).owner == "DEYE"


def test_higher_solax_economic_priority_is_not_overridden() -> None:
    plan = current_plan(deye_priority=1.0, solax_priority=2.0)
    session = SlotExecutionSession()
    session.load_plan(plan, NOW)
    decision = session.decide(NOW, SlotSafetyStatus())
    assert decision.owner == "SOLAX"
    assert decision.intent is not None
    assert decision.intent.intent_type is PlannerIntentType.DISCHARGE_SOLAX


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (SlotSafetyStatus(export_limit_clear=False), FailureReason.PCC_EXPORT_LIMIT),
        (SlotSafetyStatus(soc_floor_clear=False), FailureReason.INVALID_INTENT),
        (SlotSafetyStatus(cross_transfer_clear=False), FailureReason.CROSS_BATTERY_FLOW),
        (SlotSafetyStatus(telemetry_fresh=False), FailureReason.STALE_TELEMETRY),
        (SlotSafetyStatus(command_readback_ok=False), FailureReason.READBACK_MISMATCH),
    ],
)
def test_safety_veto_requests_rollback(status: SlotSafetyStatus, reason: FailureReason) -> None:
    session = SlotExecutionSession()
    session.load_plan(current_plan(deye_kw=1, solax_kw=0), NOW)
    decision = session.decide(NOW, status)
    assert decision.state is ExecutionState.ROLLBACK
    assert decision.reason is reason
    assert decision.intent is None


def test_cross_direction_battery_targets_hold_without_intent() -> None:
    session = SlotExecutionSession()
    session.load_plan(current_plan(deye_kw=1, solax_kw=-1), NOW)
    decision = session.decide(NOW, SlotSafetyStatus())
    assert decision.state is ExecutionState.HOLD
    assert decision.reason is FailureReason.CROSS_BATTERY_FLOW
    assert decision.intent is None


class Caller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_service(self, service: str, **kwargs: object) -> None:
        self.calls.append((service, kwargs))


def open_gates(**overrides: object) -> PhysicalExecutionGates:
    values: dict[str, object] = {
        "static_writer_enabled": True,
        "physical_execution_enabled": True,
        "authority": ControlAuthority.ENERGY_V2_EXECUTOR,
        "energy_v2_enabled": True,
        "safe_to_enable": True,
        "export_enabled": True,
        "service_mode": False,
        "telemetry_fresh": True,
        "plan_fresh": True,
        "export_limit_clear": True,
        "soc_floor_clear": True,
        "cross_transfer_clear": True,
        "writer_conflicts_clear": True,
    }
    values.update(overrides)
    return PhysicalExecutionGates(**values)  # type: ignore[arg-type]


def test_physical_gate_defaults_off_and_legacy_mode_cannot_write() -> None:
    caller = Caller()
    writer = PhysicalCommandWriter(caller)
    plan = deye_export_sequence(SolaxCommandAdapter(), DeyeCommandAdapter())
    assert writer.execute(plan, PhysicalExecutionGates()) is False
    assert writer.execute(plan, open_gates(authority=ControlAuthority.LEGACY)) is False
    assert caller.calls == []


def test_every_active_gate_must_pass_before_physical_calls() -> None:
    fields = (
        "static_writer_enabled",
        "physical_execution_enabled",
        "energy_v2_enabled",
        "safe_to_enable",
        "export_enabled",
        "telemetry_fresh",
        "plan_fresh",
        "export_limit_clear",
        "soc_floor_clear",
        "cross_transfer_clear",
        "writer_conflicts_clear",
    )
    plan = deye_export_sequence(SolaxCommandAdapter(), DeyeCommandAdapter())
    for field in fields:
        caller = Caller()
        assert PhysicalCommandWriter(caller).execute(plan, open_gates(**{field: False})) is False
        assert caller.calls == []


def test_verified_deye_export_preserves_proven_sequence() -> None:
    caller = Caller()
    plan = deye_export_sequence(SolaxCommandAdapter(), DeyeCommandAdapter())
    assert PhysicalCommandWriter(caller).execute(plan, open_gates()) is True
    assert [call[1]["entity_id"] for call in caller.calls] == [
        "select.solax_charger_use_mode",
        "select.solax_manual_mode_select",
        "select.deye_ac_coupling",
        "switch.deye_export_surplus",
        "select.deye_time_of_use",
        "select.deye_work_mode",
    ]
    assert all("program" not in str(call).lower() for call in caller.calls)


def test_explicit_owned_rollback_uses_known_normal_sequence_after_disable() -> None:
    caller = Caller()
    restore = known_normal_restore(SolaxCommandAdapter(), DeyeCommandAdapter())
    gates = open_gates(physical_execution_enabled=False, energy_v2_enabled=False, export_enabled=False)
    assert PhysicalCommandWriter(caller).execute(restore, gates, executor_owns_control=True) is True
    assert [call[1]["entity_id"] for call in caller.calls] == [
        "select.solax_charger_use_mode",
        "select.deye_ac_coupling",
        "switch.deye_export_surplus",
        "select.deye_time_of_use",
        "select.deye_work_mode",
    ]


def test_owned_rollback_remains_available_for_stale_or_cross_flow_fault() -> None:
    caller = Caller()
    restore = known_normal_restore(SolaxCommandAdapter(), DeyeCommandAdapter())
    gates = open_gates(
        physical_execution_enabled=False,
        telemetry_fresh=False,
        cross_transfer_clear=False,
        writer_conflicts_clear=False,
    )
    assert PhysicalCommandWriter(caller).execute(restore, gates, executor_owns_control=True) is True
    assert len(caller.calls) == 5


def test_default_deployment_cannot_restore_or_write_without_owned_session() -> None:
    caller = Caller()
    restore = known_normal_restore(SolaxCommandAdapter(), DeyeCommandAdapter())
    assert PhysicalCommandWriter(caller).execute(restore, PhysicalExecutionGates(), executor_owns_control=True) is False
    assert PhysicalCommandWriter(caller).execute(restore, open_gates(physical_execution_enabled=False)) is False
    assert caller.calls == []


def test_unverified_operations_never_contain_calls_or_execute() -> None:
    solax = SolaxCommandAdapter(vpp_experimental_enabled=True)
    deye = DeyeCommandAdapter()
    plans = (solax.export(1000), solax.charge(1000), deye.hold(), deye.charge(1000))
    assert [plan.capability for plan in plans] == [
        CapabilityLevel.EXPERIMENTAL,
        CapabilityLevel.EXPERIMENTAL,
        CapabilityLevel.UNAVAILABLE,
        CapabilityLevel.UNAVAILABLE,
    ]
    caller = Caller()
    writer = PhysicalCommandWriter(caller)
    assert all(not writer.execute(plan, open_gates()) for plan in plans)
    assert caller.calls == []


def test_synchronous_command_failure_is_reported_for_rollback() -> None:
    class FailingCaller(Caller):
        def call_service(self, service: str, **kwargs: object) -> None:
            super().call_service(service, **kwargs)
            raise RuntimeError("write rejected")

    writer = PhysicalCommandWriter(FailingCaller())
    plan = deye_export_sequence(SolaxCommandAdapter(), DeyeCommandAdapter())
    assert writer.execute(plan, open_gates()) is False
    assert writer.last_error == "RuntimeError:write rejected"

    session = SlotExecutionSession()
    session.load_plan(current_plan(deye_kw=1, solax_kw=0), NOW)
    decision = session.request_rollback(FailureReason.COMMAND_TIMEOUT)
    assert decision.state is ExecutionState.ROLLBACK
    assert decision.reason is FailureReason.COMMAND_TIMEOUT


def test_diagnostics_expose_budgets_but_never_write_user_gates() -> None:
    session = SlotExecutionSession()
    session.load_plan(current_plan(deye_kw=1, solax_kw=0), NOW)
    projection = helper_projection(session, PhysicalExecutionGates(), hold_reason="gate closed")
    assert projection["input_select.energy_v2_slot_executor_state"] == "PREPARE"
    assert projection["input_number.energy_v2_haeo_deye_target_kw"] == 1
    assert projection["input_text.energy_v2_slot_hold_reason"] == "gate closed"
    assert "input_boolean.energy_v2_physical_execution_enabled" not in projection
    assert "input_select.energy_v2_execution_authority" not in projection


def test_phase6_writer_is_not_scheduled_by_appdaemon_configuration() -> None:
    config = (ROOT / "apps" / "energy_v2.yaml").read_text(encoding="utf-8")
    assert "physical_commands" not in config
    assert "slot_executor" not in config
