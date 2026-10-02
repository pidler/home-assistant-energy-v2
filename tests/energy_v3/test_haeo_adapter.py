from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v3.haeo_adapter import (
    DEYE_ACTIVE_POWER,
    OPTIMIZER_HORIZON,
    OPTIMIZER_STATUS,
    SOLAX_ACTIVE_POWER,
    HaeoError,
    current_target_from_states,
)

NOW = datetime(2026, 10, 2, 12, 7, tzinfo=UTC)
START = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)


def schema(*, start: datetime = START, periods: int = 192, solax: float = 1.25, deye: float = -2.5):
    boundaries = [start + timedelta(minutes=15 * index) for index in range(periods + 1)]
    times = boundaries[:-1]
    return {
        OPTIMIZER_STATUS: {
            "state": "success",
            "attributes": {"last_run": (NOW - timedelta(minutes=2)).isoformat()},
        },
        OPTIMIZER_HORIZON: {
            "state": boundaries[0].isoformat(),
            "attributes": {
                "forecast": [{"time": value.isoformat()} for value in boundaries],
                "period_count": periods,
                "smallest_period_seconds": 900,
            },
        },
        SOLAX_ACTIVE_POWER: {
            "state": str(solax),
            "attributes": {"forecast": [{"time": value.isoformat(), "value": solax} for value in times]},
        },
        DEYE_ACTIVE_POWER: {
            "state": str(deye),
            "attributes": {"forecast": [{"time": value.isoformat(), "value": deye} for value in times]},
        },
    }


def test_real_haeo_schema_selects_current_interval_and_converts_kw_to_w() -> None:
    states = schema()
    result = current_target_from_states(states, now=NOW)

    assert len(states[OPTIMIZER_HORIZON]["attributes"]["forecast"]) == 193
    assert len(states[SOLAX_ACTIVE_POWER]["attributes"]["forecast"]) == 192
    assert result.valid
    assert result.target is not None
    assert result.target.timestamp == datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    assert result.target.valid_until == datetime(2026, 10, 2, 12, 15, tzinfo=UTC)
    assert result.target.solax_target_w == 1250
    assert result.target.deye_target_w == -2500


def test_interval_start_is_inclusive_and_end_is_exclusive() -> None:
    states = schema()
    states[OPTIMIZER_STATUS]["attributes"]["last_run"] = datetime(2026, 10, 2, 11, 59, tzinfo=UTC).isoformat()
    at_start = current_target_from_states(states, now=datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    at_end = current_target_from_states(states, now=datetime(2026, 10, 2, 12, 15, tzinfo=UTC))

    assert at_start.target is not None and at_start.target.timestamp.hour == 12
    assert at_end.target is not None and at_end.target.timestamp.minute == 15


@pytest.mark.parametrize("status", ["pending", "failed", "unavailable"])
def test_non_successful_optimizer_fails_closed(status: str) -> None:
    states = schema()
    states[OPTIMIZER_STATUS]["state"] = status

    assert current_target_from_states(states, now=NOW).error is HaeoError.OPTIMIZER_NOT_SUCCESSFUL


def test_stale_last_run_fails_closed_even_when_status_is_success() -> None:
    states = schema()
    states[OPTIMIZER_STATUS]["attributes"]["last_run"] = (NOW - timedelta(minutes=31)).isoformat()

    assert current_target_from_states(states, now=NOW).error is HaeoError.OPTIMIZER_STALE


@pytest.mark.parametrize("entity_id", [OPTIMIZER_HORIZON, SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER])
def test_missing_forecast_fails_closed(entity_id: str) -> None:
    states = schema()
    states[entity_id]["attributes"].pop("forecast")

    assert current_target_from_states(states, now=NOW).error is HaeoError.FORECAST_MISSING


@pytest.mark.parametrize("value", [True, "bad", float("nan"), float("inf")])
def test_malformed_power_fails_closed(value: object) -> None:
    states = schema()
    states[SOLAX_ACTIVE_POWER]["attributes"]["forecast"][48]["value"] = value

    assert current_target_from_states(states, now=NOW).error is HaeoError.FORECAST_MALFORMED


def test_unaligned_power_forecast_fails_closed() -> None:
    states = schema()
    states[DEYE_ACTIVE_POWER]["attributes"]["forecast"][48]["time"] = (NOW + timedelta(minutes=1)).isoformat()

    assert current_target_from_states(states, now=NOW).error is HaeoError.FORECAST_UNALIGNED


def test_wrong_horizon_size_fails_closed() -> None:
    states = schema(periods=2)

    assert current_target_from_states(states, now=NOW).error is HaeoError.FORECAST_MALFORMED


def test_new_plan_replaces_old_values_without_accounting() -> None:
    first = current_target_from_states(schema(solax=1), now=NOW)
    replacement = current_target_from_states(schema(solax=3), now=NOW)

    assert first.target is not None and first.target.solax_target_w == 1000
    assert replacement.target is not None and replacement.target.solax_target_w == 3000
