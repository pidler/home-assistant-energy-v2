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


@dataclass(frozen=True)
class TelemetrySnapshot:
    timestamp: datetime

    solax_soc_pct: float | None
    solax_battery_power_w: float | None
    solax_pv_power_w: float | None
    solax_house_load_w: float | None
    solax_grid_import_w: float | None
    solax_grid_export_w: float | None

    deye_soc_pct: float | None
    deye_battery_power_w: float | None
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
