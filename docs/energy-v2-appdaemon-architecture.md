# Energy V2 - AppDaemon architecture for passive phase 1

Date: 2026-07-26.

This document describes the local, not-yet-deployed AppDaemon application `energy_v2`.
Phase 1 is strictly passive: shadow evaluation, safety validation, and diagnostics only.
It does not physically control SolaX or DEYE.

## Current AppDaemon status

Observed through Home Assistant MCP during the initial audit:

- AppDaemon add-on appears installed via `update.appdaemon_aktualizovat`.
- Add-on version reported by that entity: `0.18.5`.
- Production AppDaemon filesystem, add-on logs, and runtime Python version were not available through MCP.

Not verified:

- actual AppDaemon process load of `energy_v2`,
- production AppDaemon import path,
- production AppDaemon logs,
- deployment into `/config/apps` or add-on config directories.

No Home Assistant add-on service was called and no production deployment was performed.

## Files

| File | Purpose |
|---|---|
| `apps/energy_v2/__init__.py` | package marker |
| `apps/energy_v2/models.py` | dataclasses and enums |
| `apps/energy_v2/config.py` | entity map, required/optional groups, owned actuator inventory |
| `apps/energy_v2/telemetry.py` | state parsing and telemetry snapshot |
| `apps/energy_v2/safety.py` | pure validation helpers |
| `apps/energy_v2/planner.py` | pure shadow planner |
| `apps/energy_v2/diagnostics.py` | diagnostic formatting |
| `apps/energy_v2/app.py` | AppDaemon wrapper, listeners, heartbeat, diagnostics |
| `apps/energy_v2.yaml` | proposed AppDaemon app config |
| `config/packages/energy_v2_helpers.yaml` | proposed Home Assistant helper package |

The AppDaemon config uses:

```yaml
module: energy_v2.app
class: EnergyV2App
```

The Python package uses relative imports inside `apps/energy_v2`.
A stubbed local loader test verifies that `energy_v2.app` imports and exposes `EnergyV2App`
when `apps/` is on `sys.path` and the AppDaemon API is available.
Actual production loading by a running AppDaemon instance is still not verified.

## Required and optional entities

Required telemetry:

- SolaX SOC, battery power, PV power, house load, grid import, grid export
- DEYE SOC, battery power, battery state, grid power, external power, device state, connection
- buy price, sell price
- DEYE grid charging switch state
- DEYE export switch state

Optional telemetry:

- `future_sell_rank`

Required helpers:

- `input_boolean.energy_v2_enabled`
- `input_boolean.energy_v2_shadow_mode`
- `input_boolean.energy_v2_export_enabled`
- `input_boolean.energy_v2_service_mode`
- `input_boolean.energy_v2_safe_to_enable`
- `input_select.energy_v2_requested_mode`
- `input_select.energy_v2_actual_mode`
- `input_select.energy_v2_app_status`
- `input_text.energy_v2_last_fault`
- `input_text.energy_v2_last_decision`
- `input_text.energy_v2_active_conflicts`
- `input_text.energy_v2_last_evaluation_error`
- `input_datetime.energy_v2_heartbeat`
- `input_datetime.energy_v2_last_successful_evaluation`
- `input_number.energy_v2_deye_fv_ledger`
- `input_number.energy_v2_solax_fv_ledger`

Required legacy master helpers:

- `input_boolean.energy_trading_puvodni_reseni_povoleno`
- `input_boolean.energy_trading_novy_system_povolen`

Configured conflicting automations are treated as required for safety diagnostics. A missing
conflicting automation is not silently interpreted as off; it is reported in
`input_text.energy_v2_active_conflicts` as `missing_conflict:<entity_id>` and blocks safe enable.

Future owned actuators are passive in phase 1: their existence is checked and missing ones are
reported as `missing_actuator:<entity_id>`. A missing owned actuator blocks
`input_boolean.energy_v2_safe_to_enable`, but phase 1 still does not write to that actuator.

## Runtime diagnostics

Heartbeat is process liveness only and is not treated as proof of a healthy shadow controller.

Diagnostics written by the app:

