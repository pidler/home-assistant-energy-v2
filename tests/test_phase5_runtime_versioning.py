from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest

import apps.energy_v2.advisory as advisory_module
from apps.energy_v2.advisory import PV_ENTITIES, AdvisoryConfig, AdvisoryRuntime, LiveInputs, pv_profile
from apps.energy_v2.executor_runtime import (
    DEFAULT_ENTITY_MAP,
    HomeAssistantExecutorAdapter,
    RuntimeConfig,
    ShadowExecutorRuntime,
)
from apps.energy_v2.trading.cli import synthetic_example
from apps.energy_v2.trading.planner import plan_trading_schedule
from tests.test_app import install_appdaemon_stub


class Publisher:
    def __init__(self) -> None:
        self.states: list[tuple[str, str, dict[str, object]]] = []

    def set_state(self, entity_id: str, *, state: str, attributes: dict[str, object]) -> None:
        self.states.append((entity_id, state, attributes))


class MissingReader:
    def get_state(self, entity_id: str, **kwargs: object) -> None:
        return None


def test_phase5a_advisory_runtime_invokes_existing_planner(monkeypatch) -> None:
    monkeypatch.setattr(advisory_module, "ZoneInfo", lambda _name: UTC)
    data = synthetic_example()
    now = data.generated_at
    assert now is not None

    class Adapter:
        config = AdvisoryConfig(timezone="UTC")

        def collect(self, current: datetime) -> LiveInputs:
            assert current == now
            return LiveInputs(data, 1000.0, "READY", "prices", "pv", {}, {}, [])

    calls: list[object] = []

    def planner(inputs, config):
        calls.append(inputs)
        return plan_trading_schedule(inputs, config)

    runtime = AdvisoryRuntime(Adapter(), planner=planner)
    diagnostics = runtime.tick(now)

    assert calls == [data]
    assert diagnostics["status"] == "ADVISORY"


def test_phase5a_prefers_tomorrow_profile_and_validates_15_minute_energy() -> None:
    assert PV_ENTITIES[1] == "sensor.energy_production_tomorrow_2"
    day = datetime(2026, 9, 24, tzinfo=UTC).date()
    points = {
        (datetime(2026, 9, 24, tzinfo=UTC) + timedelta(minutes=15 * index)).isoformat(): 100 for index in range(96)
    }
    profile = pv_profile(
        {"state": "9.6", "attributes": {"unit_of_measurement": "kWh", "wh_period_15m": points}},
        day,
        UTC,
        tolerance=0.01,
    )

    assert len(profile) == 96
    assert sum(profile.values()) == pytest.approx(9.6)


def test_phase5a_app_publishes_diagnostics_without_inverter_service_calls(monkeypatch) -> None:
    monkeypatch.setattr(advisory_module, "ZoneInfo", lambda _name: UTC)
    install_appdaemon_stub()
    module = importlib.import_module("apps.energy_v2.advisory_app")
    app = module.Phase5AdvisoryApp()
    app.args = {"advisory": {"timezone": "UTC"}}
    published: list[tuple[str, str, dict[str, object]]] = []
    app.set_state = lambda entity_id, *, state, attributes: published.append((entity_id, state, attributes))
    app.initialize()
    app._evaluate_advisory_locked(datetime(2026, 9, 23, tzinfo=UTC))

    assert published
    assert all(entity_id.startswith("sensor.energy_v2_phase5a_") for entity_id, _state, _attributes in published)
    assert app.services == []


def test_phase5b_invalid_adapter_input_stays_shadow_only_and_safe_stops(tmp_path) -> None:
    config = RuntimeConfig(
        persistence_path=str(tmp_path / "phase5b_state.json"),
        entity_map=dict(DEFAULT_ENTITY_MAP),
    )
    publisher = Publisher()
    runtime = ShadowExecutorRuntime(HomeAssistantExecutorAdapter(MissingReader(), config), publisher, config)

    result = runtime.tick(datetime(2026, 9, 23, tzinfo=UTC))

    assert result is None
    assert runtime.current_state.value == "SAFE_STOP"
    assert publisher.states
    assert all(entity_id.startswith("sensor.energy_v2_phase5b_") for entity_id, _state, _attributes in publisher.states)
    assert config.shadow_only is True
