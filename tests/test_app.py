from __future__ import annotations

import importlib
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from apps.energy_v2.config import DEFAULT_CONFLICTING_AUTOMATIONS, ENTITY_IDS, OPTIONAL_TELEMETRY_KEYS, OWNED_ACTUATORS
from apps.energy_v2.diagnostics import compact_reasons
from apps.energy_v2.models import Mode


class StubHass:
    def __init__(self) -> None:
        self.args: dict[str, Any] = {"charge_shadow": {}}
        self.states: dict[str, Any] = {}
        self.services: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str]] = []
        self.listeners: list[str] = []
        self.timers: list[tuple[str, int | str]] = []
        self.run_every_callbacks: list[tuple[object, int]] = []
        self.turned_off: list[str] = []

    def get_state(self, entity_id: str, **kwargs: Any) -> Any:
        value = self.states.get(entity_id)
        if kwargs.get("attribute") == "all":
            if value is None:
                return None
            if isinstance(value, dict):
                return value
            # Unit-test fallback: real AppDaemon returns a timestamped dict,
            # while scalar stubs deliberately exercise the non-dict path.
            return value
        if isinstance(value, dict):
            return value.get("state")
        return value

    def call_service(self, service: str, **kwargs: Any) -> None:
        self.services.append((service, kwargs))
        entity_id = kwargs["entity_id"]
        if service == "input_boolean/turn_on":
            self.states[entity_id] = "on"
        elif service == "input_boolean/turn_off":
            self.states[entity_id] = "off"
        elif "option" in kwargs:
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
        self.run_every_callbacks.append((callback, interval))
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
            ENTITY_IDS["deye_pv_power"]: "0",
            ENTITY_IDS["solax_house_load"]: "1000",
            ENTITY_IDS["solax_inverter_power"]: "1000",
            ENTITY_IDS["solax_measured_power"]: "0",
            ENTITY_IDS["solax_measured_power_l1"]: "0",
            ENTITY_IDS["solax_measured_power_l2"]: "0",
            ENTITY_IDS["solax_measured_power_l3"]: "0",
            ENTITY_IDS["solax_grid_import"]: "0",
            ENTITY_IDS["solax_grid_export"]: "0",
            ENTITY_IDS["deye_soc"]: "50",
            ENTITY_IDS["deye_inverter_power"]: "0",
            ENTITY_IDS["deye_battery_power"]: "0",
            ENTITY_IDS["deye_battery_power_raw"]: "0",
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
            ENTITY_IDS["energy_v2_strategy"]: "SUMMER_NO_GRID_CHARGE",
            ENTITY_IDS["energy_v2_flow_state"]: "UNKNOWN",
            ENTITY_IDS["energy_v2_flow_summary"]: "",
            ENTITY_IDS["energy_v2_flow_warning"]: "",
            ENTITY_IDS["energy_v2_flow_violation"]: "",
            ENTITY_IDS["energy_v2_instant_grid_export_w"]: "0",
            ENTITY_IDS["energy_v2_rolling_15min_export_w"]: "0",
            ENTITY_IDS["energy_v2_export_window_covered_s"]: "0",
            ENTITY_IDS["energy_v2_export_sample_age_s"]: "0",
            ENTITY_IDS["energy_v2_export_limit_state"]: "UNKNOWN",
            ENTITY_IDS["energy_v2_export_limit_summary"]: "",
            ENTITY_IDS["energy_v2_deye_fv_ledger"]: "1",
            ENTITY_IDS["energy_v2_solax_fv_ledger"]: "0",
            ENTITY_IDS["energy_v2_control_shadow_enabled"]: "on",
        }
    )
    states.update({entity_id: "off" for entity_id in DEFAULT_CONFLICTING_AUTOMATIONS})
    states.update({entity_id: "present" for entity_id in OWNED_ACTUATORS})
    return states


def helper_value(app: StubHass, key: str) -> Any:
    return app.states[ENTITY_IDS[key]]


