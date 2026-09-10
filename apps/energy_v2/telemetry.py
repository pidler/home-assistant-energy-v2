from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .config import ENTITY_IDS
from .models import (
    ControlTelemetrySnapshot,
    NumericTelemetrySample,
    TelemetryClass,
    TelemetryQuality,
    TelemetrySnapshot,
)

INVALID_STATES = {"unknown", "unavailable", "", "none", "null"}


@dataclass(frozen=True)
class TelemetryFreshnessConfig:
    """Production-derived Phase 4 telemetry freshness policy."""

    solax_fast_power_max_age_s: float = 60.0
    deye_fast_power_max_age_s: float = 30.0
    fast_input_max_skew_s: float = 20.0
    solax_source_health_window_s: float = 60.0
    deye_source_health_window_s: float = 30.0


def validate_freshness_config(config: TelemetryFreshnessConfig) -> tuple[str, ...]:
    errors: list[str] = []
    for name, value in vars(config).items():
        if not math.isfinite(value) or value <= 0:
            errors.append(f"{name} must be finite and greater than zero")
    return tuple(errors)


@dataclass(frozen=True)
class _RawNumericState:
    value: float | None
    timestamp: datetime | None
    age_s: float | None
    quality: TelemetryQuality


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
    def __init__(
        self,
        app: StateReader,
        entity_ids: dict[str, str] | None = None,
        freshness: TelemetryFreshnessConfig | None = None,
    ) -> None:
        self.app = app
        self.entity_ids = entity_ids or ENTITY_IDS
        self.freshness = freshness or TelemetryFreshnessConfig()
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
        maximum_age_s: float | None = None,
    ) -> ControlTelemetrySnapshot:
        sampled_at = now or datetime.now().astimezone()
        raw = {
            key: self._raw_numeric(key, sampled_at)
            for key in (
                "solax_inverter_power",
                "deye_inverter_power",
                "solax_measured_power",
                "solax_battery_power",
                "deye_battery_power",
                "solax_soc",
                "deye_soc",
                "solax_pv_power",
                "deye_pv_power",
            )
        }
        # maximum_age_s is retained only for callers of the pre-policy API. New
        # Phase 4 code always uses the explicit per-source configuration.
        solax_age = maximum_age_s or self.freshness.solax_fast_power_max_age_s
        deye_age = maximum_age_s or self.freshness.deye_fast_power_max_age_s
        solax_health = self._source_health(
            raw,
            ("solax_inverter_power", "solax_battery_power"),
            self.freshness.solax_source_health_window_s,
            "SolaX",
        )
        deye_health = self._source_health(
            raw,
            ("deye_inverter_power", "deye_battery_power"),
            self.freshness.deye_source_health_window_s,
            "DEYE",
        )
        if parse_bool_state(self.app.get_state(self.entity_ids.get("deye_connection", ""))) is False:
            deye_health = (False, None, "DEYE connection entity explicitly reports disconnected")
        return ControlTelemetrySnapshot(
            sampled_at=sampled_at,
            solax_inverter_power=self._classified_sample(
                "solax_inverter_power", raw["solax_inverter_power"], TelemetryClass.FAST_POWER, solax_age, solax_health
            ),
            deye_inverter_power=self._classified_sample(
                "deye_inverter_power", raw["deye_inverter_power"], TelemetryClass.FAST_POWER, deye_age, deye_health
            ),
            whole_site_grid_power=self._classified_sample(
                "solax_measured_power", raw["solax_measured_power"], TelemetryClass.STABLE_ZERO, solax_age, solax_health
            ),
            solax_battery_power=self._classified_sample(
                "solax_battery_power", raw["solax_battery_power"], TelemetryClass.FAST_POWER, solax_age, solax_health
            ),
            deye_battery_power=self._classified_sample(
                "deye_battery_power", raw["deye_battery_power"], TelemetryClass.FAST_POWER, deye_age, deye_health
            ),
            solax_soc=self._classified_sample(
                "solax_soc",
                raw["solax_soc"],
                TelemetryClass.SLOW_STATE,
                solax_age,
                solax_health,
                minimum=0.0,
                maximum=100.0,
            ),
            deye_soc=self._classified_sample(
                "deye_soc",
                raw["deye_soc"],
                TelemetryClass.SLOW_STATE,
                deye_age,
                deye_health,
                minimum=0.0,
                maximum=100.0,
            ),
            solax_pv_power=self._classified_sample(
                "solax_pv_power",
                raw["solax_pv_power"],
                TelemetryClass.STABLE_ZERO,
                solax_age,
                solax_health,
                minimum=0.0,
            ),
            deye_pv_power=self._classified_sample(
                "deye_pv_power", raw["deye_pv_power"], TelemetryClass.STABLE_ZERO, deye_age, deye_health, minimum=0.0
            ),
            solax_source_healthy=solax_health[0],
            deye_source_healthy=deye_health[0],
            solax_source_health_reason=solax_health[2],
            deye_source_health_reason=deye_health[2],
        )

    def _raw_numeric(self, key: str, now: datetime) -> _RawNumericState:
        raw = self.app.get_state(self.entity_ids[key], attribute="all")
        if raw is None:
            return _RawNumericState(None, None, None, TelemetryQuality.MISSING)
        state = raw.get("state") if isinstance(raw, dict) else raw
        value = parse_float_state(state)
        timestamp = _state_timestamp(raw, now)
        age_s = max((now - timestamp).total_seconds(), 0.0) if timestamp is not None else None
        if value is None:
            return _RawNumericState(None, timestamp, age_s, TelemetryQuality.INVALID)
        if timestamp is None:
            return _RawNumericState(value, None, None, TelemetryQuality.INVALID)
        return _RawNumericState(value, timestamp, age_s, TelemetryQuality.VALID)

    def _source_health(
        self,
        raw: dict[str, _RawNumericState],
        keys: tuple[str, ...],
        window_s: float,
        source: str,
    ) -> tuple[bool, datetime | None, str]:
        candidates = [
            (key, raw[key])
            for key in keys
            if raw[key].quality is TelemetryQuality.VALID
            and raw[key].timestamp is not None
            and raw[key].age_s is not None
            and raw[key].age_s <= window_s
        ]
        if not candidates:
            return False, None, f"{source} source has no valid fast telemetry within {window_s:.0f} s"
        key, newest = min(candidates, key=lambda item: item[1].age_s if item[1].age_s is not None else math.inf)
        return True, newest.timestamp, f"{source} source healthy via {self.entity_ids[key]}"

    def _classified_sample(
        self,
        key: str,
        raw: _RawNumericState,
        telemetry_class: TelemetryClass,
        maximum_age_s: float,
        source_health: tuple[bool, datetime | None, str],
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> NumericTelemetrySample:
        entity_id = self.entity_ids[key]
        source_healthy, health_timestamp, health_reason = source_health
        value_valid = raw.value is not None
        if value_valid and minimum is not None and raw.value < minimum:
            value_valid = False
        if value_valid and maximum is not None and raw.value > maximum:
            value_valid = False
        if raw.quality in (TelemetryQuality.MISSING, TelemetryQuality.INVALID) or not value_valid:
            quality = raw.quality if raw.value is None else TelemetryQuality.INVALID
            reason = f"{entity_id} value is missing or outside its valid range"
            return NumericTelemetrySample(
                raw.value,
                raw.timestamp,
                raw.age_s,
                False,
                quality,
                entity_id,
                value_valid,
                source_healthy,
                reason,
                None,
                telemetry_class,
            )
        if not source_healthy:
            return NumericTelemetrySample(
                raw.value,
                raw.timestamp,
                raw.age_s,
                False,
                TelemetryQuality.STALE,
                entity_id,
                True,
                False,
                health_reason,
                None,
                telemetry_class,
            )
        own_fresh = raw.age_s is not None and raw.age_s <= maximum_age_s
        stable_zero = telemetry_class is TelemetryClass.STABLE_ZERO and raw.value == 0
        slow_state = telemetry_class is TelemetryClass.SLOW_STATE
        effective_fresh = own_fresh or stable_zero or slow_state
        effective_timestamp = raw.timestamp if own_fresh else health_timestamp
        if effective_fresh:
            reason = (
                f"{entity_id} stable zero accepted with healthy source"
                if stable_zero and not own_fresh
                else f"{entity_id} slow state accepted with healthy source"
                if slow_state and not own_fresh
                else f"{entity_id} has fresh state and healthy source"
            )
            quality = TelemetryQuality.VALID
        else:
            reason = f"{entity_id} state age exceeds {maximum_age_s:.0f} s despite healthy source"
            quality = TelemetryQuality.STALE
        return NumericTelemetrySample(
            raw.value,
            raw.timestamp,
            raw.age_s,
            effective_fresh,
            quality,
            entity_id,
            True,
            True,
            reason,
            effective_timestamp,
            telemetry_class,
        )

    def numeric_sample(self, key: str, now: datetime, maximum_age_s: float) -> NumericTelemetrySample:
        entity_id = self.entity_ids[key]
        raw = self._raw_numeric(key, now)
        if raw.quality is TelemetryQuality.MISSING:
            return NumericTelemetrySample(None, None, None, False, TelemetryQuality.MISSING, entity_id, False, False)
        if raw.quality is TelemetryQuality.INVALID:
            return NumericTelemetrySample(
                raw.value, raw.timestamp, raw.age_s, False, TelemetryQuality.INVALID, entity_id, False, False
            )
        value, timestamp, age_s = raw.value, raw.timestamp, raw.age_s
        fresh = age_s is not None and age_s <= maximum_age_s
        quality = TelemetryQuality.VALID if fresh else TelemetryQuality.STALE
        return NumericTelemetrySample(
            value,
            timestamp,
            age_s,
            fresh,
            quality,
            entity_id,
            True,
            fresh,
            "Legacy single-sample freshness check",
            timestamp,
        )


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
