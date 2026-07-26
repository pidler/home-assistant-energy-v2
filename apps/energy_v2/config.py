from __future__ import annotations

ENTITY_IDS: dict[str, str] = {
    "solax_soc": "sensor.solax_battery_capacity",
    "solax_battery_power": "sensor.solax_battery_power_charge",
    "solax_pv_power": "sensor.solax_pv_power_total",
    "solax_house_load": "sensor.solax_house_load",
    "solax_grid_import": "sensor.solax_grid_import",
    "solax_grid_export": "sensor.solax_grid_export",
    "deye_soc": "sensor.deye_battery",
    "deye_battery_power": "sensor.deye_battery_power",
    "deye_battery_state": "sensor.deye_battery_state",
    "deye_grid_power": "sensor.deye_grid_power",
    "deye_external_power": "sensor.deye_external_power",
    "deye_device_state": "sensor.deye_device_state",
    "deye_connection": "binary_sensor.deye_connection",
    "buy_price": "sensor.current_buy_electricity_price_15min",
    "sell_price": "sensor.current_sell_electricity_price_15min",
    "future_sell_rank": "sensor.energy_trading_budouci_poradi_prodejni_ceny",
    "deye_grid_charging": "switch.deye_battery_grid_charging",
    "deye_export_surplus": "switch.deye_export_surplus",
    "legacy_enabled": "input_boolean.energy_trading_puvodni_reseni_povoleno",
    "current_energy_trading_enabled": "input_boolean.energy_trading_novy_system_povolen",
    "energy_v2_enabled": "input_boolean.energy_v2_enabled",
    "energy_v2_shadow_mode": "input_boolean.energy_v2_shadow_mode",
    "energy_v2_export_enabled": "input_boolean.energy_v2_export_enabled",
    "energy_v2_service_mode": "input_boolean.energy_v2_service_mode",
    "energy_v2_safe_to_enable": "input_boolean.energy_v2_safe_to_enable",
    "energy_v2_requested_mode": "input_select.energy_v2_requested_mode",
    "energy_v2_actual_mode": "input_select.energy_v2_actual_mode",
    "energy_v2_last_fault": "input_text.energy_v2_last_fault",
    "energy_v2_last_decision": "input_text.energy_v2_last_decision",
    "energy_v2_active_conflicts": "input_text.energy_v2_active_conflicts",
    "energy_v2_heartbeat": "input_datetime.energy_v2_heartbeat",
    "energy_v2_last_successful_evaluation": "input_datetime.energy_v2_last_successful_evaluation",
    "energy_v2_last_evaluation_error": "input_text.energy_v2_last_evaluation_error",
    "energy_v2_app_status": "input_select.energy_v2_app_status",
    "energy_v2_deye_fv_ledger": "input_number.energy_v2_deye_fv_ledger",
    "energy_v2_solax_fv_ledger": "input_number.energy_v2_solax_fv_ledger",
}

REQUIRED_TELEMETRY_KEYS: tuple[str, ...] = (
    "solax_soc",
    "solax_battery_power",
    "solax_pv_power",
    "solax_house_load",
    "solax_grid_import",
    "solax_grid_export",
    "deye_soc",
    "deye_battery_power",
    "deye_battery_state",
    "deye_grid_power",
    "deye_external_power",
    "deye_device_state",
    "deye_connection",
    "buy_price",
    "sell_price",
    "deye_grid_charging",
    "deye_export_surplus",
)

OPTIONAL_TELEMETRY_KEYS: tuple[str, ...] = ("future_sell_rank",)

ENERGY_V2_HELPER_KEYS: tuple[str, ...] = (
    "energy_v2_enabled",
    "energy_v2_shadow_mode",
    "energy_v2_export_enabled",
    "energy_v2_service_mode",
    "energy_v2_safe_to_enable",
    "energy_v2_requested_mode",
    "energy_v2_actual_mode",
    "energy_v2_last_fault",
    "energy_v2_last_decision",
    "energy_v2_active_conflicts",
    "energy_v2_heartbeat",
    "energy_v2_last_successful_evaluation",
    "energy_v2_last_evaluation_error",
    "energy_v2_app_status",
    "energy_v2_deye_fv_ledger",
    "energy_v2_solax_fv_ledger",
)

LEGACY_MASTER_HELPER_KEYS: tuple[str, ...] = (
    "legacy_enabled",
    "current_energy_trading_enabled",
)

SOLAX_BATTERY_CHARGING_POSITIVE = True
DEYE_BATTERY_DISCHARGING_POSITIVE = True
DEYE_GRID_EXPORT_NEGATIVE = True

OWNED_ACTUATORS: tuple[str, ...] = (
    "select.solax_charger_use_mode",
    "select.solax_manual_mode_select",
    "number.solax_battery_discharge_max_current",
    "number.solax_battery_charge_max_current",
    "number.solax_remotecontrol_active_power",
    "number.solax_remotecontrol_autorepeat_duration",
    "select.solax_remotecontrol_power_control",
    "button.solax_remotecontrol_trigger",
    "select.deye_work_mode",
    "select.deye_time_of_use",
    "select.deye_ac_coupling",
    "switch.deye_export_surplus",
    "switch.deye_battery_grid_charging",
    "number.deye_grid_max_export_power",
    "number.deye_export_surplus_power",
    "number.deye_grid_max_import_power",
    "number.deye_battery_grid_charging_current",
)

DEFAULT_CONFLICTING_AUTOMATIONS: tuple[str, ...] = (
    "automation.fve_solax_zpet_do_self_use_po_vybiti_deye",
    "automation.rizeni_solaxu_podle_kalendare_nakup",
    "automation.rizeni_solaxu_podle_kalendare_prodej",
    "automation.deye_rizeni_baterie_dle_ceny_nabijeni_vybijeni",
    "automation.gridcontrol_charge",
    "automation.spust_vybijeno_deye_v_konkretni_cas",
    "automation.vypne_vybijeni_deye_a_zapne_self_use_mod_na_solaxu",
    "automation.zpnout_nabijeni_deye_v_ucity_cas",
)
