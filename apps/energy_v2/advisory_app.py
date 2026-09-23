"""Isolated AppDaemon advisory entry point. No service/controller capabilities used."""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict
from datetime import UTC, datetime

import appdaemon.plugins.hass.hassapi as hass

from .advisory import (
    EXECUTOR_INTENT_ENTITY,
    OUTPUTS,
    AdvisoryConfig,
    AdvisoryRuntime,
    LiveAdapter,
    executor_intent_payload,
    render_report,
    safe_none_intent,
)
from .trading.models import BatteryParameters, BatteryRole, PlannerConfig


class Phase5AdvisoryApp(hass.Hass):
    def initialize(self):
        settings = dict(self.args.get("advisory", {}))
        # Policies are data only; no controller is constructed or resolved.
        if "batteries" in settings:
            settings["batteries"] = tuple(
                BatteryParameters(**{**b, "role": BatteryRole(b["role"])}) for b in settings["batteries"]
            )
        if "planner" in settings:
            from datetime import time

            policy = dict(settings["planner"])
            for key in tuple(policy):
                if key.endswith("local_time") and policy[key] is not None:
                    policy[key] = time.fromisoformat(policy[key])
            settings["planner"] = PlannerConfig(**policy)
        self.runtime = AdvisoryRuntime(LiveAdapter(self, AdvisoryConfig(**settings)))
        self.last_logged_id = None
        self.rendered_report = "No valid advisory plan"
        self._evaluation_lock = threading.Lock()
        self._publication_instance_id = secrets.token_hex(16)
        self._publication_sequence = 0
        self.run_every(self.evaluate_advisory, "now", 10)

    def _log_failure(self, stage, error):
        detail = str(error).replace("\n", " ")[:160]
        self.log(f"PHASE5A_{stage}_FAILED:{type(error).__name__}:{detail}"[:240], level="ERROR")

    def _next_publication_id(self):
        self._publication_sequence += 1
        return f"{self._publication_instance_id}:{self._publication_sequence:x}"

    def _updating_attributes(self, now, publication_id):
        return {
            "advisory_only": True,
            "valid": False,
            "committed": False,
            "publication_id": publication_id,
            "evaluated_at": now.isoformat(),
        }

    def evaluate_advisory(self, kwargs):
        if not self._evaluation_lock.acquire(blocking=False):
            self.log("PHASE5A_EVALUATION_SKIPPED:ALREADY_RUNNING", level="WARNING")
            return
        try:
            self._evaluate_advisory_locked(datetime.now().astimezone())
        finally:
            self._evaluation_lock.release()

    def _evaluate_advisory_locked(self, now):
        publication_id = self._next_publication_id()
        preliminary_state, preliminary_attributes = safe_none_intent(
            now,
            publication_id_value=publication_id,
            plan_id="REPLANNING",
            reason_code="REPLANNING",
            reason="Prior intent invalidated before advisory evaluation",
        )
        try:
            self.set_state(
                OUTPUTS["status"],
                state="UPDATING",
                attributes=self._updating_attributes(now, publication_id),
            )
        except Exception as error:
            self._log_failure("UPDATING_STATUS_PUBLICATION", error)
            try:
                self.set_state(
                    EXECUTOR_INTENT_ENTITY,
                    state=preliminary_state,
                    attributes=preliminary_attributes,
                )
            except Exception as invalidation_error:
                self._log_failure("PRELIMINARY_INTENT_INVALIDATION", invalidation_error)
            return
        try:
            self.set_state(
                EXECUTOR_INTENT_ENTITY,
                state=preliminary_state,
                attributes=preliminary_attributes,
            )
        except Exception as error:
            self._log_failure("PRELIMINARY_INTENT_INVALIDATION", error)
            return

        try:
            diagnostics = self.runtime.tick(now)
        except Exception as error:
            self._log_failure("EVALUATION", error)
            try:
                self.set_state(
                    OUTPUTS["status"],
                    state="DEGRADED",
                    attributes={
                        **self._updating_attributes(now, publication_id),
                        "error": (f"{type(error).__name__}:{error}").replace("\n", " ")[:160],
                    },
                )
            except Exception as publication_error:
                self._log_failure("DEGRADED_STATUS_PUBLICATION", publication_error)
            return

        try:
            for key in (key for key in OUTPUTS if key != "status"):
                self.set_state(
                    OUTPUTS[key],
                    state=str(diagnostics[key])[:255],
                    attributes={
                        "friendly_name": "Phase 5A " + key.replace("_", " "),
                        "advisory_only": True,
                        "evaluated_at": now.isoformat(),
                        "valid": self.runtime.valid and diagnostics["status"] == "ADVISORY",
                        "deye_operating_state": self.runtime.adapter.last_deye_state,
                    },
                )
            intent_state, intent_attributes = executor_intent_payload(
                self.runtime,
                now,
                publication_id_value=publication_id,
            )
            self.set_state(EXECUTOR_INTENT_ENTITY, state=intent_state, attributes=intent_attributes)
            committed = diagnostics["status"] == "ADVISORY"
            committed_at = datetime.now().astimezone(UTC) if committed else None
            self.set_state(
                OUTPUTS["status"],
                state=str(diagnostics["status"])[:255],
                attributes={
                    "friendly_name": "Phase 5A status",
                    "advisory_only": True,
                    "evaluated_at": now.isoformat(),
                    "valid": committed,
                    "committed": committed,
                    "committed_at": committed_at.isoformat() if committed_at else None,
                    "publication_id": publication_id,
                    "plan_id": intent_attributes["plan_id"],
                    "intent_id": intent_attributes["intent_id"],
                    "deye_operating_state": self.runtime.adapter.last_deye_state,
                },
            )
        except Exception as error:
            self._log_failure("FINAL_PUBLICATION", error)
            try:
                self.set_state(
                    EXECUTOR_INTENT_ENTITY,
                    state=preliminary_state,
                    attributes=preliminary_attributes,
                )
            except Exception as invalidation_error:
                self._log_failure("FINAL_INTENT_INVALIDATION", invalidation_error)
            return

        plan = self.runtime.plan
        if plan and plan["id"] != self.last_logged_id:
            self.last_logged_id = plan["id"]
            self.rendered_report = render_report(plan)
            self.log(self.rendered_report)
            structured = {**plan, "result": asdict(plan["result"]), "replan": self.runtime.events[-1]}
            self.log("PHASE5A_STRUCTURED_PLAN " + json.dumps(structured, default=str, sort_keys=True))
        if diagnostics["status"] != "ADVISORY":
            self.rendered_report = "LAST_KNOWN / INVALID: " + str(diagnostics["degraded_inputs"])
