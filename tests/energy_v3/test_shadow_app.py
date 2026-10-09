from __future__ import annotations

import importlib
import inspect
import sys
import types
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest


def appdaemon_4_5_13_http_payload(state: Any, attributes: dict[str, Any]) -> dict[str, Any]:
    """Reproduce AppDaemon's clean_http_kwargs() path before its HA REST POST."""

    def clean(value: Any) -> Any:
        if value is True:
            return "true"
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, Mapping):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, Iterable):
            return [clean(item) for item in value]
        return str(value)

    def remove_literals(value: Any) -> Any:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            return {key: remove_literals(item) for key, item in value.items() if item not in (None, False)}
        if isinstance(value, Iterable):
            return [remove_literals(item) for item in value if item not in (None, False)]
        return value

    return remove_literals(clean({"state": state, "attributes": attributes}))


class ShadowStubHass:
    def __init__(self) -> None:
        self.args: dict[str, Any] = {}
        self.states: dict[str, Any] = {}
        self.listeners: list[str] = []
        self.run_every_callbacks: list[tuple[object, int]] = []
        self.run_in_callbacks: dict[object, object] = {}
        self.services: list[object] = []
        self.published: dict[str, dict[str, Any]] = {}
        self.state_snapshots: list[dict[str, Any]] = []
        self.get_state_calls: list[str | None] = []
        self.reject_merged_metadata = False
        self.simulate_appdaemon_http_cleaning = False
        self.http_payloads: list[tuple[str, dict[str, Any]]] = []
        self.fail_set_state_once: set[str] = set()
        self.fail_run_in_once = False

    def get_state(self, entity_id: str | None = None, **_kwargs: Any) -> Any:
        self.get_state_calls.append(entity_id)
        if entity_id is None:
            source = self.state_snapshots.pop(0) if self.state_snapshots else self.states
            return deepcopy(source)
        return deepcopy(self.states.get(entity_id))

    def listen_state(self, _callback: object, entity_id: str) -> None:
        self.listeners.append(entity_id)

    def run_every(self, callback: object, _start: str, interval: int) -> None:
        self.run_every_callbacks.append((callback, interval))

    def run_in(self, callback: object, _delay: float) -> None:
        if self.fail_run_in_once:
            self.fail_run_in_once = False
            raise RuntimeError("simulated scheduling failure")
        self.run_in_callbacks[len(self.run_in_callbacks)] = callback

    def set_state(self, entity_id: str, **kwargs: Any) -> None:
        if entity_id in self.fail_set_state_once:
            self.fail_set_state_once.remove(entity_id)
            raise RuntimeError("simulated publication failure")
        if self.simulate_appdaemon_http_cleaning:
            payload = appdaemon_4_5_13_http_payload(kwargs.get("state"), kwargs.get("attributes", {}))
            self.http_payloads.append((entity_id, payload))
            if "state" not in payload:
                raise RuntimeError("simulated Home Assistant HTTP 400: state omitted by AppDaemon")
        if self.reject_merged_metadata and not kwargs.get("replace", False):
            existing = self.published.get(entity_id, {}).get("attributes", {})
            merged = {**existing, **kwargs.get("attributes", {})}
            if {"last_changed", "last_updated", "last_reported", "context"} & merged.keys():
                raise RuntimeError("simulated Home Assistant HTTP 400")
        self.published[entity_id] = kwargs


def import_shadow_app():
    appdaemon = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass_pkg = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = ShadowStubHass
    appdaemon.plugins = plugins
    plugins.hass = hass_pkg
    hass_pkg.hassapi = hassapi
    sys.modules.update(
        {
            "appdaemon": appdaemon,
            "appdaemon.plugins": plugins,
            "appdaemon.plugins.hass": hass_pkg,
            "appdaemon.plugins.hass.hassapi": hassapi,
        }
    )
    sys.modules.pop("apps.energy_v3.shadow_app", None)
    return importlib.import_module("apps.energy_v3.shadow_app")