- `input_datetime.energy_v2_heartbeat`: periodic process heartbeat
- `input_datetime.energy_v2_last_successful_evaluation`: last completed shadow evaluation
- `input_text.energy_v2_last_evaluation_error`: last shadow tick failure or safety/config error
- `input_select.energy_v2_app_status`: `STARTING`, `HEALTHY`, `DEGRADED`, `CONFIG_ERROR`
- `input_select.energy_v2_requested_mode`: requested shadow mode or `DISABLED`
- `input_select.energy_v2_actual_mode`: always `DISABLED` in phase 1
- `input_boolean.energy_v2_safe_to_enable`: safety readiness, independent from process health
- `input_text.energy_v2_last_fault`: last enable rejection
- `input_text.energy_v2_last_decision`: last diagnostic decision text
- `input_text.energy_v2_active_conflicts`: active or missing conflict diagnostics plus optional missing items

If `input_boolean.energy_v2_shadow_mode` is `off`:

- `requested_mode` is set to `DISABLED`,
- `actual_mode` is set to `DISABLED`,
- no active trading recommendation is published,
- heartbeat and basic diagnostics continue.

## Safety validation

Telemetry validation rejects:

- missing required telemetry,
- non-finite numeric values (`NaN`, `inf`, `-inf`),
- SolaX SOC outside 0-100%,
- DEYE SOC outside 0-100%,
- `future_sell_rank < 1` when that optional entity is available,
- DEYE disconnected or device state other than `Normal`.

Negative power values remain valid because SolaX and DEYE use different sign conventions.

Safe enable additionally blocks:

- service mode,
- missing required entities,
- missing conflicting automations,
- missing future owned actuator entities,
- active legacy master helpers,
- active conflicting automations.

`input_boolean.energy_v2_safe_to_enable` is set to `on` only when these safety checks pass.
It does not trigger any physical control in phase 1.

If an Energy V2 helper is missing, the app logs a configuration error and skips the service call
for that missing helper. This prevents one missing diagnostic helper from causing a chain of
additional write failures.

Phase 1 may turn off only its own `input_boolean.energy_v2_enabled` when enabled state is unsafe.
It does not disable legacy automations automatically.

## Shadow planner

`plan_shadow_mode(...)` is a pure function. It can recommend:

- `FAULT`
- `IDLE`
- `PV_CHARGE_DEYE`
- `EXPORT_DEYE`
- `EXPORT_SOLAX`

The recommendation is diagnostic only. `actual_mode` remains `DISABLED`.

Grid Charge is intentionally absent from phase 1.
Ledger helpers are read only; no ledger calculation or mutation is implemented.

## Physical control boundary

No physical control path was added.

`execute_mode()` is intentionally implemented as:

```python
def execute_mode(self, mode: Mode) -> None:
    raise RuntimeError("Physical control is intentionally disabled in Energy V2 phase 1")
```

The method is not called by the application.

The only writes are to Energy V2 diagnostic helpers and, on unsafe enable, to
`input_boolean.energy_v2_enabled`.

`Mode` and `AppStatus` use `enum.StrEnum` when available. The code includes a compatibility
fallback equivalent to `class StrEnum(str, Enum)` for Python older than 3.11. Tests verify the
string enum semantics used by the application.

## Verification levels

The repository distinguishes these checks:

- Python syntax check: `python -m compileall apps tests`
- Unit tests: pure telemetry, safety, and planner tests
- Stubbed AppDaemon import test: `tests/test_app.py` injects a fake
  `appdaemon.plugins.hass.hassapi.Hass`, imports `apps.energy_v2.app`, verifies `EnergyV2App`,
  and exercises selected runtime paths without Home Assistant, AppDaemon, token, or network.
- Actual AppDaemon load: not verified yet; requires deploying into the real AppDaemon add-on and
  checking its logs.
- Production deployment: not performed.

GitHub Actions run syntax check, `ruff check`, `ruff format --check`, and pytest.

## Next step

Before phase 2, deploy the package to a test AppDaemon environment, verify the actual AppDaemon
module loading path and logs, then keep shadow mode enabled long enough to compare recommendations
against the existing system without allowing physical control.
