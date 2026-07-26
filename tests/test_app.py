from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

from apps.energy_v2.config import DEFAULT_CONFLICTING_AUTOMATIONS, ENTITY_IDS, OPTIONAL_TELEMETRY_KEYS, OWNED_ACTUATORS


class StubHass:
    def __init__(self) -> None:
        self.args: dict[str, Any] = {}
        self.states: dict[str, Any] = {}
        self.services: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str]] = []
        self.listeners: list[str] = []
        self.timers: list[tuple[str, int | str]] = []
        self.turned_off: list[str] = []

    def get_state(self, entity_id: str, **kwargs: Any) -> Any:
        if kwargs.get("attribute") == "all":
            if entity_id not in self.states:
                return None
            return {"state": self.states[entity_id]}
        return self.states.get(entity_id)

    def call_service(self, service: str, **kwargs: Any) -> None:
        self.services.append((service, kwargs))
        entity_id = kwargs["entity_id"]
        if "option" in kwargs:
            self.states[entity_id] = kwargs["option"]
        elif "value" in kwargs:
            self.states[entity_id] = kwargs["value"]
        elif "datetime" in kwargs:
            self.states[entity_id] = kwargs["datetime"]

    def turn_off(self, entity_id: str) -> None:
        self.turned_off.append(entity_id)
        self.states[entity_id] = "off"

    def listen_state(self, callback: object, entity_id: str) -> None:
        self.listeners.append(entity_id)

    def run_every(self, callback: object, start: str, interval: int) -> tuple[str, int]:
        self.timers.append(("every", interval))
        return ("every", interval)

    def run_in(self, callback: object, delay: int) -> tuple[str, int]:
        self.timers.append(("in", delay))
        return ("in", delay)

    def cancel_timer(self, handle: object) -> None:
        self.timers.append(("cancel", 0))

    def log(self, message: str, *args: Any, level: str = "INFO") -> None:
        self.logs.append((level, message % args if args else message))


def install_appdaemon_stub() -> None:
    appdaemon = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass_pkg = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = StubHass
    appdaemon.plugins = plugins
    plugins.hass = hass_pkg
    hass_pkg.hassapi = hassapi
    sys.modules["appdaemon"] = appdaemon
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hass_pkg
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi


def import_app_module():
    install_appdaemon_stub()
    apps_dir = str(Path(__file__).resolve().parents[1] / "apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    for module_name in tuple(sys.modules):
        if module_name == "energy_v2" or module_name.startswith("energy_v2."):
            sys.modules.pop(module_name)
    return importlib.import_module("energy_v2.app")


def valid_states() -> dict[str, Any]:
    states = {entity_id: "off" for entity_id in ENTITY_IDS.values()}
    states.update(
        {
            ENTITY_IDS["solax_soc"]: "96",
            ENTITY_IDS["solax_battery_power"]: "0",
            ENTITY_IDS["solax_pv_power"]: "5000",
            ENTITY_IDS["solax_house_load"]: "1000",
            ENTITY_IDS["solax_grid_import"]: "0",
            ENTITY_IDS["solax_grid_export"]: "0",
            ENTITY_IDS["deye_soc"]: "50",
            ENTITY_IDS["deye_battery_power"]: "0",
            ENTITY_IDS["deye_battery_state"]: "idle",
            ENTITY_IDS["deye_grid_power"]: "0",
            ENTITY_IDS["deye_external_power"]: "0",
            ENTITY_IDS["deye_device_state"]: "Normal",
            ENTITY_IDS["deye_connection"]: "on",
            ENTITY_IDS["buy_price"]: "2",
            ENTITY_IDS["sell_price"]: "3.5",
            ENTITY_IDS["future_sell_rank"]: "5",
            ENTITY_IDS["deye_grid_charging"]: "off",
            ENTITY_IDS["deye_export_surplus"]: "off",
            ENTITY_IDS["legacy_enabled"]: "off",
            ENTITY_IDS["current_energy_trading_enabled"]: "off",
            ENTITY_IDS["energy_v2_enabled"]: "on",
            ENTITY_IDS["energy_v2_shadow_mode"]: "on",
            ENTITY_IDS["energy_v2_export_enabled"]: "on",
            ENTITY_IDS["energy_v2_service_mode"]: "off",
            ENTITY_IDS["energy_v2_deye_fv_ledger"]: "1",
            ENTITY_IDS["energy_v2_solax_fv_ledger"]: "0",
        }
    )
    states.update({entity_id: "off" for entity_id in DEFAULT_CONFLICTING_AUTOMATIONS})
    states.update({entity_id: "present" for entity_id in OWNED_ACTUATORS})
    return states


def helper_value(app: StubHass, key: str) -> Any:
    return app.states[ENTITY_IDS[key]]


def test_stubbed_appdaemon_import_matches_appdaemon_module_config() -> None:
    module = import_app_module()
    assert hasattr(module, "EnergyV2App")
    assert module.__name__ == "energy_v2.app"


def test_shadow_mode_off_disables_requested_and_actual_mode() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_shadow_mode"]] = "off"
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "DISABLED"
    assert helper_value(app, "energy_v2_actual_mode") == "DISABLED"
    assert "no trading recommendation" in helper_value(app, "energy_v2_last_decision")


def test_missing_required_entity_blocks_enable() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    del app.states[ENTITY_IDS["solax_soc"]]
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "FAULT"
    assert helper_value(app, "energy_v2_app_status") == "CONFIG_ERROR"
    assert app.turned_off == [ENTITY_IDS["energy_v2_enabled"]]
    assert "Required entities are missing" in helper_value(app, "energy_v2_last_evaluation_error")


def test_missing_conflicting_automation_is_diagnostic_and_blocks_enable() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    del app.states[DEFAULT_CONFLICTING_AUTOMATIONS[0]]
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "FAULT"
    assert "missing_conflict:" in helper_value(app, "energy_v2_active_conflicts")
    assert "Conflicting automations are missing" in helper_value(app, "energy_v2_last_evaluation_error")


def test_missing_optional_entity_is_diagnostic_but_not_blocking() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    for key in OPTIONAL_TELEMETRY_KEYS:
        del app.states[ENTITY_IDS[key]]
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_app_status") == "HEALTHY"
    assert "missing_optional:" in helper_value(app, "energy_v2_active_conflicts")


def test_shadow_tick_failure_keeps_process_heartbeat() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.initialize()

    def fail_snapshot() -> object:
        raise RuntimeError("boom")

    app.telemetry.snapshot = fail_snapshot
    app._shadow_tick()
    app._heartbeat_tick()

    assert helper_value(app, "energy_v2_app_status") == "DEGRADED"
    assert "boom" in helper_value(app, "energy_v2_last_evaluation_error")
    assert helper_value(app, "energy_v2_heartbeat")


def test_successful_shadow_tick_sets_last_successful_evaluation_time() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_app_status") == "HEALTHY"
    assert helper_value(app, "energy_v2_last_successful_evaluation")
