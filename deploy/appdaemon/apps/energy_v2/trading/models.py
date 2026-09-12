from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
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


class PhysicalLimitStatus(StrEnum):
    MODEL_ASSUMPTION = "MODEL_ASSUMPTION"
    CONFIRMED_PHYSICAL_LIMIT = "CONFIRMED_PHYSICAL_LIMIT"


class BatteryRole(StrEnum):
    TRADING_BATTERY = "TRADING_BATTERY"
    HOUSE_RESERVE_BATTERY = "HOUSE_RESERVE_BATTERY"


class CheckpointType(StrEnum):
    EVENING_RESERVE = "EVENING_RESERVE"
    RECOVERY_TARGET = "RECOVERY_TARGET"


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
    terminal_reserve_soc_pct: float = 10.0
    power_limit_status: PhysicalLimitStatus = PhysicalLimitStatus.MODEL_ASSUMPTION
    role: BatteryRole = BatteryRole.TRADING_BATTERY

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
        if not isinstance(self.power_limit_status, PhysicalLimitStatus):
            raise ValueError(f"{self.name} power limit status must be explicit")
        if not isinstance(self.role, BatteryRole):
            raise ValueError(f"{self.name} battery role must be explicit")


@dataclass(frozen=True)
class SocCheckpoint:
    battery_name: str
    timestamp: datetime
    minimum_soc_pct: float
    checkpoint_type: CheckpointType
    reason: str
    hard: bool = True

    def validate(self, battery: BatteryParameters) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("SOC checkpoint timestamps must be timezone-aware")
        if not battery.minimum_soc_pct <= self.minimum_soc_pct <= battery.maximum_soc_pct:
            raise ValueError(f"SOC checkpoint outside limits for {battery.name}")
        if not self.reason:
            raise ValueError("SOC checkpoint reason is required")


