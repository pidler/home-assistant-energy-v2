from __future__ import annotations

from datetime import datetime
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from .config import (
    DEFAULT_CONFLICTING_AUTOMATIONS,
    ENERGY_V2_HELPER_KEYS,
    ENTITY_IDS,
    LEGACY_MASTER_HELPER_KEYS,
    OPTIONAL_TELEMETRY_KEYS,
    OWNED_ACTUATORS,
    REQUIRED_TELEMETRY_KEYS,
)
from .diagnostics import (
    app_status_value,
    compact_reasons,
    format_conflicts,
    format_decision,
    heartbeat_value,
    mode_value,
)
from .models import AppStatus, Mode, PlannerDecision
from .planner import plan_shadow_mode
from .safety import find_active_conflicts, find_missing_entities, safe_to_enable, validate_telemetry
from .telemetry import TelemetryReader, parse_bool_state, parse_float_state


class EnergyV2App(hass.Hass):
    """Passive shadow-only Energy V2 AppDaemon application.

    Phase 1 intentionally performs no physical SolaX/DEYE control.
    """

    def initialize(self) -> None:
        self.log_prefix = "ENERGY_V2"
        self.entity_ids = dict(ENTITY_IDS)
        self.entity_ids.update(self.args.get("entity_ids", {}))
        self.entity_ids["energy_v2_enabled"] = self.args.get("enabled_entity", self.entity_ids["energy_v2_enabled"])
        self.entity_ids["energy_v2_shadow_mode"] = self.args.get(
            "shadow_mode_entity", self.entity_ids["energy_v2_shadow_mode"]
        )
        self.entity_ids["energy_v2_export_enabled"] = self.args.get(
            "export_enabled_entity", self.entity_ids["energy_v2_export_enabled"]
        )
        self.conflicting_automations = tuple(self.args.get("conflicting_automations", DEFAULT_CONFLICTING_AUTOMATIONS))
        self.telemetry = TelemetryReader(self, self.entity_ids)
        self._debounce_handle: Any | None = None
        self._last_decision: PlannerDecision | None = None
        self._last_conflicts: tuple[str, ...] = ()
        self._last_fault_text = ""
        self._missing_required_entities: tuple[str, ...] = ()
        self._missing_optional_entities: tuple[str, ...] = ()
        self._missing_conflicting_automations: tuple[str, ...] = ()
        self._missing_owned_actuators: tuple[str, ...] = ()

        self._info("initializing passive shadow application")
        self._set_helper("energy_v2_app_status", app_status_value(AppStatus.STARTING))
        self._validate_required_entity_configuration()
        self._register_state_listeners()
        self.run_every(self._shadow_tick, "now+5", 15 * 60)
        self.run_every(self._heartbeat_tick, "now+10", 10)
        self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
        self._set_helper("energy_v2_requested_mode", mode_value(Mode.DISABLED))
        self.run_in(self._shadow_tick, 1)

    def execute_mode(self, mode: Mode) -> None:
        raise RuntimeError("Physical control is intentionally disabled in Energy V2 phase 1")

    def _validate_required_entity_configuration(self) -> None:
        missing_keys = [key for key, entity_id in self.entity_ids.items() if not entity_id]
        if missing_keys:
            self._error("missing configured entity IDs: %s", ", ".join(missing_keys))
            self._set_helper("energy_v2_app_status", app_status_value(AppStatus.CONFIG_ERROR))

    def _register_state_listeners(self) -> None:
        watched_keys = (
            *REQUIRED_TELEMETRY_KEYS,
            *OPTIONAL_TELEMETRY_KEYS,
            *LEGACY_MASTER_HELPER_KEYS,
            "energy_v2_enabled",
            "energy_v2_shadow_mode",
            "energy_v2_export_enabled",
            "energy_v2_service_mode",
        )
        for key in watched_keys:
            self.listen_state(self._schedule_shadow_tick, self.entity_ids[key])
        for entity_id in self.conflicting_automations:
            self.listen_state(self._schedule_shadow_tick, entity_id)
        for entity_id in OWNED_ACTUATORS:
            self.listen_state(self._physical_actuator_changed, entity_id)

    def _schedule_shadow_tick(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        if self._debounce_handle is not None:
            self.cancel_timer(self._debounce_handle)
        self._debounce_handle = self.run_in(self._shadow_tick, 3)

    def _shadow_tick(self, kwargs: dict[str, Any] | None = None) -> None:
        self._debounce_handle = None
        try:
            self._refresh_entity_existence_diagnostics()
            snapshot = self.telemetry.snapshot()
            telemetry_validation = validate_telemetry(snapshot)
            conflict_states = {entity_id: str(self.get_state(entity_id)) for entity_id in self.conflicting_automations}
            active_conflicts = find_active_conflicts(conflict_states, self.conflicting_automations)
            legacy_enabled = parse_bool_state(self.get_state(self.entity_ids["legacy_enabled"])) is True
            current_enabled = (
                parse_bool_state(self.get_state(self.entity_ids["current_energy_trading_enabled"])) is True
            )
            service_mode = parse_bool_state(self.get_state(self.entity_ids["energy_v2_service_mode"])) is True
            energy_v2_enabled = parse_bool_state(self.get_state(self.entity_ids["energy_v2_enabled"])) is True
            shadow_mode_enabled = parse_bool_state(self.get_state(self.entity_ids["energy_v2_shadow_mode"])) is True
            export_enabled = parse_bool_state(self.get_state(self.entity_ids["energy_v2_export_enabled"])) is True

            enable_validation = safe_to_enable(
                telemetry_validation,
                legacy_enabled,
                current_enabled,
                active_conflicts,
                self._missing_required_entities,
                self._missing_conflicting_automations,
                service_mode,
            )

            if shadow_mode_enabled:
                decision = plan_shadow_mode(
                    snapshot,
                    telemetry_valid=telemetry_validation.valid and enable_validation.valid,
                    export_enabled=export_enabled,
                    deye_ledger_kwh=parse_float_state(self.get_state(self.entity_ids["energy_v2_deye_fv_ledger"]))
                    or 0.0,
                    solax_ledger_kwh=parse_float_state(self.get_state(self.entity_ids["energy_v2_solax_fv_ledger"]))
                    or 0.0,
                    deye_min_soc_pct=float(self.args.get("deye_min_soc_pct", 15.0)),
                    deye_max_soc_pct=float(self.args.get("deye_max_soc_pct", 90.0)),
                    solax_min_soc_pct=float(self.args.get("solax_min_soc_pct", 30.0)),
                    solax_pv_charge_start_soc_pct=float(self.args.get("solax_pv_charge_start_soc_pct", 95.0)),
                    pv_reserve_w=float(self.args.get("pv_reserve_w", 1000.0)),
                    minimum_sell_price=float(self.args.get("minimum_sell_price", 3.0)),
                    maximum_future_rank=int(self.args.get("maximum_future_rank", 12)),
                )
                decision_text = format_decision(decision)
            else:
                decision = PlannerDecision(Mode.DISABLED, "Shadow mode is disabled", "high")
                decision_text = "DISABLED: shadow mode off; no trading recommendation published"

            diagnostics = (
                active_conflicts
                + self._prefix_entities("missing_conflict", self._missing_conflicting_automations)
                + self._prefix_entities("missing_optional", self._missing_optional_entities)
                + self._prefix_entities("missing_actuator", self._missing_owned_actuators)
            )
            self._set_helper("energy_v2_active_conflicts", format_conflicts(diagnostics))
            self._set_helper("energy_v2_last_decision", decision_text)

            if not shadow_mode_enabled:
                status = AppStatus.CONFIG_ERROR if not enable_validation.valid else AppStatus.HEALTHY
                error_text = compact_reasons(enable_validation.reasons) if not enable_validation.valid else ""
                self._mark_shadow_disabled_evaluation(status, error_text)
            elif energy_v2_enabled and not enable_validation.valid:
                fault_text = compact_reasons(enable_validation.reasons)
                self._set_helper("energy_v2_requested_mode", mode_value(Mode.FAULT))
                self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
                self._set_helper("energy_v2_last_fault", fault_text)
                self._set_helper("energy_v2_last_evaluation_error", fault_text)
                self._set_helper("energy_v2_app_status", app_status_value(AppStatus.CONFIG_ERROR))
                self.turn_off(self.entity_ids["energy_v2_enabled"])
                self._warn_once_fault(fault_text)
            elif energy_v2_enabled:
                self._set_helper("energy_v2_requested_mode", mode_value(decision.mode))
                self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
                self._set_helper(
                    "energy_v2_last_decision",
                    "Energy V2 phase 1 is shadow-only; physical control is not implemented.",
                )
                self._mark_successful_shadow_evaluation(AppStatus.HEALTHY)
            else:
                self._mark_successful_shadow_evaluation(AppStatus.HEALTHY, requested_mode=Mode.DISABLED)

            if diagnostics != self._last_conflicts:
                self._last_conflicts = diagnostics
                if diagnostics:
                    self._warning("active/missing conflicts: %s", ", ".join(diagnostics))
            if self._last_decision != decision:
                self._last_decision = decision
                self._info("shadow decision: %s", format_decision(decision))
            if not telemetry_validation.valid:
                self._warning("invalid telemetry: %s", compact_reasons(telemetry_validation.reasons))
        except Exception as exc:  # pragma: no cover - AppDaemon runtime guard
            error_text = f"unexpected shadow tick error: {exc!r}"
            self._set_helper("energy_v2_last_evaluation_error", error_text)
            self._set_helper("energy_v2_app_status", app_status_value(AppStatus.DEGRADED))
            self._error(error_text)

    def _heartbeat_tick(self, kwargs: dict[str, Any] | None = None) -> None:
        self._set_helper("energy_v2_heartbeat", heartbeat_value())

    def _physical_actuator_changed(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]
    ) -> None:
        self._warning(
            "external actuator change entity=%s old=%r new=%r time=%s recommended=%s enabled=%s shadow=%s",
            entity,
            old,
            new,
            datetime.now().astimezone().isoformat(),
            self._last_decision.mode.value if self._last_decision else "unknown",
            self.get_state(self.entity_ids["energy_v2_enabled"]),
            self.get_state(self.entity_ids["energy_v2_shadow_mode"]),
        )

    def _set_helper(self, key: str, value: str) -> None:
        entity_id = self.entity_ids[key]
        domain = entity_id.split(".", 1)[0]
        if domain == "input_select":
            self.call_service("input_select/select_option", entity_id=entity_id, option=value)
        elif domain == "input_text":
            self.call_service("input_text/set_value", entity_id=entity_id, value=value[:255])
        elif domain == "input_datetime":
            self.call_service("input_datetime/set_datetime", entity_id=entity_id, datetime=value)
        else:
            self._error("refusing to write unsupported helper domain for %s", entity_id)

    def _refresh_entity_existence_diagnostics(self) -> None:
        required_entity_ids = tuple(
            self.entity_ids[key]
            for key in (*REQUIRED_TELEMETRY_KEYS, *ENERGY_V2_HELPER_KEYS, *LEGACY_MASTER_HELPER_KEYS)
        )
        optional_entity_ids = tuple(self.entity_ids[key] for key in OPTIONAL_TELEMETRY_KEYS)
        required_states = {entity_id: self._entity_state_object(entity_id) for entity_id in required_entity_ids}
        optional_states = {entity_id: self._entity_state_object(entity_id) for entity_id in optional_entity_ids}
        conflict_states = {
            entity_id: self._entity_state_object(entity_id) for entity_id in self.conflicting_automations
        }
        actuator_states = {entity_id: self._entity_state_object(entity_id) for entity_id in OWNED_ACTUATORS}
        self._missing_required_entities = find_missing_entities(required_states, required_entity_ids)
        self._missing_optional_entities = find_missing_entities(optional_states, optional_entity_ids)
        self._missing_conflicting_automations = find_missing_entities(conflict_states, self.conflicting_automations)
        self._missing_owned_actuators = find_missing_entities(actuator_states, OWNED_ACTUATORS)

    def _entity_state_object(self, entity_id: str) -> object:
        return self.get_state(entity_id, attribute="all")

    def _prefix_entities(self, prefix: str, entity_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(f"{prefix}:{entity_id}" for entity_id in entity_ids)

    def _mark_successful_shadow_evaluation(self, status: AppStatus, requested_mode: Mode | None = None) -> None:
        if requested_mode is not None:
            self._set_helper("energy_v2_requested_mode", mode_value(requested_mode))
        self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
        self._set_helper("energy_v2_last_evaluation_error", "")
        self._set_helper("energy_v2_last_successful_evaluation", heartbeat_value())
        self._set_helper("energy_v2_app_status", app_status_value(status))

    def _mark_shadow_disabled_evaluation(self, status: AppStatus, error_text: str) -> None:
        self._set_helper("energy_v2_requested_mode", mode_value(Mode.DISABLED))
        self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
        self._set_helper("energy_v2_last_evaluation_error", error_text)
        self._set_helper("energy_v2_last_successful_evaluation", heartbeat_value())
        self._set_helper("energy_v2_app_status", app_status_value(status))

    def _warn_once_fault(self, text: str) -> None:
        if text != self._last_fault_text:
            self._last_fault_text = text
            self._warning("enable rejected: %s", text)

    def _info(self, message: str, *args: Any) -> None:
        self.log(f"{self.log_prefix} " + message, *args, level="INFO")

    def _warning(self, message: str, *args: Any) -> None:
        self.log(f"{self.log_prefix} " + message, *args, level="WARNING")

    def _error(self, message: str, *args: Any) -> None:
        self.log(f"{self.log_prefix} " + message, *args, level="ERROR")
