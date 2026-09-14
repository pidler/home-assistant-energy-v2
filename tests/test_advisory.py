from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.energy_v2.advisory import (
    OUTPUTS,
    PRICE_ENTITIES,
    PV_ENTITIES,
    AdvisoryConfig,
    AdvisoryRuntime,
    LiveAdapter,
    price_profile,
    pv_profile,
    render_report,
)
from apps.energy_v2.config import ENTITY_IDS
from tests.test_deye_state_freshness import NOW, reported, runtime_states
from tests.test_telemetry import MappingReader


def live_states():
    states = runtime_states()
    for name in ("deye_soc", "solax_soc"):
        states[ENTITY_IDS[name]] = reported(52)
    for index, entity in enumerate(PRICE_ENTITIES):
        states[entity] = reported(8 if index == 0 else 4)
        states[entity]["attributes"] = {
            (NOW + timedelta(minutes=15 * i)).isoformat(): (8 if index == 0 else 4) for i in range(8)
        }
    zone = ZoneInfo("Europe/Prague")
    for offset, entity in enumerate(PV_ENTITIES):
        day = NOW.astimezone(zone).replace(hour=0, minute=0, second=0) + timedelta(days=offset)
        states[entity] = reported(0)
        states[entity]["attributes"] = {
            "unit_of_measurement": "kWh",
            "wh_period_15m": {(day + timedelta(minutes=15 * i)).isoformat(): 0 for i in range(96)},
        }
    return states


def adapted(states=None):
    config = AdvisoryConfig()
    adapter = LiveAdapter(MappingReader(states or live_states()), config)
    return adapter.collect(NOW), adapter


def test_valid_adaptation_off_load_and_unclamped_soc():
    states = live_states()
    states[ENTITY_IDS["deye_soc"]] = reported(7)
    data, _ = adapted(states)
    assert data.planner_input.initial_soc_pct["DEYE"] == 7
    assert data.deye_state == "INTENTIONAL_OFF"
    assert data.load_w == 315
    assert data.planner_input.load_forecast_quality.value == "FALLBACK"
    assert all(s.load_forecast_kwh == 315 / 4000 for s in data.planner_input.slots)
    assert any("DEYE_START_REQUIRED" in w for w in data.warnings)


@pytest.mark.parametrize(
    "entity",
    [
        ENTITY_IDS["deye_soc"],
        ENTITY_IDS["solax_soc"],
        ENTITY_IDS["deye_inverter_power"],
        PRICE_ENTITIES[0],
        PV_ENTITIES[1],
    ],
)
def test_stale_inputs_rejected(entity):
    states = live_states()
    states[entity]["last_reported"] = (NOW - timedelta(days=2)).isoformat()
    with pytest.raises(ValueError):
        adapted(states)


@pytest.mark.parametrize("state,expected", [("off", "UNAVAILABLE"), ("on", "STARTING")])
def test_deye_unready_status_retained(state, expected):
    states = live_states()
    if state == "off":
        states[ENTITY_IDS["deye_connection"]] = reported("off")
    else:
        states[ENTITY_IDS["deye_switch"]] = reported("on", changed=NOW)
    adapter = LiveAdapter(MappingReader(states), AdvisoryConfig())
    runtime = AdvisoryRuntime(adapter)
    assert runtime.tick(NOW)["status"] == "DEGRADED"
    assert adapter.last_deye_state == expected


def test_deye_persistent_fault_rejected():
    states = live_states()
    states[ENTITY_IDS["deye_switch"]] = reported("on")
    runtime = AdvisoryRuntime(LiveAdapter(MappingReader(states), AdvisoryConfig()))
    assert "UNEXPECTED_FAULT" in runtime.tick(NOW)["degraded_inputs"]


