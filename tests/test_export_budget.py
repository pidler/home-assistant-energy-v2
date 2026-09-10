from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.energy_v2.export_budget import FixedQuarterExportTracker, trailing_window_budget
from apps.energy_v2.flow import RollingExportAverage


def test_fixed_quarter_export_budget_tracks_energy_and_remaining_power() -> None:
    start = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    tracker = FixedQuarterExportTracker()
    tracker.add_sample(start, 9800)
    result = tracker.add_sample(start + timedelta(minutes=5), 9800)
    assert round(result.used_export_kwh, 3) == 0.817
    assert result.seconds_remaining == 600
    assert round(result.budget_power_w) == 9800


def test_fixed_quarter_resets_at_quarter_boundary() -> None:
    start = datetime(2026, 9, 10, 12, 14, tzinfo=UTC)
    tracker = FixedQuarterExportTracker()
    tracker.add_sample(start, 9000)
    tracker.add_sample(start + timedelta(seconds=30), 9000)
    result = tracker.add_sample(start + timedelta(minutes=1), 1000)
    assert result.used_export_kwh == 0
    assert result.seconds_remaining == 900


def test_fixed_quarter_splits_sample_interval_at_boundary() -> None:
    before = datetime(2026, 9, 10, 12, 14, 58, tzinfo=UTC)
    tracker = FixedQuarterExportTracker()
    tracker.add_sample(before, 9000)

    result = tracker.add_sample(before + timedelta(seconds=5), 1000)

    assert result.used_export_kwh == 9000 * 3 / 3_600_000
    assert result.seconds_remaining == 897
    after = tracker.add_sample(before + timedelta(seconds=7), 1000)
    assert after.used_export_kwh == (9000 * 3 + 1000 * 2) / 3_600_000


def test_trailing_budget_is_diagnostic_and_preserves_rolling_input() -> None:
    rolling = RollingExportAverage(5000, 450, False)
    result = trailing_window_budget(rolling)
    assert result.used_export_kwh == 0.625
    assert result.seconds_remaining == 450
    assert result.budget_power_w == 14_600
