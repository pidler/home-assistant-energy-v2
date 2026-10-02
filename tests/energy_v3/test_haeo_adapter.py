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
    last_run = NOW - timedelta(minutes=2)
    published = last_run + timedelta(milliseconds=100)
    return {
        OPTIMIZER_STATUS: {
            "state": "success",
            "attributes": {"last_run": last_run.isoformat()},
            "last_changed": (last_run - timedelta(hours=1)).isoformat(),
            "last_updated": (last_run - timedelta(minutes=10)).isoformat(),
            "last_reported": published.isoformat(),
        },
        OPTIMIZER_HORIZON: {
            "state": boundaries[0].isoformat(),
            "attributes": {
                "forecast": [{"time": value.isoformat()} for value in boundaries],
                "period_count": periods,
                "smallest_period_seconds": 900,
            },
            "last_updated": (last_run - timedelta(minutes=12)).isoformat(),
            "last_reported": (last_run - timedelta(minutes=12)).isoformat(),
        },
        SOLAX_ACTIVE_POWER: {
            "state": str(solax),
            "attributes": {"forecast": [{"time": value.isoformat(), "value": solax} for value in times]},
            "last_changed": (last_run - timedelta(hours=2)).isoformat(),
            "last_updated": (last_run - timedelta(minutes=5)).isoformat(),
            "last_reported": (published + timedelta(milliseconds=10)).isoformat(),
        },
        DEYE_ACTIVE_POWER: {
            "state": str(deye),
            "attributes": {"forecast": [{"time": value.isoformat(), "value": deye} for value in times]},
            "last_changed": (last_run - timedelta(hours=2)).isoformat(),
            "last_updated": (last_run - timedelta(minutes=5)).isoformat(),
            "last_reported": (published + timedelta(milliseconds=20)).isoformat(),
        },
    }


def set_publication_time(states, last_run: datetime) -> None:
    states[OPTIMIZER_STATUS]["attributes"]["last_run"] = last_run.isoformat()
    for offset, entity_id in enumerate((OPTIMIZER_STATUS, SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER)):
        states[entity_id]["last_reported"] = (last_run + timedelta(milliseconds=offset + 1)).isoformat()


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
    set_publication_time(states, datetime(2026, 10, 2, 11, 59, tzinfo=UTC))
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


def test_unchanged_forecast_with_old_last_updated_and_fresh_last_reported_is_accepted() -> None:
    states = schema()

    assert states[SOLAX_ACTIVE_POWER]["last_updated"] < states[OPTIMIZER_STATUS]["attributes"]["last_run"]
    assert states[DEYE_ACTIVE_POWER]["last_updated"] < states[OPTIMIZER_STATUS]["attributes"]["last_run"]
    assert current_target_from_states(states, now=NOW).valid


def test_new_optimizer_status_with_old_battery_last_reported_fails_closed() -> None:
    states = schema()
    stale_publication = NOW - timedelta(minutes=10)
    states[SOLAX_ACTIVE_POWER]["last_reported"] = stale_publication.isoformat()
    states[DEYE_ACTIVE_POWER]["last_reported"] = stale_publication.isoformat()

    assert current_target_from_states(states, now=NOW).error is HaeoError.PUBLICATION_UNCERTAIN


def test_sequential_publication_is_accepted_only_after_all_entities_are_current() -> None:
    states = schema()
    last_run = NOW - timedelta(seconds=1)
    stale = last_run - timedelta(minutes=10)
    states[OPTIMIZER_STATUS]["attributes"]["last_run"] = last_run.isoformat()
    states[OPTIMIZER_STATUS]["last_reported"] = (last_run + timedelta(milliseconds=1)).isoformat()
    states[SOLAX_ACTIVE_POWER]["last_reported"] = stale.isoformat()
    states[DEYE_ACTIVE_POWER]["last_reported"] = stale.isoformat()

    assert current_target_from_states(states, now=NOW).error is HaeoError.PUBLICATION_UNCERTAIN

    states[SOLAX_ACTIVE_POWER]["last_reported"] = (last_run + timedelta(milliseconds=2)).isoformat()
    assert current_target_from_states(states, now=NOW).error is HaeoError.PUBLICATION_UNCERTAIN

    states[DEYE_ACTIVE_POWER]["last_reported"] = (last_run + timedelta(milliseconds=3)).isoformat()
    assert current_target_from_states(states, now=NOW).valid


def test_publication_outside_tolerance_fails_closed() -> None:
    states = schema()
    last_run = datetime.fromisoformat(states[OPTIMIZER_STATUS]["attributes"]["last_run"])
    states[DEYE_ACTIVE_POWER]["last_reported"] = (last_run + timedelta(seconds=6)).isoformat()

    assert current_target_from_states(states, now=NOW).error is HaeoError.PUBLICATION_UNCERTAIN


def test_forecast_replacement_between_snapshots_fails_closed() -> None:
    before = schema(solax=1)
    after = schema(solax=3)

    result = current_target_from_states(after, comparison_states=before, now=NOW)

    assert result.error is HaeoError.PUBLICATION_UNCERTAIN


def test_identical_snapshots_of_partially_published_generation_fail_closed() -> None:
    states = schema()
    last_run = NOW - timedelta(seconds=1)
    states[OPTIMIZER_STATUS]["attributes"]["last_run"] = last_run.isoformat()
    states[OPTIMIZER_STATUS]["last_reported"] = (last_run + timedelta(milliseconds=1)).isoformat()
    states[SOLAX_ACTIVE_POWER]["last_reported"] = (last_run - timedelta(minutes=1)).isoformat()
    states[DEYE_ACTIVE_POWER]["last_reported"] = (last_run - timedelta(minutes=1)).isoformat()

    result = current_target_from_states(states, comparison_states=states, now=NOW)

    assert result.error is HaeoError.PUBLICATION_UNCERTAIN


def test_older_horizon_publication_is_accepted_when_boundaries_are_current_and_aligned() -> None:
    states = schema()
    states[OPTIMIZER_HORIZON]["last_updated"] = (NOW - timedelta(hours=1)).isoformat()
    states[OPTIMIZER_HORIZON]["last_reported"] = (NOW - timedelta(hours=1)).isoformat()

    assert current_target_from_states(states, now=NOW).valid


@pytest.mark.parametrize("value", [None, "not-a-timestamp", "2026-10-02T12:00:00"])
@pytest.mark.parametrize("entity_id", [OPTIMIZER_STATUS, SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER])
def test_missing_or_malformed_last_reported_fails_closed(entity_id: str, value: str | None) -> None:
    states = schema()
    if value is None:
        states[entity_id].pop("last_reported")
    else:
        states[entity_id]["last_reported"] = value

    assert current_target_from_states(states, now=NOW).error is HaeoError.PUBLICATION_UNCERTAIN


def test_publication_implausibly_in_the_future_fails_closed() -> None:
    states = schema()
    states[SOLAX_ACTIVE_POWER]["last_reported"] = (NOW + timedelta(seconds=6)).isoformat()

    assert current_target_from_states(states, now=NOW).error is HaeoError.PUBLICATION_UNCERTAIN


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
