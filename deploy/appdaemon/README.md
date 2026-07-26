# ENERGY V2 AppDaemon deployment package

This directory is a copy-ready package for the passive ENERGY V2 phase 1 AppDaemon app.

Do not restart Home Assistant Core for this step. Reload or restart only the AppDaemon add-on
after Home Assistant helpers already exist and have been verified.

## Contents

- `deploy/appdaemon/apps/energy_v2/`
- `deploy/appdaemon/apps/energy_v2.yaml`

These files must match the repository `apps/` directory. Verify with:

```bash
python scripts/verify_deployment_files.py
```

## Where to copy

The actual AppDaemon add-on path was not confirmed through MCP. Common Home Assistant OS add-on
paths are:

- `/addon_configs/a0d7b954_appdaemon/`
- `/addon_configs/a0d7b954_appdaemon/apps/`

The path often visible inside the AppDaemon add-on/container is:

- `/config/apps/`

Do not treat any of these paths as confirmed until they are checked in the real add-on
environment.

## Manual deployment sequence

1. Confirm all `energy_v2_*` Home Assistant helpers exist.
2. Keep:
   - `input_boolean.energy_v2_enabled = off`
   - `input_boolean.energy_v2_shadow_mode = on`
   - `input_boolean.energy_v2_export_enabled = off`
   - `input_boolean.energy_v2_service_mode = off`
3. Back up the current AppDaemon config and apps directory.
4. Copy `deploy/appdaemon/apps/energy_v2/` into the real AppDaemon apps directory.
5. Copy `deploy/appdaemon/apps/energy_v2.yaml` into the real AppDaemon apps config location.
6. Reload or restart only the AppDaemon add-on.
7. Check the AppDaemon log for:
   - import of `energy_v2.app`,
   - initialization of `EnergyV2App`,
   - no traceback.
8. Verify Home Assistant helpers:
   - heartbeat updates,
   - last successful evaluation updates,
   - `input_select.energy_v2_actual_mode = DISABLED`,
   - `input_select.energy_v2_requested_mode = DISABLED`,
   - `input_boolean.energy_v2_safe_to_enable = off` while legacy/conflicting systems remain active.

Do not turn on `input_boolean.energy_v2_enabled` during phase 1 deployment verification.
