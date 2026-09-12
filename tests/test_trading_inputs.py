from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.energy_v2.trading.inputs import (
    assemble_slots,
    build_time_of_day_load_profile,
    calculate_whole_site_load_w,
    parse_timestamped_prices,
    resample_solcast_30min_to_15min_energy,
)
from apps.energy_v2.trading.models import ForecastQuality
from apps.energy_v2.trading.rolling import ReplanSnapshot, replan_reasons


def test_price_attributes_are_used_directly_without_duplication() -> None:
    start = datetime(2026, 9, 11, tzinfo=UTC)
    attributes = {
        start.isoformat(): 4.0,
        (start + timedelta(minutes=15)).isoformat(): 5.0,
        "friendly_name": "price",
    }
    assert parse_timestamped_prices(attributes) == {start: 4.0, start + timedelta(minutes=15): 5.0}


def test_price_parser_keeps_all_available_future_slots_not_fixed_96() -> None:
    start = datetime(2026, 9, 11, tzinfo=UTC)
    attributes = {(start + timedelta(minutes=15 * index)).isoformat(): index for index in range(137)}
    assert len(parse_timestamped_prices(attributes, now=start)) == 137


def test_assemble_slots_uses_full_common_horizon() -> None:
    start = datetime(2026, 9, 11, tzinfo=UTC)
    timestamps = [start + timedelta(minutes=15 * index) for index in range(110)]
    buy = {timestamp: 8.0 for timestamp in timestamps}
    sell = {timestamp: 4.0 for timestamp in timestamps}
    pv = {timestamp: 0.0 for timestamp in timestamps}
    load = {timestamp: 0.25 for timestamp in timestamps}
    slots = assemble_slots(buy, sell, pv, load)
    assert len(slots) == 110
    assert slots[-1].timestamp == timestamps[-1]


def test_assemble_slots_appends_explicit_guard_only_horizon() -> None:
    start = datetime(2026, 9, 11, 23, 30, tzinfo=UTC)
    priced = [start, start + timedelta(minutes=15)]
    physical = priced + [start + timedelta(minutes=30), start + timedelta(minutes=45)]
    buy = {timestamp: 8.0 for timestamp in priced}
    sell = {timestamp: 4.0 for timestamp in priced}
    pv = {timestamp: 0.0 for timestamp in physical}
    load = {timestamp: 0.1 for timestamp in physical}

    slots = assemble_slots(buy, sell, pv, load, guard_until=start + timedelta(hours=1))

    assert len(slots) == 4
    assert not slots[1].is_guard_only
    assert slots[2].is_guard_only
    assert slots[2].buy_price_czk_per_kwh is None
    assert slots[2].sell_price_czk_per_kwh is None


def test_assemble_slots_rejects_missing_guard_forecast() -> None:
    start = datetime(2026, 9, 11, 23, 45, tzinfo=UTC)
    buy = {start: 8.0}
    sell = {start: 4.0}
    pv = {start: 0.0}
    load = {start: 0.1}

    with pytest.raises(ValueError, match="PV/load data missing for guard slot"):
        assemble_slots(buy, sell, pv, load, guard_until=start + timedelta(minutes=30))


def test_solcast_resampling_preserves_energy() -> None:
    start = datetime(2026, 9, 11, tzinfo=UTC)
    source = [
        {"period_start": start.isoformat(), "pv_estimate": 4.0},
        {"period_start": (start + timedelta(minutes=30)).isoformat(), "pv_estimate": 2.0},
    ]
    result = resample_solcast_30min_to_15min_energy(source)
    assert len(result) == 4
    assert sum(result.values()) == pytest.approx(4 * 0.5 + 2 * 0.5)


def test_load_profile_accepts_prepared_whole_site_history() -> None:
    target = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    history = []
    for day in range(14):
        timestamp = target - timedelta(days=day + 1)
        for quarter in range(96):
            point = timestamp.replace(hour=quarter // 4, minute=(quarter % 4) * 15)
            solax_ac = 1_400.0 if point.weekday() < 5 else 2_400.0
            deye_ac = 100.0
            grid_export = 500.0
            history.append((point, calculate_whole_site_load_w(solax_ac, deye_ac, grid_export)))
    profile, quality = build_time_of_day_load_profile(history, [target])
    assert quality is ForecastQuality.MEASURED_MODEL
    assert profile[target] == pytest.approx(0.25)


def test_whole_site_load_formula_obeys_grid_export_sign() -> None:
    assert calculate_whole_site_load_w(4_000, 2_000, 1_500) == pytest.approx(4_500)
    assert calculate_whole_site_load_w(4_000, 2_000, -1_500) == pytest.approx(7_500)


def test_load_profile_explicit_fallback() -> None:
    target = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    profile, quality = build_time_of_day_load_profile([], [target], fallback_power_w=800)
    assert quality is ForecastQuality.FALLBACK
    assert profile[target] == pytest.approx(0.2)


def test_rolling_replan_reports_all_material_triggers() -> None:
    start = datetime(2026, 9, 11, tzinfo=UTC)
    previous = ReplanSnapshot(start, "today", 10, {"DEYE": 50}, {"DEYE": 50}, 4, 4)
    current = ReplanSnapshot(start + timedelta(minutes=15), "today+d1", 12, {"DEYE": 45}, {"DEYE": 50}, 4, 5)
    assert replan_reasons(previous, current) == (
        "PRICE_HORIZON_CHANGED",
        "PERIODIC_15_MINUTE_REPLAN",
        "PV_FORECAST_CHANGED",
        "SOC_DEVIATION",
        "LOAD_DEVIATION",
    )
