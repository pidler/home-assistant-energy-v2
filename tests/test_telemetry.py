from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.energy_v2.models import TelemetryQuality
from apps.energy_v2.telemetry import TelemetryReader, parse_bool_state, parse_float_state


def test_parse_float_numeric_value() -> None:
    assert parse_float_state("12.5") == 12.5


def test_parse_float_unknown() -> None:
    assert parse_float_state("unknown") is None


def test_parse_float_unavailable() -> None:
    assert parse_float_state("unavailable") is None


def test_parse_float_none() -> None:
    assert parse_float_state(None) is None


def test_parse_float_non_numeric_text() -> None:
    assert parse_float_state("not-a-number") is None


def test_parse_float_negative_power() -> None:
    assert parse_float_state("-10482") == -10482.0


def test_parse_float_nan_rejected() -> None:
    assert parse_float_state("NaN") is None


def test_parse_float_positive_infinity_rejected() -> None:
    assert parse_float_state("inf") is None


def test_parse_float_negative_infinity_rejected() -> None:
    assert parse_float_state("-inf") is None


def test_parse_bool_on_off() -> None:
    assert parse_bool_state("on") is True
    assert parse_bool_state("off") is False


class SampleReader:
    def __init__(self, value: object) -> None:
        self.value = value

    def get_state(self, entity_id: str, **kwargs: object) -> object:
        return self.value


def test_numeric_sample_reports_timestamp_age_and_freshness() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    reader = TelemetryReader(
        SampleReader({"state": "123", "last_updated": (now - timedelta(seconds=4)).isoformat()}),
        {"power": "sensor.power"},
    )
    result = reader.numeric_sample("power", now, 5)
    assert result.value == 123
    assert result.age_s == 4
    assert result.fresh
    assert result.quality is TelemetryQuality.VALID


def test_numeric_sample_reports_stale_quality() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    reader = TelemetryReader(
        SampleReader({"state": "123", "last_updated": (now - timedelta(seconds=30)).isoformat()}),
        {"power": "sensor.power"},
    )
    result = reader.numeric_sample("power", now, 5)
    assert not result.fresh
    assert result.quality is TelemetryQuality.STALE


def test_timestamped_dict_without_timestamp_is_invalid_not_fresh() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    reader = TelemetryReader(SampleReader({"state": "123", "attributes": {}}), {"power": "sensor.power"})

    result = reader.numeric_sample("power", now, 5)

    assert result.value == 123
    assert result.timestamp is None
    assert result.age_s is None
    assert not result.fresh
    assert result.quality is TelemetryQuality.INVALID
