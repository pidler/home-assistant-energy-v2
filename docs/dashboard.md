# ENERGY V2 dashboard

The versioned dashboard is stored at `homeassistant/dashboards/energy_v2.yaml`.
It is a repository-only, read-only redesign of the standalone YAML Lovelace
dashboard served at `/energy-v2`. Updating this file does not deploy or reload
the production Home Assistant dashboard.

## Design goals

- show the essential operating state on the overview without debug noise;
- use the ENERGY V2 whole-site load instead of the SolaX-only house-load value;
- keep prices, battery telemetry and export limits easy to scan;
- separate Phase 5A from the older simple shadow decision;
- keep detailed flow, export-budget and Phase 3/4 internals in diagnostics;
- expose physical actuator values only through read-only Markdown or history;
- use only standard Home Assistant cards and existing audited entities.

## Views

### Přehled

The overview contains one consolidated status/blocker card, prominent PV,
whole-site load and PCC power, both battery summaries, a below-floor SOC
warning derived from the existing DEYE SOC, buy/sell prices, a clear Phase 5A
runtime placeholder and compact export indicators.

`input_number.energy_v2_whole_site_load_w` is the authoritative overview load.
`sensor.solax_house_load` is retained only as raw diagnostic telemetry because
it does not represent the complete AC-coupled site.

### Trading / Planner

This view contains the available price inputs and clearly labels
`input_text.energy_v2_last_decision` as the old simple shadow planner. Phase 5A
current/next action, horizon, freshness, trajectories, economic value, reserve,
below-floor recovery and DEYE power-state fields show `NOT CONNECTED` rather
than referencing invented entities.

### Baterie

This view separates normalised and raw DEYE power, shows connection and device
state, exposes actuator/current-limit values read-only, and shows SolaX SOC,
battery power, PV and inverter AC power. A 12-hour standard history graph and
the instantaneous energy-above-floor estimate support review.

### Grid / Export

`sensor.solax_measured_power` remains the authoritative PCC measurement, with
positive values meaning export. One consolidated six-hour graph replaces the
duplicated one-hour/six-hour grid comparisons. Rolling and fixed-quarter
diagnostics retain the 9.8 kW operational target and 10 kW legal 15-minute
limit distinction.

### Diagnostika

Runtime heartbeat/evaluation, source quality, raw telemetry, flow diagnostics,
Phase 4 shadow commands, anti-transfer, legacy conflicts, Phase 3 inactive
diagnostics, sign conventions and read-only actuator history live here.

### Pokročilé

This view is read-only and documents system parameters, thresholds, strategy
and safety invariants. It prominently states `PHYSICAL CONTROL NOT READY`.

## Phase 5A runtime boundary

Phase 5A trading and DEYE power-state modules are pure repository computation.
They do not currently publish Home Assistant entities or run on an AppDaemon
scheduler. The dashboard therefore does not claim a current action, next
action, horizon, SOC trajectory, below-floor recovery status or power-state
schedule. Explicit placeholders reserve the intended locations without
fabricating runtime state.

## Read-only safety

The dashboard contains no:

- service calls;
- button cards;
- tap, hold or double-tap actions;
- editable input helper rows;
- direct SolaX or DEYE controls;
- custom/HACS cards.

The following physical entities are displayed only as Markdown state or
history and cannot be changed by this dashboard:

- `number.deye_battery_max_charging_current`;
- `switch.deye_battery_grid_charging`;
- `switch.deye_export_surplus`.

Legacy master states and active conflicts remain visible because they are
safety interlocks, not because the dashboard controls them.

## Validation

Run:

```bash
python scripts/verify_dashboard.py
python -m pytest
```

The verifier enforces the six-view structure, approved standard card types,
known entity references, read-only actuator placement and absence of service or
interaction actions.
