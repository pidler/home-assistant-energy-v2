# ENERGY V2 Home Assistant installation preparation

This document prepares the manual Home Assistant helper installation for ENERGY V2 phase 1.
No production Home Assistant configuration was changed while preparing this document.

## Current `configuration.yaml` finding

The current production `configuration.yaml` read through MCP contains no existing top-level
`homeassistant:` section.

It currently includes entries such as:

```yaml
default_config:

frontend:
  themes: !include_dir_merge_named themes

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml
```

## Required patch

Because there is no existing `homeassistant:` section, add exactly one new section:

```diff
 # Loads default set of integrations. Do not remove.
 default_config:
+
+homeassistant:
+  packages: !include_dir_named packages
 
 # Load frontend themes from the themes folder
 frontend:
   themes: !include_dir_merge_named themes
```

If a future version of `configuration.yaml` already contains `homeassistant:`, do not create a
second section. Add only this line inside the existing section:

```yaml
  packages: !include_dir_named packages
```

## Helper package file

Copy this repository file:

```text
homeassistant/packages/energy_v2_helpers.yaml
```

to:

```text
/config/packages/energy_v2_helpers.yaml
```

The package defines these helpers:

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

Safe initial values:

- `energy_v2_enabled = off`
- `energy_v2_shadow_mode = on`
- `energy_v2_export_enabled = off`
- `energy_v2_service_mode = off`
- `energy_v2_safe_to_enable = off`
- `energy_v2_requested_mode = DISABLED`
- `energy_v2_actual_mode = DISABLED`
- `energy_v2_app_status = STARTING`
- both ledgers = `0`

## Manual Home Assistant procedure

1. Create `/config/packages` if it does not already exist.
2. Copy `energy_v2_helpers.yaml` into `/config/packages/`.
3. Edit `/config/configuration.yaml` with the patch above.
4. In Home Assistant, run the configuration check.
5. If the configuration check fails, do not restart Home Assistant.
6. If the configuration check succeeds, restart Home Assistant Core only.
7. Verify that all helpers listed above exist.
8. Verify their safe initial values.

Do not create duplicate helpers through the UI while using this package.

## After helpers exist

1. Keep:
   - `input_boolean.energy_v2_enabled = off`
   - `input_boolean.energy_v2_shadow_mode = on`
   - `input_boolean.energy_v2_export_enabled = off`
   - `input_boolean.energy_v2_service_mode = off`
2. Copy the AppDaemon deployment package from `deploy/appdaemon/apps/`.
3. Reload or restart only the AppDaemon add-on.
4. Check the AppDaemon log.
5. Verify:
   - `energy_v2.app` loaded,
   - `EnergyV2App` initialized,
   - heartbeat updates,
   - last successful evaluation updates,
   - `actual_mode = DISABLED`,
   - `requested_mode = DISABLED`,
   - `safe_to_enable = off` while the legacy system and conflicting automation remain active.
6. Do not turn on `input_boolean.energy_v2_enabled`.
