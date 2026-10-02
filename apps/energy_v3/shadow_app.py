"""Read-only AppDaemon runtime for ENERGY V3.1 shadow diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

from .controller import decide
from .haeo_adapter import HAEO_ENTITY_IDS, HaeoTargetResult, current_target_from_states
from .models import Decision
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
        self.run_in(self._coalesced_evaluation, self._coalesce_seconds)

    def _coalesced_evaluation(self, _kwargs: Any) -> None:
        self._evaluation_pending = False
        self._evaluate()

    def _periodic_evaluation(self, _kwargs: Any) -> None:
        self._evaluate()

    def _evaluate(self) -> None:
        now = datetime.now(UTC)
        states = {
            entity_id: self.get_state(entity_id, attribute="all")
            for entity_id in dict.fromkeys((*HAEO_ENTITY_IDS, *TELEMETRY_ENTITY_IDS))
        }
        haeo = current_target_from_states(states, now=now, max_optimizer_age=self._optimizer_max_age)
        telemetry = safety_snapshot_from_states(states, now=now)
        decision = decide(haeo.target, telemetry.snapshot, now=now) if haeo.target is not None else None
        self._publish(haeo, telemetry, decision, now)

    def _publish(
        self,
        haeo: HaeoTargetResult,
        telemetry: TelemetryResult,
        decision: Decision | None,
        now: datetime,
    ) -> None:
        target = haeo.target
        common = {"shadow_mode": True, "physical_control": False, "evaluated_at": now.isoformat()}
        interval_state = target.timestamp.isoformat() if target else "invalid"
        self.set_state(
            DIAGNOSTIC_ENTITIES["interval"],
            state=interval_state,
            attributes={
                **common,
                "valid_until": target.valid_until.isoformat() if target else None,
                "optimizer_status": haeo.optimizer_status,
                "optimizer_last_run": haeo.optimizer_last_run.isoformat() if haeo.optimizer_last_run else None,
                "input_valid": haeo.valid,
                "input_error": haeo.error.value if haeo.error else None,
            },
        )
        self._set_power("solax_target", target.solax_target_w if target else None, common)
        self._set_power("deye_target", target.deye_target_w if target else None, common)
        self.set_state(
            DIAGNOSTIC_ENTITIES["decision"],
            state=decision.state.value if decision else "RETURN_TO_NORMAL",
            attributes={**common, "controller_evaluated": decision is not None},
        )
        reason = decision.reason.value if decision else f"HAEO_{haeo.error.value if haeo.error else 'INVALID'}"
        self.set_state(DIAGNOSTIC_ENTITIES["reason"], state=reason, attributes=common)
        self.set_state(
            DIAGNOSTIC_ENTITIES["telemetry"],
            state=telemetry.status.value,
            attributes={
                **common,
                "issues": list(telemetry.issues),
                "manual_legacy_control": "observed_not_owned",
                **telemetry.observed_modes,
            },
        )
        self.set_state(
            DIAGNOSTIC_ENTITIES["export_authorization"],
            state="missing",
            attributes={**common, "discharge_allowed": False, "source": None},
        )

    def _set_power(self, key: str, value: float | None, common: dict[str, Any]) -> None:
        self.set_state(
            DIAGNOSTIC_ENTITIES[key],
            state="unavailable" if value is None else value,
            attributes={**common, "unit_of_measurement": "W", "device_class": "power"},
        )