def test_price_order_normalized_and_missing_slot_rejected():
    raw = {NOW.isoformat(): 8, (NOW + timedelta(minutes=30)).isoformat(): 8}
    with pytest.raises(ValueError, match="missing price slot"):
        price_profile(raw, NOW)
    raw[(NOW + timedelta(minutes=15)).isoformat()] = 7
    assert list(price_profile(raw, NOW)) == sorted(datetime.fromisoformat(k) for k in raw)


@pytest.mark.parametrize("bad", ["nan", -1])
def test_bad_pv_values_rejected(bad):
    states = live_states()
    profile = states[PV_ENTITIES[1]]["attributes"]["wh_period_15m"]
    profile[next(iter(profile))] = bad
    with pytest.raises(ValueError):
        adapted(states)


def test_pv_96_slot_day_and_sum_validation():
    raw = live_states()[PV_ENTITIES[1]]
    day = (NOW + timedelta(days=1)).astimezone(ZoneInfo("Europe/Prague")).date()
    assert len(pv_profile(raw, day, ZoneInfo("Europe/Prague"), 0.2)) == 96
    raw["state"] = 5
    with pytest.raises(ValueError, match="sum"):
        pv_profile(raw, day, ZoneInfo("Europe/Prague"), 0.2)
    raw["state"] = 0
    raw["attributes"]["wh_period_15m"].popitem()
    with pytest.raises(ValueError, match="96"):
        pv_profile(raw, day, ZoneInfo("Europe/Prague"), 0.2)


def test_buy_sell_sanity():
    states = live_states()
    states[PRICE_ENTITIES[0]]["attributes"][NOW.isoformat()] = 1
    with pytest.raises(ValueError, match="buy price"):
        adapted(states)


class FixedAdapter:
    def __init__(self, inputs):
        self.inputs = inputs
        self.config = AdvisoryConfig(minimum_interval_s=30, debounce_s=5)
        self.fail = False

    def collect(self, now):
        if self.fail:
            raise ValueError("stale SOC")
        return self.inputs


def baseline():
    data, _ = adapted()
    adapter = FixedAdapter(data)
    runtime = AdvisoryRuntime(adapter)
    assert runtime.tick(NOW)["status"] == "ADVISORY"
    return runtime, adapter


def test_baseline_timeline_trace_and_report():
    runtime, _ = baseline()
    plan = runtime.plan
    assert plan["result"].expected_export_kwh >= 0
    assert plan["timeline"][0]["start"] == NOW
    assert plan["timeline"][-1]["end"] == plan["result"].economic_horizon_end
    assert all(a["action"] != b["action"] for a, b in zip(plan["timeline"], plan["timeline"][1:], strict=False))
    assert plan["trace"][0]["soc_before"] == {"DEYE": 52, "SolaX": 52}
    assert all(t["export_cap_ok"] for t in plan["trace"])
    report = render_report(plan)
    for phrase in ("ADVISORY", "LOW confidence", "Import/export", "Terminal SOC", "Reserve"):
        assert phrase in report
    assert report == render_report(plan)


@pytest.mark.parametrize(
    "field,value,trigger",
    [
        ("prices_hash", "new_prices", "PRICE_HORIZON"),
        ("pv_hash", "new_pv", "PV_PROFILE"),
        ("deye_state", "READY", "DEYE_STATE"),
        ("load_w", 900, "LOAD_DEVIATION"),
    ],
)
def test_meaningful_changes_replan_with_debounce(field, value, trigger):
    runtime, adapter = baseline()
    old = runtime.plan["id"]
    adapter.inputs = replace(adapter.inputs, **{field: value})
    assert runtime.tick(NOW + timedelta(seconds=1))["status"] == "LAST_KNOWN"
    runtime.tick(NOW + timedelta(seconds=29))
    assert runtime.plan["id"] == old
    runtime.tick(NOW + timedelta(seconds=30))
    assert runtime.plan["id"] != old
    assert trigger in runtime.events[-1]["trigger"]
    assert len(runtime.events) == 2


