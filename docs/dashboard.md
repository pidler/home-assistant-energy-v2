# ENERGY V2 dashboard

Date: 2026-08-03.

Dashboard YAML is versioned at:

```text
homeassistant/dashboards/energy_v2.yaml
```

It is deployed as the standalone YAML Lovelace dashboard at `/energy-v2` from Phase 2 commit
`0a39a6ca2f0cbe295a61a53f5e64f3c624d5fb32`. The live dashboard exposes all three documented
views and remains read-only.

## Purpose

The dashboard gives a quick read-only view of ENERGY V2 phase 2:

- AppDaemon application state,
- shadow-only safety state,
- current energy flows,
- telemetry inputs,
- grid import/export,
- rolling 15-minute export average,
- flow warnings and violations,
- export-limit diagnostics,
- confirmed system parameters.

Creating the dashboard file in the repository does not control SolaX, DEYE, Grid Charge, PV Charge,
export, legacy automations, or any Home Assistant production state.

## Views

### Přehled

Fast operational overview:

- ENERGY V2 app status, shadow mode, enabled state, safe-to-enable, strategy, requested/actual mode,
- activation blockers and last evaluation error,
- SolaX and DEYE telemetry,
- current energy flows,
- phase 2 flow state, warning and violation,
- instant export and rolling 15-minute export average gauges,
- SOC and estimated energy above minimum SOC,
- basic price/planner diagnostics.

### Diagnostika

Detailed phase 2 diagnostics:

- raw SolaX telemetry,
- raw DEYE telemetry,
- DEYE switch state history as read-only history graph,
- price telemetry,
- sign conventions,
- comparison of grid measurements over one and six hours,
- flow diagnostics,
- export-average diagnostics,
- runtime diagnostics.

### Nastavení

Read-only configuration and safety reference:

- confirmed system parameters,
- flow thresholds,
- strategy status,
- safety invariants,
- admin helper states shown through markdown, not controls.

## Entities used

Telemetry:

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
- `sensor.current_buy_electricity_price_15min`
- `sensor.current_sell_electricity_price_15min`
- `sensor.energy_trading_budouci_poradi_prodejni_ceny`

Read-only switch state history:

- `switch.deye_battery_grid_charging`
- `switch.deye_export_surplus`

ENERGY V2 helpers:

- `input_boolean.energy_v2_enabled`
- `input_boolean.energy_v2_shadow_mode`
- `input_boolean.energy_v2_export_enabled`
- `input_boolean.energy_v2_service_mode`
- `input_boolean.energy_v2_safe_to_enable`
- `input_select.energy_v2_strategy`
- `input_select.energy_v2_requested_mode`
- `input_select.energy_v2_actual_mode`
- `input_select.energy_v2_app_status`
- `input_select.energy_v2_flow_state`
- `input_select.energy_v2_export_limit_state`
- `input_text.energy_v2_last_fault`
- `input_text.energy_v2_last_decision`
- `input_text.energy_v2_active_conflicts`
- `input_text.energy_v2_last_evaluation_error`
- `input_text.energy_v2_flow_summary`
- `input_text.energy_v2_flow_warning`
- `input_text.energy_v2_flow_violation`
- `input_text.energy_v2_export_limit_summary`
- `input_datetime.energy_v2_heartbeat`
- `input_datetime.energy_v2_last_successful_evaluation`
- `input_datetime.energy_v2_last_flow_violation`
- `input_datetime.energy_v2_last_export_average_violation`
- `input_datetime.energy_v2_last_valid_export_sample`
- `input_number.energy_v2_instant_grid_export_w`
- `input_number.energy_v2_rolling_15min_export_w`
- `input_number.energy_v2_export_window_covered_s`
- `input_number.energy_v2_export_sample_age_s`

Legacy master state only:

- `input_boolean.energy_trading_puvodni_reseni_povoleno`
- `input_boolean.energy_trading_novy_system_povolen`

## Read-only design

The dashboard avoids control surfaces for phase 2:

- no service calls,
- no `tap_action`, `hold_action`, or `double_tap_action`,
- no button cards,
- no custom cards,
- no direct actuator entity rows for SolaX or DEYE,
- DEYE switch states are shown only as history and markdown state text,
- ENERGY V2 strategy and admin helpers are shown through markdown state text.

The dashboard cannot intentionally enable ENERGY V2, export, Grid Charge, PV Charge, service mode,
or any inverter control path.

## Standard cards used

- `markdown`
- `entities`
- `gauge`
- `grid`
- `history-graph`

No HACS or custom card dependency is required.

## How to validate before deployment

Run:

```bash
python scripts/verify_dashboard.py
python -m pytest
```

The dashboard verifier checks:

- valid YAML,
- exactly three required views,
- only known entities,
- no service calls,
- no tap/hold/double-tap actions,
- no active actuator cards,
- no custom cards,
- documentation references the dashboard path.

It does not contact Home Assistant.

## Future deployment procedure

Do not update the production dashboard from this branch until PR #2 has completed code review.

For future deployment, use a separate approved change:

1. Copy or mount `homeassistant/dashboards/energy_v2.yaml` into the Home Assistant configuration.
2. Add it as a standalone YAML dashboard in Home Assistant Lovelace configuration.
3. Reload Lovelace/dashboard configuration only if required.
4. Do not restart Home Assistant or AppDaemon unless separately approved.
5. Verify that dashboard cards render and no control surfaces are present.

This repository change does not edit production `configuration.yaml`.

## Confirmed measurement authority

- Main whole-connection grid telemetry: `sensor.solax_measured_power`.
- Sign convention: positive means export, negative means import.
- Phase sensors `sensor.solax_measured_power_l1`, `sensor.solax_measured_power_l2`, and
  `sensor.solax_measured_power_l3` are diagnostic only.
- SolaX battery power uses `sensor.solax_battery_power_charge`.
- DEYE normalized battery power uses `sensor.battery_power_otoceny`.
- Raw DEYE battery power `sensor.deye_battery_power` is diagnostic only and has the opposite sign.

## Rollback / removal

To remove a future deployment:

1. Remove the dashboard reference from the Home Assistant Lovelace dashboard configuration.
2. Leave ENERGY V2 helpers and AppDaemon app untouched unless a separate rollback is requested.
3. Reload Lovelace/dashboard configuration if required.

Removing the dashboard does not affect AppDaemon operation because the dashboard is only a viewer.

## Expected Phase 2 dashboard result

### Běžný správný stav

```text
ENERGY V2: HEALTHY
Mode: SHADOW
Strategy: SUMMER_NO_GRID_CHARGE
Safe to enable: NO - legacy system active

Flow: NORMAL
Grid import: low or transient
Grid export: within target
15min export average: below limit
Physical control: DISABLED
```

### Nežádoucí import

```text
SolaX SOC > 10 %
nebo DEYE SOC > 10 %
trvalý grid import
baterie nepokrývají dům

Flow:
UNEXPECTED_GRID_IMPORT
```

`UNEXPECTED_GRID_IMPORT` is a planned future diagnostic state. The current PR does not yet publish
that classification.

### Přelévání mezi bateriemi

```text
SolaX battery discharging
DEYE battery charging

Flow:
SOLAX_TO_DEYE
```

### Překročení okamžitého exportního cíle

```text
Instant export > 9 800 W
15min average < 10 000 W

State:
EXPORT_INSTANT_ABOVE_TARGET
Warning only
```

### Skutečné porušení exportního limitu

```text
15min time-weighted average > 10 000 W

State:
EXPORT_AVERAGE_LIMIT_VIOLATION
```

Phase 2 still does not perform any physical response.
