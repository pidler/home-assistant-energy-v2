# Phase 3 — DEYE surplus-charge shadow

Status: **APPROVED** design, **IMPLEMENTED** in `feature/phase-3-deye-charge-shadow`, **NOT DEPLOYED**,
**NOT VALIDATED**. Active charge control, export, and discharge are **DEFERRED**.

The pure state machine has the states `DISABLED`, `WAITING_FOR_SOLAX`, `START_CONFIRMATION`,
`CHARGE_DEYE_FULL`, `TRANSFER_CONFIRMATION`, `CHARGE_DEYE_LIMITED`, `RETURN_CONFIRMATION`,
`SOLAX_PRIORITY`, and `FAULT`.

It starts only after SolaX SOC is above 90% and PV is above 1000 W continuously for 30 seconds.
It recommends 240 A initially. A reduction is considered only after ten seconds of both actual
DEYE charging (>300 W) and SolaX discharge (>300 W). The recommended current uses time-weighted
actual charge power, actual SolaX discharge, actual DEYE voltage, and a 300 W reserve; it is
rounded down and clamped to 0–240 A. PV below the threshold alone does not reduce a full or already
limited current; it only blocks return to full current.

`sensor.battery_power_otoceny` is used directly: positive means DEYE charging. The raw DEYE power
sensor is not used for control calculations.

All mismatches are diagnostic. The shadow controller cannot invoke physical services.

Confirmation helpers use the **elapsed seconds** convention. FAULT requires 60 seconds of stable,
valid telemetry before the state machine may return to its current-condition state.

## Rollback follow-up  2026-08-03

The first production attempt ended with ROLLBACK_COMPLETED after AppDaemon
reported KeyError: 'charge_shadow'. Production remains on healthy Phase 2.
Phase 3 is IMPLEMENTED, NOT DEPLOYED, and NOT VALIDATED; this correction has
not been redeployed.

The charge_shadow section is a required, centrally validated AppDaemon mapping.
Missing or invalid configuration produces a CONFIG_ERROR and a disabled,
zero-current shadow recommendation without any physical service call.

The live number.deye_battery_max_charging_current entity reports min 0 A,
max 350 A, step 1 A, mode box. ENERGY V2 retains its independent operational
maximum of 240 A. Recommendations are clamped to both that configured
operational maximum and the live entity maximum when available.
