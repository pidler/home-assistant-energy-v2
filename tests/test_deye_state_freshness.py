from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v2.config import ENTITY_IDS
from apps.energy_v2.deye_state import DeyeAssessment, DeyeStateAdapter, DeyeStateConfig
from apps.energy_v2.deye_state import DeyeOperatingState as State
from apps.energy_v2.models import BatteryId, TelemetryQuality
from apps.energy_v2.observations import observation_time
from apps.energy_v2.safety import validate_telemetry
from apps.energy_v2.shadow_controller import ShadowControlCore
from apps.energy_v2.telemetry import TelemetryReader
from tests.test_safety import snapshot as safety_snapshot
from tests.test_shadow_controller import availability, command
from tests.test_telemetry import MappingReader, control_states

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def reported(value, now=NOW, changed=None):
    changed = changed or now - timedelta(hours=2)
    return {
        "state": str(value),
        "last_changed": changed.isoformat(),
        "last_updated": changed.isoformat(),
        "last_reported": now.isoformat(),
    }


def feedback(now=NOW, switch="off", state="Fault", fault="Tz_Integ_Fault failure", power=0, changed=None):
    return {
        key: reported(value, now, changed)
        for key, value in {"switch": switch, "connection": "on", "state": state, "fault": fault, "power": power}.items()
    }


def test_unchanged_freshly_reported_off_is_confirmed():
    result = DeyeStateAdapter().evaluate(feedback(), NOW)
    assert result.state is State.INTENTIONAL_OFF
    assert not result.dispatch_ready


@pytest.mark.parametrize("bad", ["off", "unknown", "unavailable"])
def test_communication_loss_while_off(bad):
    data = feedback()
    data["connection"] = reported(bad)
    result = DeyeStateAdapter().evaluate(data, NOW)
    assert result.state is State.UNAVAILABLE
    assert not result.dispatch_ready


def test_shutdown_requires_measured_settling():
    adapter = DeyeStateAdapter()
    assert adapter.evaluate(feedback(power=100), NOW).state is State.STOPPING
    at = NOW + timedelta(seconds=1)
    data = feedback(at)
    data["power"] = reported(0, at, at)
    assert adapter.evaluate(data, at).state is State.STOPPING
    for seconds, expected in ((10, State.STOPPING), (21, State.INTENTIONAL_OFF)):
        now = NOW + timedelta(seconds=seconds)
        data = feedback(now)
        data["power"] = reported(0, now, at)
        assert adapter.evaluate(data, now).state is expected


def test_off_on_temporary_fault_then_normal_ready():
    adapter = DeyeStateAdapter(DeyeStateConfig(startup_window_s=180))
    assert adapter.evaluate(feedback(), NOW).state is State.INTENTIONAL_OFF
    on = NOW + timedelta(seconds=1)
    assert adapter.evaluate(feedback(on, switch="on", changed=on), on).state is State.STARTING
    now = on + timedelta(seconds=120)
    data = feedback(now, switch="on", changed=on)
    assert adapter.evaluate(data, now).state is State.STARTING
    normal_at = now + timedelta(seconds=1)
    data = feedback(normal_at, switch="on", state="Normal", fault="OK", changed=on)
    data["state"] = reported("Normal", normal_at, normal_at)
    data["fault"] = reported("OK", normal_at, normal_at)
    assert adapter.evaluate(data, normal_at).state is State.STARTING
    now = normal_at + timedelta(seconds=10)
    for raw in data.values():
        raw["last_reported"] = now.isoformat()
    result = adapter.evaluate(data, now)
    assert result.state is State.READY and result.dispatch_ready


def test_persistent_fault_expires_startup_window():
    adapter = DeyeStateAdapter(DeyeStateConfig(startup_window_s=45))
    assert adapter.evaluate(feedback(switch="on", changed=NOW), NOW).state is State.STARTING
    later = NOW + timedelta(seconds=45)
    assert adapter.evaluate(feedback(later, switch="on", changed=NOW), later).state is State.UNEXPECTED_FAULT


