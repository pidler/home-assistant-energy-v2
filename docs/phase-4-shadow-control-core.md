# Phase 4 shadow control core

Phase 4 inserts a pure, testable command layer between a future optimizer and inverter-specific
execution. It is strictly shadow-only: it computes commands and diagnostic translations but has no
physical execution interface.

## Sign convention

- grid: positive export, negative import,
- battery: positive charging, negative discharging,
- PV: positive production,
- load: positive consumption.

## Pipeline and cadence

The 15-minute planner, 5-second flow safety monitor and 5-second shadow control loop remain
separate:

    telemetry -> whole-site load -> SiteCommand -> export diagnostics
              -> DEYE-first allocator -> shadow adapters -> runtime safety -> helpers

The Phase 4 command source is deliberately a static diagnostic target (shadow_site_target_w,
default 0 W). A trading optimizer is not part of this phase.

## Whole-site load

The controller uses:

    L_site = sensor.solax_inverter_power + sensor.deye_power - sensor.solax_measured_power

All three samples carry their source timestamp, age, freshness and quality. The result is invalid
when a sample is missing, stale, non-finite, or timestamp skew exceeds the configured tolerance.
A small negative result may be accepted only inside the measurement tolerance; a materially
negative result is invalid and is never silently clamped.

sensor.solax_house_load remains a legacy diagnostic and is not required by the Phase 4 load model.

## Export budgets

Two independent diagnostic views are maintained:

- the existing time-weighted trailing 900-second window,
- a fixed wall-clock quarter (00-15, 15-30, 30-45, 45-00).

Both calculate used energy, remaining operational and legal energy, time remaining, an available
power budget and projected average. The legal limit is 10,000 W and the operational target is
9,800 W. Neither interpretation is declared legally authoritative in Phase 4.

## Allocation

For discharge the allocator computes:

    D_required = grid_target + site_load - usable_PV
    D_DEYE = min(D_required, D_DEYE_available)
    D_SolaX = min(D_required - D_DEYE, D_SolaX_available)

DEYE is preferred and SolaX receives only the residual. SOC, availability, command expiry,
operational export budget and inverter power limits are applied before simulation. Unmet targets
are explicitly SATURATED.

Direction changes pass through a simulated break-before-make sequence:

    DISCHARGE -> HOLD -> confirmed zero flow -> CHARGE

The opposite transition follows the same rule. No corrective service call is made.

## Shadow adapters

The DEYE adapter proposes diagnostic values for Export First, TOU enable, TOU power/SOC, max sell
power and discharge limits. Requested, allowed, predicted and measured power remain separate.
TOU Power is treated as a ceiling, never as confirmed exact battery power.

The SolaX adapter supports BATTERY_RESIDUAL and preferred future GRID_TRIM. GRID_TRIM is based on
the confirmed whole-site grid feedback, but the adapter emits only proposed settings and marks
the Remote Control trigger as NOT_CALLED.

## Phase 3 integration

- **KEEP:** DeyeChargeShadowController, its time-aware surplus-charge states and existing
  production diagnostics.
- **EXTEND:** shared normalized battery signs, telemetry quality and runtime transfer diagnostics.
- **REWORK:** future physical fault recovery must use the Phase 4 common command/anti-transfer
  layer rather than duplicating Phase 3 correction logic.

Phase 3 continues to run independently in shadow mode during this development phase.

## Safety boundary

- EnergyV2App.execute_mode() remains a deliberate exception.
- Adapters import no AppDaemon API and expose no service or Modbus method.
- Only input_* Energy V2 helpers receive diagnostic updates.
- Grid Charge remains disallowed in Phase 4 commands.
- No Home Assistant/AppDaemon deployment or restart is part of this change.

## Physical validation still required

Before any active phase: validate SolaX Grid Control sign, response time, TTL and trigger behavior;
validate DEYE TOU power/current semantics and persistence; measure readback latency; validate
restart/failure behavior; and confirm the distributor's fixed-quarter versus rolling-window
interpretation.