def test_phase4_control_tick_publishes_helpers_only() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.initialize()
    app.services.clear()

    app._control_tick()

    assert helper_value(app, "energy_v2_load_quality") == "VALID"
    assert helper_value(app, "energy_v2_command_status") in {
        "READY",
        "UNVERIFIED",
        "SATURATED",
        "BREAK_BEFORE_MAKE",
    }
    assert all(service.startswith("input_") for service, _kwargs in app.services)
    assert all(kwargs["entity_id"].startswith("input_") for _service, kwargs in app.services)
    assert all(kwargs["entity_id"] not in OWNED_ACTUATORS for _service, kwargs in app.services)


def test_phase4_requires_both_pv_inputs_and_never_substitutes_zero() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["deye_pv_power"]] = "unavailable"
    app.initialize()

    app._control_tick()

    assert helper_value(app, "energy_v2_command_status") == "FAULT"
    assert "Invalid PV inputs" in helper_value(app, "energy_v2_saturation_reason")


def test_phase4_stale_pv_input_faults_instead_of_becoming_zero() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["deye_pv_power"]] = {
        "state": "0",
        "last_updated": "2000-01-01T00:00:00+00:00",
    }
    app.initialize()

    app._control_tick()

    assert helper_value(app, "energy_v2_command_status") == "FAULT"
    assert "Stale PV inputs" in helper_value(app, "energy_v2_saturation_reason")


def test_phase4_stale_battery_power_is_unverified_for_both_inverters() -> None:
    module = import_app_module()
    for key in ("deye_battery_power", "solax_battery_power"):
        app = module.EnergyV2App()
        app.states = valid_states()
        app.states[ENTITY_IDS[key]] = {
            "state": "0",
            "last_updated": "2000-01-01T00:00:00+00:00",
        }
        app.initialize()

        app._control_tick()

        assert helper_value(app, "energy_v2_command_status") == "UNVERIFIED"
        assert helper_value(app, "energy_v2_anti_transfer_state") == "UNVERIFIED"
        assert "battery-power feedback is not fresh" in helper_value(app, "energy_v2_saturation_reason")


def test_planner_control_and_flow_intervals_are_separate() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.initialize()
    intervals = [interval for _callback, interval in app.run_every_callbacks]
    assert 900 in intervals
    assert intervals.count(5) >= 2


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


def test_missing_owned_actuator_is_diagnostic_and_blocks_enable() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    del app.states[OWNED_ACTUATORS[0]]
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "FAULT"
    assert helper_value(app, "energy_v2_safe_to_enable") == "off"
    assert "missing_actuator:" in helper_value(app, "energy_v2_active_conflicts")
    assert "Owned actuator entities are missing" in helper_value(app, "energy_v2_last_evaluation_error")


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


def test_missing_diagnostic_helper_is_not_written() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    missing_helper = ENTITY_IDS["energy_v2_last_evaluation_error"]
    del app.states[missing_helper]
    app.initialize()

    app._shadow_tick()

    assert all(kwargs["entity_id"] != missing_helper for _service, kwargs in app.services)
    assert any("refusing to write missing helper" in message for _level, message in app.logs)


def test_safe_to_enable_off_when_conflict_active() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[DEFAULT_CONFLICTING_AUTOMATIONS[0]] = "on"
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_safe_to_enable") == "off"
    assert helper_value(app, "energy_v2_requested_mode") == "DISABLED"
    assert helper_value(app, "energy_v2_actual_mode") == "DISABLED"
    assert helper_value(app, "energy_v2_app_status") == "HEALTHY"
    assert helper_value(app, "energy_v2_last_decision") != "FAULT: Telemetry is not valid (confidence=high)"
    assert "Safe-to-enable blocked:" in helper_value(app, "energy_v2_last_evaluation_error")
    assert DEFAULT_CONFLICTING_AUTOMATIONS[0] in helper_value(app, "energy_v2_last_evaluation_error")
    assert "Legacy energy_trading system is enabled" not in helper_value(app, "energy_v2_last_evaluation_error")


