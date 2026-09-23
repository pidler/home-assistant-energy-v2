from __future__ import annotations

from apps.energy_v2.advisory import LiveAdapter
from apps.energy_v2.config import ENTITY_IDS
from apps.energy_v2.deye_state import DeyeOperatingState
from tests.test_telemetry import NOW, MappingReader, control_states, timestamped


def advisory_adapter(
    *, switch: str = "on", state: str = "Normal", fault: str = "OK", connection_age_s: float = 5, power_age_s: float = 5
):
    states = control_states()
    states.update(
        {
            ENTITY_IDS["deye_switch"]: timestamped(switch, 9_000),
            ENTITY_IDS["deye_device_state"]: timestamped(state, 9_000),
            ENTITY_IDS["deye_device_fault"]: timestamped(fault, 9_000),
            ENTITY_IDS["deye_connection"]: timestamped("on", connection_age_s),
            ENTITY_IDS["deye_inverter_power"]: timestamped(200, power_age_s),
        }
    )
    reader = MappingReader(states)
    adapter = LiveAdapter(reader, object())
    snapshot = adapter.telemetry.control_snapshot(now=NOW)
    return adapter, snapshot


def test_phase5a_accepts_stable_deye_state_with_fresh_source_evidence() -> None:
    adapter, snapshot = advisory_adapter()

    assert snapshot.deye_operating.state is DeyeOperatingState.UNAVAILABLE
    state, reason = adapter._advisory_deye_state(snapshot, NOW)

    assert state is DeyeOperatingState.READY
    assert reason == "Phase 5A advisory source health confirmed"


def test_phase5a_blocks_bad_stable_deye_values() -> None:
    for kwargs in ({"switch": "off"}, {"state": "Fault"}, {"fault": "Temperature is too high"}):
        adapter, snapshot = advisory_adapter(**kwargs)

        state, _reason = adapter._advisory_deye_state(snapshot, NOW)

        assert state is DeyeOperatingState.UNAVAILABLE


def test_phase5a_blocks_stale_connection_or_power() -> None:
    for kwargs in ({"connection_age_s": 31}, {"power_age_s": 31}):
        adapter, snapshot = advisory_adapter(**kwargs)

        state, _reason = adapter._advisory_deye_state(snapshot, NOW)

        assert state is DeyeOperatingState.UNAVAILABLE


def test_phase5b_strict_readiness_is_unchanged_for_stale_stable_feedback() -> None:
    adapter, snapshot = advisory_adapter()

    assert snapshot.deye_operating.state is DeyeOperatingState.UNAVAILABLE
