from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .models import ForecastQuality, TradingSlotInput


def calculate_whole_site_load_w(solax_inverter_power_w: float, deye_power_w: float, grid_power_w: float) -> float:
    """Return authoritative whole-site load; grid is positive export."""

    return solax_inverter_power_w + deye_power_w - grid_power_w


def assemble_slots(
    buy_prices: dict[datetime, float],
    sell_prices: dict[datetime, float],
    pv_forecast_kwh: dict[datetime, float],
    load_forecast_kwh: dict[datetime, float],
) -> tuple[TradingSlotInput, ...]:
    """Join prepared series on their complete common 15-minute horizon."""

    timestamps = sorted(buy_prices.keys() & sell_prices.keys() & pv_forecast_kwh.keys() & load_forecast_kwh.keys())
    if not timestamps:
        raise ValueError("price, PV and load series have no common timestamps")
    if any(right - left != timedelta(minutes=15) for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError("common input horizon is not continuous at 15-minute cadence")
    return tuple(
        TradingSlotInput(
            timestamp=timestamp,
            buy_price_czk_per_kwh=buy_prices[timestamp],
            sell_price_czk_per_kwh=sell_prices[timestamp],
            pv_forecast_kwh=pv_forecast_kwh[timestamp],
            load_forecast_kwh=load_forecast_kwh[timestamp],
        )
        for timestamp in timestamps
    )


def parse_timestamped_prices(attributes: dict[str, Any], *, now: datetime | None = None) -> dict[datetime, float]:
    """Read the timestamp-keyed 15-minute values exposed by the HA price sensors."""

    result: dict[datetime, float] = {}
    for key, value in attributes.items():
        try:
            timestamp = datetime.fromisoformat(key)
            price = float(value)
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            continue
        if now is None or timestamp >= now:
            result[timestamp] = price
    return dict(sorted(result.items()))


def resample_solcast_30min_to_15min_energy(
    detailed_forecast: Iterable[dict[str, Any]],
) -> dict[datetime, float]:
    """Convert Solcast's 30-minute average kW points to 15-minute kWh slots.

    Every average-power point represents 0.5 h. Splitting it into two equal
    0.25 h energy slots preserves forecast energy exactly.
    """

    result: dict[datetime, float] = {}
    for item in detailed_forecast:
        timestamp = datetime.fromisoformat(str(item["period_start"]))
        if timestamp.tzinfo is None:
            raise ValueError("Solcast timestamps must be timezone-aware")
        power_kw = float(item["pv_estimate"])
        if power_kw < 0:
            raise ValueError("Solcast PV forecast cannot be negative")
        quarter_energy_kwh = power_kw * 0.25
        result[timestamp] = quarter_energy_kwh
        result[timestamp + timedelta(minutes=15)] = quarter_energy_kwh
    return dict(sorted(result.items()))


def build_time_of_day_load_profile(
    history: Iterable[tuple[datetime, float]],
    target_timestamps: Iterable[datetime],
    *,
    fallback_power_w: float = 500.0,
) -> tuple[dict[datetime, float], ForecastQuality]:
    """Build a load forecast from prepared authoritative whole-site history."""

    buckets: dict[tuple[bool, int, int], list[float]] = defaultdict(list)
    for timestamp, power_w in history:
        if timestamp.tzinfo is None or power_w < 0:
            continue
        quarter = (timestamp.minute // 15) * 15
        buckets[(timestamp.weekday() >= 5, timestamp.hour, quarter)].append(power_w)
    targets = tuple(target_timestamps)
    enough_history = bool(buckets) and sum(len(values) for values in buckets.values()) >= 96
    quality = ForecastQuality.MEASURED_MODEL if enough_history else ForecastQuality.SIMPLE_BASELINE
    result: dict[datetime, float] = {}
    for timestamp in targets:
        key = (timestamp.weekday() >= 5, timestamp.hour, (timestamp.minute // 15) * 15)
        values = buckets.get(key)
        power_w = median(values) if values else fallback_power_w
        result[timestamp] = power_w / 1000 * 0.25
    if not buckets:
        quality = ForecastQuality.FALLBACK
    return result, quality