def test_restart_does_not_restart_startup_allowance():
    result = DeyeStateAdapter().evaluate(feedback(switch="on"), NOW)
    assert result.state is State.UNEXPECTED_FAULT


def test_fault_after_ready_has_no_startup_grace():
    adapter = DeyeStateAdapter()
    on_at = NOW - timedelta(seconds=20)
    data = feedback(switch="on", state="Normal", fault="OK", changed=on_at)
    assert adapter.evaluate(data, NOW).state is State.READY
    now = NOW + timedelta(seconds=1)
    assert adapter.evaluate(feedback(now, switch="on", changed=on_at), now).state is State.UNEXPECTED_FAULT


@pytest.mark.parametrize("key", ["switch", "connection", "power", "state", "fault"])
def test_stale_feedback_is_not_an_off_confirmation(key):
    data = feedback()
    data[key]["last_reported"] = (NOW - timedelta(seconds=31)).isoformat()
    assert DeyeStateAdapter().evaluate(data, NOW).state is State.UNAVAILABLE


def test_nonfinite_off_power_cannot_be_assumed_zero():
    assert DeyeStateAdapter().evaluate(feedback(power="nan"), NOW).state is State.UNAVAILABLE


def test_repeated_cached_reads_do_not_complete_settling():
    adapter = DeyeStateAdapter()
    data = feedback(changed=NOW)
    assert adapter.evaluate(data, NOW).state is State.STOPPING
    assert adapter.evaluate(data, NOW + timedelta(seconds=25)).state is State.STOPPING


def test_unrecognized_fault_while_off_is_not_hidden():
    result = DeyeStateAdapter().evaluate(feedback(fault="Temperature is too high"), NOW)
    assert result.state is State.UNEXPECTED_FAULT


@pytest.mark.parametrize("stamp", ["bad", "2026-09-10T12:00:01+00:00", "2026-09-10T12:00:00"])
def test_invalid_report_timestamp_fails_closed(stamp):
    data = reported(0)
    data["last_reported"] = stamp
    assert observation_time(data, NOW) is None


def test_scalar_cache_read_cannot_fabricate_observation():
    reader = TelemetryReader(MappingReader({"sensor.test": "0"}), {"power": "sensor.test"})
    assert not reader.numeric_sample("power", NOW, 30).fresh


def runtime_states():
    states = control_states()
    states.update(
        {
            ENTITY_IDS[key]: reported(value)
            for key, value in {
                "deye_switch": "off",
                "deye_device_state": "Fault",
                "deye_device_fault": "Tz_Integ_Fault failure",
                "deye_connection": "on",
                "deye_inverter_power": 0,
                "deye_battery_power_raw": 6,
                "deye_battery_power": -6,
                "solax_inverter_power": 315,
                "solax_measured_power": 0,
            }.items()
        }
    )
    return states


def evaluate(states):
    telemetry = TelemetryReader(MappingReader(states)).control_snapshot(now=NOW)
    site_command = replace(command(), created_at=NOW, expires_at=NOW + timedelta(seconds=30))
    result = ShadowControlCore().evaluate(
        telemetry, site_command, deye=availability(BatteryId.DEYE), solax=availability(BatteryId.SOLAX)
    )
    return telemetry, result


def test_valid_load_off_and_deye_never_dispatches():
    telemetry, result = evaluate(runtime_states())
    assert telemetry.deye_operating.state is State.INTENTIONAL_OFF
    assert telemetry.deye_inverter_power.effective_fresh
    assert telemetry.deye_inverter_power.value_changed_at == NOW - timedelta(hours=2)
    assert telemetry.deye_inverter_power.observed_at == NOW
    assert telemetry.deye_inverter_power.source_health_at == NOW
    assert result.load.quality is TelemetryQuality.VALID
    assert result.load.load_w == 315
    assert result.allocation.deye.target_power_w == 0


