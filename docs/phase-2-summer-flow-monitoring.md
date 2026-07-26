# ENERGY V2 phase 2 - summer flow monitoring

Date: 2026-07-26.

Phase 2 extends the passive AppDaemon shadow controller with diagnostics for current physical
energy flows. It is still shadow-only and does not control SolaX, DEYE, legacy automations, or any
physical actuator.

## Purpose of the summer strategy

`SUMMER_NO_GRID_CHARGE` is the only implemented strategy in this phase.

The summer rule is:

- no active battery charging from the grid,
- future DEYE charging may be allowed only when current telemetry indicates real PV surplus,
- SolaX battery must not discharge into DEYE,
- when one battery exports, the other battery must not absorb that exported energy.

`WINTER_GRID_OPTIMIZATION` and `SERVICE` are accepted helper values, but they are not implemented
in phase 2. When selected, the planner remains passive and returns `DISABLED`.

This phase does not track the historical origin of energy already stored in either battery. It
only evaluates current measured power flows.

## Sign conventions

The conventions are represented by `SignConventions` in one place:

- `sensor.solax_battery_power_charge`: positive means SolaX battery charging, negative means
  SolaX battery discharging.
- `sensor.deye_battery_power`: positive means DEYE battery discharging, negative means DEYE
  battery charging.
- `sensor.deye_grid_power`: positive means grid import, negative means grid export.

Pure conversion helpers always return non-negative magnitudes:

- `solax_battery_charging_power_w`
- `solax_battery_discharging_power_w`
- `deye_battery_charging_power_w`
- `deye_battery_discharging_power_w`
- `grid_import_power_w`
- `grid_export_power_w`

## Flow snapshot

`derive_flow_snapshot(...)` produces:

- grid import/export,
- SolaX battery charge/discharge,
- DEYE battery charge/discharge,
- SolaX PV power,
- house load.

All values are watts and are non-negative magnitudes.

## Flow states

- `UNKNOWN`: telemetry is not valid enough to assess flows.
- `NORMAL`: no monitored warning or violation pattern is present.
- `GRID_IMPORT`: grid import is above the warning threshold.
- `GRID_EXPORT`: grid export is above the minimum export threshold.
- `LIKELY_PV_SURPLUS_CHARGE`: DEYE appears to charge from current PV surplus.
- `SOLAX_TO_DEYE`: SolaX battery discharges while DEYE charges.
- `DEYE_TO_SOLAX`: DEYE battery discharges while SolaX charges.
- `CROSS_CHARGING`: one battery exports while the other battery charges.
- `AMBIGUOUS`: reserved for future cases where telemetry is contradictory but not clearly one of
  the states above.

`LIKELY_PV_SURPLUS_CHARGE` is intentionally probabilistic. It is not an absolute claim about
energy origin.

## Thresholds

Defaults:

- grid import warning: 200 W
- grid import violation: 500 W
- battery flow warning: 300 W
- battery flow violation: 500 W
- minimum DEYE charge: 300 W
- minimum export: 300 W
- PV surplus reserve: 500 W

The values are diagnostics only in this phase.

## Time tolerance and debounce

Immediate samples are not treated as persistent violations.

Defaults:

- warning persistence: 5 s
- violation persistence: 10 s

Short spikes are reported as transients. Persistent states are reported as warnings or violations.
No physical reaction is performed.

## Warning vs violation

Warnings are visible conditions that may be normal during passive operation, for example ordinary
house grid import.

Violations are flow patterns that would be unsafe for a future active controller, for example:

- SolaX battery discharging while DEYE charges,
- DEYE discharging while SolaX charges,
- export from one battery while the other battery charges,
- sustained grid import above the configured violation threshold.

If a flow violation is active, the shadow planner recommends `FAULT`. Because phase 2 keeps
`energy_v2_enabled` off, `requested_mode` and `actual_mode` remain `DISABLED`.

## Diagnostics

New Home Assistant helpers:

- `input_select.energy_v2_strategy`
- `input_select.energy_v2_flow_state`
- `input_text.energy_v2_flow_summary`
- `input_text.energy_v2_flow_warning`
- `input_text.energy_v2_flow_violation`
- `input_datetime.energy_v2_last_flow_violation`

These helpers do not drive any control path.

In-memory counters track transient imports, persistent imports, SolaX-to-DEYE events,
DEYE-to-SolaX events, and rough state durations. They reset when AppDaemon restarts.

## Why phase 2 is still passive

The system has two AC-coupled hybrid inverters. A wrong physical command can create battery
cycling, grid import, or export while another battery charges. Phase 2 therefore only measures and
classifies flows so thresholds and sign conventions can be validated in production before any
active `PV_CHARGE_DEYE` mode exists.

## Production observation target

After code review and passive deployment, observe for several days:

- whether sign conventions match real telemetry,
- frequency and duration of grid import warnings,
- whether DEYE charging can be reliably distinguished from PV surplus charging,
- whether legacy automations create `SOLAX_TO_DEYE`, `DEYE_TO_SOLAX`, or `CROSS_CHARGING`,
- whether diagnostic text is actionable and not too noisy.

## Conditions before active PV_CHARGE_DEYE

Before implementing active `PV_CHARGE_DEYE`, the following must be true:

- production flow states are stable and explainable,
- `LIKELY_PV_SURPLUS_CHARGE` has been validated against real operation,
- grid import and battery cross-charging false positives are understood,
- legacy conflict handling is decided,
- safe-to-enable is not blocked by unknown entities,
- physical actuator write paths are reviewed separately,
- rollback/fault-safe behavior is specified before any command is sent to SolaX or DEYE.