def test_safe_to_enable_error_text_is_not_truncated_when_it_fits() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["legacy_enabled"]] = "on"
    app.states[DEFAULT_CONFLICTING_AUTOMATIONS[0]] = "on"
    app.initialize()

    app._shadow_tick()

    error_text = helper_value(app, "energy_v2_last_evaluation_error")
    assert "Conflicting automations are active" in error_text
    assert "Legacy energy_trading system is enabled" in error_text
    assert "+2 total" not in error_text


def test_safe_to_enable_on_with_complete_valid_configuration() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_safe_to_enable") == "on"


def test_mode_strenum_fallback_semantics() -> None:
    assert isinstance(Mode.IDLE, str)
    assert Mode.IDLE == "IDLE"
    assert Mode.IDLE.value == "IDLE"


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


def test_flow_helpers_are_written_on_successful_shadow_tick() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["deye_battery_power"]] = "800"
    app.states[ENTITY_IDS["solax_measured_power"]] = "0"
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_flow_state") == "LIKELY_PV_SURPLUS_CHARGE"
    assert "DEYE batt charge/discharge 800/0 W" in helper_value(app, "energy_v2_flow_summary")


def test_unimplemented_strategy_stays_passive_and_disables_recommendation() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["energy_v2_strategy"]] = "WINTER_GRID_OPTIMIZATION"
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "DISABLED"
    assert helper_value(app, "energy_v2_actual_mode") == "DISABLED"
    assert "not implemented" in helper_value(app, "energy_v2_last_decision")


def test_instant_export_above_legal_limit_does_not_fault_when_average_is_safe() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["solax_measured_power"]] = "11000"
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "DISABLED"
    assert helper_value(app, "energy_v2_actual_mode") == "DISABLED"
    assert helper_value(app, "energy_v2_export_limit_state") == "EXPORT_INSTANT_ABOVE_TARGET"
    assert "Flow violation detected" not in helper_value(app, "energy_v2_last_decision")


def test_system_parameters_load_from_appdaemon_args() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.args = {
        "solax_rated_power_w": 12_500.0,
        "deye_battery_capacity_kwh": 31.5,
        "target_export_limit_w": 9_700.0,
        "legal_export_average_limit_w": 9_900.0,
        "export_average_window_s": 600.0,
    }
    app.states = valid_states()
    app.initialize()

    assert app.system_parameters.solax_rated_power_w == 12_500.0
    assert app.system_parameters.deye_battery_capacity_kwh == 31.5
    assert app.system_parameters.target_export_limit_w == 9_700.0
    assert app.system_parameters.legal_export_average_limit_w == 9_900.0
    assert app.export_average_tracker.window_s == 600.0


def test_flow_thresholds_and_tick_interval_load_from_appdaemon_args() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.args = {
        "flow_tick_interval_s": 7,
        "flow_thresholds": {
            "grid_import_warning_w": 250,
            "grid_import_violation_w": 600,
            "warning_persistence_s": 6,
            "violation_persistence_s": 12,
        },
    }
    app.states = valid_states()
    app.initialize()

    assert app.flow_tick_interval_s == 7
    assert app.flow_thresholds.grid_import_warning_w == 250
    assert app.flow_thresholds.grid_import_violation_w == 600
    assert app.flow_thresholds.warning_persistence_s == 6
    assert app.flow_thresholds.violation_persistence_s == 12
    assert ("every", 7) in app.timers


def test_no_physical_service_paths_are_added_by_phase_2() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["solax_measured_power"]] = "11000"
    app.initialize()

    app._shadow_tick()

    service_names = {service for service, _kwargs in app.services}
    assert service_names <= {
        "input_select/select_option",
        "input_text/set_value",
        "input_datetime/set_datetime",
        "input_boolean/turn_on",
        "input_boolean/turn_off",
        "input_number/set_value",
    }
    assert all("solax" not in kwargs["entity_id"] for _service, kwargs in app.services)
    assert all("deye_" not in kwargs["entity_id"] for _service, kwargs in app.services)


