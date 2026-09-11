from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from .allocator import AllocationParameters, BatteryAvailability, ShadowPowerAllocator
from .charge_controller import (
    ChargeControllerParameters,
    ChargeTelemetry,
    DeyeChargeShadowController,
    validate_charge_controller_parameters,
)
from .config import (
    DEFAULT_CONFLICTING_AUTOMATIONS,
    ENERGY_V2_HELPER_KEYS,
    ENTITY_IDS,
    LEGACY_MASTER_HELPER_KEYS,
    OPTIONAL_TELEMETRY_KEYS,
    OWNED_ACTUATORS,
    REQUIRED_TELEMETRY_KEYS,
    parse_charge_shadow_config,
)
from .diagnostics import (
    app_status_value,
    compact_reasons,
    format_conflicts,
    format_decision,
    heartbeat_value,
    mode_value,
)
from .flow import (
    ExportLimitAssessment,
    FlowAssessment,
    FlowDebouncer,
    FlowSnapshot,
    FlowState,
    FlowThresholds,
    RollingExportAverageTracker,
    SignConventions,
    SystemParameters,
    assess_export_limit,
    derive_flow_snapshot,
    merge_export_limit_assessment,
    summarize_export_limit,
    summarize_flow,
    validate_flow_thresholds,
    validate_system_parameters,
)
from .load_model import LoadModelParameters, validate_load_model_parameters
from .models import (
    AppStatus,
    BatteryId,
    Mode,
    PlannerDecision,
    SiteCommand,
    Strategy,
    TelemetryQuality,
    ValidationResult,
)
from .planner import plan_shadow_mode
from .safety import find_active_conflicts, find_missing_entities, safe_to_enable, validate_telemetry
from .shadow_controller import ShadowControlCore
from .telemetry import (
    TelemetryFreshnessConfig,
    TelemetryReader,
    parse_bool_state,
    parse_float_state,
    parse_text_state,
    validate_freshness_config,
)


