# Phase 6: HAEO energy-budget slot executor

Phase 6 adds a repository-only execution foundation. It is not registered as an
AppDaemon app and it is not deployed. The live HAEO plan entity/attribute contract
must be verified before runtime wiring.

## Boundaries

- HAEO remains the only economic optimizer.
- `HaeoPlanAdapter` validates one fresh 15-minute slot and preserves HAEO's
  positive-discharge / negative-charge sign convention.
- Late entry scales energy to the remaining slot time.
- `SlotExecutionSession` integrates measured, direction-matching battery power.
- The session emits one existing `PlannerIntent` at a time for the Phase 5B safety
  and transition engine. It does not bypass Phase 5B.
- Opposite battery directions are held fail-closed to prevent cross-transfer.
- Higher HAEO economic priority wins; DEYE is only an equal-priority tie-breaker.

## Physical command boundary

`PhysicalCommandWriter` is the only new service-call boundary. Default configuration
cannot write because both its static deployment gate and the new Home Assistant
physical-execution helper are off. Active commands additionally require ENERGY V2
authority, master enable, safe-to-enable, export enable, fresh plan/telemetry, SOC,
export and cross-transfer clearance, and no writer conflict.

An executor-owned active session may use the known restore sequence after the
runtime helper is disabled or safety fails. That exception requires the static
writer gate and an executor ownership token. It deliberately remains available
for stale telemetry, cross-transfer detection, writer conflict or user disable,
because those conditions are rollback triggers. A default deployment owns no
session and therefore performs no restore or inverter write.

## Capability classification

| Operation | Capability |
|---|---|
| SolaX normal (`Self Use Mode`) | VERIFIED |
| SolaX hold (`Manual Mode`; `Stop Charge and Discharge`) | VERIFIED |
| SolaX charge/export using Mode 8 VPP | EXPERIMENTAL, no physical calls |
| DEYE normal proven four-step restore | VERIFIED |
| DEYE export after SolaX hold | VERIFIED |
| DEYE hold | UNAVAILABLE |
| DEYE charge | UNAVAILABLE |

The proven DEYE export sequence does not write TOU Program 1-6 power. TOU is mode
enablement only; Max Sell Power remains an external technical/safety ceiling.

## Unresolved runtime contract

The live system currently exposes no discoverable HAEO plan sensor. Runtime wiring
still needs the exact HAEO entity, publication timestamp, plan identity, slot list,
per-battery target fields, and optional economic-priority fields. Until this is
known, Phase 6 remains importable and fully unit-tested but unscheduled.