def test_flow_tick_runs_without_planner_or_physical_service_calls() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["solax_measured_power"]] = "-700"
    app.initialize()

    app._flow_tick()

    assert helper_value(app, "energy_v2_flow_state") == "GRID_IMPORT"
    assert helper_value(app, "energy_v2_requested_mode") == "DISABLED"
    service_names = {service for service, _kwargs in app.services}
    assert service_names <= {
        "input_select/select_option",
        "input_text/set_value",
        "input_datetime/set_datetime",
        "input_boolean/turn_on",
        "input_boolean/turn_off",
        "input_number/set_value",
    }
    assert all("solax" not in kwargs["entity_id"] for _service, kwargs in app.services)
    assert all("deye_" not in kwargs["entity_id"] for _service, kwargs in app.services)


def test_charge_shadow_publishes_helpers_only_and_never_calls_physical_service() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_charge_shadow_enabled"]] = "on"
    app.states[ENTITY_IDS["deye_battery_voltage"]] = "54"
    app.states[ENTITY_IDS["deye_charge_current"]] = "240"
    app.states[ENTITY_IDS["solax_charger_use_mode"]] = "Self Use Mode"
    app.states[ENTITY_IDS["deye_ac_coupling"]] = "Grid"
    app.states[ENTITY_IDS["deye_time_of_use"]] = "Disabled"
    app.states[ENTITY_IDS["deye_work_mode"]] = "Zero Export To CT"
    app.initialize()

    app._flow_tick()

    assert helper_value(app, "energy_v2_charge_shadow_state") == "START_CONFIRMATION"
    assert all(service.startswith("input_") for service, _kwargs in app.services)
    assert all(kwargs["entity_id"].startswith("input_") for _service, kwargs in app.services)


def test_flow_tick_persists_warning_and_violation_without_entity_change() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["solax_measured_power"]] = "-700"
    app.initialize()

    app._flow_tick()
    app.flow_debouncer.grid_import_warning_since -= timedelta(seconds=6)
    app.flow_debouncer.grid_import_violation_since -= timedelta(seconds=11)
    app._flow_tick()

    assert "Grid import is persistent" in helper_value(app, "energy_v2_flow_warning")
    assert "Grid import violation" in helper_value(app, "energy_v2_flow_violation")
    assert app.flow_debouncer.persistent_import_count == 1


def test_rolling_average_uses_solax_measured_power_source() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["solax_measured_power"]] = "5000"
    app.states[ENTITY_IDS["deye_grid_power"]] = "-11000"
    app.initialize()

    app._flow_tick()

    assert helper_value(app, "energy_v2_instant_grid_export_w") == 5000.0


def test_import_on_solax_measured_power_does_not_reduce_export_below_zero() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["solax_measured_power"]] = "-5000"
    app.initialize()

    app._flow_tick()

    assert helper_value(app, "energy_v2_instant_grid_export_w") == 0.0


def test_invalid_config_uses_safe_defaults_and_blocks_enable() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.args = {
        "flow_tick_interval_s": 0,
        "flow_thresholds": {"grid_import_warning_w": 700, "grid_import_violation_w": 500},
    }
    app.states = valid_states()
    app.initialize()

    app._shadow_tick()

    assert app.flow_tick_interval_s == 5
    assert helper_value(app, "energy_v2_safe_to_enable") == "off"
    assert helper_value(app, "energy_v2_actual_mode") == "DISABLED"
    assert "grid_import_violation_w must be >= grid_import_warning_w" in helper_value(
        app, "energy_v2_last_evaluation_error"
    )


def test_invalid_required_safety_telemetry_reports_specific_error() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["solax_soc"]] = "unavailable"
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_requested_mode") == "DISABLED"
    assert "Invalid telemetry:" in helper_value(app, "energy_v2_last_evaluation_error")
    assert "SolaX SOC is missing" in helper_value(app, "energy_v2_last_evaluation_error")


def test_invalid_main_grid_sensor_states_are_invalid_telemetry() -> None:
    module = import_app_module()
    invalid_values = ("unknown", "unavailable", "NaN", "inf", "-inf")

    for invalid_value in invalid_values:
        app = module.EnergyV2App()
        app.states = valid_states()
        app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
        app.states[ENTITY_IDS["solax_measured_power"]] = invalid_value
        app.initialize()

        app._shadow_tick()

        assert "SolaX measured grid power is missing" in helper_value(app, "energy_v2_last_evaluation_error")


