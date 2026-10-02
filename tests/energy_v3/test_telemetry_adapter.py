from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from apps.energy_v3 import DecisionReason, decide
from apps.energy_v3.telemetry_adapter import ENTITY_IDS, TelemetryStatus, safety_snapshot_from_states

NOW = datetime(2026, 10, 2, 12, 7, tzinfo=UTC)


def record(state: object, *, age_seconds: int = 5):
    return {"state": str(state), "attributes": {}, "last_updated": (NOW - timedelta(seconds=age_seconds)).isoformat()}


def telemetry_states():
    values = {
        "solax_soc": 95,
        "solax_battery_power": 120,
        "solax_inverter_power": 500,
        "solax_mode": "Manual Mode",
        "solax_run_mode": "Normal Mode",
        "deye_soc": 60,
        "deye_battery_power": -57,
        "deye_battery_power_raw": 57,
        "deye_inverter_power": -81,
        "deye_work_mode": "Export First",
        "deye_battery_fault": "off",
        "deye_battery_alarm": "off",
        "deye_device_fault": "OK",
        "pcc_power": -395,
    }
    return {ENTITY_IDS[key]: record(value) for key, value in values.items()}


def test_verified_telemetry_is_normalized_without_using_deye_inverter_sign() -> None:
    result = safety_snapshot_from_states(telemetry_states(), now=NOW)

    assert result.status is TelemetryStatus.INCOMPLETE
    assert result.snapshot.measured_solax_battery_power_w == 120
    assert result.snapshot.measured_deye_battery_power_w == -57
    assert result.snapshot.pcc_export_w == 0
    assert result.snapshot.export_authorization is None
    assert result.snapshot.writer_conflict is False
    assert result.observed_modes["solax_mode"] == "Manual Mode"


def test_positive_pcc_value_is_export() -> None:
    states = telemetry_states()
    states[ENTITY_IDS["pcc_power"]] = record(900)

    assert safety_snapshot_from_states(states, now=NOW).snapshot.pcc_export_w == 900


def test_raw_and_normalized_deye_mismatch_fails_closed() -> None:
    states = telemetry_states()
    states[ENTITY_IDS["deye_battery_power"]] = record(57)
    result = safety_snapshot_from_states(states, now=NOW)

    assert result.status is TelemetryStatus.INVALID
    assert result.snapshot.measured_deye_battery_power_w is None


def test_dynamic_heartbeat_staleness_marks_all_telemetry_stale() -> None:
    states = telemetry_states()
    states[ENTITY_IDS["solax_inverter_power"]] = record(500, age_seconds=61)
    result = safety_snapshot_from_states(states, now=NOW)

    assert result.status is TelemetryStatus.STALE
    assert result.snapshot.telemetry_fresh is False


def test_missing_solax_fault_evidence_is_explicit_and_fail_closed() -> None:
    result = safety_snapshot_from_states(telemetry_states(), now=NOW)

    assert result.snapshot.solax_fault is None
    assert "solax_fault:evidence_missing" in result.issues
    assert result.status is TelemetryStatus.INCOMPLETE
    assert result.freshness_basis == "home_assistant_last_updated"
    assert result.physical_measurement_freshness_verified is False


def test_missing_export_authorization_blocks_discharge_but_not_charge_evaluation() -> None:
    from apps.energy_v3.haeo_adapter import current_target_from_states
    from tests.energy_v3.test_haeo_adapter import schema

    discharge = current_target_from_states(schema(solax=1, deye=0), now=NOW).target
    charge = current_target_from_states(schema(solax=-1, deye=0), now=NOW).target
    snapshot = safety_snapshot_from_states(telemetry_states(), now=NOW).snapshot
    # Supply only the otherwise unavailable SolaX fault evidence to isolate export authorization behavior.
    snapshot = replace(snapshot, solax_fault=False)

    assert discharge is not None
    assert decide(discharge, snapshot, now=NOW).reason is DecisionReason.EXPORT_AUTHORIZATION_MISSING
    assert charge is not None and decide(charge, snapshot, now=NOW).reason is DecisionReason.CAPABILITY_UNSUPPORTED
