from __future__ import annotations

from datetime import datetime
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from .config import DEFAULT_CONFLICTING_AUTOMATIONS, ENTITY_IDS, OWNED_ACTUATORS
from .diagnostics import compact_reasons, format_conflicts, format_decision, heartbeat_value, mode_value
from .models import Mode, PlannerDecision
from .planner import plan_shadow_mode
from .safety import find_active_conflicts, safe_to_enable, validate_telemetry
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

        self._info("initializing passive shadow application")
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

    def _register_state_listeners(self) -> None:
        watched_keys = (
            "solax_soc",
            "solax_battery_power",
            "solax_pv_power",
            "solax_house_load",
            "deye_soc",
            "deye_battery_power",
            "deye_battery_state",
            "deye_device_state",
            "deye_connection",
            "buy_price",
            "sell_price",
            "future_sell_rank",
            "legacy_enabled",
            "current_energy_trading_enabled",
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
            export_enabled = parse_bool_state(self.get_state(self.entity_ids["energy_v2_export_enabled"])) is True

            enable_validation = safe_to_enable(
                telemetry_validation,
                legacy_enabled,
                current_enabled,
                active_conflicts,
                service_mode,
            )

            decision = plan_shadow_mode(
                snapshot,
                telemetry_valid=telemetry_validation.valid and enable_validation.valid,
                export_enabled=export_enabled,
                deye_ledger_kwh=parse_float_state(self.get_state(self.entity_ids["energy_v2_deye_fv_ledger"])) or 0.0,
                solax_ledger_kwh=parse_float_state(self.get_state(self.entity_ids["energy_v2_solax_fv_ledger"])) or 0.0,
                deye_min_soc_pct=float(self.args.get("deye_min_soc_pct", 15.0)),
                deye_max_soc_pct=float(self.args.get("deye_max_soc_pct", 90.0)),
                solax_min_soc_pct=float(self.args.get("solax_min_soc_pct", 30.0)),
                solax_pv_charge_start_soc_pct=float(self.args.get("solax_pv_charge_start_soc_pct", 95.0)),
                pv_reserve_w=float(self.args.get("pv_reserve_w", 1000.0)),
                minimum_sell_price=float(self.args.get("minimum_sell_price", 3.0)),
                maximum_future_rank=int(self.args.get("maximum_future_rank", 12)),
            )

            self._set_helper("energy_v2_active_conflicts", format_conflicts(active_conflicts))
            self._set_helper("energy_v2_last_decision", format_decision(decision))

            if energy_v2_enabled and not enable_validation.valid:
                fault_text = compact_reasons(enable_validation.reasons)
                self._set_helper("energy_v2_requested_mode", mode_value(Mode.FAULT))
                self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
                self._set_helper("energy_v2_last_fault", fault_text)
                self.turn_off(self.entity_ids["energy_v2_enabled"])
                self._warn_once_fault(fault_text)
            elif energy_v2_enabled:
                self._set_helper("energy_v2_requested_mode", mode_value(decision.mode))
                self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
                self._set_helper(
                    "energy_v2_last_decision",
                    "Energy V2 phase 1 je pouze shadow; fyzické řízení není implementováno.",
                )
            else:
                self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))

            if active_conflicts != self._last_conflicts:
                self._last_conflicts = active_conflicts
                if active_conflicts:
                    self._warning("active conflicts: %s", ", ".join(active_conflicts))
            if self._last_decision != decision:
                self._last_decision = decision
                self._info("shadow decision: %s", format_decision(decision))
            if not telemetry_validation.valid:
                self._warning("invalid telemetry: %s", compact_reasons(telemetry_validation.reasons))
        except Exception as exc:  # pragma: no cover - AppDaemon runtime guard
            self._error("unexpected shadow tick error: %r", exc)

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
