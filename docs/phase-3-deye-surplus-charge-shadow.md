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
