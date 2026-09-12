# Phase 5A: DEYE power-state shadow overlay

## Scope

`trading/power_state.py` is a pure deterministic overlay above an existing Phase 5A trading plan. It calculates
when DEYE is required for planned PV charging, house-load supply or grid export, and when it would be eligible to
turn off. It does not change the LP, publish a command, import AppDaemon, call Home Assistant, write Modbus or
operate `switch.deye`.

The overlay deliberately reports eligibility rather than claiming the inverter is physically off. Physical
switching remains out of scope and requires controlled validation of startup behavior, shutdown behavior and
device telemetry.

## States

- `ON_REQUIRED`: the slot contains DEYE battery activity above the configured threshold.
- `STARTING`: the deterministic planning slot before upcoming activity. The default ten-minute lead cannot be
  represented exactly on a 15-minute grid, so it maps to the complete preceding slot.
- `READY`: the inverter remains available because of minimum-on time, shutdown delay or anti-cycling.
- `OFF_ELIGIBLE`: there is no activity, startup lead, minimum-on constraint or shutdown delay, and the idle gap is
  at least the minimum-off duration.
- `OFF`: reserved for a future observed/runtime state. The shadow overlay never turns the inverter off.

Every slot reports state, `deye_required`, reason, next activity and telemetry expectation. A planned active slot
annotated as off would be flagged as an overlay conflict; construction is designed and tested to prevent that.

## Explicit model assumptions

| Parameter | Shadow default | Status |
| --- | ---: | --- |
| Activity threshold | 0.001 kWh/slot (4 W average) | `MODEL_ASSUMPTION` |
| Startup lead | 10 minutes, rounded up to one prior 15-minute slot | `MODEL_ASSUMPTION` |
| Minimum on time | 30 minutes | `MODEL_ASSUMPTION` |
| Minimum off time | 30 minutes | `MODEL_ASSUMPTION` |
| Shutdown delay | 15 minutes | `MODEL_ASSUMPTION` |
| DEYE idle-on power | not configured | `UNKNOWN` |
| Startup energy | not configured | `UNKNOWN` |

Timing values are review assumptions, not confirmed physical constraints. Idle saving is `N/A` until idle power is
explicitly configured. If startup energy is also configured, it is deducted for modeled OFF-to-ON transitions.
Because this pure overlay has no observed inverter-state input, minimum-on time is assumed already satisfied at the
start of the horizon. A future runtime consumer must supply and validate actual state dwell time before switching.

## Anti-cycling and OFF windows

Required-on blocks include startup lead, activity, shutdown delay and minimum-on time. Blocks separated by less
than the configured minimum-off duration are merged. Each remaining `OFF_ELIGIBLE` interval reports `off_from`,
`on_again_by`, `next_required_activity` and `off_duration`. The summary reports total required-on and off-eligible
duration, transition counts and the longest off window.

No battery flow is permitted in a slot classified as off-eligible. The overlay does not feed that result back into
the economic LP; an active DEYE flow combined with an off state is instead a model conflict.

## Future telemetry semantics

The model exposes `DEYE_EXPECTED_OFF`. In a future runtime integration, stale/unavailable DEYE power, battery power
or device state during an expected-off interval must not automatically be treated as a fault. This commit does not
change Phase 4 telemetry health or fault behavior and does not connect the expectation to physical control.

## Physical validation still required

Before any use of `switch.deye`, validate actual idle consumption, startup energy, reliable startup lead time,
minimum safe on/off durations, switch semantics, telemetry behavior while off, recovery from failed startup and
interaction with SolaX and AC coupling. Physical switching is **not ready**.