def test_soc_deviation_and_no_replan_storm():
    runtime, adapter = baseline()
    data = adapter.inputs.planner_input
    adapter.inputs = replace(adapter.inputs, planner_input=replace(data, initial_soc_pct={"DEYE": 48, "SolaX": 52}))
    for seconds in range(1, 31):
        runtime.tick(NOW + timedelta(seconds=seconds))
    assert len(runtime.events) == 2
    assert "SOC_DEVIATION" in runtime.events[-1]["trigger"]
    for seconds in range(31, 59):
        runtime.tick(NOW + timedelta(seconds=seconds))
    assert len(runtime.events) == 2


def test_stale_and_planner_failure_preserve_last_known_invalid():
    runtime, adapter = baseline()
    previous = runtime.plan
    adapter.fail = True
    diagnostics = runtime.tick(NOW + timedelta(seconds=1))
    assert diagnostics["status"] == "LAST_KNOWN"
    assert diagnostics["current_action"] == "HOLD / DEGRADED"
    assert diagnostics["net_result"] == "unavailable"
    adapter.fail = False

    def fail(*args):
        raise RuntimeError("solver failed")

    runtime.planner = fail
    runtime.tick(NOW + timedelta(seconds=30))
    diagnostics = runtime.tick(NOW + timedelta(seconds=36))
    assert runtime.plan is previous
    assert "PLANNER_ERROR" in diagnostics["degraded_inputs"]
    assert diagnostics["status"] == "LAST_KNOWN"


def test_horizon_expiry_invalidates_without_waiting_for_solver():
    runtime, _ = baseline()
    at = runtime.plan["result"].economic_horizon_end
    assert runtime.diagnostics(at)["status"] == "LAST_KNOWN"
    assert runtime.diagnostics(at)["net_result"] == "unavailable"