def runtime_states(now: datetime):
    from apps.energy_v3.haeo_adapter import (
        DEYE_ACTIVE_POWER,
        OPTIMIZER_HORIZON,
        OPTIMIZER_STATUS,
        SOLAX_ACTIVE_POWER,
    )
    from apps.energy_v3.telemetry_adapter import ENTITY_IDS

    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    boundaries = [start + timedelta(minutes=15 * index) for index in range(193)]
    records: dict[str, Any] = {
        OPTIMIZER_STATUS: {
            "state": "success",
            "attributes": {"last_run": now.isoformat()},
            "last_updated": (now + timedelta(milliseconds=10)).isoformat(),
            "last_reported": (now + timedelta(milliseconds=10)).isoformat(),
        },
        OPTIMIZER_HORIZON: {
            "state": boundaries[0].isoformat(),
            "attributes": {"forecast": [{"time": value.isoformat()} for value in boundaries]},
            "last_updated": (now - timedelta(minutes=12)).isoformat(),
            "last_reported": (now - timedelta(minutes=12)).isoformat(),
        },
        SOLAX_ACTIVE_POWER: {
            "state": "1",
            "attributes": {"forecast": [{"time": value.isoformat(), "value": 1} for value in boundaries[:-1]]},
            "last_updated": (now + timedelta(milliseconds=20)).isoformat(),
            "last_reported": (now + timedelta(milliseconds=20)).isoformat(),
        },
        DEYE_ACTIVE_POWER: {
            "state": "0",
            "attributes": {"forecast": [{"time": value.isoformat(), "value": 0} for value in boundaries[:-1]]},
            "last_updated": (now + timedelta(milliseconds=30)).isoformat(),
            "last_reported": (now + timedelta(milliseconds=30)).isoformat(),
        },
    }
    values = {
        "solax_soc": 95,
        "solax_battery_power": 0,
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
    for key, value in values.items():
        records[ENTITY_IDS[key]] = {
            "state": str(value),
            "attributes": {},
            "last_updated": now.isoformat(),
        }
    return records


def test_runtime_subscribes_and_only_schedules_read_only_evaluation() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}

    app.initialize()

    assert set(app.listeners) == set((*module.HAEO_ENTITY_IDS, *module.TELEMETRY_ENTITY_IDS))
    assert app.run_every_callbacks[0][1] == 30
    assert app.services == []


def test_shadow_publishes_targets_even_when_execution_is_rejected() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.initialize()

    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["solax_target"]]["state"] == "1000.0"
    assert app.published[module.DIAGNOSTIC_ENTITIES["deye_target"]]["state"] == "0.0"
    assert app.published[module.DIAGNOSTIC_ENTITIES["decision"]]["state"] == "RETURN_TO_NORMAL"
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "TELEMETRY_MISSING"
    assert app.published[module.DIAGNOSTIC_ENTITIES["export_authorization"]]["state"] == "missing"
    assert app.published[module.DIAGNOSTIC_ENTITIES["telemetry"]]["attributes"]["manual_legacy_control"] == (
        "observed_not_owned"
    )
    decision_attributes = app.published[module.DIAGNOSTIC_ENTITIES["decision"]]["attributes"]
    assert decision_attributes["haeo_valid"] is True
    assert decision_attributes["telemetry_status"] == "incomplete"
    assert decision_attributes["missing_fault_evidence"] is True
    assert decision_attributes["export_authorization_status"] == "missing"
    assert decision_attributes["hardware_capability_status"] == "unsupported"
    assert decision_attributes["controller_reason"] == "TELEMETRY_MISSING"
    assert (
        app.published[module.DIAGNOSTIC_ENTITIES["telemetry"]]["attributes"]["physical_measurement_freshness_verified"]
        is False
    )
    assert app.services == []


def test_runtime_reads_two_atomic_appdaemon_snapshots() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.initialize()

    app._evaluate()

    assert app.get_state_calls == [None, None]
    assert app.published[module.DIAGNOSTIC_ENTITIES["solax_target"]]["state"] == "1000.0"


def test_haeo_update_between_atomic_snapshots_fails_closed() -> None:
    module = import_shadow_app()
    from apps.energy_v3.haeo_adapter import SOLAX_ACTIVE_POWER

    app = module.EnergyV3ShadowApp()
    app.args = {}
    before = runtime_states(datetime.now(UTC))
    after = deepcopy(before)
    after[SOLAX_ACTIVE_POWER]["attributes"]["forecast"][0]["value"] = 2
    app.state_snapshots = [before, after]
    app.initialize()

    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "HAEO_PUBLICATION_UNCERTAIN"
    assert app.published[module.DIAGNOSTIC_ENTITIES["solax_target"]]["state"] == "unavailable"


def test_rapid_updates_are_coalesced() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.initialize()

    app._input_changed("sensor.a", "state", "1", "2", {})
    app._input_changed("sensor.b", "state", "1", "2", {})

    assert len(app.run_in_callbacks) == 1


def test_listener_scheduling_error_does_not_permanently_stop_evaluation() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.initialize()
    app.fail_run_in_once = True

    app._input_changed("sensor.a", "state", "1", "2", {})
    app._input_changed("sensor.a", "state", "2", "3", {})

    assert app._evaluation_pending is True
    assert len(app.run_in_callbacks) == 1
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "RUNTIME_ERROR"


def test_shadow_source_contains_no_physical_service_call_path() -> None:
    module = import_shadow_app()
    source = inspect.getsource(module.EnergyV3ShadowApp)
    forbidden = ("call_service", "turn_on", "turn_off", "select_option", "write_register")

    assert not any(token in source for token in forbidden)


