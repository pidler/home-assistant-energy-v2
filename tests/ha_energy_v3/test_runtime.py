"""Execute the shipped YAML with HA 2026.10.4, not a second Python controller.

Only local helpers/template/script integrations are loaded. No inverter integration,
SSH, Modbus or network transport is configured. Commissioned values below are
synthetic test fixtures, never proposed production defaults.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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
        self.results = []
        self.base_time = datetime.now(UTC)
        self.time = None
        hass.bus.async_listen("call_service", lambda event: self.calls.append(event.data))
        hass.bus.async_listen("energy_v3_preview_result", lambda event: self.results.append(event.data["result"]))

    def now(self):
        return self.time or datetime.now(UTC)

    @contextmanager
    def at(self, seconds):
        self.time = self.base_time + timedelta(seconds=seconds)
        with (
            patch("homeassistant.util.dt.now", return_value=self.time),
            patch("homeassistant.util.dt.utcnow", return_value=self.time),
        ):
            yield
        self.time = None

    def applied(self, mode, power=None, until=None):
        self.set(
            "input_text.energy_v3_applied",
            json.dumps(
                dict(
                    m=mode,
                    p=(0 if mode in ZERO_MODES else 2400) if power is None else power,
                    u=(0 if mode in ZERO_MODES else self.now().timestamp() + 60) if until is None else until,
                )
            ),
        )

    async def tick(self, source):
        from homeassistant.helpers import config_validation as cv
        from homeassistant.helpers.script import Script

        automation = next(a for a in load_package()["automation"] if a["id"] == f"energy_v3_{source}")
        script = Script(self.hass, cv.SCRIPT_SCHEMA(automation["actions"]), f"test_{source}", "test")
        await script.async_run({"trigger": {"id": "periodic", "platform": "time_pattern"}}, context=Context())
        await self.hass.async_block_till_done()
        return self.results[-1]

    def set(self, entity, state, attrs=None):
        self.hass.states.async_set(entity, state, attrs or {})

    def state(self, entity):
        return self.hass.states.get(entity).state

    async def call(self, script, **data):
        result = await self.hass.services.async_call(
            "script", f"energy_v3_{script}", data, blocking=True, return_response=True
        )
        await self.hass.async_block_till_done()
        return result

    async def submit(self, mode="SELF_USE", power=None, **overrides):
        now = self.now()
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
            deye_refresh_verified=True,
            deye_refresh_via="watchdog",
            deye_renew_before_s=10,
            solax_timeout_s=0,
            solax_refresh_verified=True,
            solax_renew_before_s=10,
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
        self.set(entity, str(value), {"sampled_at": (self.now() - timedelta(seconds=age)).isoformat()})

    def fresh_samples(self):
        for entity in (
            "sensor.deye_external_power",
            "sensor.solax_battery_capacity",
            "sensor.deye_battery",
            "sensor.solax_battery_power_charge",
            "sensor.deye_battery_power",
        ):
            self.sample(entity, self.state(entity))

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
                rig.applied(previous)
                result = await rig.submit(target)
                assert result["reason"] == "ok", result
                assert result["previous_preview_mode"] == previous
                assert result["preview_mode"] == target
                assert result["hardware_confirmed"] is False
                plan = result["plan"]
                if previous == target:
                    assert result["change"] == "check"
                    assert plan == []
                    continue
                assert result["change"] == "transition"
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
        await rig.commissioned()
        assert (await rig.submit("DEYE_SELL"))["preview_mode"] == "DEYE_SELL"
        saved.update(json.loads(rig.state("input_text.energy_v3_command")))

    async def second(rig):
        restored = json.loads(rig.state("input_text.energy_v3_command"))
        assert restored == dict(saved, x=True)
        assert rig.state("input_boolean.energy_v3_enabled") == "off"
        assert json.loads(rig.state("input_text.energy_v3_applied"))["m"] == "SAFE"
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


@pytest.mark.parametrize("mode", ["SOLAX_SELL", "SOLAX_AC_CHARGE", "DEYE_SELL", "DEYE_AC_CHARGE"])
def test_periodic_checks_and_new_ids_do_not_restart_or_prolong_hw_lease(run, mode):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            first = await rig.submit(mode)
            assert first["change"] == "transition"
            original = rig.state("input_text.energy_v3_applied")
            # Actual readbacks remain separate from the simulated operation.
            rig.set("sensor.solax_modbus_power_control", "Disabled")
            rig.set("select.deye_remote_mode", "Disabled")
        for offset, source in [(5, "safety"), (10, "supervisor"), (15, "safety")]:
            with rig.at(offset):
                rig.fresh_samples()
                before = len(rig.calls)
                result = await rig.tick(source)
                assert result["change"] == "check"
                assert result["plan"] == []
                assert rig.state("input_text.energy_v3_applied") == original
                assert not any(
                    c["domain"] in {"input_text", "input_select", "input_boolean"} for c in rig.calls[before:]
                )
                assert result["hw_readback"]["solax_vpp"] == "Disabled"
                assert result["hw_readback"]["deye_remote_observed"] == "Disabled"
                assert result["hardware_confirmed"] is False
        with rig.at(16):
            rig.fresh_samples()
            continuation = await rig.submit(mode)
            assert continuation["change"] == "check"
            assert continuation["plan"] == []
            assert rig.state("input_text.energy_v3_applied") == original
            request = json.loads(rig.state("input_text.energy_v3_command"))
            assert request["e"] > first["simulated_applied"]["u"]

    run(scenario)


@pytest.mark.parametrize("mode", ["SOLAX_SELL", "SOLAX_AC_CHARGE", "DEYE_SELL", "DEYE_AC_CHARGE"])
def test_same_mode_power_update_is_one_setpoint_without_stop_start(run, mode):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            first = await rig.submit(mode)
        with rig.at(1):
            rig.fresh_samples()
            result = await rig.submit(mode, power=3600)
            assert result["change"] == "setpoint"
            commands = [step for step in result["plan"] if "entity" in step]
            expected = (
                "number.solax_remotecontrol_push_mode_power_8_9_direct"
                if mode.startswith("SOLAX_")
                else "number.deye_remote_constant_power_setpoint"
            )
            assert [s["entity"] for s in commands] == [expected]
            assert result["simulated_applied"]["p"] == 3600
            if mode.startswith("DEYE_"):
                # This fixture verifies watchdog-register refresh, not a setpoint heartbeat.
                assert result["simulated_applied"]["u"] == first["simulated_applied"]["u"]
        with rig.at(2):
            rig.fresh_samples()
            # Different raw requests with the same effective hardware quantum are no-ops.
            equivalent = await rig.submit(mode, power=3601)
            assert equivalent["change"] == "check"
            assert equivalent["plan"] == []

    run(scenario)


@pytest.mark.parametrize("mode", ["SOLAX_SELL", "SOLAX_AC_CHARGE", "DEYE_SELL", "DEYE_AC_CHARGE"])
def test_only_due_verified_refresh_renews_once_without_a_transition(run, mode):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            first = await rig.submit(mode)
            due = 50 if mode.startswith("SOLAX_") else 20
        with rig.at(due):
            rig.fresh_samples()
            result = await rig.tick("supervisor")
            assert result["change"] == "renew"
            assert len([s for s in result["plan"] if "entity" in s]) == 1
            assert result["simulated_applied"]["u"] > first["simulated_applied"]["u"]
            assert not any(s.get("value") == "Disabled" for s in result["plan"])
            assert not any("battery_max_" in s.get("entity", "") for s in result["plan"])
            assert result["simulated_applied"]["u"] <= json.loads(rig.state("input_text.energy_v3_command"))["e"]
            repeat = await rig.tick("safety")
            assert repeat["change"] == "check"
            assert repeat["plan"] == []

    run(scenario)


@pytest.mark.parametrize("mode", ["SOLAX_SELL", "DEYE_SELL"])
def test_lease_extension_allows_due_refresh_but_not_a_stop_start(run, mode):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            await rig.submit(mode)
            due = 50 if mode.startswith("SOLAX_") else 20
        with rig.at(due):
            rig.fresh_samples()
            # The new optimizer lease permits a due renewal; ID is not a transition key.
            result = await rig.submit(mode)
            assert result["change"] == "renew"
            assert not any(s.get("value") == "Disabled" for s in result["plan"])
            repeated = await rig.tick("safety")
            assert repeated["change"] == "check"
            assert repeated["plan"] == []

    run(scenario)


@pytest.mark.parametrize("failure", ["expiry", "fault", "disabled"])
def test_safe_exit_is_planned_once_and_then_only_checked(run, failure):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            await rig.submit("DEYE_SELL")
        with rig.at(1):
            rig.fresh_samples()
            if failure == "expiry":
                record = json.loads(rig.state("input_text.energy_v3_command"))
                record.update(t=rig.now().timestamp() - 121, e=rig.now().timestamp() - 1)
                rig.set("input_text.energy_v3_command", json.dumps(record))
            elif failure == "disabled":
                rig.set("input_boolean.energy_v3_enabled", "off")
            else:
                rig.set("sensor.deye_device_fault", "FAULT")
            result = await rig.tick("supervisor")
            assert result["change"] == "transition"
            assert result["preview_mode"] == "SAFE"
            assert any(s.get("value") == "Disabled" for s in result["plan"])
            for source in ("safety", "supervisor", "safety"):
                repeated = await rig.tick(source)
                assert repeated["change"] == "check"
                assert repeated["plan"] == []
                assert repeated["simulated_applied"]["m"] == "SAFE"

    run(scenario)


@pytest.mark.parametrize("unverified", [True, False])
def test_refresh_needs_verified_profile_and_cannot_outlive_optimizer_lease(run, unverified):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            rig.profile(deye_refresh_verified=not unverified)
            await rig.submit("DEYE_SELL", valid_until=(rig.now() + timedelta(seconds=40)).isoformat())
        with rig.at(20):
            rig.fresh_samples()
            result = await rig.tick("safety")
            assert result["change"] == "check"
            assert result["plan"] == []
            assert result["reason"] == ("renewal_unverified" if unverified else "renewal_waiting_for_longer_lease")
        with rig.at(30):
            rig.fresh_samples()
            result = await rig.tick("supervisor")
            assert result["reason"] == "simulated_hw_lease_elapsed"
            assert result["change"] == "transition"
            assert result["preview_mode"] == "SAFE"
            assert (await rig.tick("safety"))["plan"] == []

    run(scenario)


@pytest.mark.parametrize("via", ["watchdog", "setpoint"])
def test_due_deye_power_change_combines_only_required_setpoint_and_refresh(run, via):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            rig.profile(deye_refresh_via=via)
            first = await rig.submit("DEYE_SELL")
        with rig.at(20):
            rig.fresh_samples()
            result = await rig.submit("DEYE_SELL", power=3600)
            assert result["change"] == "setpoint"
            assert result["renewal_prepared"] is True
            commands = [s["entity"] for s in result["plan"] if "entity" in s]
            expected = ["number.deye_remote_constant_power_setpoint"]
            if via == "watchdog":
                expected.append("number.deye_remote_watchdog")
            assert commands == expected
            assert result["simulated_applied"]["u"] > first["simulated_applied"]["u"]
            assert (await rig.tick("safety"))["plan"] == []

    run(scenario)


def test_shortened_lease_stops_once_and_elapsed_command_cannot_be_revived_by_a_new_id(run):
    async def scenario(rig):
        with rig.at(0):
            await rig.commissioned()
            await rig.submit("DEYE_SELL")
        with rig.at(1):
            rig.fresh_samples()
            shorter = await rig.submit("DEYE_SELL", valid_until=(rig.now() + timedelta(seconds=10)).isoformat())
            assert shorter["reason"] == "shortened_lease"
            assert shorter["change"] == "transition"
            assert shorter["preview_mode"] == "SAFE"
            assert (await rig.tick("safety"))["plan"] == []
        with rig.at(2):
            rig.fresh_samples()
            rig.set("input_boolean.energy_v3_fault", "off")
            await rig.submit("DEYE_SELL")
        with rig.at(33):
            rig.fresh_samples()
            fresh_id = await rig.submit("DEYE_SELL")
            assert fresh_id["reason"] == "simulated_hw_lease_elapsed"
            assert fresh_id["preview_mode"] == "SAFE"
            assert not any(s.get("value") == "Remote mode 3" for s in fresh_id["plan"])
            assert (await rig.tick("supervisor"))["plan"] == []

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


@pytest.mark.parametrize(
    "mode, entity, expected",
    [
        ("SOLAX_SELL", "number.solax_remotecontrol_push_mode_power_8_9_direct", 2400),
        ("SOLAX_AC_CHARGE", "number.solax_remotecontrol_push_mode_power_8_9_direct", -2400),
        ("DEYE_SELL", "number.deye_remote_constant_power_setpoint", 20.4),
        ("DEYE_AC_CHARGE", "number.deye_remote_constant_power_setpoint", -20.4),
    ],
)
def test_adapter_units_signs_quantize_toward_zero(run, mode, entity, expected):
    async def scenario(rig):
        await rig.commissioned()
        result = await rig.submit(mode, power=2449)
        assert result["reason"] == "ok"
        commands = [step for step in result["plan"] if step.get("entity") == entity]
        assert commands[-1]["value"] == expected
        assert commands[-1]["unit"] == ("percent" if mode.startswith("DEYE_") else "W_battery")
        opposite = (
            "number.deye_battery_max_charging_current"
            if mode == "DEYE_SELL"
            else "number.deye_battery_max_discharging_current"
        )
        if mode.startswith("DEYE_"):
            assert all(step["value"] == 0 for step in result["plan"] if step.get("entity") == opposite)

    run(scenario)


def test_normalized_pcc_entity_is_signed_and_unavailable_without_its_source(run):
    async def scenario(rig):
        assert rig.state("sensor.energy_v3_pcc_export_w") == "unavailable"
        await rig.commissioned()
        await rig.hass.async_block_till_done()
        assert float(rig.state("sensor.energy_v3_pcc_export_w")) == 1000
        rig.sample("sensor.deye_external_power", 1500)
        await rig.hass.async_block_till_done()
        assert float(rig.state("sensor.energy_v3_pcc_export_w")) == -1500
        rig.sample("sensor.solax_measured_power", 0)
        rig.sample("sensor.deye_external_power", "unavailable")
        await rig.hass.async_block_till_done()
        assert rig.state("sensor.energy_v3_pcc_export_w") == "unavailable"

    run(scenario)
