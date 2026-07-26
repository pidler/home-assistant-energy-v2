# ENERGY V2 roadmap

## Phase 1 - passive shadow controller

Status: complete and merged to `main`.

- AppDaemon app loads passively.
- Telemetry and safety readiness are diagnosed.
- No physical inverter control exists.

## Phase 2 - summer flow monitoring

Status: in review in PR #2.

- Monitor current SolaX, DEYE, PV, house, and grid flows.
- Monitor aggregate grid export against one shared export budget.
- Calculate time-weighted 15-minute export average.
- Provide a read-only dashboard for status, telemetry, flow diagnostics and export-limit review.
- Keep all outputs diagnostic only.

## Future phase - passive deployment of phase 2

After code review:

- review the dashboard together with the phase 2 AppDaemon changes,
- deploy phase 2 AppDaemon code passively,
- add the dashboard only through a separately approved Home Assistant Lovelace change,
- verify helper creation,
- observe flow states and export average for several days,
- compare diagnostics with real inverter behavior.

## Future phase - active PV_CHARGE_DEYE design

Not started.

Prerequisites:

- confirmed sign conventions in production,
- reliable export-average diagnostics,
- legacy conflict strategy,
- reviewed actuator writes,
- independent watchdog/fault-safe design.

## Future phase - central export regulator

Not implemented in phase 2.

The regulator must:

- work with aggregate grid export for the whole connection,
- prevent independent SolaX and DEYE export budgets,
- never intentionally request more than the 9,800 W operational target,
- protect the 15-minute average independently from economic trading logic.
