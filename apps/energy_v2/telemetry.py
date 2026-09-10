from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Protocol

from .config import ENTITY_IDS
from .models import (
    ControlTelemetrySnapshot,
    NumericTelemetrySample,
    TelemetryQuality,
    TelemetrySnapshot,
)

INVALID_STATES = {"unknown", "unavailable", "", "none", "null"}


class StateReader(Protocol):
    def get_state(self, entity_id: str, **kwargs: Any) -> Any: ...


def parse_float_state(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in INVALID_STATES:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric_value):
        return None
    return numeric_value


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

    def _optional_float(self, key: str) -> float | None:
        return parse_float_state(self._state(key))

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
            solax_house_load_w=self._optional_float("solax_house_load"),
            solax_measured_power_w=self._float("solax_measured_power", errors),
            solax_measured_power_l1_w=self._optional_float("solax_measured_power_l1"),
            solax_measured_power_l2_w=self._optional_float("solax_measured_power_l2"),
            solax_measured_power_l3_w=self._optional_float("solax_measured_power_l3"),
            solax_grid_import_w=self._float("solax_grid_import", errors),
            solax_grid_export_w=self._float("solax_grid_export", errors),
            deye_soc_pct=self._float("deye_soc", errors),
            deye_battery_power_w=self._float("deye_battery_power", errors),
            deye_battery_power_raw_w=self._optional_float("deye_battery_power_raw"),
            deye_battery_state=self._text("deye_battery_state", errors),
            deye_grid_power_w=self._float("deye_grid_power", errors),
            deye_external_power_w=self._float("deye_external_power", errors),
            deye_device_state=self._text("deye_device_state", errors),
            deye_connected=self._bool("deye_connection", errors),
            buy_price=self._float("buy_price", errors),
            sell_price=self._float("sell_price", errors),
            future_sell_rank=self._optional_float("future_sell_rank"),
            deye_grid_charging_enabled=self._bool("deye_grid_charging", errors),
            deye_export_enabled=self._bool("deye_export_surplus", errors),
            solax_inverter_power_w=self._float("solax_inverter_power", errors),
            deye_inverter_power_w=self._float("deye_inverter_power", errors),
        )
        self.last_errors = tuple(errors)
        return snap

    def control_snapshot(
        self,
        *,
        now: datetime | None = None,
        maximum_age_s: float = 15.0,
    ) -> ControlTelemetrySnapshot:
        sampled_at = now or datetime.now().astimezone()
        return ControlTelemetrySnapshot(
            sampled_at=sampled_at,
            solax_inverter_power=self.numeric_sample("solax_inverter_power", sampled_at, maximum_age_s),
            deye_inverter_power=self.numeric_sample("deye_inverter_power", sampled_at, maximum_age_s),
            whole_site_grid_power=self.numeric_sample("solax_measured_power", sampled_at, maximum_age_s),
            solax_battery_power=self.numeric_sample("solax_battery_power", sampled_at, maximum_age_s),
            deye_battery_power=self.numeric_sample("deye_battery_power", sampled_at, maximum_age_s),
            solax_soc=self.numeric_sample("solax_soc", sampled_at, maximum_age_s),
            deye_soc=self.numeric_sample("deye_soc", sampled_at, maximum_age_s),
            solax_pv_power=self.numeric_sample("solax_pv_power", sampled_at, maximum_age_s),
            deye_pv_power=self.numeric_sample("deye_pv_power", sampled_at, maximum_age_s),
        )

    def numeric_sample(self, key: str, now: datetime, maximum_age_s: float) -> NumericTelemetrySample:
        entity_id = self.entity_ids[key]
        raw = self.app.get_state(entity_id, attribute="all")
        if raw is None:
            return NumericTelemetrySample(None, None, None, False, TelemetryQuality.MISSING, entity_id)
        state = raw.get("state") if isinstance(raw, dict) else raw
        value = parse_float_state(state)
        timestamp = _state_timestamp(raw, now)
        age_s = max((now - timestamp).total_seconds(), 0.0) if timestamp is not None else None
        if value is None:
            return NumericTelemetrySample(None, timestamp, age_s, False, TelemetryQuality.INVALID, entity_id)
        if timestamp is None:
            return NumericTelemetrySample(value, None, None, False, TelemetryQuality.INVALID, entity_id)
        fresh = age_s is not None and age_s <= maximum_age_s
        quality = TelemetryQuality.VALID if fresh else TelemetryQuality.STALE
        return NumericTelemetrySample(value, timestamp, age_s, fresh, quality, entity_id)


def _state_timestamp(raw: object, fallback: datetime) -> datetime | None:
    if not isinstance(raw, dict):
        return fallback
    value = raw.get("last_updated") or raw.get("last_changed")
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None and fallback.tzinfo is not None:
        parsed = parsed.replace(tzinfo=fallback.tzinfo)
    return parsed
