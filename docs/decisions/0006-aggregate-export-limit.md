# ADR 0006 - Aggregate export limit

Date: 2026-07-27.

## Status

Accepted for phase 2 diagnostics.

## Context

The system contains two 12 kW hybrid inverters. If controlled independently, they can theoretically
request a combined export above the permitted grid export.

The permitted grid export is 10 kW evaluated as a 15-minute average. Individual short
instantaneous spikes are not the deciding criterion.

ENERGY V2 also needs an operational target below the permitted average to keep reserve for
measurement latency, uncontrolled PV surplus, and controller reaction time.

## Decision

ENERGY V2 uses 9.8 kW as its operational aggregate export target.

ENERGY V2 monitors a time-weighted rolling 15-minute average export.

The rolling export average uses `sensor.solax_measured_power` as the confirmed whole-connection
grid authority:

- positive value = export,
- negative value = import,
- import is clamped to zero for export averaging.

Instantaneous export above 9.8 kW or 10 kW is a diagnostic warning, not an automatic fault.

All export sources share one aggregate export budget:

- SolaX export,
- DEYE export,
- uncontrolled PV surplus reaching the grid.

## Consequences

Future SolaX and DEYE export must be coordinated by one central export regulator.

There must not be two independent export limits of 9.8 kW.

The future planner must not intentionally request more than 9.8 kW aggregate export.

Protection of the 15-minute export average must be independent from economic trading logic.

If export telemetry is missing, stale, or ambiguous, ENERGY V2 must not start a new active export.

Phase 2 does not implement this regulator. It only implements passive diagnostics.
