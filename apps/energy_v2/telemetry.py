from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .config import ENTITY_IDS
from .models import TelemetrySnapshot

INVALID_STATES = {"unknown", "unavailable", "", "none", "null"}


class StateReader(Protocol):
    def get_state(self, entity_id: str, **kwargs: Any) -> Any:
        ...


def parse_float_state(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in INVALID_STATES:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bool_state(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in INVALID_STATES:
            return None
        if normalized in {"on", "true", "yes", "1", "enabled"}:
            return True
        if normalized in {"off", "false", "no", "0", "disabled"}:
            return False
    return None


def parse_text_state(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in INVALID_STATES:
        return None
    return str(value)


class TelemetryReader:
    def __init__(self, app: StateReader, entity_ids: dict[str, str] | None = None) -> None:
        self.app = app
        self.entity_ids = entity_ids or ENTITY_IDS
        self.last_errors: tuple[str, ...] = ()

    def _state(self, key: str) -> Any:
        entity_id = self.entity_ids[key]
        return self.app.get_state(entity_id)

    def _float(self, key: str, errors: list[str]) -> float | None:
        value = parse_float_state(self._state(key))
        if value is None:
            errors.append(f"{self.entity_ids[key]} is missing or not numeric")
        return value

    def _bool(self, key: str, errors: list[str]) -> bool | None:
        value = parse_bool_state(self._state(key))
        if value is None:
            errors.append(f"{self.entity_ids[key]} is missing or not boolean")
        return value

    def _text(self, key: str, errors: list[str]) -> str | None:
        value = parse_text_state(self._state(key))
        if value is None:
            errors.append(f"{self.entity_ids[key]} is missing")
        return value

    def snapshot(self) -> TelemetrySnapshot:
        errors: list[str] = []
        snap = TelemetrySnapshot(
            timestamp=datetime.now().astimezone(),
            solax_soc_pct=self._float("solax_soc", errors),
            solax_battery_power_w=self._float("solax_battery_power", errors),
            solax_pv_power_w=self._float("solax_pv_power", errors),
            solax_house_load_w=self._float("solax_house_load", errors),
            solax_grid_import_w=self._float("solax_grid_import", errors),
            solax_grid_export_w=self._float("solax_grid_export", errors),
            deye_soc_pct=self._float("deye_soc", errors),
            deye_battery_power_w=self._float("deye_battery_power", errors),
            deye_battery_state=self._text("deye_battery_state", errors),
            deye_grid_power_w=self._float("deye_grid_power", errors),
            deye_external_power_w=self._float("deye_external_power", errors),
            deye_device_state=self._text("deye_device_state", errors),
            deye_connected=self._bool("deye_connection", errors),
            buy_price=self._float("buy_price", errors),
            sell_price=self._float("sell_price", errors),
            future_sell_rank=self._float("future_sell_rank", errors),
            deye_grid_charging_enabled=self._bool("deye_grid_charging", errors),
            deye_export_enabled=self._bool("deye_export_surplus", errors),
        )
        self.last_errors = tuple(errors)
        return snap

