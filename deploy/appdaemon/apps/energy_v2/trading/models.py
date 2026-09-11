from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

from ..models import StrEnum


class ForecastQuality(StrEnum):
    MEASURED_MODEL = "MEASURED_MODEL"
    SIMPLE_BASELINE = "SIMPLE_BASELINE"
    FALLBACK = "FALLBACK"


class TradingAction(StrEnum):
    HOLD = "HOLD"
    CHARGE_FROM_PV = "CHARGE_FROM_PV"
    EXPORT = "EXPORT"
    IMPORT_FOR_LOAD = "IMPORT_FOR_LOAD"


@dataclass(frozen=True)
class BatteryParameters:
    name: str
    capacity_kwh: float
    minimum_soc_pct: float = 10.0
    maximum_soc_pct: float = 100.0
    max_charge_power_w: float = 10_000.0
    max_discharge_power_w: float = 10_000.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    terminal_reserve_soc_pct: float = 20.0

    def validate(self) -> None:
        values = (
            self.capacity_kwh,
            self.minimum_soc_pct,
            self.maximum_soc_pct,
            self.max_charge_power_w,
            self.max_discharge_power_w,
            self.charge_efficiency,
            self.discharge_efficiency,
            self.terminal_reserve_soc_pct,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError(f"{self.name} parameters must be finite")
        if self.capacity_kwh <= 0:
            raise ValueError(f"{self.name} capacity must be positive")
        if not 0 <= self.minimum_soc_pct <= self.terminal_reserve_soc_pct <= self.maximum_soc_pct <= 100:
            raise ValueError(f"{self.name} SOC limits/reserve are inconsistent")
        if self.max_charge_power_w < 0 or self.max_discharge_power_w < 0:
            raise ValueError(f"{self.name} power limits must be non-negative")
        if not 0 < self.charge_efficiency <= 1 or not 0 < self.discharge_efficiency <= 1:
            raise ValueError(f"{self.name} efficiencies must be in (0, 1]")


@dataclass(frozen=True)
class TradingSlotInput:
    timestamp: datetime
    buy_price_czk_per_kwh: float
    sell_price_czk_per_kwh: float
    pv_forecast_kwh: float
    load_forecast_kwh: float

    def validate(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("slot timestamps must be timezone-aware")
        values = (
            self.buy_price_czk_per_kwh,
            self.sell_price_czk_per_kwh,
            self.pv_forecast_kwh,
            self.load_forecast_kwh,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("slot values must be finite")
        if self.buy_price_czk_per_kwh < self.sell_price_czk_per_kwh:
            raise ValueError("buy price must be at least sell price for a linear net-meter model")
        if self.pv_forecast_kwh < 0 or self.load_forecast_kwh < 0:
            raise ValueError("PV and load forecasts must be non-negative")


@dataclass(frozen=True)
class PlannerConfig:
    slot_hours: float = 0.25
    site_export_limit_w: float = 9_800.0
    site_import_limit_w: float = 30_000.0
    grid_charging_allowed: bool = False
    cycling_penalty_czk_per_kwh: float = 0.02
    solax_tie_break_penalty_czk_per_kwh: float = 0.0001
    terminal_value_factor: float = 0.60
    terminal_value_floor_czk_per_kwh: float = 1.0
    terminal_reserve_shortfall_factor: float = 1.05

    def validate(self) -> None:
        values = (
            self.slot_hours,
            self.site_export_limit_w,
            self.site_import_limit_w,
            self.cycling_penalty_czk_per_kwh,
            self.solax_tie_break_penalty_czk_per_kwh,
            self.terminal_value_factor,
            self.terminal_value_floor_czk_per_kwh,
            self.terminal_reserve_shortfall_factor,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("planner configuration must be finite")
        if self.slot_hours != 0.25:
            raise ValueError("Phase 5A supports exactly 15-minute slots")
        if self.site_export_limit_w <= 0 or self.site_import_limit_w <= 0:
            raise ValueError("grid limits must be positive")
        if self.grid_charging_allowed:
            raise ValueError("grid charging is not implemented or allowed in the summer planner")
        if self.cycling_penalty_czk_per_kwh < 0 or self.solax_tie_break_penalty_czk_per_kwh < 0:
            raise ValueError("penalties must be non-negative")
        if not 0 <= self.terminal_value_factor <= 1:
            raise ValueError("terminal value factor must be between zero and one")
        if self.terminal_value_floor_czk_per_kwh < 0:
            raise ValueError("terminal value floor must be non-negative")
        if self.terminal_reserve_shortfall_factor < 1:
            raise ValueError("terminal reserve shortfall factor must be at least one")


@dataclass(frozen=True)
class PlannerInput:
    slots: tuple[TradingSlotInput, ...]
    batteries: tuple[BatteryParameters, ...]
    initial_soc_pct: dict[str, float]
    load_forecast_quality: ForecastQuality = ForecastQuality.SIMPLE_BASELINE
    generated_at: datetime | None = None

    def validate(self) -> None:
        if not self.slots:
            raise ValueError("at least one planning slot is required")
        if not self.batteries:
            raise ValueError("at least one battery is required")
        names = [battery.name for battery in self.batteries]
        if len(set(names)) != len(names):
            raise ValueError("battery names must be unique")
        timestamps = [slot.timestamp for slot in self.slots]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("slots must be unique and sorted")
        if any(right - left != timedelta(minutes=15) for left, right in zip(timestamps, timestamps[1:], strict=False)):
            raise ValueError("slots must have continuous 15-minute cadence")
        for slot in self.slots:
            slot.validate()
        for battery in self.batteries:
            battery.validate()
            soc = self.initial_soc_pct.get(battery.name)
            if soc is None:
                raise ValueError(f"initial SOC missing for {battery.name}")
            if not isfinite(soc) or not battery.minimum_soc_pct <= soc <= battery.maximum_soc_pct:
                raise ValueError(f"initial SOC outside limits for {battery.name}")


@dataclass(frozen=True)
class BatterySlotPlan:
    charge_from_pv_kwh: float
    discharge_to_load_kwh: float
    discharge_to_export_kwh: float
    planned_power_w: float
    projected_soc_pct: float


@dataclass(frozen=True)
class TradingSlotPlan:
    timestamp: datetime
    buy_price_czk_per_kwh: float
    sell_price_czk_per_kwh: float
    pv_forecast_kwh: float
    load_forecast_kwh: float
    planned_grid_import_kwh: float
    planned_grid_export_kwh: float
    batteries: dict[str, BatterySlotPlan]
    action: TradingAction
    reason: str


@dataclass(frozen=True)
class ImportantDecision:
    timestamp: datetime
    action: TradingAction
    reason: str


@dataclass(frozen=True)
class PlannerResult:
    generated_at: datetime
    horizon_start: datetime
    horizon_end: datetime
    slots: tuple[TradingSlotPlan, ...]
    initial_soc_pct: dict[str, float]
    terminal_soc_pct: dict[str, float]
    terminal_value_czk_per_kwh: dict[str, float]
    terminal_reserved_kwh: dict[str, float]
    terminal_reserve_shortfall_kwh: dict[str, float]
    expected_export_kwh: float
    expected_import_kwh: float
    expected_revenue_czk: float
    expected_import_cost_czk: float
    expected_net_grid_value_czk: float
    objective_value_czk: float
    load_forecast_quality: ForecastQuality
    important_decisions: tuple[ImportantDecision, ...]


@dataclass(frozen=True)
class PlanComparison:
    optimizer_net_value_czk: float
    manual_net_value_czk: float
    difference_czk: float
    optimizer_terminal_soc_pct: dict[str, float]
    manual_terminal_soc_pct: dict[str, float]