@dataclass(frozen=True)
class TradingSlotInput:
    timestamp: datetime
    buy_price_czk_per_kwh: float | None
    sell_price_czk_per_kwh: float | None
    pv_forecast_kwh: float
    load_forecast_kwh: float

    @property
    def is_guard_only(self) -> bool:
        return self.buy_price_czk_per_kwh is None

    def validate(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("slot timestamps must be timezone-aware")
        if (self.buy_price_czk_per_kwh is None) != (self.sell_price_czk_per_kwh is None):
            raise ValueError("buy and sell prices must both be available or both be unavailable")
        values = (self.pv_forecast_kwh, self.load_forecast_kwh)
        if not self.is_guard_only:
            values += (self.buy_price_czk_per_kwh, self.sell_price_czk_per_kwh)  # type: ignore[arg-type]
        if not all(isfinite(value) for value in values):
            raise ValueError("slot values must be finite")
        if (
            not self.is_guard_only and self.buy_price_czk_per_kwh < self.sell_price_czk_per_kwh  # type: ignore[operator]
        ):
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
    terminal_price_lookback_hours: float = 3.0
    terminal_continuation_price_czk_per_kwh: float | None = None
    solax_evening_checkpoint_local_time: time | None = None
    solax_evening_reserve_soc_pct: float = 30.0
    solax_morning_trading_start_local_time: time | None = None
    solax_morning_trading_end_local_time: time | None = None
    solax_recovery_deadline_local_time: time | None = None
    solax_morning_conditional_floor_pct: float = 15.0
    solax_recovery_target_soc_pct: float = 30.0
    operational_checkpoint_shortfall_penalty_czk_per_kwh: float = 1_000.0
    guard_grid_import_penalty_czk_per_kwh: float = 1_000.0
    guard_trading_battery_load_tie_break_czk_per_kwh: float = 0.001

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
            self.terminal_price_lookback_hours,
            self.solax_evening_reserve_soc_pct,
            self.solax_morning_conditional_floor_pct,
            self.solax_recovery_target_soc_pct,
            self.operational_checkpoint_shortfall_penalty_czk_per_kwh,
            self.guard_grid_import_penalty_czk_per_kwh,
            self.guard_trading_battery_load_tie_break_czk_per_kwh,
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
        if self.operational_checkpoint_shortfall_penalty_czk_per_kwh <= 0:
            raise ValueError("operational checkpoint shortfall penalty must be positive")
        if self.guard_grid_import_penalty_czk_per_kwh <= 0:
            raise ValueError("guard grid import penalty must be positive")
        if self.guard_trading_battery_load_tie_break_czk_per_kwh < 0:
            raise ValueError("guard trading-battery load tie-break must be non-negative")
        if self.terminal_price_lookback_hours <= 0:
            raise ValueError("terminal price lookback must be positive")
        if self.terminal_continuation_price_czk_per_kwh is not None and not isfinite(
            self.terminal_continuation_price_czk_per_kwh
        ):
            raise ValueError("explicit terminal continuation price must be finite")
        morning_times = (
            self.solax_morning_trading_start_local_time,
            self.solax_morning_trading_end_local_time,
            self.solax_recovery_deadline_local_time,
        )
        if any(value is not None for value in morning_times) and not all(value is not None for value in morning_times):
            raise ValueError("morning trading start, end and recovery deadline must be configured together")
        if all(value is not None for value in morning_times) and not (
            morning_times[0] < morning_times[1] < morning_times[2]  # type: ignore[operator]
        ):
            raise ValueError("morning trading start, end and recovery deadline must be ordered within one day")
        if not 10 <= self.solax_morning_conditional_floor_pct <= self.solax_recovery_target_soc_pct <= 100:
            raise ValueError("SolaX morning floor/recovery target are inconsistent")
        if not 10 <= self.solax_evening_reserve_soc_pct <= 100:
            raise ValueError("SolaX evening reserve must be between 10 and 100 percent")
        if (
            self.solax_evening_checkpoint_local_time is not None
            and self.solax_morning_trading_start_local_time is not None
            and self.solax_evening_checkpoint_local_time == self.solax_morning_trading_start_local_time
        ):
            raise ValueError("evening checkpoint and morning trading start must differ")


@dataclass(frozen=True)
class PlannerInput:
    slots: tuple[TradingSlotInput, ...]
    batteries: tuple[BatteryParameters, ...]
    initial_soc_pct: dict[str, float]
    load_forecast_quality: ForecastQuality = ForecastQuality.SIMPLE_BASELINE
    generated_at: datetime | None = None
    soc_checkpoints: tuple[SocCheckpoint, ...] = ()

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
        priced = [index for index, slot in enumerate(self.slots) if not slot.is_guard_only]
        if not priced:
            raise ValueError("at least one economically priced slot is required")
        if priced != list(range(priced[-1] + 1)):
            raise ValueError("guard-only slots must follow the continuous economic horizon")
        for battery in self.batteries:
            battery.validate()
            soc = self.initial_soc_pct.get(battery.name)
            if soc is None:
                raise ValueError(f"initial SOC missing for {battery.name}")
            if not isfinite(soc) or not battery.minimum_soc_pct <= soc <= battery.maximum_soc_pct:
                raise ValueError(f"initial SOC outside limits for {battery.name}")
        battery_map = {battery.name: battery for battery in self.batteries}
        for checkpoint in self.soc_checkpoints:
            battery = battery_map.get(checkpoint.battery_name)
            if battery is None:
                raise ValueError(f"SOC checkpoint battery is unknown: {checkpoint.battery_name}")
            checkpoint.validate(battery)


@dataclass(frozen=True)
class BatterySlotPlan:
    charge_from_pv_kwh: float
    discharge_to_load_kwh: float
    discharge_to_export_kwh: float
    planned_power_w: float
    projected_soc_pct: float
    active_soc_floor_pct: float
    trading_export_blocked: bool
    overnight_trading_export_blocked: bool


@dataclass(frozen=True)
class TradingSlotPlan:
    timestamp: datetime
    buy_price_czk_per_kwh: float | None
    sell_price_czk_per_kwh: float | None
    pv_forecast_kwh: float
    load_forecast_kwh: float
    planned_grid_import_kwh: float
    planned_grid_export_kwh: float
    batteries: dict[str, BatterySlotPlan]
    action: TradingAction
    reason: str
    guard_only: bool


@dataclass(frozen=True)
class ImportantDecision:
    timestamp: datetime
    action: TradingAction
    reason: str


@dataclass(frozen=True)
class SocCheckpointResult:
    battery_name: str
    timestamp: datetime
    checkpoint_type: CheckpointType
    target_soc_pct: float
    actual_soc_pct: float
    shortfall_pct: float
    hard: bool
    reason: str


@dataclass(frozen=True)
class MorningRecoveryAssessment:
    battery_name: str
    trading_window_start: datetime
    trading_window_end: datetime
    recovery_deadline: datetime
    conditional_floor_pct: float
    recovery_target_pct: float
    forecast_pv_surplus_for_recovery_kwh: float
    maximum_storable_recovery_kwh: float
    required_recovery_kwh: float
    candidate_feasible: bool
    selected: bool
    morning_start_soc_pct: float
    minimum_projected_soc_pct: float
    expected_recovery_soc_pct: float
    expected_recovery_time: datetime | None


@dataclass(frozen=True)
class PlannerResult:
    generated_at: datetime
    horizon_start: datetime
    horizon_end: datetime
    economic_horizon_end: datetime
    slots: tuple[TradingSlotPlan, ...]
    initial_soc_pct: dict[str, float]
    terminal_soc_pct: dict[str, float]
    terminal_stored_kwh: dict[str, float]
    minimum_physical_soc_pct: dict[str, float]
    terminal_reserve_target_soc_pct: dict[str, float]
    terminal_value_czk_per_kwh: dict[str, float]
    terminal_continuation_price_czk_per_kwh: float
    terminal_value_method: str
    terminal_reserve_target_kwh: dict[str, float]
    terminal_reserve_shortfall_kwh: dict[str, float]
    max_charge_power_w: dict[str, float]
    max_discharge_power_w: dict[str, float]
    power_limit_status: dict[str, PhysicalLimitStatus]
    battery_role: dict[str, BatteryRole]
    checkpoints: tuple[SocCheckpointResult, ...]
    morning_recovery: dict[str, MorningRecoveryAssessment]
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
    optimizer_grid_cashflow_czk: float
    manual_grid_cashflow_czk: float
    grid_cashflow_difference_czk: float
    optimizer_terminal_stored_kwh: float
    manual_terminal_stored_kwh: float
    terminal_stored_energy_difference_kwh: float
    optimizer_terminal_value_adjustment_czk: float
    manual_terminal_value_adjustment_czk: float
    terminal_value_adjustment_difference_czk: float
    optimizer_comparable_value_czk: float
    manual_comparable_value_czk: float
    comparable_value_difference_czk: float
    optimizer_terminal_soc_pct: dict[str, float]
    manual_terminal_soc_pct: dict[str, float]
