# Phase 5A advisory runtime

This feature implements stage A (advisory) only. Stages B (supervised execution),
C (automatic execution) and D (exception reporting) are not implemented.

## Architecture and installation boundary

`energy_v2.advisory_app.Phase5AdvisoryApp` is a separate AppDaemon application.
It reads states through `LiveAdapter`, calls the existing pure LP planner, and
publishes only the fixed `sensor.energy_v2_phase5a_*` allowlist using `set_state`.
It never constructs the Phase 4 controller, translates commands for an inverter,
or calls HA services. The configuration is in apps/energy_v2.yaml and mirrored
under deploy/appdaemon/apps. These are repository artifacts, not a deployment.

The existing planner is unchanged. Battery policies default to DEYE 32 kWh,
SolaX 24 kWh, physical floor 10%, DEYE terminal target 10%, SolaX terminal target
30%, and SolaX evening checkpoint at 18:00 Europe/Prague. These remain model
assumptions; configurable advisory.batteries and advisory.planner accept existing
planner parameter fields. Local-time policy fields use HH:MM strings. Morning
conditional recovery is available through planner configuration but not enabled
by default. Grid charging stays prohibited by existing planner validation.

The runtime requires PuLP and an available CBC solver, plus timezone data
(`tzdata` on Windows). Development dependencies are declared in requirements-dev.txt.
Missing dependencies or invalid policies must be corrected before deployment.

## Input contract

- SOC: sensor.solax_battery_capacity and sensor.deye_battery. Numeric physical
  range 0..100; never raised to the configured floor. Observation age defaults
  to 1800 seconds. Below-floor recovery is delegated to the existing planner.
- Load: sensor.solax_inverter_power + sensor.deye_power -
  sensor.solax_measured_power, using TelemetryReader and the authoritative
  load model. The old numeric load helper is never an input. The merged
  report/source-health, coherence and DEYE intentional-OFF guards apply.
- Prices: sensor.current_buy_electricity_price_15min and
  sensor.current_sell_electricity_price_15min. Attributes contain ISO timestamp
  keys and CZK/kWh values. Timezones required; slots align to quarters, normalize
  to chronological UTC order, have no duplicates/gaps, and share the same horizon.
  Buy >= sell is required by the existing net-meter LP model. Negative prices are
  permitted within that assumption. Non-timestamp display metadata is ignored.
- PV: sensor.energy_production_today_2 and sensor.energy_production_tomorrow_2,
  attributes.wh_period_15m is an explicit map of ISO timestamp -> Wh per quarter.
  Both daily profiles must contain 96 nonnegative finite quarter slots, start at
  local midnight, cover the expected local date, and match state (Wh or kWh)
  within configurable 0.2 kWh. No Solcast substitution. Different live schemas
  fail closed and require a documented adapter; they were not verified via HA.
  DST days with 92/100 slots deliberately fail closed pending a DST-specific
  profile contract rather than inventing energy or losing repeated-hour data.
- Prices/PV require valid observation evidence <= 21600 seconds old by default.
  last_reported is receipt evidence, not inverter measurement time; no freshness
  is synthesized by reading a cache. Source metadata is retained in the plan.
- DEYE adapter uses switch, state, fault, AC power and connection timestamps.
  READY and INTENTIONAL_OFF allow valid load calculation; STOPPING, STARTING,
  UNEXPECTED_FAULT and UNAVAILABLE suppress new plans with explicit state/reason.
  OFF is non-dispatchable. Economic use of OFF DEYE is explicitly hypothetical:
  timeline says DEYE_START_REQUIRED, startup timing/energy is not priced, and
  readiness is never inferred from the planner's wish to use the battery.

## Load forecast and economic horizon

The existing time-of-day history builder is reused with no fabricated history.
Initially every period is a constant fallback at the latest validated whole-site
load. Source, LOW confidence, quality FALLBACK and all fallback periods are stored
and reported. This is not a high-confidence forecast. Future historical data must
be prepared with the same authoritative load equation and freshness guards.

Plans begin at the next full quarter (the current boundary if exactly aligned).
SOC is assumed unchanged until that boundary and this limitation is reported.
All available paired economic slots must be covered by PV. No guard extension is
invented. The plan labels NO_GUARD_HORIZON; future house reserve cannot be promised
beyond available inputs. Terminal reserve is a soft economic target, distinct
from physical floors and hard configured checkpoints.

## Replanning and failure behavior

The app checks inputs every 10 seconds. Triggers: source price profile/horizon
change, PV profile change, SOC change >= 3 percentage points, load change >=300 W,
DEYE state change, invalid-to-valid recovery, and horizon remaining <=1800 s.
Thresholds, 20-second debounce and 120-second minimum attempt interval are
configurable. The first valid evaluation plans immediately. Subsequent attempts,
including failed solvers, are rate-limited. Source fingerprinting excludes receipt
clock updates; advancing time alone does not masquerade as a new price profile.

Required invalid input suppresses current advice immediately, even during debounce.
Previous plans remain in memory explicitly LAST_KNOWN, with current/next advice and
economics unavailable. A successful replacement records trigger, previous/new ID,
time, economic delta, terminal-SOC delta and whether the timeline changed. Only the
last 100 events are held in memory. Expired plans are never published as current.

## Output contract and reporting

All outputs are runtime-created diagnostic sensors, not input helpers or actuators:
`sensor.energy_v2_phase5a_` followed by:

- status (ADVISORY, DEGRADED, LAST_KNOWN)
- plan_id, generated_at, valid_until
- current_action, next_action, next_action_time
- net_result (CZK), import_kwh, export_kwh
- terminal_deye_soc, terminal_solax_soc (%)
- reserve_status, degraded_inputs, replan_reason, summary

Each carries evaluated_at, valid, advisory_only and deye_operating_state attributes.
States are capped at 255 characters. Full plan metadata, source timestamps/quality,
trajectory, checkpoint results, timeline and decision trace stay in runtime memory
and structured AppDaemon logs. No legacy decision helper is overloaded. There is
no persistent-plan recovery across process restarts; startup creates a fresh plan.
Dashboard Trading / Planner uses these sensors and hides retained diagnostics if
evaluated_at is older than 30 seconds, including a stopped/crashed runtime.

Contiguous identical high-level actions collapse into periods. Labels map actual
planned PV charge/export flows; house supply/import without such activity is
HOUSE / IDLE. OFF DEYE activity adds DEYE_START_REQUIRED. Trace records prices,
SOC before each transition, remaining PV/load, active SolaX floor, export-cap
check, availability and deterministic PLANNER_<action>/DEYE_NOT_READY codes.
Planner explanations are retained; the runtime does not invent objective causality.

No notifier abstraction exists. Telegram is renderer-only: a deterministic report
is exposed in `rendered_report` and logged on each new baseline, including date,
SOCs, PV, fallback load quality, timeline, economics, terminal SOC, reserve and
warnings. It is suitable for a future daily advisory notification hook, but this
feature sends nothing and adds no credentials or notification service calls.
Invalidation replaces the exposed report with LAST_KNOWN / INVALID text.

## Future supervised handoff

Any stage B integration must separately approve input validity, current READY
confirmation, physical limits, conflict protections and authorization. Baseline
slots and startup annotations are advisory data, never actuator commands. No
execution consumer, controller callback, switch/service mapping or master-enable
path is supplied by this feature. Legacy automations are unchanged.
