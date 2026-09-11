from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReplanSnapshot:
    evaluated_at: datetime
    price_revision: str
    pv_total_kwh: float
    actual_soc_pct: dict[str, float]
    planned_soc_pct: dict[str, float]
    forecast_load_kwh: float
    actual_load_kwh: float


def replan_reasons(
    previous: ReplanSnapshot,
    current: ReplanSnapshot,
    *,
    interval: timedelta = timedelta(minutes=15),
    pv_change_fraction: float = 0.15,
    soc_deviation_pct: float = 3.0,
    load_deviation_fraction: float = 0.20,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if current.price_revision != previous.price_revision:
        reasons.append("PRICE_HORIZON_CHANGED")
    if current.evaluated_at - previous.evaluated_at >= interval:
        reasons.append("PERIODIC_15_MINUTE_REPLAN")
    pv_base = max(previous.pv_total_kwh, 0.1)
    if abs(current.pv_total_kwh - previous.pv_total_kwh) / pv_base >= pv_change_fraction:
        reasons.append("PV_FORECAST_CHANGED")
    if any(
        abs(current.actual_soc_pct[name] - current.planned_soc_pct[name]) >= soc_deviation_pct
        for name in current.actual_soc_pct.keys() & current.planned_soc_pct.keys()
    ):
        reasons.append("SOC_DEVIATION")
    load_base = max(current.forecast_load_kwh, 0.1)
    if abs(current.actual_load_kwh - current.forecast_load_kwh) / load_base >= load_deviation_fraction:
        reasons.append("LOAD_DEVIATION")
    return tuple(reasons)