def test_adapter_exception_publishes_fail_safe_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.initialize()
    monkeypatch.setattr(module, "current_target_from_states", lambda *_args, **_kwargs: 1 / 0)

    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["decision"]]["state"] == "RETURN_TO_NORMAL"
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "RUNTIME_ERROR"
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["attributes"]["runtime_error_type"] == (
        "ZeroDivisionError"
    )


def test_partial_publication_is_replaced_with_fail_safe_diagnostics() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.initialize()
    app.fail_set_state_once.add(module.DIAGNOSTIC_ENTITIES["interval"])

    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["decision"]]["state"] == "RETURN_TO_NORMAL"
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "RUNTIME_ERROR"
    assert app.published[module.DIAGNOSTIC_ENTITIES["telemetry"]]["state"] == "runtime_error"


def test_diagnostic_publication_replaces_stale_home_assistant_metadata() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.reject_merged_metadata = True
    app.published[module.DIAGNOSTIC_ENTITIES["solax_target"]] = {
        "state": "unavailable",
        "attributes": {
            "last_changed": "old",
            "last_updated": "old",
            "last_reported": "old",
            "context": {"id": "old"},
        },
    }
    app.initialize()

    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["solax_target"]]["state"] == "1000.0"
    assert app.published[module.DIAGNOSTIC_ENTITIES["solax_target"]]["replace"] is True
    assert all(payload["replace"] is True for payload in app.published.values())


def test_appdaemon_http_cleaner_reproduces_numeric_zero_omission() -> None:
    payload = appdaemon_4_5_13_http_payload(0.0, {"unit_of_measurement": "W"})

    assert "state" not in payload


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0.0"),
        (526.315789, "526.315789"),
        (-526.315789, "-526.315789"),
        (None, "unavailable"),
    ],
)
def test_power_diagnostics_survive_appdaemon_http_cleaning(value: float | None, expected: str) -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.simulate_appdaemon_http_cleaning = True

    app._set_power("solax_target", value, {"shadow_mode": True, "physical_control": False})

    entity_id, payload = app.http_payloads[-1]
    assert entity_id == module.DIAGNOSTIC_ENTITIES["solax_target"]
    assert payload["state"] == expected
    assert payload["attributes"]["unit_of_measurement"] == "W"
    assert payload["attributes"]["device_class"] == "power"
    assert app.published[entity_id]["replace"] is True


def test_normal_and_fail_safe_diagnostics_always_have_http_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.simulate_appdaemon_http_cleaning = True
    app.initialize()

    app._evaluate()
    normal_payloads = app.http_payloads.copy()
    assert {entity_id for entity_id, _payload in normal_payloads} == set(module.DIAGNOSTIC_ENTITIES.values())
    assert all("state" in payload for _entity_id, payload in normal_payloads)
    assert app.published[module.DIAGNOSTIC_ENTITIES["deye_target"]]["state"] == "0.0"

    app.http_payloads.clear()
    monkeypatch.setattr(module, "current_target_from_states", lambda *_args, **_kwargs: 1 / 0)
    app._evaluate()

    assert {entity_id for entity_id, _payload in app.http_payloads} == set(module.DIAGNOSTIC_ENTITIES.values())
    assert all("state" in payload for _entity_id, payload in app.http_payloads)
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "RUNTIME_ERROR"


def test_runtime_recovers_on_next_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.initialize()
    original = module.current_target_from_states
    failures = iter((True, False))

    def fail_once(*args: Any, **kwargs: Any):
        if next(failures):
            raise RuntimeError("transient")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "current_target_from_states", fail_once)
    app._evaluate()
    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "TELEMETRY_MISSING"
    assert app.published[module.DIAGNOSTIC_ENTITIES["decision"]]["attributes"]["publication_complete"] is True


def test_restart_with_stale_optimizer_data_does_not_reuse_a_decision() -> None:
    module = import_shadow_app()
    app = module.EnergyV3ShadowApp()
    app.args = {}
    app.states = runtime_states(datetime.now(UTC))
    app.states[module.HAEO_ENTITY_IDS[0]]["attributes"]["last_run"] = (
        datetime.now(UTC) - timedelta(minutes=31)
    ).isoformat()
    app.initialize()

    app._evaluate()

    assert app.published[module.DIAGNOSTIC_ENTITIES["decision"]]["state"] == "RETURN_TO_NORMAL"
    assert app.published[module.DIAGNOSTIC_ENTITIES["reason"]]["state"] == "HAEO_OPTIMIZER_STALE"


def test_v3_deployment_template_is_disabled_and_v2_configuration_is_unchanged() -> None:
    root = Path(__file__).resolve().parents[2]
    template = root / "deploy/appdaemon/apps/energy_v3_shadow.yaml.disabled"

    assert template.exists()
    assert not (root / "deploy/appdaemon/apps/energy_v3_shadow.yaml").exists()
    assert (root / "deploy/appdaemon/apps/energy_v2.yaml").exists()
