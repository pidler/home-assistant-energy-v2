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
