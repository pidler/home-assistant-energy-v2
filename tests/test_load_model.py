from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v2.load_model import (
    LoadModelParameters,
    estimate_whole_site_load,
    validate_load_model_parameters,
)
from apps.energy_v2.models import NumericTelemetrySample, TelemetryQuality

NOW = datetime(2026, 9, 10, 18, 20, tzinfo=UTC)


def sample(value: float | None, *, age: float = 0, name: str = "sensor.test") -> NumericTelemetrySample:
    timestamp = NOW - timedelta(seconds=age) if value is not None else None
    quality = TelemetryQuality.VALID if value is not None else TelemetryQuality.MISSING
    return NumericTelemetrySample(value, timestamp, age if value is not None else None, age <= 15, quality, name)


def healthy_stable_zero(age: float, *, effective_age: float = 15) -> NumericTelemetrySample:
    return NumericTelemetrySample(
        0,
        NOW - timedelta(seconds=age),
        age,
        True,
        TelemetryQuality.VALID,
        "sensor.solax_measured_power",
        True,
        True,
        "stable zero accepted",
        NOW - timedelta(seconds=effective_age),
    )


def healthy_fast(value: float, age: float) -> NumericTelemetrySample:
    timestamp = NOW - timedelta(seconds=age)
    return NumericTelemetrySample(
        value,
        timestamp,
        age,
        True,
        TelemetryQuality.VALID,
        "sensor.fast_power",
        True,
        True,
        "source healthy",
        timestamp,
    )


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
        LoadModelParameters(maximum_timestamp_skew_s=5, coherence_jitter_s=0),
    )
    assert result.quality is TelemetryQuality.SKEWED


def test_production_5_and_15_second_cadence_is_valid_with_twenty_second_skew() -> None:
    result = estimate_whole_site_load(sample(100, age=15), sample(200, age=5), healthy_stable_zero(3600))
    assert result.quality is TelemetryQuality.VALID
    assert result.timestamp_skew_s == 10
    assert result.load_w == 300


def test_excessive_effective_timestamp_skew_is_rejected() -> None:
    result = estimate_whole_site_load(
        healthy_fast(100, age=50), healthy_fast(200, age=5), healthy_stable_zero(3600, effective_age=50)
    )
    assert result.quality is TelemetryQuality.SKEWED
    assert result.timestamp_skew_s == 45


@pytest.mark.parametrize("actual_skew_s", [20.0, 20.1, 24.9, 25.0])
def test_coherence_jitter_accepts_boundary_and_values_below_it(actual_skew_s: float) -> None:
    result = estimate_whole_site_load(
        healthy_fast(100, age=actual_skew_s),
        healthy_fast(200, age=0),
        healthy_stable_zero(3600, effective_age=actual_skew_s),
        LoadModelParameters(maximum_timestamp_skew_s=20, coherence_jitter_s=5),
    )

    assert result.quality is TelemetryQuality.VALID
    assert result.timestamp_skew_s == actual_skew_s


def test_coherence_jitter_rejects_value_above_boundary() -> None:
    result = estimate_whole_site_load(
        healthy_fast(100, age=25.1),
        healthy_fast(200, age=0),
        healthy_stable_zero(3600, effective_age=25.1),
        LoadModelParameters(maximum_timestamp_skew_s=20, coherence_jitter_s=5),
    )

    assert result.quality is TelemetryQuality.SKEWED
    assert result.timestamp_skew_s == 25.1


def test_production_coherence_replay_accepts_20_017332_seconds_but_rejects_25_024222() -> None:
    parameters = LoadModelParameters(maximum_timestamp_skew_s=20, coherence_jitter_s=5)

    accepted = estimate_whole_site_load(
        healthy_fast(100, age=20.017332),
        healthy_fast(200, age=0),
        healthy_stable_zero(3600, effective_age=20.017332),
        parameters,
    )
    rejected = estimate_whole_site_load(
        healthy_fast(100, age=25.024222),
        healthy_fast(200, age=0),
        healthy_stable_zero(3600, effective_age=25.024222),
        parameters,
    )

    assert accepted.quality is TelemetryQuality.VALID
    assert rejected.quality is TelemetryQuality.SKEWED


def test_coherence_jitter_never_promotes_stale_sample() -> None:
    result = estimate_whole_site_load(
        NumericTelemetrySample(
            100,
            NOW - timedelta(seconds=10),
            10,
            False,
            TelemetryQuality.STALE,
            "sensor.stale",
        ),
        healthy_fast(200, age=0),
        healthy_stable_zero(3600, effective_age=10),
        LoadModelParameters(maximum_timestamp_skew_s=20, coherence_jitter_s=5),
    )

    assert result.quality is TelemetryQuality.STALE
    assert result.load_w is None


def test_old_zero_grid_with_recent_health_uses_coherence_jitter() -> None:
    result = estimate_whole_site_load(
        healthy_fast(100, age=23),
        healthy_fast(200, age=2),
        healthy_stable_zero(600, effective_age=23),
        LoadModelParameters(maximum_timestamp_skew_s=20, coherence_jitter_s=5),
    )

    assert result.quality is TelemetryQuality.VALID
    assert result.timestamp_skew_s == 21
    assert result.load_w == 300


def test_invalid_coherence_jitter_is_rejected_without_changing_freshness() -> None:
    errors = validate_load_model_parameters(
        LoadModelParameters(maximum_timestamp_skew_s=20, coherence_jitter_s=float("inf"))
    )

    assert errors == ("coherence_jitter_s must be finite and non-negative",)


def test_significantly_negative_load_is_not_clamped() -> None:
    result = estimate_whole_site_load(sample(100), sample(100), sample(1000))
    assert result.quality is TelemetryQuality.INVALID
    assert result.load_w is None


def test_small_negative_load_is_within_measurement_tolerance() -> None:
    result = estimate_whole_site_load(sample(100), sample(100), sample(250))
    assert result.load_w == 0
    assert "tolerance" in result.reason
