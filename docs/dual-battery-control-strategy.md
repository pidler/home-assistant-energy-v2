# Energy V2 - Dual-battery control strategy

Date: 2026-09-07
Status: architecture decision / implementation guidance
Scope: SolaX X3-HYBRID G4.2 + DEYE SUN-12K-SG05LP3-EU, each with its own battery, controlled by Energy V2 via AppDaemon.

## Purpose

This document records the intended control strategy for two independent hybrid inverters connected to the same AC system. It exists to prevent the implementation from drifting back toward two independent self-consumption controllers that can unintentionally transfer energy from one battery into the other.

The AppDaemon application is the supervisory EMS. The inverter-local controllers remain responsible for electrical safety and low-level inverter/battery behavior, but Energy V2 decides which battery is allowed to charge or discharge and when.

## Core problem: unintended battery-to-battery transfer

If both inverters are allowed to regulate the same AC bus independently, one inverter can discharge while the other interprets the resulting AC power as surplus and charges its own battery.

Example:

```text
DEYE battery -> DEYE inverter -> AC bus -> SolaX inverter -> SolaX battery
```

The reverse direction is also possible.

This is normally undesirable because it adds conversion losses, causes unnecessary cycling, can defeat price-based dispatch decisions, and makes the two inverter controllers fight each other.

Energy V2 therefore needs explicit arbitration rules, not only SOC thresholds.

## Architectural decision

### 1. AppDaemon is the single supervisory controller

Energy V2 decides the operating role of each inverter. The two inverters must not independently make conflicting charge/discharge decisions for the same AC balance.

The state machine determines what is allowed. A lower-level actuator layer translates that intent into vendor-specific commands.

Conceptually each inverter should expose the following abstract intent to the planner/executor:

```text
allow_charge
allow_discharge
target_or_limit_charge_power
target_or_limit_discharge_power
```

The implementation may differ between SolaX and DEYE.

### 2. Charge/discharge current limits are not the primary EMS abstraction

`Max Charge Current` and `Max Discharge Current` are useful limits and may be needed on DEYE, but they should not be treated as the primary system-level control concept.

The planner should reason in terms of roles and power flow. Vendor-specific current limits belong in the actuator/adapter layer.

Where a direct power-control interface exists, prefer it over repeatedly rewriting maximum battery-current settings.

### 3. SolaX should use Remote Power Control / Mode 8 where practical

For the SolaX G4 family, Remote Power Control / Mode 8 is the preferred fast actuator when supported by the installed integration and verified on the actual inverter.

Use it for deliberate battery charge/discharge/freeze behavior instead of using maximum battery-current settings as the fast regulation loop.

The existing battery current limits remain safety/operating limits.

Before production activation, verify the exact Home Assistant entities/services, sign convention, update frequency, timeout/fallback behavior, and actual inverter response.

### 4. DEYE may use work mode / TOU / SOC / charge-discharge current limits

DEYE does not need to be controlled identically to SolaX.

Energy V2 can use DEYE as the coarser dispatch resource and SolaX as the more finely controlled actuator if that proves stable in testing.

For DEYE, the practical actuator set may include:

- work mode,
- TOU controls,
- SOC constraints,
- grid-charge enable,
- export/sell controls,
- maximum charge current,
- maximum discharge current.

If power targets must be translated to battery current, the adapter should calculate approximately:

```text
I = P / V_battery
```

and apply bounds, deadband, hysteresis, minimum update interval, and safe fallback behavior.

Do not run a high-frequency control loop by rewriting persistent configuration unless verified safe for the specific DEYE register/integration.

## Required battery priority policy

### Charging priority

1. House consumption is supplied normally.
2. SolaX battery has first priority for available PV surplus.
3. DEYE battery is second priority.
4. Grid export receives only the remaining energy, subject to export strategy and distributor constraints.

This priority MUST NOT be implemented as a simple rule such as "wait until SolaX reaches 100%, then enable DEYE".

Near full SOC, a battery can accept progressively less power. Waiting for 100% can waste available PV production or cause unnecessary export.

Energy V2 must allow spillover charging into DEYE when SolaX is no longer able to absorb all available surplus.

### Spillover charging decision

Use a combination of:

- SolaX SOC,
- actual SolaX battery charging power,
- measured grid export / available surplus,
- persistence timer / hysteresis.

Illustrative logic only:

```text
if SolaX SOC is below the normal priority threshold:
    DEYE charge remains blocked
else if grid export remains significant while SolaX is charging:
    allow DEYE to absorb the remaining surplus
```

A possible implementation starting point is:

```text
SolaX SOC >= 90%
AND grid export > configurable threshold
AND condition persists for 30-60 s
=> allow DEYE spillover charging
```

The 90% value is not a hard design constant. It must be configurable and validated against real charge taper behavior.

The measured AC surplus is more important than the SOC threshold alone.

## Discharge priority policy

When discharge is economically justified:

1. DEYE discharges first.
2. SolaX remains reserved for household resilience / later consumption.
3. SolaX may discharge only when allowed by the price strategy and only down to a logical reserve SOC.
4. Hardware/inverter minimum SOC remains a separate lower safety boundary.

Conceptually:

```text
DEYE = primary trading / flexible battery
SolaX = priority-charge / household-reserve battery
```

This is a logical role assignment and can later be revisited if measurements show a better strategy.

## SolaX reserve SOC

Energy V2 must distinguish between:

- inverter/BMS hard minimum SOC,
- Energy V2 household reserve SOC.

Example:

```text
hardware minimum SOC: 10%
Energy V2 reserve SOC: 35%
```

Then 35-100% may be available to normal price-based dispatch while 10-35% remains reserved unless a dedicated emergency/reserve-support policy allows its use.

