from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.energy_v2.load_model import LoadModelParameters, estimate_whole_site_load
from apps.energy_v2.models import NumericTelemetrySample, TelemetryQuality

NOW = datetime(2026, 9, 10, 18, 20, tzinfo=UTC)


def sample(value: float | None, *, age: float = 0, name: str = "sensor.test") -> NumericTelemetrySample:
    timestamp = NOW - timedelta(seconds=age) if value is not None else None
    quality = TelemetryQuality.VALID if value is not None else TelemetryQuality.MISSING
    return NumericTelemetrySample(value, timestamp, age if value is not None else None, age <= 15, quality, name)


def test_replay_2026_09_10_deye_export_whole_site_load() -> None:
    # Observed during the read-only export audit: 82 W + 9797 W - 9329 W.
    result = estimate_whole_site_load(sample(82), sample(9797), sample(9329))
    assert result.load_w == 550
    assert result.quality is TelemetryQuality.VALID


def test_whole_site_load_during_import() -> None:
    result = estimate_whole_site_load(sample(500), sample(0), sample(-700))
    assert result.load_w == 1200


def test_whole_site_load_at_zero_grid_flow() -> None:
    assert estimate_whole_site_load(sample(600), sample(400), sample(0)).load_w == 1000


def test_stale_load_sensor_is_not_used() -> None:
    result = estimate_whole_site_load(sample(100, age=20), sample(200), sample(0))
    assert result.quality is TelemetryQuality.STALE
    assert result.load_w is None


def test_timestamp_skew_is_invalid() -> None:
    result = estimate_whole_site_load(
        sample(100, age=7),
        sample(200),
        sample(0),
        LoadModelParameters(maximum_timestamp_skew_s=5),
    )
    assert result.quality is TelemetryQuality.SKEWED


def test_significantly_negative_load_is_not_clamped() -> None:
    result = estimate_whole_site_load(sample(100), sample(100), sample(1000))
    assert result.quality is TelemetryQuality.INVALID
    assert result.load_w is None


def test_small_negative_load_is_within_measurement_tolerance() -> None:
    result = estimate_whole_site_load(sample(100), sample(100), sample(250))
    assert result.load_w == 0
    assert "tolerance" in result.reason
