# ENERGY V2 safety invariants

## Passive phase invariant

Phase 2 is diagnostic only.

Phase 3 DEYE surplus-charge shadow is also diagnostic only. Its controller may write only
`input_*` ENERGY V2 diagnostic helpers. It must never call a physical `select`, `switch`, `number`,
`script`, SolaX, or DEYE service. Mode mismatches are reported as `WOULD_CORRECT` or
`WOULD_ENTER_FAULT`; they are never corrected.

- No physical SolaX actuator may be written.
- No physical DEYE actuator may be written.
- `execute_mode()` must remain blocked.
- Grid Charge must not be implemented.
- Active PV Charge must not be implemented.
- Export regulation must not be implemented.
- Ledger helpers must not be written.
- Legacy `energy_trading_*` entities and automations must not be changed.
- The phase 2 dashboard must remain read-only and must not expose service calls or active actuator controls.

## Legal export limit

The permitted grid export is 10,000 W evaluated as a 15-minute average.

## Operational target

ENERGY V2 must not intentionally request aggregate export above 9,800 W.

The 9,800 W value is an operational control target, not the legal limit.

## Instantaneous excursions

A short instantaneous excursion above 9,800 W or 10,000 W is not by itself a limit violation.

## Actual violation

A violation is based on the time-weighted 15-minute average exceeding 10,000 W.

## Aggregation

SolaX, DEYE and uncontrolled PV surplus must share one aggregate grid-export budget.

There must not be two independent export budgets of 9,800 W.

The confirmed aggregate grid telemetry authority is `sensor.solax_measured_power`.

- positive value = export to the distribution grid,
- negative value = import from the distribution grid.

Phase sensors `sensor.solax_measured_power_l1`, `sensor.solax_measured_power_l2`, and
`sensor.solax_measured_power_l3` are diagnostics only and must not create physical reactions in
phase 2.

SolaX and normalized DEYE battery power use one internal convention:

- positive value = battery charging,
- negative value = battery discharging.

DEYE raw `sensor.deye_battery_power` has the opposite convention and is diagnostic only.

## Missing or stale telemetry

If aggregate export telemetry is unavailable, stale, or ambiguous, ENERGY V2 must not treat the
export state as safe for starting a new active export.

Stale export diagnostics must not overwrite last known numeric export helpers with zero unless
zero is a real measured value.