class EnergyV2App(hass.Hass):
    """Passive shadow-only Energy V2 AppDaemon application.

    Phases 1-4 intentionally perform no physical SolaX/DEYE control.
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
        self._config_errors: tuple[str, ...] = ()
        self.telemetry_freshness = self._telemetry_freshness_from_args()
        freshness_errors = validate_freshness_config(self.telemetry_freshness)
        self._config_errors = (*self._config_errors, *freshness_errors)
        if freshness_errors:
            self.telemetry_freshness = TelemetryFreshnessConfig()
        self.load_parameters = LoadModelParameters(
            maximum_timestamp_skew_s=self.telemetry_freshness.fast_input_max_skew_s,
            coherence_jitter_s=self._float_arg("fast_input_coherence_jitter_s", 5.0),
        )
        load_model_errors = validate_load_model_parameters(self.load_parameters)
        self._config_errors = (*self._config_errors, *load_model_errors)
        if load_model_errors:
            self.load_parameters = LoadModelParameters(
                maximum_timestamp_skew_s=self.telemetry_freshness.fast_input_max_skew_s
            )
        self.telemetry = TelemetryReader(self, self.entity_ids, self.telemetry_freshness)
        self.sign_conventions = SignConventions(
            solax_battery_charging_positive=self._bool_arg("solax_battery_charging_positive", True),
            deye_battery_charging_positive=self._bool_arg("deye_battery_charging_positive", True),
            grid_export_positive=self._bool_arg("grid_export_positive", True),
        )
        self.system_parameters = self._system_parameters_from_args()
        self.flow_thresholds = self._flow_thresholds_from_args()
        self.flow_tick_interval_s = self._flow_tick_interval_from_args()
        self._config_errors = (
            *self._config_errors,
            *validate_system_parameters(self.system_parameters),
            *validate_flow_thresholds(self.flow_thresholds),
        )
        if self._config_errors:
            self.system_parameters = SystemParameters()
            self.flow_thresholds = FlowThresholds()
            self.flow_tick_interval_s = 5
        self.flow_debouncer = FlowDebouncer(self.flow_thresholds)
        self.charge_controller_parameters = self._charge_controller_parameters_from_args()
        self._charge_config_errors = tuple(
            error for error in self._config_errors if "charge_shadow" in error or "AppDaemon args" in error
        )
        self._config_errors = (
            *self._config_errors,
            *validate_charge_controller_parameters(self.charge_controller_parameters),
        )
        self.charge_controller = DeyeChargeShadowController(self.charge_controller_parameters)
        self.export_average_tracker = RollingExportAverageTracker(
            window_s=self.system_parameters.export_average_window_s,
            max_sample_age_s=self.system_parameters.export_sample_max_age_s,
        )
        self.control_tick_interval_s = float(self.args.get("shadow_control_interval_s", 5.0))
        self.shadow_site_target_w = float(self.args.get("shadow_site_target_w", 0.0))
        self.shadow_control = ShadowControlCore(
            load_parameters=self.load_parameters,
            allocator=ShadowPowerAllocator(
                AllocationParameters(
                    operational_export_limit_w=self.system_parameters.target_export_limit_w,
                    slew_limit_w_per_tick=float(self.args.get("shadow_slew_limit_w_per_tick", 2000.0)),
                    zero_flow_tolerance_w=float(self.args.get("shadow_zero_flow_tolerance_w", 300.0)),
                    zero_flow_confirmation_s=float(self.args.get("shadow_zero_flow_confirmation_s", 10.0)),
                )
            ),
        )
        self._debounce_handle: Any | None = None
        self._flow_tick_running = False
        self._control_tick_running = False
        self._shadow_tick_running = False
        self._last_decision: PlannerDecision | None = None
        self._last_conflicts: tuple[str, ...] = ()
        self._last_fault_text = ""
        self._last_telemetry_error_text = ""
        self._missing_required_entities: tuple[str, ...] = ()
        self._missing_optional_entities: tuple[str, ...] = ()
        self._missing_energy_v2_helpers: tuple[str, ...] = ()
        self._missing_conflicting_automations: tuple[str, ...] = ()
        self._missing_owned_actuators: tuple[str, ...] = ()
        self._charge_conflict_times: deque[datetime] = deque()
        self._charge_last_mismatch = ""

        self._info("initializing passive shadow application")
        self._refresh_energy_v2_helper_existence()
        self._set_helper("energy_v2_app_status", app_status_value(AppStatus.STARTING))
        self._validate_required_entity_configuration()
        self._register_state_listeners()
        self.run_every(self._shadow_tick, "now+5", 15 * 60)
        self.run_every(self._flow_tick, "now+5", self.flow_tick_interval_s)
        self.run_every(self._control_tick, "now+7", self.control_tick_interval_s)
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
            "energy_v2_strategy",
        )
        for key in watched_keys:
            self.listen_state(self._schedule_shadow_tick, self.entity_ids[key])
        for entity_id in self.conflicting_automations:
            self.listen_state(self._schedule_shadow_tick, entity_id)
        for entity_id in OWNED_ACTUATORS:
            self.listen_state(self._physical_actuator_changed, entity_id)

    def _schedule_shadow_tick(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict[str, Any]) -> None:
        self._cancel_debounce_timer()
        self._debounce_handle = self.run_in(self._debounced_shadow_tick, 3)

    def _cancel_debounce_timer(self) -> None:
        handle = self._debounce_handle
        self._debounce_handle = None
        if handle is not None and self.timer_running(handle):
            self.cancel_timer(handle, silent=True)

    def _debounced_shadow_tick(self, kwargs: dict[str, Any] | None = None) -> None:
        self._debounce_handle = None
        self._shadow_tick(kwargs)

    def _shadow_tick(self, kwargs: dict[str, Any] | None = None) -> None:
        if self._shadow_tick_running:
            self._warning("skipping overlapping shadow planner tick")
            return
        self._shadow_tick_running = True
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
            strategy = self._selected_strategy()

            flow_assessment = self._evaluate_flow_monitoring(snapshot, telemetry_validation)

            enable_validation = safe_to_enable(
                self._merge_config_validation(telemetry_validation),
                legacy_enabled,
                current_enabled,
                active_conflicts,
                self._missing_required_entities,
                self._missing_conflicting_automations,
                self._missing_owned_actuators,
                service_mode,
            )
            self._set_safe_to_enable_helper(enable_validation.valid)

            if shadow_mode_enabled:
                decision = plan_shadow_mode(
                    snapshot,
                    telemetry_valid=telemetry_validation.valid and not self._config_errors,
                    export_enabled=export_enabled,
                    deye_ledger_kwh=parse_float_state(self.get_state(self.entity_ids["energy_v2_deye_fv_ledger"]))
                    or 0.0,
                    solax_ledger_kwh=parse_float_state(self.get_state(self.entity_ids["energy_v2_solax_fv_ledger"]))
                    or 0.0,
                    deye_min_soc_pct=self.system_parameters.deye_min_soc_pct,
                    deye_max_soc_pct=float(self.args.get("deye_max_soc_pct", 90.0)),
                    solax_min_soc_pct=self.system_parameters.solax_min_soc_pct,
                    solax_pv_charge_start_soc_pct=float(self.args.get("solax_pv_charge_start_soc_pct", 95.0)),
                    pv_reserve_w=float(self.args.get("pv_reserve_w", 1000.0)),
                    minimum_sell_price=float(self.args.get("minimum_sell_price", 3.0)),
                    maximum_future_rank=int(self.args.get("maximum_future_rank", 12)),
                    strategy=strategy,
                    flow_assessment=flow_assessment,
                )
                if self._config_errors:
                    decision = PlannerDecision(Mode.DISABLED, "Configuration is invalid", "high")
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
                error_text = self._evaluation_error_text(
                    self._merge_config_validation(telemetry_validation), enable_validation
                )
                self._mark_shadow_disabled_evaluation(status, error_text)
            elif energy_v2_enabled and not enable_validation.valid:
                fault_text = self._evaluation_error_text(
                    self._merge_config_validation(telemetry_validation), enable_validation
                )
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
                error_text = self._evaluation_error_text(
                    self._merge_config_validation(telemetry_validation), enable_validation
                )
                self._mark_successful_shadow_evaluation(
                    AppStatus.HEALTHY,
                    requested_mode=Mode.DISABLED,
                    error_text=error_text,
                )

            if diagnostics != self._last_conflicts:
                self._last_conflicts = diagnostics
                if diagnostics:
                    self._warning("active/missing conflicts: %s", ", ".join(diagnostics))
            if self._last_decision != decision:
                self._last_decision = decision
                self._info("shadow decision: %s", format_decision(decision))
            telemetry_error_text = compact_reasons(telemetry_validation.reasons)
            if not telemetry_validation.valid and telemetry_error_text != self._last_telemetry_error_text:
                self._last_telemetry_error_text = telemetry_error_text
                self._warning("invalid telemetry: %s", telemetry_error_text)
        except Exception as exc:  # pragma: no cover - AppDaemon runtime guard
            error_text = f"unexpected shadow tick error: {exc!r}"
            self._set_helper("energy_v2_last_evaluation_error", error_text)
            self._set_helper("energy_v2_app_status", app_status_value(AppStatus.DEGRADED))
            self._error(error_text)
        finally:
            self._shadow_tick_running = False

    def _flow_tick(self, kwargs: dict[str, Any] | None = None) -> None:
        if self._flow_tick_running:
            self._warning("skipping overlapping passive flow tick")
            return
        self._flow_tick_running = True
        try:
            snapshot = self.telemetry.snapshot()
            telemetry_validation = validate_telemetry(snapshot)
            self._evaluate_flow_monitoring(snapshot, telemetry_validation)
            self._evaluate_charge_shadow()
        except Exception as exc:  # pragma: no cover - AppDaemon runtime guard
            error_text = f"unexpected flow tick error: {exc!r}"
            self._set_helper("energy_v2_last_evaluation_error", error_text)
            self._set_helper("energy_v2_app_status", app_status_value(AppStatus.DEGRADED))
            self._error(error_text)
        finally:
            self._flow_tick_running = False

    def _control_tick(self, kwargs: dict[str, Any] | None = None) -> None:
        """Evaluate and publish Phase 4 shadow commands without executing them."""
        if self._control_tick_running:
            self._warning("skipping overlapping shadow control tick")
            return
        if parse_bool_state(self.get_state(self.entity_ids["energy_v2_control_shadow_enabled"])) is not True:
            return
        self._control_tick_running = True
        try:
            now = datetime.now().astimezone()
            telemetry = self.telemetry.control_snapshot(now=now)
            command = SiteCommand(
                command_id=f"shadow-{int(now.timestamp())}",
                created_at=now,
                expires_at=now + timedelta(seconds=max(self.control_tick_interval_s * 3, 15)),
                grid_target_w=self.shadow_site_target_w,
                max_export_w=self.system_parameters.target_export_limit_w,
                transfer_allowed=False,
                preferred_battery=BatteryId.DEYE,
                grid_charge_allowed=False,
            )
            deye = BatteryAvailability(
                BatteryId.DEYE,
                telemetry.deye_soc.quality is TelemetryQuality.VALID
                and telemetry.deye_battery_power.quality is TelemetryQuality.VALID
                and parse_bool_state(self.get_state(self.entity_ids["deye_connection"])) is True,
                telemetry.deye_soc.value,
                self.system_parameters.deye_min_soc_pct,
                float(self.args.get("deye_max_soc_pct", 100.0)),
                self.system_parameters.deye_rated_power_w,
                self.system_parameters.deye_rated_power_w,
                telemetry.deye_battery_power.value
                if telemetry.deye_battery_power.quality is TelemetryQuality.VALID
                else None,
            )
            solax = BatteryAvailability(
                BatteryId.SOLAX,
                telemetry.solax_soc.quality is TelemetryQuality.VALID
                and telemetry.solax_battery_power.quality is TelemetryQuality.VALID,
                telemetry.solax_soc.value,
                self.system_parameters.solax_min_soc_pct,
                100.0,
                self.system_parameters.solax_rated_power_w,
                self.system_parameters.solax_rated_power_w,
                telemetry.solax_battery_power.value
                if telemetry.solax_battery_power.quality is TelemetryQuality.VALID
                else None,
            )
            result = self.shadow_control.evaluate(
                telemetry,
                command,
                deye=deye,
                solax=solax,
            )
            self._publish_shadow_control_diagnostics(result)
        except Exception as exc:  # pragma: no cover - AppDaemon runtime guard
            error_text = f"unexpected shadow control tick error: {exc!r}"
            self._set_helper("energy_v2_command_status", "FAULT")
            self._set_helper("energy_v2_saturation_reason", error_text)
            self._set_helper("energy_v2_last_evaluation_error", error_text)
            self._error(error_text)
        finally:
            self._control_tick_running = False

    def _publish_shadow_control_diagnostics(self, result: object) -> None:
        from .shadow_controller import ShadowControlResult

        if not isinstance(result, ShadowControlResult):
            raise TypeError("invalid shadow control result")
        load = result.load
        allocation = result.allocation
        fixed = result.fixed_quarter_budget
        trailing = result.trailing_budget
        if load.load_w is not None:
            self._set_helper("energy_v2_whole_site_load_w", f"{load.load_w:.1f}")
        self._set_helper("energy_v2_load_quality", load.quality.value)
        self._set_helper("energy_v2_load_summary", load.reason)
        self._set_helper("energy_v2_requested_site_target_w", f"{allocation.requested_grid_target_w:.1f}")
        self._set_helper("energy_v2_budget_diagnostic_target_w", f"{result.budget_diagnostic_target_w:.1f}")
        self._set_helper("energy_v2_deye_requested_power_w", f"{result.deye.requested_power_w:.1f}")
        self._set_helper("energy_v2_deye_allowed_power_w", f"{result.deye.allowed_power_w:.1f}")
        self._set_helper("energy_v2_deye_predicted_power_w", f"{result.deye.simulated_power_w:.1f}")
        if result.deye.measured_actual_power_w is not None:
            self._set_helper("energy_v2_deye_actual_power_w", f"{result.deye.measured_actual_power_w:.1f}")
        self._set_helper("energy_v2_solax_requested_power_w", f"{result.solax.requested_power_w:.1f}")
        self._set_helper("energy_v2_solax_predicted_power_w", f"{result.solax.simulated_power_w:.1f}")
        if result.solax.measured_actual_power_w is not None:
            self._set_helper("energy_v2_solax_actual_power_w", f"{result.solax.measured_actual_power_w:.1f}")
        if result.grid_actual_w is not None:
            self._set_helper("energy_v2_whole_site_grid_actual_w", f"{result.grid_actual_w:.1f}")
        if result.grid_error_w is not None:
            self._set_helper("energy_v2_grid_error_w", f"{result.grid_error_w:.1f}")
        anti_state = (
            "FAULT"
            if result.runtime_anti_transfer_state.startswith("FAULT")
            else "CLEAR"
            if result.runtime_anti_transfer_state == "CLEAR"
            else "UNVERIFIED"
        )
        self._set_helper("energy_v2_anti_transfer_state", anti_state)
        self._set_helper("energy_v2_break_before_make_state", allocation.break_before_make_state)
        self._set_helper("energy_v2_command_status", result.command_status.value)
        self._set_helper("energy_v2_command_expiry", allocation.site_command.expires_at.isoformat())
        self._set_helper("energy_v2_saturation_reason", result.reason)
        self._set_helper("energy_v2_fixed_used_export_kwh", f"{fixed.used_export_kwh:.4f}")
        self._set_helper("energy_v2_fixed_remaining_operational_kwh", f"{fixed.remaining_operational_kwh:.4f}")
        self._set_helper("energy_v2_fixed_remaining_legal_kwh", f"{fixed.remaining_legal_kwh:.4f}")
        self._set_helper("energy_v2_fixed_seconds_remaining", f"{fixed.seconds_remaining:.1f}")
        self._set_helper("energy_v2_fixed_budget_power_w", f"{fixed.budget_power_w:.1f}")
        self._set_helper("energy_v2_fixed_projected_average_w", f"{fixed.projected_average_w:.1f}")
        self._set_helper("energy_v2_trailing_remaining_operational_kwh", f"{trailing.remaining_operational_kwh:.4f}")
        self._set_helper("energy_v2_trailing_remaining_legal_kwh", f"{trailing.remaining_legal_kwh:.4f}")
        self._set_helper("energy_v2_trailing_budget_power_w", f"{trailing.budget_power_w:.1f}")
        self._set_helper("energy_v2_trailing_projected_average_w", f"{trailing.projected_average_w:.1f}")

    def _evaluate_charge_shadow(self) -> None:
        """Publish only shadow diagnostics. This method has no physical service-call path."""
        enabled = parse_bool_state(self.get_state(self.entity_ids["energy_v2_charge_shadow_enabled"])) is True
        if self._charge_config_errors:
            reason = "CONFIG_ERROR: " + "; ".join(self._charge_config_errors)
            self._set_helper("energy_v2_charge_shadow_state", "DISABLED")
            self._set_helper("energy_v2_charge_recommended_state", "DISABLED")
            self._set_helper("energy_v2_recommended_deye_charge_current_a", "0")
            self._set_helper("energy_v2_charge_decision_reason", reason)
            self._set_helper("energy_v2_charge_block_reason", reason)
            self._set_helper("energy_v2_charge_shadow_summary", reason)
            return
        if not enabled:
            return
        now = datetime.now().astimezone()
        telemetry = ChargeTelemetry(
            timestamp=now,
            solax_soc_pct=parse_float_state(self.get_state(self.entity_ids["solax_soc"])),
            solax_pv_power_w=parse_float_state(self.get_state(self.entity_ids["solax_pv_power"])),
            solax_battery_power_w=parse_float_state(self.get_state(self.entity_ids["solax_battery_power"])),
            deye_battery_power_w=parse_float_state(self.get_state(self.entity_ids["deye_battery_power"])),
            deye_battery_voltage_v=parse_float_state(self.get_state(self.entity_ids["deye_battery_voltage"])),
        )
        decision = self.charge_controller.evaluate(telemetry, enabled=True)
        live_maximum = self._deye_current_entity_maximum_a()
        if live_maximum is not None and decision.recommended_current_a > live_maximum:
            decision = replace(
                decision,
                recommended_current_a=live_maximum,
                reason=f"{decision.reason}; clamped to live entity maximum {live_maximum:g} A",
            )
        self._set_helper("energy_v2_charge_shadow_state", decision.state.value)
        self._set_helper("energy_v2_charge_recommended_state", decision.state.value)
        self._set_helper("energy_v2_recommended_deye_charge_current_a", f"{decision.recommended_current_a:.0f}")
        self._set_helper("energy_v2_charge_decision_reason", decision.reason)
        self._set_helper("energy_v2_charge_block_reason", decision.block_reason)
        self._set_helper(
            "energy_v2_charge_shadow_summary",
            f"{decision.state.value}: recommend {decision.recommended_current_a:.0f} A; {decision.reason}",
        )
        self._set_helper("energy_v2_solax_discharge_average_w", f"{decision.average_solax_discharge_w or 0:.1f}")
        self._set_helper("energy_v2_deye_charge_average_w", f"{decision.average_deye_charge_w or 0:.1f}")
        self._set_helper("energy_v2_deye_voltage_average_v", f"{decision.average_deye_voltage_v or 0:.1f}")
        self._set_helper("energy_v2_calculated_safe_charge_power_w", f"{max(decision.safe_power_w or 0, 0):.1f}")
        self._set_helper("energy_v2_calculated_safe_current_a", f"{max(decision.calculated_current_a or 0, 0):.1f}")
        mismatch = self._charge_mode_mismatches(decision.recommended_current_a)
        self._set_helper("energy_v2_charge_mode_mismatches", mismatch)
        if mismatch and mismatch != self._charge_last_mismatch:
            self._charge_conflict_times.append(now)
        self._charge_last_mismatch = mismatch
        while self._charge_conflict_times and (now - self._charge_conflict_times[0]).total_seconds() > 60:
            self._charge_conflict_times.popleft()
        self._set_helper("energy_v2_charge_conflict_count", str(len(self._charge_conflict_times)))
        if not mismatch:
            state = "MATCHED"
        elif len(self._charge_conflict_times) >= self.charge_controller_parameters.conflict_attempt_limit:
            state = "WOULD_ENTER_FAULT"
        else:
            state = "WOULD_CORRECT"
        self._set_helper("energy_v2_charge_conflict_state", state)
        self._set_helper("energy_v2_charge_conflict_summary", f"{state}: {mismatch or 'all readable modes match'}")
        elapsed = self.charge_controller._elapsed(now)
        timer_key = {
            "START_CONFIRMATION": "energy_v2_charge_start_confirmation_s",
            "TRANSFER_CONFIRMATION": "energy_v2_charge_transfer_confirmation_s",
            "RETURN_CONFIRMATION": "energy_v2_charge_return_confirmation_s",
            "FAULT": "energy_v2_charge_fault_recovery_s",
        }.get(decision.state.value)
        for key in (
            "energy_v2_charge_start_confirmation_s",
            "energy_v2_charge_transfer_confirmation_s",
            "energy_v2_charge_return_confirmation_s",
            "energy_v2_charge_fault_recovery_s",
        ):
            self._set_helper(key, f"{elapsed if key == timer_key else 0:.0f}")

    def _charge_mode_mismatches(self, current_a: float) -> str:
        expected = {
            "solax_charger_use_mode": "Self Use Mode",
            "deye_ac_coupling": "Grid",
            "deye_export_surplus": "off",
            "deye_time_of_use": "Disabled",
            "deye_work_mode": "Zero Export To CT",
        }
        mismatches: list[str] = []
        for key, wanted in expected.items():
            actual = parse_text_state(self.get_state(self.entity_ids[key]))
            if actual is None:
                mismatches.append(f"{key}=UNAVAILABLE")
            elif actual != wanted:
                mismatches.append(f"{key}={actual!r}, wants {wanted!r}")
        actual_current = parse_float_state(self.get_state(self.entity_ids["deye_charge_current"]))
        if actual_current is None:
            mismatches.append("deye_charge_current=UNAVAILABLE")
        elif round(actual_current) != round(current_a):
            mismatches.append(f"deye_charge_current={actual_current:.0f}A, wants {current_a:.0f}A")
        return "; ".join(mismatches)[:255]

    def _evaluate_flow_monitoring(
        self,
        snapshot: object,
        telemetry_validation: ValidationResult,
    ) -> FlowAssessment:
        if telemetry_validation.valid:
            flow_snapshot = derive_flow_snapshot(snapshot, self.sign_conventions)
            now = datetime.now().astimezone()
            rolling_export = self.export_average_tracker.add_sample(now, flow_snapshot.grid_export_w)
            export_limit_assessment = assess_export_limit(
                flow_snapshot.grid_export_w,
                rolling_export,
                self.system_parameters,
            )
            flow_assessment = merge_export_limit_assessment(
                self.flow_debouncer.assess(flow_snapshot, now),
                export_limit_assessment,
            )
            self._publish_flow_diagnostics(flow_snapshot, flow_assessment)
            self._publish_export_limit_diagnostics(export_limit_assessment)
            self._set_helper("energy_v2_last_valid_export_sample", heartbeat_value())
        else:
            flow_assessment = FlowAssessment(FlowState.UNKNOWN, warnings=("Telemetry is not valid",))
            self._publish_invalid_flow_diagnostics(flow_assessment)
            self._publish_unknown_export_limit_diagnostics()
        return flow_assessment

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
        if entity_id in self._missing_energy_v2_helpers:
            self._error("refusing to write missing helper %s", entity_id)
            return
        domain = entity_id.split(".", 1)[0]
        if domain == "input_select":
            self.call_service("input_select/select_option", entity_id=entity_id, option=value)
        elif domain == "input_text":
            self.call_service("input_text/set_value", entity_id=entity_id, value=value[:255])
        elif domain == "input_datetime":
            self.call_service("input_datetime/set_datetime", entity_id=entity_id, datetime=value)
        elif domain == "input_number" and key.startswith("energy_v2_"):
            self.call_service("input_number/set_value", entity_id=entity_id, value=float(value))
        elif domain == "input_boolean" and key == "energy_v2_safe_to_enable":
            service = "input_boolean/turn_on" if value == "on" else "input_boolean/turn_off"
            self.call_service(service, entity_id=entity_id)
        else:
            self._error("refusing to write unsupported helper domain for %s", entity_id)

    def _set_safe_to_enable_helper(self, safe: bool) -> None:
        self._set_helper("energy_v2_safe_to_enable", "on" if safe else "off")

    def _selected_strategy(self) -> Strategy:
        value = parse_text_state(self.get_state(self.entity_ids["energy_v2_strategy"]))
        try:
            return Strategy(value or Strategy.SUMMER_NO_GRID_CHARGE.value)
        except ValueError:
            self._warning("unknown strategy %r; keeping phase 2 passive", value)
            return Strategy.SERVICE

    def _charge_controller_parameters_from_args(self) -> ChargeControllerParameters:
        parameters, errors = parse_charge_shadow_config(self.args)
        self._config_errors = (*self._config_errors, *errors)
        return parameters

    def _deye_current_entity_maximum_a(self) -> float | None:
        state = self.get_state(self.entity_ids["deye_charge_current"], attribute="all")
        if not isinstance(state, dict):
            return None
        attributes = state.get("attributes")
        if not isinstance(attributes, dict):
            return None
        maximum = parse_float_state(attributes.get("max"))
        return maximum if maximum is not None and maximum >= 0 else None

    def _publish_flow_diagnostics(self, snapshot: FlowSnapshot, assessment: FlowAssessment) -> None:
        self._set_helper("energy_v2_flow_state", assessment.state.value)
        self._set_helper("energy_v2_flow_summary", summarize_flow(snapshot, assessment))
        self._set_helper("energy_v2_flow_warning", compact_reasons(assessment.warnings + assessment.transients))
        self._set_helper("energy_v2_flow_violation", compact_reasons(assessment.violations))
        if assessment.violations:
            self._set_helper("energy_v2_last_flow_violation", heartbeat_value())

    def _publish_invalid_flow_diagnostics(self, assessment: FlowAssessment) -> None:
        self._set_helper("energy_v2_flow_state", assessment.state.value)
        self._set_helper("energy_v2_flow_summary", "UNKNOWN: telemetry is not valid")
        self._set_helper("energy_v2_flow_warning", compact_reasons(assessment.warnings))
        self._set_helper("energy_v2_flow_violation", "")

    def _publish_export_limit_diagnostics(self, assessment: ExportLimitAssessment) -> None:
        self._set_helper("energy_v2_instant_grid_export_w", f"{assessment.instant_export_w:.3f}")
        self._set_helper("energy_v2_rolling_15min_export_w", f"{assessment.rolling_average.average_w:.3f}")
        self._set_helper("energy_v2_export_window_covered_s", f"{assessment.rolling_average.covered_duration_s:.3f}")
        if assessment.rolling_average.last_sample_age_s is not None:
            self._set_helper("energy_v2_export_sample_age_s", f"{assessment.rolling_average.last_sample_age_s:.3f}")
        self._set_helper("energy_v2_export_limit_state", assessment.state.value)
        self._set_helper("energy_v2_export_limit_summary", summarize_export_limit(assessment))
        if assessment.violations:
            self._set_helper("energy_v2_last_export_average_violation", heartbeat_value())

    def _publish_unknown_export_limit_diagnostics(self) -> None:
        average = self.export_average_tracker.average(datetime.now().astimezone())
        if average.last_sample_age_s is not None:
            self._set_helper("energy_v2_export_sample_age_s", f"{average.last_sample_age_s:.3f}")
        self._set_helper("energy_v2_export_limit_state", "UNKNOWN")
        self._set_helper(
            "energy_v2_export_limit_summary",
            "UNKNOWN: telemetry is not valid; numeric export helpers contain last known values",
        )

    def _system_parameters_from_args(self) -> SystemParameters:
        defaults = SystemParameters()
        return SystemParameters(
            solax_rated_power_w=self._float_arg("solax_rated_power_w", defaults.solax_rated_power_w),
            solax_battery_capacity_kwh=self._float_arg(
                "solax_battery_capacity_kwh", defaults.solax_battery_capacity_kwh
            ),
            solax_min_soc_pct=self._float_arg("solax_min_soc_pct", defaults.solax_min_soc_pct),
            deye_rated_power_w=self._float_arg("deye_rated_power_w", defaults.deye_rated_power_w),
            deye_battery_capacity_kwh=self._float_arg("deye_battery_capacity_kwh", defaults.deye_battery_capacity_kwh),
            deye_min_soc_pct=self._float_arg("deye_min_soc_pct", defaults.deye_min_soc_pct),
            target_export_limit_w=self._float_arg("target_export_limit_w", defaults.target_export_limit_w),
            legal_export_average_limit_w=self._float_arg(
                "legal_export_average_limit_w", defaults.legal_export_average_limit_w
            ),
            export_average_warning_w=self._float_arg("export_average_warning_w", defaults.export_average_warning_w),
            export_average_window_s=self._float_arg("export_average_window_s", defaults.export_average_window_s),
            export_sample_max_age_s=self._float_arg("export_sample_max_age_s", defaults.export_sample_max_age_s),
        )

    def _telemetry_freshness_from_args(self) -> TelemetryFreshnessConfig:
        defaults = TelemetryFreshnessConfig()
        return TelemetryFreshnessConfig(
            solax_fast_power_max_age_s=self._float_arg(
                "solax_fast_power_max_age_s", defaults.solax_fast_power_max_age_s
            ),
            deye_fast_power_max_age_s=self._float_arg("deye_fast_power_max_age_s", defaults.deye_fast_power_max_age_s),
            fast_input_max_skew_s=self._float_arg("fast_input_max_skew_s", defaults.fast_input_max_skew_s),
            solax_source_health_window_s=self._float_arg(
                "solax_source_health_window_s", defaults.solax_source_health_window_s
            ),
            deye_source_health_window_s=self._float_arg(
                "deye_source_health_window_s", defaults.deye_source_health_window_s
            ),
        )

    def _flow_thresholds_from_args(self) -> FlowThresholds:
        defaults = FlowThresholds()
        args = self.args.get("flow_thresholds", {})
        if not isinstance(args, dict):
            self._config_errors = (*self._config_errors, "flow_thresholds must be a mapping")
            args = {}
        return FlowThresholds(
            grid_import_warning_w=self._float_arg("grid_import_warning_w", defaults.grid_import_warning_w, source=args),
            grid_import_violation_w=self._float_arg(
                "grid_import_violation_w", defaults.grid_import_violation_w, source=args
            ),
            battery_flow_warning_w=self._float_arg(
                "battery_flow_warning_w", defaults.battery_flow_warning_w, source=args
            ),
            battery_flow_violation_w=self._float_arg(
                "battery_flow_violation_w", defaults.battery_flow_violation_w, source=args
            ),
            minimum_deye_charge_w=self._float_arg("minimum_deye_charge_w", defaults.minimum_deye_charge_w, source=args),
            minimum_export_w=self._float_arg("minimum_export_w", defaults.minimum_export_w, source=args),
            pv_surplus_reserve_w=self._float_arg("pv_surplus_reserve_w", defaults.pv_surplus_reserve_w, source=args),
            warning_persistence_s=self._float_arg("warning_persistence_s", defaults.warning_persistence_s, source=args),
            violation_persistence_s=self._float_arg(
                "violation_persistence_s", defaults.violation_persistence_s, source=args
            ),
            telemetry_stale_timeout_s=self._float_arg(
                "telemetry_stale_timeout_s", defaults.telemetry_stale_timeout_s, source=args
            ),
        )

    def _flow_tick_interval_from_args(self) -> int:
        value = self._float_arg("flow_tick_interval_s", 5.0)
        if value < 1:
            self._config_errors = (*self._config_errors, "flow_tick_interval_s must be >= 1")
            return 5
        return int(value)

    def _float_arg(self, key: str, default: float, source: dict[str, Any] | None = None) -> float:
        raw = (source if source is not None else self.args).get(key, default)
        value = parse_float_state(raw)
        if value is None:
            self._config_errors = (*self._config_errors, f"{key} must be a finite number")
            return default
        return value

    def _bool_arg(self, key: str, default: bool) -> bool:
        value = parse_bool_state(self.args.get(key, default))
        if value is None:
            self._config_errors = (*self._config_errors, f"{key} must be boolean")
            return default
        return value

    def _merge_config_validation(self, telemetry_validation: ValidationResult) -> ValidationResult:
        reasons = (*telemetry_validation.reasons, *self._config_errors)
        return ValidationResult(valid=not reasons, reasons=reasons)

    def _refresh_energy_v2_helper_existence(self) -> None:
        helper_entity_ids = tuple(self.entity_ids[key] for key in ENERGY_V2_HELPER_KEYS)
        helper_states = {entity_id: self._entity_state_object(entity_id) for entity_id in helper_entity_ids}
        self._missing_energy_v2_helpers = find_missing_entities(helper_states, helper_entity_ids)
        if self._missing_energy_v2_helpers:
            self._error("missing Energy V2 helpers: %s", ", ".join(self._missing_energy_v2_helpers))

    def _refresh_entity_existence_diagnostics(self) -> None:
        self._refresh_energy_v2_helper_existence()
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

    def _mark_successful_shadow_evaluation(
        self,
        status: AppStatus,
        requested_mode: Mode | None = None,
        error_text: str = "",
    ) -> None:
        if requested_mode is not None:
            self._set_helper("energy_v2_requested_mode", mode_value(requested_mode))
        self._set_helper("energy_v2_actual_mode", mode_value(Mode.DISABLED))
        self._set_helper("energy_v2_last_evaluation_error", error_text)
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

    def _evaluation_error_text(self, telemetry: ValidationResult, enable: ValidationResult) -> str:
        if telemetry.reasons and enable.reasons:
            parts = (
                "Invalid telemetry: " + compact_reasons(telemetry.reasons, max_len=110),
                "Safe-to-enable blocked: " + compact_reasons(enable.reasons, max_len=110),
            )
            return compact_reasons(parts)
        if telemetry.reasons:
            return "Invalid telemetry: " + compact_reasons(telemetry.reasons, max_len=236)
        if enable.reasons:
            return "Safe-to-enable blocked: " + compact_reasons(enable.reasons, max_len=231)
        return ""

    def _info(self, message: str, *args: Any) -> None:
        self.log(f"{self.log_prefix} " + message, *args, level="INFO")

    def _warning(self, message: str, *args: Any) -> None:
        self.log(f"{self.log_prefix} " + message, *args, level="WARNING")

    def _error(self, message: str, *args: Any) -> None:
        self.log(f"{self.log_prefix} " + message, *args, level="ERROR")
