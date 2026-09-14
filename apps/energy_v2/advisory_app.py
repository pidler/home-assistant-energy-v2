"""Isolated AppDaemon advisory entry point. No service/controller capabilities used."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

from .advisory import OUTPUTS, AdvisoryConfig, AdvisoryRuntime, LiveAdapter, render_report
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
        self.run_every(self.evaluate_advisory, "now", 10)

    def evaluate_advisory(self, kwargs):
        now = datetime.now().astimezone()
        diagnostics = self.runtime.tick(now)
        # Publication is restricted to a fixed, non-actuating sensor allowlist.
        # No service calls, legacy helpers, entity overrides or execution hooks.
        self.set_state(
            OUTPUTS["status"],
            state="UPDATING",
            attributes={
                "advisory_only": True,
                "valid": False,
                "evaluated_at": now.isoformat(),
            },
        )
        # Commit the status last. A failed write leaves UPDATING, never ADVISORY.
        ordered_keys = [key for key in OUTPUTS if key != "status"] + ["status"]
        for key in ordered_keys:
            entity = OUTPUTS[key]
            self.set_state(
                entity,
                state=str(diagnostics[key])[:255],
                attributes={
                    "friendly_name": "Phase 5A " + key.replace("_", " "),
                    "advisory_only": True,
                    "evaluated_at": now.isoformat(),
                    "valid": self.runtime.valid and diagnostics["status"] == "ADVISORY",
                    "deye_operating_state": self.runtime.adapter.last_deye_state,
                },
            )
        plan = self.runtime.plan
        if plan and plan["id"] != self.last_logged_id:
            self.last_logged_id = plan["id"]
            self.rendered_report = render_report(plan)
            self.log(self.rendered_report)
            structured = {**plan, "result": asdict(plan["result"]), "replan": self.runtime.events[-1]}
            self.log("PHASE5A_STRUCTURED_PLAN " + json.dumps(structured, default=str, sort_keys=True))
        if diagnostics["status"] != "ADVISORY":
            self.rendered_report = "LAST_KNOWN / INVALID: " + str(diagnostics["degraded_inputs"])
