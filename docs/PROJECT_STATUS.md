# ENERGY V2 project status

Date: 2026-07-27.

## Current stable state

- `main` contains `v0.1.0-shadow`.
- Phase 1 is a passive AppDaemon shadow controller.
- Phase 1 was validated in production without physical inverter control.

## Current pull request

- PR #2 develops phase 2 summer flow monitoring.
- Phase 2 is not deployed to production.
- Phase 2 remains passive and diagnostic only.

## Phase 2 additions

- confirmed system parameters:
  - SolaX inverter 12,000 W, battery 24 kWh, minimum SOC 10%,
  - DEYE inverter 12,000 W, battery 32 kWh, minimum SOC 10%,
  - permitted grid export 10,000 W evaluated as a 15-minute average,
  - ENERGY V2 operational export target 9,800 W.
- passive current flow classification,
- passive export-limit diagnostics,
- time-weighted rolling 15-minute average export calculation.

## Not implemented

- physical SolaX control,
- physical DEYE control,
- Grid Charge,
- active PV Charge,
- export regulator,
- ledger calculation or ledger writes,
- changes to legacy `energy_trading_*` entities.
