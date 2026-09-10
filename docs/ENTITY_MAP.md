# ENERGY V2 entity map

## Existing telemetry inputs

- `sensor.solax_battery_capacity`
- `sensor.solax_battery_power_charge`
- `sensor.solax_pv_power_total`
- `sensor.solax_house_load`
- `sensor.solax_measured_power`
- `sensor.solax_measured_power_l1`
- `sensor.solax_measured_power_l2`
- `sensor.solax_measured_power_l3`
- `sensor.solax_grid_import`
- `sensor.solax_grid_export`
- `sensor.deye_battery`
- `sensor.battery_power_otoceny`
- `sensor.deye_battery_power`
- `sensor.deye_battery_state`
- `sensor.deye_grid_power`
- `sensor.deye_external_power`
- `sensor.deye_device_state`
- `binary_sensor.deye_connection`

Authority notes:

- `sensor.solax_measured_power` is the primary whole-connection grid input.
- `sensor.solax_measured_power_l1/l2/l3` are phase diagnostics only.
- `sensor.battery_power_otoceny` is the primary normalized DEYE battery-power input.
- `sensor.deye_battery_power` is raw DEYE battery power and remains diagnostic only.

## Phase 2 strategy and flow diagnostics

- `input_select.energy_v2_strategy`
- `input_select.energy_v2_flow_state`
- `input_text.energy_v2_flow_summary`
- `input_text.energy_v2_flow_warning`
- `input_text.energy_v2_flow_violation`
- `input_datetime.energy_v2_last_flow_violation`

## Phase 2 export-limit diagnostics

- `input_number.energy_v2_instant_grid_export_w`
- `input_number.energy_v2_rolling_15min_export_w`
- `input_number.energy_v2_export_window_covered_s`
- `input_number.energy_v2_export_sample_age_s`
- `input_select.energy_v2_export_limit_state`
- `input_text.energy_v2_export_limit_summary`
- `input_datetime.energy_v2_last_export_average_violation`
- `input_datetime.energy_v2_last_valid_export_sample`

These entities are diagnostic outputs only. They do not control SolaX, DEYE, Grid Charge, PV Charge
or export.

## Existing safety and runtime helpers

- `input_boolean.energy_v2_enabled`
- `input_boolean.energy_v2_shadow_mode`
- `input_boolean.energy_v2_export_enabled`
- `input_boolean.energy_v2_service_mode`
- `input_boolean.energy_v2_safe_to_enable`
- `input_select.energy_v2_requested_mode`
- `input_select.energy_v2_actual_mode`
- `input_select.energy_v2_app_status`
- `input_text.energy_v2_last_fault`
- `input_text.energy_v2_last_decision`
- `input_text.energy_v2_active_conflicts`
- `input_text.energy_v2_last_evaluation_error`
- `input_datetime.energy_v2_heartbeat`
- `input_datetime.energy_v2_last_successful_evaluation`

## Ledger helpers

- `input_number.energy_v2_deye_fv_ledger`
- `input_number.energy_v2_solax_fv_ledger`

Phase 2 does not write ledger helpers.
