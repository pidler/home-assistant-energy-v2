# ENERGY V2 roadmap

## Phase 1 - passive shadow controller

Status: complete and merged to `main`.

- AppDaemon app loads passively.
- Telemetry and safety readiness are diagnosed.
- No physical inverter control exists.

## Phase 2 - summer flow monitoring

Status: passively deployed from `0a39a6ca2f0cbe295a61a53f5e64f3c624d5fb32`; pending
multi-day production validation in PR #2.

- Monitor current SolaX, DEYE, PV, house, and grid flows.
- Monitor aggregate grid export against one shared export budget.
- Use `sensor.solax_measured_power` as the confirmed whole-connection grid authority.
- Use `sensor.battery_power_otoceny` as the normalized DEYE battery-power input.
- Run passive flow monitoring every 5 s while keeping the economic planner on 15 min cadence.
- Calculate time-weighted 15-minute export average.
- Provide a read-only dashboard for status, telemetry, flow diagnostics and export-limit review.
- Keep all outputs diagnostic only.

## Current work - validate passive Phase 2

- observe flow states, warnings, violations, stale diagnostics, and export average for several days,
- compare diagnostics with real inverter behavior and confirm sign conventions,
- retain the legacy controller as the only physical controller.

## Future phase - active PV_CHARGE_DEYE design

Not started.

Prerequisites:

- confirmed sign conventions in production,
- several days of passive observation with the `sensor.solax_measured_power` authority,
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
