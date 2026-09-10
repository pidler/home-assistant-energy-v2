"""Whole-site AC load estimation for the Phase 4 shadow controller."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .models import NumericTelemetrySample, TelemetryQuality


@dataclass(frozen=True)
class LoadModelParameters:
    maximum_sample_age_s: float = 15.0
    maximum_timestamp_skew_s: float = 5.0
    negative_load_tolerance_w: float = 150.0


@dataclass(frozen=True)
class LoadEstimate:
    load_w: float | None
    quality: TelemetryQuality
    reason: str
    timestamp_skew_s: float | None


def estimate_whole_site_load(
    solax_ac: NumericTelemetrySample,
    deye_ac: NumericTelemetrySample,
    grid: NumericTelemetrySample,
    parameters: LoadModelParameters | None = None,
) -> LoadEstimate:
    """Estimate L_site = P_solax_ac + P_deye_ac - G_site.

    Positive inverter powers feed the common AC bus; positive grid power is
    export. Inconsistent data is reported instead of being silently clamped.
    """

    p = parameters or LoadModelParameters()
    samples = (solax_ac, deye_ac, grid)
    missing = tuple(sample.entity_id for sample in samples if sample.value is None or sample.timestamp is None)
    if missing:
        return LoadEstimate(None, TelemetryQuality.MISSING, f"Missing load inputs: {', '.join(missing)}", None)
    invalid = tuple(
        sample.entity_id
        for sample in samples
        if sample.value is None or not isfinite(sample.value) or sample.quality is TelemetryQuality.INVALID
    )
    if invalid:
        return LoadEstimate(None, TelemetryQuality.INVALID, f"Invalid load inputs: {', '.join(invalid)}", None)
    stale = tuple(
        sample.entity_id
        for sample in samples
        if not sample.fresh or sample.age_s is None or sample.age_s > p.maximum_sample_age_s
    )
    if stale:
        return LoadEstimate(None, TelemetryQuality.STALE, f"Stale load inputs: {', '.join(stale)}", None)

    timestamps = [sample.timestamp for sample in samples if sample.timestamp is not None]
    skew_s = (max(timestamps) - min(timestamps)).total_seconds()
    if skew_s > p.maximum_timestamp_skew_s:
        return LoadEstimate(
            None,
            TelemetryQuality.SKEWED,
            f"Load input timestamp skew {skew_s:.1f} s exceeds {p.maximum_timestamp_skew_s:.1f} s",
            skew_s,
        )

    assert solax_ac.value is not None and deye_ac.value is not None and grid.value is not None
    load_w = solax_ac.value + deye_ac.value - grid.value
    if load_w < -p.negative_load_tolerance_w:
        return LoadEstimate(
            None,
            TelemetryQuality.INVALID,
            f"Calculated whole-site load is significantly negative: {load_w:.1f} W",
            skew_s,
        )
    if load_w < 0:
        return LoadEstimate(
            0.0,
            TelemetryQuality.VALID,
            f"Small negative load {load_w:.1f} W accepted within measurement tolerance",
            skew_s,
        )
    return LoadEstimate(load_w, TelemetryQuality.VALID, "Whole-site AC balance is valid", skew_s)
