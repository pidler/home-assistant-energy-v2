"""AppDaemon wrapper for the Phase 5B observer/publisher runtime."""

from __future__ import annotations

import appdaemon.plugins.hass.hassapi as hass

from .executor_runtime import HomeAssistantExecutorAdapter, RuntimeConfig, ShadowExecutorRuntime


class Phase5BShadowExecutorApp(hass.Hass):
    def initialize(self) -> None:
        self.runtime = None
        try:
            settings = dict(self.args.get("phase5b_shadow", {}))
            config = RuntimeConfig(**settings)
            self.runtime = ShadowExecutorRuntime(
                HomeAssistantExecutorAdapter(self, config),
                self,
                config,
            )
        except (TypeError, ValueError) as error:
            self.log(f"PHASE5B_CONFIG_REJECTED:{type(error).__name__}"[:160], level="ERROR")
            return
        self.run_every(self._tick, "now", self.runtime.config.cadence_s)

    def _tick(self, kwargs) -> None:
        if self.runtime is not None:
            self.runtime.tick()
