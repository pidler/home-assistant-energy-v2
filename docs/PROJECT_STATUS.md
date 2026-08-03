# ENERGY V2 project status

Date: 2026-08-03.

## Current stable state

- `main` contains `v0.1.0-shadow`.
- Phase 1 is a passive AppDaemon shadow controller.
- Phase 1 was validated in production without physical inverter control.

## Current pull request

- PR #2 develops phase 2 summer flow monitoring.
- Phase 2 was passively deployed to production from `0a39a6ca2f0cbe295a61a53f5e64f3c624d5fb32` on 2026-08-03.
- The deployment is running, but remains **DEPLOYED, not yet VALIDATED** by multi-day observation.
- Phase 2 remains passive and diagnostic only.
- The production dashboard was deployed separately as a read-only review surface from an earlier
  PR #2 revision; this branch does not update it.

## Phase 2 additions

- confirmed system parameters:
  - SolaX inverter 12,000 W, battery 24 kWh, minimum SOC 10%,
  - DEYE inverter 12,000 W, battery 32 kWh, minimum SOC 10%,
  - permitted grid export 10,000 W evaluated as a 15-minute average,
  - ENERGY V2 operational export target 9,800 W.
- passive current flow classification,
- passive export-limit diagnostics,
- time-weighted rolling 15-minute average export calculation.
- read-only Lovelace dashboard YAML in `homeassistant/dashboards/energy_v2.yaml`.
- hardening update:
  - `sensor.solax_measured_power` is the primary whole-connection grid authority,
  - L1/L2/L3 measured power sensors are phase diagnostics only,
  - DEYE battery power uses existing normalized `sensor.battery_power_otoceny`,
  - flow monitoring runs on an independent 5 s tick,
  - planner remains on the 15 min shadow interval,
  - event counters use edge detection rather than incrementing on every tick.

## Phase 3 development

Status: **IMPLEMENTED** in `feature/phase-3-deye-charge-shadow`; **NOT DEPLOYED** and **NOT VALIDATED**.

Phase 3 adds a pure shadow state machine for a proposed DEYE charge-current limit from SolaX PV
surplus. It only publishes diagnostics and mode mismatches. Active charging, sale, and discharge
remain **DEFERRED**.

The versioned read-only dashboard is deployed at `/energy-v2` with the `prehled`,
`diagnostika`, and `nastaveni` views.

## Not implemented

- physical SolaX control,
- physical DEYE control,
- Grid Charge,
- active PV Charge,
- export regulator,
- ledger calculation or ledger writes,
- changes to legacy `energy_trading_*` entities.