The reserve should ultimately be dynamic rather than permanently fixed. Inputs can include:

- expected overnight household consumption,
- PV forecast,
- time of day,
- future electricity prices,
- DEYE SOC,
- expected next charging opportunity.

Static reserve is acceptable as an initial implementation.

## Anti-transfer invariants

These rules should be enforced centrally and tested explicitly:

```text
If DEYE is charging from the common AC bus, SolaX discharge must be forbidden.
If SolaX is charging from the common AC bus, DEYE discharge must be forbidden.
```

Equivalent generalized invariant:

> Energy V2 must not intentionally command one battery to discharge while the other battery is charging from that resulting AC power unless an explicit battery-transfer mode exists.

There is currently no requirement for an intentional battery-transfer mode.

These invariants are more important than the exact state names.

## Recommended state-model evolution

The current phase-1 planner has only:

- `FAULT`
- `IDLE`
- `PV_CHARGE_DEYE`
- `EXPORT_DEYE`
- `EXPORT_SOLAX`

That model is too coarse for the intended final two-battery behavior.

The final state model should represent at least the following logical situations, though the exact enum design may be simplified:

```text
IDLE / HOLD
PV_CHARGE_SOLAX
PV_CHARGE_SOLAX_AND_DEYE
PV_CHARGE_DEYE
DISCHARGE_DEYE
DISCHARGE_DEYE_AND_SOLAX
DISCHARGE_SOLAX
RESERVE
SAFE / FAULT
```

Do not add states mechanically. If the implementation is cleaner with a smaller mode enum plus per-inverter permissions/targets, prefer that design. The important requirement is explicit arbitration and testable invariants.

## Price-based discharge policy

A multi-band price policy is preferable to one binary threshold. For example:

```text
LOW       -> preserve batteries / charge when appropriate
NORMAL    -> normal self-use / hold
HIGH      -> discharge DEYE first
VERY_HIGH -> discharge DEYE, then allow SolaX above reserve
```

If export arbitrage is enabled, it should remain a separate intentional action with export limits and ledger/accounting rules.

## Control-loop guidance

Do not let both inverter-local controllers simultaneously chase the same grid-power target as independent fast loops.

A robust approach is:

- state machine: decides roles and permissions,
- coarse actuator: one inverter provides the main scheduled power,
- fine actuator: the other inverter optionally trims residual import/export,
- grid meter: authoritative feedback,
- hysteresis/deadband: prevents oscillation,
- minimum dwell time: prevents rapid state changes.

This master/secondary actuator role may change by operating state.

## Required review of current implementation

The existing Energy V2 code must be audited against this document before physical control is enabled.

At minimum inspect and, where needed, revise:

1. `apps/energy_v2/models.py`
   - current `Mode` enum is insufficient for the final dual-battery strategy,
   - decide whether to add explicit states or introduce per-inverter command objects.

2. `apps/energy_v2/planner.py`
   - current PV logic calculates a simple SolaX PV surplus and recommends `PV_CHARGE_DEYE` after a SolaX SOC threshold,
   - this does not yet implement SolaX-first charging with measured spillover behavior,
   - add explicit anti-transfer rules,
   - implement DEYE-first discharge and SolaX reserve logic,
   - later incorporate dynamic reserve and price bands.

3. `apps/energy_v2/config.py`
   - verify all telemetry and actuator entities required for Mode 8 / DEYE controls,
   - add battery voltage if DEYE power-to-current translation is used,
   - make spillover thresholds, hysteresis, dwell times, reserve SOC and price bands configurable.

4. `apps/energy_v2/telemetry.py`
   - verify reliable common-grid import/export telemetry,
   - verify battery-power sign conventions,
   - expose any missing values required by the actuator layer.

5. actuator/executor layer
   - keep vendor-specific control outside the high-level planner,
   - implement SolaX and DEYE adapters separately,
   - include command verification and fail-safe behavior,
   - never rely on an assumed write succeeding.

6. tests
   - add explicit scenarios for unintended battery-to-battery transfer,
   - SolaX-first charging,
   - SolaX charge taper with DEYE spillover,
   - DEYE-first discharge,
   - SolaX reserve enforcement,
   - price-band transitions,
   - unavailable/stale telemetry,
   - actuator failure and safe fallback,
   - hysteresis / anti-chatter behavior.

## Current implementation discrepancy already identified

The current phase-1 planner contains logic equivalent to:

```text
if DEYE SOC < max
and SolaX SOC > solax_pv_charge_start_soc
and calculated PV surplus is large enough:
    recommend PV_CHARGE_DEYE
```

This was acceptable as a conservative shadow experiment, but it must not be considered the final charge-arbitration design.

The final implementation must explicitly preserve SolaX charging priority while allowing DEYE spillover when SolaX charge acceptance tapers and otherwise usable PV energy would be exported or curtailed.

## Safety and rollout

Implement this progressively:

1. planner/shadow decisions only,
2. diagnostics showing desired per-inverter roles and target/limit values,
3. verify decisions against real inverter behavior,
4. enable one actuator at a time,
5. verify anti-transfer behavior,
6. only then enable combined automatic operation.

Any loss of required telemetry, inverter communication, command verification, or safety precondition must move control to a defined safe state rather than leaving stale active commands in place.

## Summary of final intent

```text
Charging:
PV -> house -> SolaX first -> DEYE spillover -> grid

Discharging by price:
DEYE first -> SolaX only above household reserve

Supervision:
AppDaemon / Energy V2 is the single EMS authority

Control:
prefer direct power control where available;
use current limits as vendor-specific constraints/actuators, not as the high-level strategy

Safety invariant:
never unintentionally discharge one battery into the other
```
