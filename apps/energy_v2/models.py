from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - exercised only on Python < 3.11
    from enum import Enum

    class StrEnum(str, Enum):  # noqa: UP042
        pass


class Mode(StrEnum):
    DISABLED = "DISABLED"
    IDLE = "IDLE"
    PV_CHARGE_DEYE = "PV_CHARGE_DEYE"
    EXPORT_DEYE = "EXPORT_DEYE"
    EXPORT_SOLAX = "EXPORT_SOLAX"
    FAULT = "FAULT"
    SERVICE = "SERVICE"


class AppStatus(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CONFIG_ERROR = "CONFIG_ERROR"


class Strategy(StrEnum):
    SUMMER_NO_GRID_CHARGE = "SUMMER_NO_GRID_CHARGE"
    WINTER_GRID_OPTIMIZATION = "WINTER_GRID_OPTIMIZATION"
    SERVICE = "SERVICE"


class FlowState(StrEnum):
    UNKNOWN = "UNKNOWN"
    NORMAL = "NORMAL"
    GRID_IMPORT = "GRID_IMPORT"
    GRID_EXPORT = "GRID_EXPORT"
    LIKELY_PV_SURPLUS_CHARGE = "LIKELY_PV_SURPLUS_CHARGE"
    SOLAX_TO_DEYE = "SOLAX_TO_DEYE"
    DEYE_TO_SOLAX = "DEYE_TO_SOLAX"
    CROSS_CHARGING = "CROSS_CHARGING"
    AMBIGUOUS = "AMBIGUOUS"


class ExportLimitState(StrEnum):
    UNKNOWN = "UNKNOWN"
    EXPORT_WITHIN_TARGET = "EXPORT_WITHIN_TARGET"
    EXPORT_INSTANT_ABOVE_TARGET = "EXPORT_INSTANT_ABOVE_TARGET"
    EXPORT_AVERAGE_NEAR_LIMIT = "EXPORT_AVERAGE_NEAR_LIMIT"
    EXPORT_AVERAGE_LIMIT_VIOLATION = "EXPORT_AVERAGE_LIMIT_VIOLATION"


class ChargeShadowState(StrEnum):
    DISABLED = "DISABLED"
    WAITING_FOR_SOLAX = "WAITING_FOR_SOLAX"
    START_CONFIRMATION = "START_CONFIRMATION"
    CHARGE_DEYE_FULL = "CHARGE_DEYE_FULL"
    TRANSFER_CONFIRMATION = "TRANSFER_CONFIRMATION"
    CHARGE_DEYE_LIMITED = "CHARGE_DEYE_LIMITED"
    RETURN_CONFIRMATION = "RETURN_CONFIRMATION"
    SOLAX_PRIORITY = "SOLAX_PRIORITY"
    FAULT = "FAULT"


class BatteryId(StrEnum):
    DEYE = "DEYE"
    SOLAX = "SOLAX"


class BatteryAction(StrEnum):
    HOLD = "HOLD"
    CHARGE = "CHARGE"
    DISCHARGE = "DISCHARGE"


class CommandStatus(StrEnum):
    READY = "READY"
    UNVERIFIED = "UNVERIFIED"
    SATURATED = "SATURATED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    BREAK_BEFORE_MAKE = "BREAK_BEFORE_MAKE"
    FAULT = "FAULT"


class TelemetryQuality(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    SKEWED = "SKEWED"


@dataclass(frozen=True)
class NumericTelemetrySample:
    value: float | None
    timestamp: datetime | None
    age_s: float | None
    fresh: bool
    quality: TelemetryQuality
    entity_id: str = ""


@dataclass(frozen=True)
class ControlTelemetrySnapshot:
    sampled_at: datetime
    solax_inverter_power: NumericTelemetrySample
    deye_inverter_power: NumericTelemetrySample
    whole_site_grid_power: NumericTelemetrySample
    solax_battery_power: NumericTelemetrySample
    deye_battery_power: NumericTelemetrySample
    solax_soc: NumericTelemetrySample
    deye_soc: NumericTelemetrySample
    solax_pv_power: NumericTelemetrySample | None = None
    deye_pv_power: NumericTelemetrySample | None = None


@dataclass(frozen=True)
class SiteCommand:
    command_id: str
    created_at: datetime
    expires_at: datetime
    grid_target_w: float
    max_export_w: float = 9_800.0
    transfer_allowed: bool = False
    preferred_battery: BatteryId = BatteryId.DEYE
    grid_charge_allowed: bool = False


@dataclass(frozen=True)
class BatteryCommand:
    command_id: str
    site_command_id: str
    battery: BatteryId
    action: BatteryAction
    target_power_w: float
    min_soc_pct: float
    max_soc_pct: float
    grid_charge_allowed: bool
    export_allowed: bool
    expires_at: datetime


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    battery: BatteryId
    status: CommandStatus
    requested_power_w: float
    allowed_power_w: float
    simulated_power_w: float
    measured_actual_power_w: float | None
    saturated: bool
    readback_matches: bool | None
    reason: str
    proposed_settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TelemetrySnapshot:
    timestamp: datetime

    solax_soc_pct: float | None
    solax_battery_power_w: float | None
    solax_pv_power_w: float | None
    solax_house_load_w: float | None
    solax_measured_power_w: float | None
    solax_measured_power_l1_w: float | None
    solax_measured_power_l2_w: float | None
    solax_measured_power_l3_w: float | None
    solax_grid_import_w: float | None
    solax_grid_export_w: float | None

    deye_soc_pct: float | None
    deye_battery_power_w: float | None
    deye_battery_power_raw_w: float | None
    deye_battery_state: str | None
    deye_grid_power_w: float | None
    deye_external_power_w: float | None
    deye_device_state: str | None
    deye_connected: bool | None

    buy_price: float | None
    sell_price: float | None
    future_sell_rank: float | None

    deye_grid_charging_enabled: bool | None
    deye_export_enabled: bool | None
    solax_inverter_power_w: float | None = None
    deye_inverter_power_w: float | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlannerDecision:
    mode: Mode
    reason: str
    confidence: str
    flow_state: FlowState | None = None
    flow_warning: bool = False
    flow_violation: bool = False