def test_phase_measurements_are_loaded_without_sign_change() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["solax_measured_power_l1"]] = "-100"
    app.states[ENTITY_IDS["solax_measured_power_l2"]] = "0"
    app.states[ENTITY_IDS["solax_measured_power_l3"]] = "100"
    app.initialize()

    snapshot = app.telemetry.snapshot()

    assert snapshot.solax_measured_power_l1_w == -100.0
    assert snapshot.solax_measured_power_l2_w == 0.0
    assert snapshot.solax_measured_power_l3_w == 100.0


def test_missing_phase_measurement_does_not_crash_or_block_telemetry() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    del app.states[ENTITY_IDS["solax_measured_power_l2"]]
    app.initialize()

    app._shadow_tick()

    assert helper_value(app, "energy_v2_app_status") == "HEALTHY"


def test_unavailable_optional_future_rank_with_export_disabled_is_not_invalid_telemetry() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_enabled"]] = "off"
    app.states[ENTITY_IDS["energy_v2_export_enabled"]] = "off"
    app.states[ENTITY_IDS["future_sell_rank"]] = "unknown"
    app.initialize()

    app._shadow_tick()

    assert "Invalid telemetry:" not in helper_value(app, "energy_v2_last_evaluation_error")


def test_compact_reasons_is_deterministic_and_truncates_long_output() -> None:
    reasons = tuple(f"reason-{index:02d}-xxxxxxxxxxxxxxxxxxxxxxxx" for index in range(20, 0, -1))

    text = compact_reasons(reasons, max_len=80)

    assert len(text) <= 80
    assert text.startswith("reason-01-")
    assert "+20 total" in text


def test_production_deployment_yaml_initializes_charge_shadow_fail_safe() -> None:
    module = import_app_module()
    deployment = yaml.safe_load((Path(__file__).parents[1] / "deploy/appdaemon/apps/energy_v2.yaml").read_text())
    assert isinstance(deployment, dict)
    assert isinstance(deployment["energy_v2"]["charge_shadow"], dict)
    app = module.EnergyV2App()
    app.args = deployment["energy_v2"]
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_charge_shadow_enabled"]] = "on"
    app.states[ENTITY_IDS["deye_charge_current"]] = {
        "state": "240",
        "attributes": {"min": 0, "max": 350, "step": 1, "mode": "box"},
    }
    app.initialize()
    app._evaluate_charge_shadow()
    assert app.charge_controller is not None
    assert not app._charge_config_errors
    assert app.charge_controller_parameters.maximum_deye_charge_current_a == 240
    assert all(service.split("/", 1)[0].startswith("input_") for service, _ in app.services)


def test_missing_charge_shadow_is_diagnostic_disabled_and_never_physical() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.args = {}
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_charge_shadow_enabled"]] = "on"
    app.initialize()
    app._evaluate_charge_shadow()
    assert "missing required charge_shadow mapping" in app._charge_config_errors
    assert helper_value(app, "energy_v2_charge_shadow_state") == "DISABLED"
    assert helper_value(app, "energy_v2_charge_recommended_state") == "DISABLED"
    assert float(helper_value(app, "energy_v2_recommended_deye_charge_current_a")) == 0
    assert "CONFIG_ERROR" in helper_value(app, "energy_v2_charge_block_reason")
    assert all(service.split("/", 1)[0].startswith("input_") for service, _ in app.services)


def test_recommendation_is_clamped_to_live_entity_maximum() -> None:
    module = import_app_module()
    app = module.EnergyV2App()
    app.args = {"charge_shadow": {"maximum_deye_charge_current_a": 240}}
    app.states = valid_states()
    app.states[ENTITY_IDS["energy_v2_charge_shadow_enabled"]] = "on"
    app.states[ENTITY_IDS["deye_charge_current"]] = {
        "state": "100",
        "attributes": {"min": 0, "max": 100, "step": 1, "mode": "box"},
    }
    app.initialize()
    app._evaluate_charge_shadow()
    assert float(helper_value(app, "energy_v2_recommended_deye_charge_current_a")) <= 100
