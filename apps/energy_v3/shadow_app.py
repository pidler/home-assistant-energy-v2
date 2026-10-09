"""Read-only AppDaemon runtime for ENERGY V3.1 shadow diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from .controller import decide
from .haeo_adapter import HAEO_ENTITY_IDS, HaeoTargetResult, current_target_from_states
from .models import CapabilityLevel, CurrentTarget, Decision, V3Capabilities
from .telemetry_adapter import TELEMETRY_ENTITY_IDS, TelemetryResult, safety_snapshot_from_states

DIAGNOSTIC_ENTITIES = {
    "interval": "sensor.energy_v3_shadow_haeo_interval",
    "solax_target": "sensor.energy_v3_shadow_solax_target",
    "deye_target": "sensor.energy_v3_shadow_deye_target",
    "decision": "sensor.energy_v3_shadow_decision",
    "reason": "sensor.energy_v3_shadow_rejection_reason",
    "telemetry": "sensor.energy_v3_shadow_telemetry",
    "export_authorization": "sensor.energy_v3_shadow_export_authorization",
}


class EnergyV3ShadowApp(hass.Hass):
    """Observe HAEO and telemetry, run pure V3 decisions, and publish sensors only."""

    def initialize(self) -> None:
        self._evaluation_pending = False
        self._coalesce_seconds = float(self.args.get("coalesce_seconds", 1.0))
        self._periodic_seconds = int(self.args.get("periodic_seconds", 30))
        self._optimizer_max_age = timedelta(minutes=float(self.args.get("optimizer_max_age_minutes", 30)))
        for entity_id in (*HAEO_ENTITY_IDS, *TELEMETRY_ENTITY_IDS):
            self.listen_state(self._input_changed, entity_id)
        self.run_every(self._periodic_evaluation, "now", self._periodic_seconds)

    def _input_changed(self, _entity: str, _attribute: str, _old: Any, _new: Any, _kwargs: Any) -> None:
        if self._evaluation_pending:
            return
        self._evaluation_pending = True
        try:
            self.run_in(self._coalesced_evaluation, self._coalesce_seconds)
        except Exception as error:
            self._evaluation_pending = False
            self._publish_runtime_error(datetime.now(UTC), type(error).__name__)

    def _coalesced_evaluation(self, _kwargs: Any) -> None:
        self._evaluation_pending = False
        self._evaluate()

    def _periodic_evaluation(self, _kwargs: Any) -> None:
        self._evaluate()

    def _evaluate(self) -> None:
        now = datetime.now(UTC)
        try:
            first_states = self._collect_states()
            states = self._collect_states()
            haeo = current_target_from_states(
                states,
                now=now,
                max_optimizer_age=self._optimizer_max_age,
                comparison_states=first_states,
            )
            telemetry = safety_snapshot_from_states(states, now=now)
            decision = decide(haeo.target, telemetry.snapshot, now=now) if haeo.target is not None else None
            self._publish(haeo, telemetry, decision, now)
        except Exception as error:  # AppDaemon callback must survive bad inputs and publication failures.
            self._publish_runtime_error(now, type(error).__name__)

    def _collect_states(self) -> dict[str, Any]:
        all_states = self.get_state()
        if not isinstance(all_states, Mapping):
            raise TypeError("AppDaemon did not return a state snapshot")
        return {
            entity_id: all_states.get(entity_id)
            for entity_id in dict.fromkeys((*HAEO_ENTITY_IDS, *TELEMETRY_ENTITY_IDS))
        }

    def _publish(
        self,
        haeo: HaeoTargetResult,
        telemetry: TelemetryResult,
        decision: Decision | None,
        now: datetime,
    ) -> None:
        target = haeo.target
        common = {
            "shadow_mode": _diagnostic_bool(True),
            "physical_control": _diagnostic_bool(False),
            "evaluated_at": now.isoformat(),
        }
        layers = _diagnostic_layers(haeo, telemetry, decision)
        # A failed partial publication must never leave a positive decision visible.
        self.set_state(
            DIAGNOSTIC_ENTITIES["decision"],
            state="RETURN_TO_NORMAL",
            attributes={
                **common,
                **layers,
                "controller_evaluated": _diagnostic_bool(False),
                "publication_complete": _diagnostic_bool(False),
            },
            replace=True,
        )
        self.set_state(
            DIAGNOSTIC_ENTITIES["reason"],
            state="DIAGNOSTIC_PUBLICATION_IN_PROGRESS",
            attributes={**common, **layers, "publication_complete": _diagnostic_bool(False)},
            replace=True,
        )
        interval_state = target.timestamp.isoformat() if target else "invalid"
        self.set_state(
            DIAGNOSTIC_ENTITIES["interval"],
            state=interval_state,
            attributes={
                **common,
                "valid_until": target.valid_until.isoformat() if target else None,
                "optimizer_status": haeo.optimizer_status,
                "optimizer_last_run": haeo.optimizer_last_run.isoformat() if haeo.optimizer_last_run else None,
                "input_valid": _diagnostic_bool(haeo.valid),
                "input_error": haeo.error.value if haeo.error else None,
            },
            replace=True,
        )
        self._set_power("solax_target", target.solax_target_w if target else None, common)
        self._set_power("deye_target", target.deye_target_w if target else None, common)
        self.set_state(
            DIAGNOSTIC_ENTITIES["telemetry"],
            state=telemetry.status.value,
            attributes={
                **common,
                "issues": list(telemetry.issues),
                "freshness_basis": telemetry.freshness_basis,
                "physical_measurement_freshness_verified": _diagnostic_bool(
                    telemetry.physical_measurement_freshness_verified
                ),
                "manual_legacy_control": "observed_not_owned",
                **telemetry.observed_modes,
            },
            replace=True,
        )
        self.set_state(
            DIAGNOSTIC_ENTITIES["export_authorization"],
            state="missing",
            attributes={**common, "discharge_allowed": _diagnostic_bool(False), "source": None},
            replace=True,
        )
        reason = decision.reason.value if decision else f"HAEO_{haeo.error.value if haeo.error else 'INVALID'}"
        self.set_state(
            DIAGNOSTIC_ENTITIES["reason"],
            state=reason,
            attributes={**common, **layers, "publication_complete": _diagnostic_bool(True)},
            replace=True,
        )
        self.set_state(
            DIAGNOSTIC_ENTITIES["decision"],
            state=decision.state.value if decision else "RETURN_TO_NORMAL",
            attributes={
                **common,
                **layers,
                "controller_evaluated": _diagnostic_bool(decision is not None),
                "publication_complete": _diagnostic_bool(True),
            },
            replace=True,
        )

    def _publish_runtime_error(self, now: datetime, error_type: str) -> None:
        common = {
            "shadow_mode": _diagnostic_bool(True),
            "physical_control": _diagnostic_bool(False),
            "evaluated_at": now.isoformat(),
            "runtime_error_type": error_type,
        }
        values = {
            "decision": ("RETURN_TO_NORMAL", {**common, "controller_evaluated": _diagnostic_bool(False)}),
            "reason": ("RUNTIME_ERROR", common),
            "interval": (
                "invalid",
                {**common, "input_valid": _diagnostic_bool(False), "input_error": "RUNTIME_ERROR"},
            ),
            "solax_target": ("unavailable", {**common, "unit_of_measurement": "W", "device_class": "power"}),
            "deye_target": ("unavailable", {**common, "unit_of_measurement": "W", "device_class": "power"}),
            "telemetry": ("runtime_error", common),
            "export_authorization": (
                "missing",
                {**common, "discharge_allowed": _diagnostic_bool(False), "source": None},
            ),
        }
        for key, (state, attributes) in values.items():
            try:
                self.set_state(DIAGNOSTIC_ENTITIES[key], state=state, attributes=attributes, replace=True)
            except Exception:
                continue

    def _set_power(self, key: str, value: float | None, common: dict[str, Any]) -> None:
        self.set_state(
            DIAGNOSTIC_ENTITIES[key],
            state="unavailable" if value is None else str(value),
            attributes={**common, "unit_of_measurement": "W", "device_class": "power"},
            replace=True,
        )


def _diagnostic_layers(
    haeo: HaeoTargetResult,
    telemetry: TelemetryResult,
    decision: Decision | None,
) -> dict[str, Any]:
    return {
        "haeo_valid": _diagnostic_bool(haeo.valid),
        "telemetry_status": telemetry.status.value,
        "missing_fault_evidence": _diagnostic_bool(
            any("fault:evidence_missing" in issue for issue in telemetry.issues)
        ),
        "export_authorization_status": "missing",
        "hardware_capability_status": _capability_status(haeo.target),
        "controller_reason": decision.reason.value if decision else None,
    }


def _diagnostic_bool(value: bool) -> str:
    """Keep both boolean values explicit through AppDaemon's HTTP cleaner."""
    return "true" if value else "false"


def _capability_status(target: CurrentTarget | None) -> str:
    if target is None or (target.solax_target_w == 0 and target.deye_target_w == 0):
        return "not_requested"
    capabilities = V3Capabilities()
    required: list[CapabilityLevel] = []
    if target.solax_target_w:
        required.append(
            capabilities.solax.set_power_discharge if target.solax_target_w > 0 else capabilities.solax.set_power_charge
        )
    if target.deye_target_w:
        required.append(capabilities.deye.export_power if target.deye_target_w > 0 else capabilities.deye.charge_power)
        if target.deye_target_w > 0:
            required.append(capabilities.deye.enter_export)
    return "verified" if all(item is CapabilityLevel.VERIFIED for item in required) else "unsupported"