def test_advisory_modules_have_no_physical_dependencies_or_service_calls():
    import ast

    for filename in ("advisory.py", "advisory_app.py"):
        tree = ast.parse(Path("apps/energy_v2", filename).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(x in (node.module or "") for x in ("controller", "adapters", "modbus"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("call_service", "turn_on", "turn_off", "execute", "dispatch")
    assert all(entity.startswith("sensor.energy_v2_phase5a_") for entity in OUTPUTS.values())


def test_app_publication_only_uses_diagnostic_sensors(monkeypatch):
    import importlib

    from tests.test_app import install_appdaemon_stub

    install_appdaemon_stub()
    module = importlib.import_module("apps.energy_v2.advisory_app")

    class Clock:
        @staticmethod
        def now():
            return NOW

    monkeypatch.setattr(module, "datetime", Clock)
    app = module.Phase5AdvisoryApp()
    app.args = {}
    app.states = live_states()
    published = {}

    def write(entity, **kwargs):
        assert entity in OUTPUTS.values()
        published[entity] = kwargs

    def forbidden(*args, **kwargs):
        pytest.fail("physical/service path invoked")

    app.set_state = write
    app.call_service = forbidden
    app.turn_on = forbidden
    app.turn_off = forbidden
    app.initialize()
    app.evaluate_advisory({})
    assert set(published) == set(OUTPUTS.values())
    assert published[OUTPUTS["status"]]["state"] == "ADVISORY"
    assert app.services == []
    assert "ADVISORY" in app.rendered_report
    assert app.runtime.plan["deye_dispatch_eligible"] is False
    # Subsequent source failure clears published advice and economics.
    app.states[ENTITY_IDS["deye_soc"]]["state"] = "unavailable"
    app.evaluate_advisory({})
    assert published[OUTPUTS["status"]]["state"] == "LAST_KNOWN"
    assert published[OUTPUTS["net_result"]]["state"] == "unavailable"


def test_physical_controller_cannot_be_invoked(monkeypatch):
    from apps.energy_v2.charge_controller import DeyeChargeShadowController
    from apps.energy_v2.shadow_controller import ShadowControlCore

    def forbidden(*args, **kwargs):
        pytest.fail("controller constructed by advisory runtime")

    monkeypatch.setattr(ShadowControlCore, "__post_init__", forbidden)
    monkeypatch.setattr(DeyeChargeShadowController, "__init__", forbidden)
    runtime, _ = baseline()
    assert runtime.valid


@pytest.mark.parametrize("change", ["date", "sum", "alignment", "missing"])
def test_pv_profile_shape_rejected(change):
    states = live_states()
    raw = states[PV_ENTITIES[1]]
    profile = raw["attributes"]["wh_period_15m"]
    first = next(iter(profile))
    if change == "date":
        profile[(datetime.fromisoformat(first) - timedelta(days=1)).isoformat()] = profile.pop(first)
    elif change == "alignment":
        profile[(datetime.fromisoformat(first) + timedelta(minutes=1)).isoformat()] = profile.pop(first)
    elif change == "sum":
        raw["state"] = 20
    else:
        profile.pop(first)
    with pytest.raises(ValueError):
        adapted(states)


@pytest.mark.parametrize("change", ["alignment", "nan", "coverage", "missing"])
def test_price_profile_rejects_invalid_economic_horizon(change):
    states = live_states()
    prices = states[PRICE_ENTITIES[0]]["attributes"]
    first = next(iter(prices))
    if change == "alignment":
        prices[(NOW + timedelta(minutes=1)).isoformat()] = prices.pop(first)
    elif change == "nan":
        prices[first] = float("nan")
    elif change == "coverage":
        prices[(NOW + timedelta(minutes=120)).isoformat()] = 8
    else:
        prices.pop(first)
    with pytest.raises(ValueError):
        adapted(states)


def test_recovery_from_invalid_inputs_triggers_new_plan():
    runtime, adapter = baseline()
    previous = runtime.plan["id"]
    adapter.fail = True
    runtime.tick(NOW + timedelta(seconds=1))
    adapter.fail = False
    runtime.tick(NOW + timedelta(seconds=30))
    assert runtime.tick(NOW + timedelta(seconds=35))["status"] == "ADVISORY"
    assert runtime.plan["id"] != previous
    assert "INPUT_RECOVERED" in runtime.events[-1]["trigger"]


def test_initial_planner_failure_is_rate_limited():
    data, _ = adapted()
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("infeasible")

    runtime = AdvisoryRuntime(FixedAdapter(data), planner=fail)
    for second in range(30):
        assert runtime.tick(NOW + timedelta(seconds=second))["status"] == "DEGRADED"
    assert len(calls) == 1


def test_receipt_refresh_and_elapsed_slot_do_not_change_price_fingerprint():
    states = live_states()
    adapter = LiveAdapter(MappingReader(states), AdvisoryConfig())
    first = adapter.collect(NOW)
    later = NOW + timedelta(seconds=10)
    for raw in states.values():
        if isinstance(raw, dict):
            raw["last_reported"] = later.isoformat()
    second = adapter.collect(later)
    assert first.prices_hash == second.prices_hash
    assert first.pv_hash == second.pv_hash
    assert second.planner_input.slots[0].timestamp == NOW + timedelta(minutes=15)


def test_publication_failure_cannot_commit_advisory_status(monkeypatch):
    import importlib

    from tests.test_app import install_appdaemon_stub

    install_appdaemon_stub()
    module = importlib.import_module("apps.energy_v2.advisory_app")

    class Clock:
        @staticmethod
        def now():
            return NOW

    monkeypatch.setattr(module, "datetime", Clock)
    app = module.Phase5AdvisoryApp()
    app.states = live_states()
    app.args = {}
    status = []

    def publish(entity, **kwargs):
        if entity == OUTPUTS["status"]:
            status.append(kwargs["state"])
        if entity == OUTPUTS["next_action"]:
            raise RuntimeError("HA publication unavailable")

    app.set_state = publish
    app.initialize()
    with pytest.raises(RuntimeError):
        app.evaluate_advisory({})
    assert status == ["UPDATING"]
