# DEYE state and freshness foundation

This repository-only change prepares passive runtime inputs. It adds no Phase 5A scheduler,
publication, Telegram integration, physical control, services, Modbus operations or deployment.
The existing Phase 4 helper publisher still emits diagnostics when the app is deployed separately.

## Operating state

The read-only adapter consumes switch.deye, sensor.deye_device_state,
sensor.deye_device_fault, sensor.deye_power and binary_sensor.deye_connection.
A fresh switch OFF is the configured indication of shutdown intent for this installation.
It is not evidence of electrical isolation or zero battery consumption.

* INTENTIONAL_OFF: fresh coherent OFF feedback and finite measured near-zero AC output
  settled for shutdown_settle_s. OK and the observed Tz_Integ_Fault failure are accepted
  while OFF; other faults remain unexpected. No dispatch.
* STOPPING: output is nonzero or its settling duration is unconfirmed. No dispatch.
* STARTING: recent ON transition with readiness pending. Temporary Fault is tolerated only
  within startup_window_s. Normal plus OK must persist for ready_confirmation_s.
* READY: fresh coherent Normal/OK feedback and readiness confirmed. Higher-level limits still apply.
* UNEXPECTED_FAULT: persistent failure outside the startup window, failure after READY,
  or an unrecognized fault while OFF. No dispatch.
* UNAVAILABLE: missing, invalid, stale, disconnected or incoherent feedback. No dispatch.

Defaults in apps/energy_v2.yaml: startup window 300 s, ready confirmation 10 s,
shutdown settling 20 s, near-zero tolerance 10 W, feedback maximum age 30 s, skew 20 s.
These are conservative configurable policy defaults, not a measured universal startup time.
Switch last_changed anchors the startup window across process restarts. Stable state-change
history plus current coherent reports can confirm readiness/settling on initial observation.
After communication loss, confirmation requires new observation progress. Repeated cached reads
cannot advance confirmation. READY is rechecked on each runtime evaluation.

## Freshness

Numeric samples expose value_changed_at, observed_at and source_health_at separately.
The legacy timestamp/age fields now refer to observation time. last_reported is preferred;
if absent, last_updated then last_changed provide conservative fallback evidence.
Malformed, future, naive or contradictory explicit report timestamps fail closed. Scalar cached
values without timestamps are not assigned the current time. No polling time fabricates freshness.

DEYE connection health also honors its explicit attributes.timestamp receipt time when present.
A recent HA report cannot override an old connection-source timestamp. Other sensor attributes
are not assumed to be acquisition timestamps without documented semantics.

The configured DEYE derived battery sensor is a known sign inversion of sensor.deye_battery_power.
When the raw source is present, a matching numeric derived value uses the raw observation time.
A mismatching value is invalid; a stale raw source stays stale even if the derived entity reports.
No value is fabricated. With no raw source, only the derived entity's own timestamp evidence remains.
Existing source-health policies for slow SOC and stable-zero grid/PV remain in place.

AppDaemon must expose actual report metadata for unchanged FAST_POWER samples to remain fresh.
If its cache omits last_reported and has only old change timestamps, these inputs intentionally
remain stale. This implementation does not add a HA polling bridge or claim a live deployment test.

## Load and safety

L_site = sensor.solax_inverter_power + sensor.deye_power - sensor.solax_measured_power.
The adapter assesses the same DEYE AC sample as the load model. The runtime requires READY
or INTENTIONAL_OFF plus the existing numeric freshness and timestamp-coherence checks.
OFF never substitutes zero for absent, non-finite or stale feedback, nor clamps measured output.
The near-zero tolerance is only a state-classification tolerance; calculation uses measured power.

Passive telemetry validation explicitly permits confirmed INTENTIONAL_OFF. Strict validation
remains the default and is still used for safe_to_enable. DEYE availability in shadow allocation,
legacy DEYE recommendations and charge-shadow readiness is gated by confirmed READY.

Existing energy_v2_load_quality and energy_v2_load_summary publish explicit validity.
The summary includes evaluation time and labels invalid retained numeric output LAST KNOWN.
A control-evaluation exception also invalidates these diagnostics. No new HA helpers are required.
Consumers must use quality and evaluation-time freshness, never the numeric helper alone.

## Validation

Regression tests cover OFF reports, source receipt and report expiry, settling, startup,
READY and faults, restart/reconnect, derived feedback, load validity and strict safety separation.
All tests run locally with stubs; no Home Assistant or inverter calls are made.
