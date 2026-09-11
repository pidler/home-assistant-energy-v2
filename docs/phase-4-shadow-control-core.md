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

All three samples carry their state timestamp, age, value validity, source health, effective
freshness, effective timestamp and quality. The result is invalid when a sample is missing,
non-finite, effectively stale, or effective timestamp skew exceeds the configured tolerance.
A small negative result may be accepted only inside the measurement tolerance; a materially
negative result is invalid and is never silently clamped.

The telemetry reader does not treat Home Assistant `last_updated` as a communication heartbeat.
It classifies inputs as `FAST_POWER`, `SLOW_STATE`, or `STABLE_ZERO`. SolaX source health is
inferred from recent valid SolaX AC or battery-power telemetry; DEYE uses the corresponding DEYE
signals and an explicit disconnected state is a veto. A slow SOC sample remains usable while its
source is healthy. Exactly zero PV or grid power remains usable while its source is healthy. A
non-zero power sample must still satisfy its own age limit.

Production-derived defaults are configurable in AppDaemon:

- `solax_fast_power_max_age_s`: 60 s,
- `deye_fast_power_max_age_s`: 30 s,
- `fast_input_max_skew_s`: 20 s,
- `fast_input_coherence_jitter_s`: 5 s,
- `solax_source_health_window_s`: 60 s,
- `deye_source_health_window_s`: 30 s.

Cross-source coherence keeps the 20-second semantic skew separate from a bounded 5-second
scheduling/update allowance. The production SolaX and DEYE integrations naturally update at
approximately 15-second and 5-second cadences; Home Assistant state ordering and scheduler jitter
can therefore produce a healthy effective-timestamp difference just above 20 seconds. Coherence
accepts `skew <= base + jitter` (25 seconds with the defaults) and rejects `skew > base + jitter`.
The allowance is evaluated only after every compared sample is finite, `VALID`, effectively fresh,
and backed by a healthy source. It cannot promote a stale sample, extend battery freshness, extend
a source-health window, or accept an old non-zero grid/PV value.

The production replay that motivated this policy used SolaX/grid effective evidence at
`07:59:09.076867Z` and DEYE AC evidence at `07:59:29.094199Z`: an exact 20.017332-second skew that
was formatted as 20.0 seconds but rejected by the old 20-second boundary. A later 25.024222-second
gap remains deliberately rejected by the new policy; the jitter allowance is bounded, not a
freshness extension.

When an old SOC or stable-zero state is accepted, its effective timestamp is the timestamp of the
fast signal that proved source health. This prevents a legitimate unchanged state from creating
artificial multi-hour skew. If neither fast signal is recent, all dependent slow/stable samples
become stale. This is a conservative inference, not a substitute for a future integration-level
communication heartbeat.

sensor.solax_house_load remains a legacy diagnostic and is not required by the Phase 4 load model.

Usable PV is independently validated as:

    PV_total = sensor.solax_pv_power_total + sensor.deye_pv_power

Both PV samples must be finite, non-negative and effectively fresh. A stable exact zero is valid
only with a healthy corresponding source. A missing, negative, stale, or non-zero over-age input
faults the shadow evaluation; it is never silently replaced with 0 W.

## Export budgets

Two independent diagnostic views are maintained:

- the existing time-weighted trailing 900-second window,
- a fixed wall-clock quarter (00-15, 15-30, 30-45, 45-00).

Both calculate used energy, remaining operational and legal energy, time remaining, an available
power budget and projected average. The legal limit is 10,000 W and the operational target is
9,800 W. Neither interpretation is declared legally authoritative in Phase 4.
Intervals crossing a wall-clock quarter boundary are split at that boundary, retaining the
post-boundary portion in the new quarter.

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
DEYE has no confirmed physical HOLD work mode. An abstract HOLD command is therefore marked
UNVERIFIED and emits no work-mode or register proposal.

The SolaX adapter keeps BATTERY_RESIDUAL and GRID_TRIM as different quantities. BATTERY_RESIDUAL
uses battery power, while GRID_TRIM receives an explicit whole-site grid target from SiteCommand.
It never substitutes battery residual power for a Grid Control target. The adapter emits only
proposed settings and marks the Remote Control trigger as NOT_CALLED.

Battery-power own-state freshness and source health are required for availability, anti-transfer
feedback and zero-flow confirmation. Unlike SOC and exact zero PV/grid, battery feedback receives
no stable-value exception. A missing or stale sample resets transfer confirmation and produces UNVERIFIED,
never READY. The existing FlowDebouncer confirms a continuously observed transfer for 10 seconds:
a shorter event is TRANSFER_SUSPECTED and only a confirmed event becomes FAULT/RAMPING_DOWN in
shadow diagnostics.

## Phase 3 integration

- **KEEP:** DeyeChargeShadowController, its time-aware surplus-charge states and existing
  production diagnostics.
- **EXTEND:** shared normalized battery signs, telemetry quality and runtime transfer diagnostics.
- **REWORK:** future physical fault recovery must use the Phase 4 common command/anti-transfer
  layer rather than duplicating Phase 3 correction logic.

Phase 3 continues to run independently in shadow mode during this development phase.

State-triggered planner evaluation uses one debounced AppDaemon timer. Cleanup first clears the
stored handle and calls `cancel_timer(..., silent=True)` only when `timer_running()` confirms that
the handle is still active. Silent cancellation also closes the narrow race where a timer expires
between those two AppDaemon calls. The debounce callback clears its own handle; the periodic tick no
longer discards a pending debounce handle. Missing, expired, or repeatedly cleaned handles are
therefore harmless and do not generate AppDaemon invalid-callback warnings.

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
