"""Diagnostic fixed-quarter and trailing-window export budgets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

from .flow import RollingExportAverage


@dataclass(frozen=True)
class ExportBudget:
    used_export_kwh: float
    remaining_operational_kwh: float
    remaining_legal_kwh: float
    seconds_remaining: float
    budget_power_w: float
    projected_average_w: float
    complete: bool


def quarter_start(at: datetime) -> datetime:
    return at.replace(minute=(at.minute // 15) * 15, second=0, microsecond=0)


@dataclass
class FixedQuarterExportTracker:
    legal_limit_w: float = 10_000.0
    operational_target_w: float = 9_800.0
    _quarter_start: datetime | None = None
    _used_export_ws: float = 0.0
    _last_at: datetime | None = None
    _last_export_w: float = 0.0

    def add_sample(self, at: datetime, export_w: float) -> ExportBudget:
        if not isfinite(export_w):
            return self.budget(at)
        start = quarter_start(at)
        if self._quarter_start != start or self._last_at is None or at < self._last_at:
            self._quarter_start = start
            self._used_export_ws = 0.0
            self._last_at = at
            self._last_export_w = max(export_w, 0.0)
            return self.budget(at)
        duration_s = max((at - self._last_at).total_seconds(), 0.0)
        self._used_export_ws += self._last_export_w * duration_s
        self._last_at = at
        self._last_export_w = max(export_w, 0.0)
        return self.budget(at)

    def budget(self, at: datetime) -> ExportBudget:
        start = self._quarter_start or quarter_start(at)
        end = start + timedelta(minutes=15)
        seconds_remaining = max((end - at).total_seconds(), 0.0)
        used_kwh = self._used_export_ws / 3_600_000.0
        operational_kwh = self.operational_target_w * 0.25 / 1000.0
        legal_kwh = self.legal_limit_w * 0.25 / 1000.0
        remaining_operational = max(operational_kwh - used_kwh, 0.0)
        remaining_legal = max(legal_kwh - used_kwh, 0.0)
        budget_power = remaining_operational * 3_600_000.0 / seconds_remaining if seconds_remaining else 0.0
        projected_ws = self._used_export_ws + self._last_export_w * seconds_remaining
        return ExportBudget(
            used_export_kwh=used_kwh,
            remaining_operational_kwh=remaining_operational,
            remaining_legal_kwh=remaining_legal,
            seconds_remaining=seconds_remaining,
            budget_power_w=max(min(budget_power, self.operational_target_w), 0.0),
            projected_average_w=projected_ws / 900.0,
            complete=seconds_remaining == 0,
        )


def trailing_window_budget(
    rolling: RollingExportAverage,
    *,
    legal_limit_w: float = 10_000.0,
    operational_target_w: float = 9_800.0,
) -> ExportBudget:
    """Diagnostic remaining budget for the not-yet-covered part of 900 seconds."""

    covered_s = min(max(rolling.covered_duration_s, 0.0), 900.0)
    used_ws = max(rolling.average_w, 0.0) * covered_s
    remaining_s = max(900.0 - covered_s, 0.0)
    operational_ws = operational_target_w * 900.0
    legal_ws = legal_limit_w * 900.0
    remaining_operational_ws = max(operational_ws - used_ws, 0.0)
    remaining_legal_ws = max(legal_ws - used_ws, 0.0)
    budget_power = remaining_operational_ws / remaining_s if remaining_s else 0.0
    projected = (used_ws + max(rolling.average_w, 0.0) * remaining_s) / 900.0
    return ExportBudget(
        used_ws / 3_600_000.0,
        remaining_operational_ws / 3_600_000.0,
        remaining_legal_ws / 3_600_000.0,
        remaining_s,
        budget_power,
        projected,
        rolling.window_complete,
    )
