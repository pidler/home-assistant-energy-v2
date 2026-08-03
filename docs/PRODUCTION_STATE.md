# ENERGY V2 production state

Date: 2026-08-03.

Phase 2 was passively deployed from `0a39a6ca2f0cbe295a61a53f5e64f3c624d5fb32`.

Live verification at approximately 14:39 CEST established:

- Home Assistant Core was restarted once after a successful `ha core check`.
- The AppDaemon add-on was not restarted; it reloaded the Phase 2 application automatically.
- heartbeat advanced every 10 seconds, flow/export helpers advanced every few seconds, and the
  planner/last successful evaluation remained healthy.
- current `requested_mode` and `actual_mode` were both `DISABLED`; `energy_v2_enabled`, export,
  service mode, and `safe_to_enable` were all `off`.
- active legacy/conflicting automations correctly kept `safe_to_enable` blocked.
- no ENERGY V2 traceback was present in the available Home Assistant log buffer.

This is a successful passive deployment, but **not production validation**. Observe it for several
days before considering any future active design.

Phase 3 DEYE surplus-charge shadow code is not deployed. Production remains Phase 2 only.

Known state after phase 1:

- passive AppDaemon phase 1 was deployed,
- the application loaded successfully,
- production telemetry was valid,
- physical inverter control was not implemented,
- `safe_to_enable` remained blocked by legacy/conflicting systems.

Phase 2 helpers, dashboard YAML, and AppDaemon files are deployed. The AppDaemon application
remains strictly diagnostic: its only service calls write ENERGY V2 helpers and `execute_mode()`
raises unconditionally.

Production dashboard:

- the versioned dashboard is deployed at `/energy-v2`,
- it contains `prehled`, `diagnostika`, and `nastaveni`,
- it is read-only and does not provide physical control.

Physical control remains inactive:

- no SolaX actuator write path is active,
- no DEYE actuator write path is active,
- Grid Charge is not implemented,
- PV Charge is not implemented,
- active export control is not implemented.

## Phase 3 rollback  2026-08-03

The Phase 3 shadow deployment attempt ended with ROLLBACK_COMPLETED after
KeyError: 'charge_shadow'. Production was restored to healthy Phase 2.
Phase 3 remains IMPLEMENTED, NOT DEPLOYED, and NOT VALIDATED. The fix in PR #3
has not been redeployed. No SolaX or DEYE physical service was called.
The DEYE current entity range is 0350 A; the ENERGY V2 operational cap is 240 A.