def test_no_zero_substitution_without_power_report():
    states = runtime_states()
    states[ENTITY_IDS["deye_inverter_power"]].pop("last_reported")
    telemetry, result = evaluate(states)
    assert not telemetry.deye_inverter_power.effective_fresh
    assert result.load.load_w is None


def test_off_load_invalid_during_communication_loss():
    states = runtime_states()
    states[ENTITY_IDS["deye_connection"]] = reported("off")
    _, result = evaluate(states)
    assert result.load.load_w is None


def test_off_load_invalid_while_stopping():
    states = runtime_states()
    states[ENTITY_IDS["deye_inverter_power"]] = reported(100)
    _, result = evaluate(states)
    assert result.load.load_w is None


def test_matching_stale_derived_power_uses_fresh_source():
    states = runtime_states()
    states[ENTITY_IDS["deye_battery_power"]].pop("last_reported")
    telemetry, _ = evaluate(states)
    assert telemetry.deye_battery_power.effective_fresh
    assert telemetry.deye_battery_power.observed_at == NOW
    assert telemetry.deye_battery_power.value == -6


def test_disagreeing_stale_derived_power_is_invalid():
    states = runtime_states()
    states[ENTITY_IDS["deye_battery_power"]] = reported(-100)
    telemetry, _ = evaluate(states)
    assert telemetry.deye_battery_power.quality is TelemetryQuality.INVALID


def test_stale_underlying_source_cannot_be_refreshed_by_derived_report():
    states = runtime_states()
    states[ENTITY_IDS["deye_battery_power_raw"]].pop("last_reported")
    telemetry, _ = evaluate(states)
    assert not telemetry.deye_battery_power.effective_fresh


def test_passive_off_exception_does_not_weaken_strict_validation():
    assessed = DeyeStateAdapter().evaluate(feedback(), NOW)
    snap = replace(safety_snapshot(), deye_device_state="Fault", deye_operating=assessed)
    assert validate_telemetry(snap, passive=True).valid
    assert not validate_telemetry(snap).valid


@pytest.mark.parametrize("state", [State.STARTING, State.STOPPING, State.UNAVAILABLE, State.UNEXPECTED_FAULT])
def test_nonready_never_passes_strict_validation(state):
    snap = replace(safety_snapshot(), deye_operating=DeyeAssessment(state, "test", NOW))
    assert not validate_telemetry(snap).valid


@pytest.mark.parametrize(
    "kwargs", [{"startup_window_s": 0}, {"feedback_max_age_s": float("nan")}, {"ready_confirmation_s": 500}]
)
def test_invalid_adapter_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        DeyeStateConfig(**kwargs)


def test_fresh_ha_reports_do_not_hide_stale_connection_source_timestamp():
    data = feedback()
    data["connection"]["attributes"] = {"timestamp": (NOW - timedelta(minutes=5)).timestamp()}
    assert DeyeStateAdapter().evaluate(data, NOW).state is State.UNAVAILABLE


def test_incoherent_feedback_is_unavailable():
    data = feedback()
    data["switch"]["last_reported"] = (NOW - timedelta(seconds=25)).isoformat()
    assert DeyeStateAdapter().evaluate(data, NOW).state is State.UNAVAILABLE


def test_reconnect_requires_new_settling_evidence():
    adapter = DeyeStateAdapter()
    assert adapter.evaluate(feedback(), NOW).state is State.INTENTIONAL_OFF
    data = feedback(NOW + timedelta(seconds=1))
    data["connection"]["state"] = "off"
    assert adapter.evaluate(data, NOW + timedelta(seconds=1)).state is State.UNAVAILABLE
    # Same switch transition after reconnect: cannot reuse pre-disconnect settling.
    data = feedback(NOW + timedelta(seconds=2))
    data["switch"]["last_changed"] = (NOW - timedelta(hours=2)).isoformat()
    assert adapter.evaluate(data, NOW + timedelta(seconds=2)).state is State.STOPPING
