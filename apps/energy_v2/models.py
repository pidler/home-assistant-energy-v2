from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Mode(str, Enum):
    DISABLED = "DISABLED"
    IDLE = "IDLE"
    PV_CHARGE_DEYE = "PV_CHARGE_DEYE"
    EXPORT_DEYE = "EXPORT_DEYE"
    EXPORT_SOLAX = "EXPORT_SOLAX"
    FAULT = "FAULT"
    SERVICE = "SERVICE"


@dataclass(frozen=True)
class TelemetrySnapshot:
    timestamp: datetime

    solax_soc_pct: Optional[float]
    solax_battery_power_w: Optional[float]
    solax_pv_power_w: Optional[float]
    solax_house_load_w: Optional[float]
    solax_grid_import_w: Optional[float]
    solax_grid_export_w: Optional[float]

    deye_soc_pct: Optional[float]
    deye_battery_power_w: Optional[float]
    deye_battery_state: Optional[str]
    deye_grid_power_w: Optional[float]
    deye_external_power_w: Optional[float]
    deye_device_state: Optional[str]
    deye_connected: Optional[bool]

    buy_price: Optional[float]
    sell_price: Optional[float]
    future_sell_rank: Optional[float]

    deye_grid_charging_enabled: Optional[bool]
    deye_export_enabled: Optional[bool]


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlannerDecision:
    mode: Mode
    reason: str
    confidence: str

