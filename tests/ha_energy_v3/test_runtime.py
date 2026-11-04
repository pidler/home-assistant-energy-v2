"""Execute the shipped YAML with HA 2026.10.4, not a second Python controller.

Only local helpers/template/script integrations are loaded. No inverter integration,
SSH, Modbus or network transport is configured. Commissioned values below are
synthetic test fixtures, never proposed production defaults.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

pytest.importorskip("homeassistant.core", reason="Run the dedicated HA 3.14 CI job for runtime tests")

from homeassistant.bootstrap import async_load_base_functionality  # noqa: E402
from homeassistant.config_entries import ConfigEntries  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from homeassistant import loader  # noqa: E402


def load_package():
    path = Path(__file__).resolve().parents[2] / "homeassistant/packages/energy_v3.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


MODES = ["SAFE", "SELF_USE", "PV_AUTO", "DEYE_SELL", "SOLAX_SELL", "SOLAX_AC_CHARGE", "DEYE_AC_CHARGE"]
ZERO_MODES = {"SAFE", "SELF_USE", "PV_AUTO"}


class Rig:
    def __init__(self, hass):
        self.hass = hass
        self.calls = []
        hass.bus.async_listen("call_service", lambda event: self.calls.append(event.data))

    def set(self, entity, state, attrs=None):
        self.hass.states.async_set(entity, state, attrs or {})

    def state(self, entity):
        return self.hass.states.get(entity).state

    async def call(self, script, **data):
        return await self.hass.services.async_call(
            "script", f"energy_v3_{script}", data, blocking=True, return_response=True
        )

    async def submit(self, mode="SELF_USE", power=None, **overrides):
        now = datetime.now(UTC)
        data = dict(
            mode=mode,
            power_w=(0 if mode in ZERO_MODES else 2400) if power is None else power,
            issued_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=120)).isoformat(),
            command_id=f"test-{now.timestamp()}",
        )
        data.update(overrides)
        return await self.call("submit", **data)

    async def commissioned(self):
        await self.hass.services.async_call(
            "input_select",
            "select_option",
            {"entity_id": "input_select.energy_v3_topology", "option": "asymmetric"},
            blocking=True,
        )
        for entity in ("energy_v3_enabled", "energy_v3_topology_verified"):
            await self.hass.services.async_call(
                "input_boolean", "turn_on", {"entity_id": f"input_boolean.{entity}"}, blocking=True
            )
        await self.hass.async_block_till_done()
        cfg = dict(self.hass.states.get("sensor.energy_v3_configuration").attributes["profile"])
        cfg.update(
            native_export_verified=True,
            deye_remote_verified=True,
            solax_vpp_verified=True,
            soc_limits_verified=True,
            communication_verified=True,
            sample_time_attribute="sampled_at",
            max_sample_age_s=20,
            solax_healthy_states=["TEST_NORMAL"],
            pcc_multiplier=-1,
            solax_min_soc=30,
            solax_max_soc=90,
            deye_min_soc=20,
            deye_max_soc=90,
            solax_max_power_w=6000,
            deye_max_power_w=6000,
            deye_charge_current_a=100,
            deye_discharge_current_a=100,
            deye_watchdog_s=30,
            solax_timeout_s=0,
            solax_pv_limit_w=12000,
            pv_auto_verified=True,
            pv_auto_work_mode="Zero Export To CT",
            pv_auto_tou="Disabled",
        )
        self.profile(cfg)
        self.set("binary_sensor.deye_connection", "on")
        self.set("sensor.deye_device_fault", "OK")
        self.set("sensor.solax_run_mode", "TEST_NORMAL")
        for entity, value in {
            "sensor.deye_external_power": -1000,
            "sensor.solax_battery_capacity": 60,
            "sensor.deye_battery": 60,
            "sensor.solax_battery_power_charge": 0,
            "sensor.deye_battery_power": 0,
        }.items():
            self.sample(entity, value)

    def sample(self, entity, value, age=0):
        self.set(entity, str(value), {"sampled_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat()})

    def profile(self, cfg=None, **updates):
        cfg = dict(cfg or self.hass.states.get("sensor.energy_v3_configuration").attributes["profile"])
        cfg.update(updates)
        self.set("sensor.energy_v3_configuration", cfg["topology"], {"profile": cfg})


@pytest.fixture
def run(tmp_path):
    def invoke(scenario, *, automations=False):
        async def execute():
            hass = HomeAssistant(str(tmp_path))
            loader.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await async_load_base_functionality(hass)
            package = load_package()
            try:
                for domain in ("input_boolean", "input_select", "input_text", "template", "script"):
                    assert await async_setup_component(hass, domain, {domain: package[domain]})
                if automations:
                    assert await async_setup_component(hass, "automation", {"automation": package["automation"]})
                await hass.async_start()
                await hass.async_block_till_done()
                rig = Rig(hass)
                await scenario(rig)
                # Defense independent of YAML lint: inspect actual runtime service dispatch.
                assert all(c["domain"] in {"script", "input_boolean", "input_select", "input_text"} for c in rig.calls)
                assert all(
                    not str(c.get("service_data", {}).get("entity_id", "")).startswith(
                        ("select.solax", "select.deye", "number.solax", "number.deye")
                    )
                    for c in rig.calls
                )
            finally:
                await hass.async_stop(force=True)

        return asyncio.run(execute())

    return invoke


def test_native_ha_schemas_and_default_disabled(run):
    async def scenario(rig):
        assert rig.state("input_boolean.energy_v3_enabled") == "off"
        assert rig.state("input_boolean.energy_v3_topology_verified") == "off"
        assert rig.state("input_select.energy_v3_topology") == "current"
        result = await rig.submit("SOLAX_SELL")
        assert result["reason"] == "disabled"
        assert result["actual_mode"] == "DISABLED"
        assert result["executed"] is False
        assert result["preview_mode"] == "SAFE"

    run(scenario)


def test_all_49_proposed_transitions_keep_break_before_make(run):
    async def scenario(rig):
        await rig.commissioned()
        for previous in MODES:
            for target in MODES:
                rig.set("input_select.energy_v3_preview_mode", previous)
                result = await rig.submit(target)
                assert result["reason"] == "ok", result
                assert result["previous_preview_mode"] == previous
                assert result["preview_mode"] == target
                assert result["hardware_confirmed"] is False
                plan = result["plan"]
                barriers = [s.get("barrier") for s in plan]
                assert barriers.index("confirm_native_hold") < barriers.index("confirm_vpp_disabled_and_battery_idle")
                stop = barriers.index("confirm_native_self_use")
                if target.startswith("SOLAX_"):
                    start = next(
                        i for i, step in enumerate(plan) if step.get("value") == "Individual Setting - Duration Mode"
                    )
                    assert stop < start
                    assert all(
                        step.get("value") == 0
                        for step in plan
                        if step.get("entity", "").startswith("number.deye_battery_max_")
                    )
                if target in {"PV_AUTO", "DEYE_SELL", "DEYE_AC_CHARGE"}:
                    releases = [
                        i
                        for i, step in enumerate(plan)
                        if step.get("entity", "").startswith("number.deye_battery_max_") and step.get("value", 0) > 0
                    ]
                    assert releases and min(releases) > stop

    run(scenario)


@pytest.mark.parametrize(
    "patch, reason",
    [
        ({"mode": "BOTH_SELL"}, "invalid_mode"),
        ({"power_w": True}, "invalid_power"),
        ({"power_w": "2000"}, "invalid_power"),
        ({"power_w": -1}, "invalid_power"),
        ({"power_w": float("nan")}, "invalid_power"),
        ({"power_w": float("inf")}, "invalid_power"),
        ({"power_w": 12001}, "invalid_power"),
        ({"mode": "SAFE", "power_w": 1}, "unexpected_power"),
        ({"mode": "SOLAX_SELL", "power_w": 99}, "power_below_step"),
        ({"command_id": "bad id"}, "invalid_id"),
        ({"command_id": "a" * 49}, "invalid_id"),
        ({"issued_at": "2026-11-04T14:00:00"}, "invalid_time"),
        ({"valid_until": "nonsense"}, "invalid_time"),
    ],
)
def test_validation_preserves_the_last_accepted_request(run, patch, reason):
    async def scenario(rig):
        await rig.submit()
        before = rig.state("input_text.energy_v3_command")
        result = (
            await rig.submit(**dict(mode="SOLAX_SELL", **patch)) if "mode" not in patch else await rig.submit(**patch)
        )
        assert result == dict(accepted=False, reason=reason, executed=False)
        assert rig.state("input_text.energy_v3_command") == before

    run(scenario)


def test_expiry_future_ttl_idempotence_conflict_and_replay(run):
    async def scenario(rig):
        now = datetime.now(UTC)
        data = dict(
            command_id="optimizer-1", issued_at=now.isoformat(), valid_until=(now + timedelta(seconds=90)).isoformat()
        )
        await rig.submit(**data)
        original = rig.state("input_text.energy_v3_command")
        duplicate = await rig.submit(**data)
        assert duplicate["reason"] == "duplicate_no_renewal"
        assert rig.state("input_text.energy_v3_command") == original
        conflict = await rig.submit(**dict(data, valid_until=(now + timedelta(seconds=100)).isoformat()))
        assert conflict["reason"] == "id_conflict"
        old = await rig.submit(
            command_id="older",
            issued_at=(now - timedelta(seconds=1)).isoformat(),
            valid_until=(now + timedelta(seconds=30)).isoformat(),
        )
        assert old["reason"] == "old_command"
        future = await rig.submit(issued_at=(now + timedelta(seconds=30)).isoformat())
        assert future["reason"] == "future_command"
        ttl = await rig.submit(valid_until=(now + timedelta(seconds=300)).isoformat())
        assert ttl["reason"] == "ttl_too_long"
        expired = await rig.submit(
            issued_at=(now - timedelta(seconds=30)).isoformat(), valid_until=(now - timedelta(seconds=1)).isoformat()
        )
        assert expired["reason"] == "expired_command"
        assert rig.state("input_text.energy_v3_command") == original

    run(scenario)


@pytest.mark.parametrize(
    "profile, entity, value, reason",
    [
        ({"topology": "current"}, None, None, "current_topology"),
        ({}, "input_boolean.energy_v3_topology_verified", "off", "topology_unverified"),
        ({"native_export_verified": False}, None, None, "native_export_unverified"),
        ({"pcc_multiplier": None}, None, None, "pcc_polarity_unverified"),
        ({"communication_verified": False}, None, None, "communication_unverified"),
        ({}, "binary_sensor.deye_connection", "off", "device_unhealthy"),
        ({}, "sensor.deye_device_fault", "FAULT", "device_unhealthy"),
        ({}, "sensor.solax_run_mode", "unavailable", "device_unhealthy"),
        ({"solax_min_soc": None}, None, None, "invalid_soc_limits"),
        ({"solax_max_power_w": 2000}, None, None, "solax_power_limit"),
    ],
)
def test_commissioning_and_health_gates(run, profile, entity, value, reason):
    async def scenario(rig):
        await rig.commissioned()
        rig.profile(**profile)
        if entity:
            rig.set(entity, value)
        result = await rig.submit("SOLAX_SELL")
        assert result["reason"] == reason
        assert result["preview_mode"] == "SAFE"
        assert not any(step.get("value") == "Individual Setting - Duration Mode" for step in result["plan"])

    run(scenario)


@pytest.mark.parametrize(
    "mode, sensor, soc, reason",
    [
        ("SOLAX_SELL", "sensor.solax_battery_capacity", 30, "solax_soc_floor"),
        ("SOLAX_AC_CHARGE", "sensor.solax_battery_capacity", 90, "solax_soc_ceiling"),
        ("DEYE_SELL", "sensor.deye_battery", 20, "deye_soc_floor"),
        ("DEYE_AC_CHARGE", "sensor.deye_battery", 90, "deye_soc_ceiling"),
    ],
)
def test_soc_direction_boundaries(run, mode, sensor, soc, reason):
    async def scenario(rig):
        await rig.commissioned()
        rig.sample(sensor, soc)
        assert (await rig.submit(mode))["reason"] == reason

    run(scenario)


@pytest.mark.parametrize(
    "value,age,reason",
    [(0, 30, "telemetry_stale"), ("unavailable", 0, "telemetry_unavailable"), (-10001, 0, "export_alarm")],
)
def test_pcc_is_not_replaced_by_zero_or_solax(run, value, age, reason):
    async def scenario(rig):
        await rig.commissioned()
        rig.sample("sensor.solax_measured_power", 0)
        rig.sample("sensor.deye_external_power", value, age)
        assert (await rig.submit("DEYE_SELL"))["reason"] == reason

    run(scenario)


def test_expiry_latches_safe_and_no_hardware_lease_outlives_request(run):
    async def scenario(rig):
        await rig.commissioned()
        result = await rig.submit("SOLAX_SELL", valid_until=(datetime.now(UTC) + timedelta(seconds=30)).isoformat())
        assert result["reason"] == "insufficient_solax_lease"
        rig.set("input_boolean.energy_v3_fault", "off")
        await rig.submit("DEYE_SELL")
        r = json.loads(rig.state("input_text.energy_v3_command"))
        clock = datetime.now(UTC).timestamp()
        r.update(t=clock - 121, e=clock - 1)
        rig.set("input_text.energy_v3_command", json.dumps(r))
        result = await rig.call("reconcile")
        assert result["reason"] == "expired"
        assert result["preview_mode"] == "SAFE"
        assert rig.state("input_boolean.energy_v3_fault") == "on"

    run(scenario)


def test_automations_validate_and_startup_cancels_without_losing_watermark(run):
    async def scenario(rig):
        await rig.submit()
        before = json.loads(rig.state("input_text.energy_v3_command"))
        package = load_package()
        assert await async_setup_component(rig.hass, "automation", {"automation": package["automation"]})
        await rig.hass.async_block_till_done()
        # Exercise the actual startup action sequence, including the serialized reconcile.
        from homeassistant.helpers import config_validation as cv
        from homeassistant.helpers.script import Script

        sequence = cv.SCRIPT_SCHEMA(package["automation"][0]["actions"])
        script = Script(rig.hass, sequence, "test_startup", "test")
        await script.async_run({"trigger": {"id": "startup"}}, context=Context())
        after = json.loads(rig.state("input_text.energy_v3_command"))
        assert after == dict(before, x=True)
        assert rig.state("input_boolean.energy_v3_enabled") == "off"
        result = await rig.submit(
            command_id=before["i"],
            issued_at=datetime.fromtimestamp(before["t"], UTC).isoformat(),
            valid_until=datetime.fromtimestamp(before["e"], UTC).isoformat(),
        )
        assert result["reason"] == "cancelled_replay"

    run(scenario)


def test_real_restart_restores_watermark_but_cancels_request(run):
    saved = {}

    async def first(rig):
        await rig.submit()
        saved.update(json.loads(rig.state("input_text.energy_v3_command")))

    async def second(rig):
        restored = json.loads(rig.state("input_text.energy_v3_command"))
        assert restored == dict(saved, x=True)
        assert rig.state("input_boolean.energy_v3_enabled") == "off"
        assert rig.state("input_select.energy_v3_preview_mode") == "SAFE"
        assert (await rig.call("reconcile"))["reason"] == "disabled"

    run(first, automations=True)
    run(second, automations=True)


@pytest.mark.parametrize("corrupt", ["broken-json", '["not-a-record"]', '{"i":"old","t":"bad","e":1}'])
def test_corrupt_watermark_never_silently_resets_replay_protection(run, corrupt):
    async def scenario(rig):
        rig.set("input_text.energy_v3_command", corrupt)
        result = await rig.submit()
        assert result["reason"] == "corrupt_watermark"
        assert rig.state("input_text.energy_v3_command") == corrupt

    run(scenario)


def test_adapter_incomplete_profile_keeps_hold_and_does_not_release(run):
    async def scenario(rig):
        await rig.commissioned()
        rig.profile(deye_charge_current_a=None)
        result = await rig.submit("DEYE_AC_CHARGE")
        assert result["reason"] == "deye_current_limits_unverified"
        assert result["preview_mode"] == "SAFE"
        assert all(
            step.get("value", 0) == 0
            for step in result["plan"]
            if step.get("entity", "").startswith("number.deye_battery_max_")
        )
        assert rig.state("input_boolean.energy_v3_fault") == "on"

    run(scenario)
