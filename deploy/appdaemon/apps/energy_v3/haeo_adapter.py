"""Read-only conversion of HAEO forecast entities into a V3 current target."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any

from .models import CurrentTarget

OPTIMIZER_STATUS = "sensor.optimizer_status"
OPTIMIZER_HORIZON = "sensor.optimizer_forecast_horizon"
SOLAX_ACTIVE_POWER = "sensor.battery_solax_active_power"
DEYE_ACTIVE_POWER = "sensor.battery_deye_active_power"
HAEO_ENTITY_IDS = (OPTIMIZER_STATUS, OPTIMIZER_HORIZON, SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER)
EXPECTED_PERIODS = 192


class HaeoError(StrEnum):
    STATUS_UNAVAILABLE = "STATUS_UNAVAILABLE"
    OPTIMIZER_NOT_SUCCESSFUL = "OPTIMIZER_NOT_SUCCESSFUL"
    LAST_RUN_INVALID = "LAST_RUN_INVALID"
    OPTIMIZER_STALE = "OPTIMIZER_STALE"
    FORECAST_MISSING = "FORECAST_MISSING"
    FORECAST_MALFORMED = "FORECAST_MALFORMED"
    FORECAST_UNALIGNED = "FORECAST_UNALIGNED"
    PUBLICATION_UNCERTAIN = "PUBLICATION_UNCERTAIN"
    INTERVAL_NOT_CURRENT = "INTERVAL_NOT_CURRENT"


@dataclass(frozen=True)
class HaeoTargetResult:
    target: CurrentTarget | None
    error: HaeoError | None
    optimizer_status: str
    optimizer_last_run: datetime | None

    @property
    def valid(self) -> bool:
        return self.target is not None and self.error is None


def current_target_from_states(
    states: Mapping[str, Mapping[str, Any] | None],
    *,
    now: datetime,
    max_optimizer_age: timedelta = timedelta(minutes=30),
    interval: timedelta = timedelta(minutes=15),
    publication_tolerance: timedelta = timedelta(seconds=5),
    comparison_states: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> HaeoTargetResult:
    """Build the target for ``now`` from one coherent HAEO forecast publication."""

    if not _aware(now):
        raise ValueError("now must be timezone-aware")
    status_record = states.get(OPTIMIZER_STATUS)
    if not status_record:
        return HaeoTargetResult(None, HaeoError.STATUS_UNAVAILABLE, "unavailable", None)
    status = str(status_record.get("state", "unavailable"))
    if status != "success":
        return HaeoTargetResult(None, HaeoError.OPTIMIZER_NOT_SUCCESSFUL, status, None)

    last_run = _parse_timestamp(_attributes(status_record).get("last_run"))
    if last_run is None:
        return HaeoTargetResult(None, HaeoError.LAST_RUN_INVALID, status, None)
    age = now - last_run
    if age < -timedelta(seconds=5) or age > max_optimizer_age:
        return HaeoTargetResult(None, HaeoError.OPTIMIZER_STALE, status, last_run)

    if comparison_states is not None and _publication_signature(states) != _publication_signature(comparison_states):
        return HaeoTargetResult(None, HaeoError.PUBLICATION_UNCERTAIN, status, last_run)

    horizon = _forecast(states.get(OPTIMIZER_HORIZON))
    solax = _forecast(states.get(SOLAX_ACTIVE_POWER))
    deye = _forecast(states.get(DEYE_ACTIVE_POWER))
    if horizon is None or solax is None or deye is None:
        return HaeoTargetResult(None, HaeoError.FORECAST_MISSING, status, last_run)
    publication_times = {
        entity_id: _record_timestamp(states.get(entity_id), "last_reported")
        for entity_id in (OPTIMIZER_STATUS, SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER)
    }
    if any(value is None for value in publication_times.values()):
        return HaeoTargetResult(None, HaeoError.PUBLICATION_UNCERTAIN, status, last_run)
    typed_publication_times = {key: value for key, value in publication_times.items() if value is not None}
    if any(value - now > timedelta(seconds=5) for value in typed_publication_times.values()):
        return HaeoTargetResult(None, HaeoError.PUBLICATION_UNCERTAIN, status, last_run)
    status_publication = typed_publication_times[OPTIMIZER_STATUS]
    if status_publication < last_run or status_publication - last_run > publication_tolerance:
        return HaeoTargetResult(None, HaeoError.PUBLICATION_UNCERTAIN, status, last_run)
    if len(horizon) != EXPECTED_PERIODS + 1 or len(solax) != EXPECTED_PERIODS or len(deye) != EXPECTED_PERIODS:
        return HaeoTargetResult(None, HaeoError.FORECAST_MALFORMED, status, last_run)

    boundaries = [_parse_timestamp(item.get("time")) for item in horizon]
    solax_times = [_parse_timestamp(item.get("time")) for item in solax]
    deye_times = [_parse_timestamp(item.get("time")) for item in deye]
    if any(value is None for value in (*boundaries, *solax_times, *deye_times)):
        return HaeoTargetResult(None, HaeoError.FORECAST_MALFORMED, status, last_run)
    typed_boundaries = [value for value in boundaries if value is not None]
    typed_solax_times = [value for value in solax_times if value is not None]
    typed_deye_times = [value for value in deye_times if value is not None]
    expected_step = interval.total_seconds()
    steps = [
        (right - left).total_seconds() for left, right in zip(typed_boundaries, typed_boundaries[1:], strict=False)
    ]
    if (
        any(step != expected_step for step in steps)
        or typed_solax_times != typed_boundaries[:-1]
        or typed_deye_times != typed_boundaries[:-1]
    ):
        return HaeoTargetResult(None, HaeoError.FORECAST_UNALIGNED, status, last_run)

    fresh_publications = [status_publication]
    for entity_id in (SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER):
        publication = typed_publication_times[entity_id]
        if last_run <= publication <= last_run + publication_tolerance:
            fresh_publications.append(publication)
            continue
        # AppDaemon updates its cache from state_changed events. When HAEO
        # republishes an identical forecast, Home Assistant advances
        # last_reported without emitting a state_changed event, so AppDaemon
        # retains the boundary publication in both metadata fields. The
        # complete, aligned payload is safe only for this bounded cache case.
        last_updated = _record_timestamp(states.get(entity_id), "last_updated")
        cache_age = last_run - publication
        if last_updated != publication or cache_age < timedelta(0) or cache_age > interval + publication_tolerance:
            return HaeoTargetResult(None, HaeoError.PUBLICATION_UNCERTAIN, status, last_run)
    if max(fresh_publications) - min(fresh_publications) > publication_tolerance:
        return HaeoTargetResult(None, HaeoError.PUBLICATION_UNCERTAIN, status, last_run)

    active_index = next(
        (
            index
            for index, (start, end) in enumerate(zip(typed_boundaries, typed_boundaries[1:], strict=False))
            if start <= now < end
        ),
        None,
    )
    if active_index is None:
        return HaeoTargetResult(None, HaeoError.INTERVAL_NOT_CURRENT, status, last_run)
    solax_kw = _finite_number(solax[active_index].get("value"))
    deye_kw = _finite_number(deye[active_index].get("value"))
    if solax_kw is None or deye_kw is None:
        return HaeoTargetResult(None, HaeoError.FORECAST_MALFORMED, status, last_run)
    return HaeoTargetResult(
        CurrentTarget(
            timestamp=typed_boundaries[active_index],
            valid_until=typed_boundaries[active_index + 1],
            solax_target_w=solax_kw * 1000.0,
            deye_target_w=deye_kw * 1000.0,
        ),
        None,
        status,
        last_run,
    )


def _attributes(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("attributes", {})
    return value if isinstance(value, Mapping) else {}


def _forecast(record: Mapping[str, Any] | None) -> list[Mapping[str, Any]] | None:
    if not record:
        return None
    value = _attributes(record).get("forecast")
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return None
    return value


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if _aware(parsed) else None


def _record_timestamp(record: Mapping[str, Any] | None, key: str) -> datetime | None:
    return _parse_timestamp(record.get(key)) if record else None


def _publication_signature(states: Mapping[str, Mapping[str, Any] | None]) -> tuple[object, ...]:
    """Return the semantic HAEO publication identity without inventing a plan ID.

    Mapping key order is not part of the HAEO contract. Comparing
    ``repr(forecast)`` made that irrelevant representation detail part of the
    identity. Keep the publication metadata, but compare only the ordered HAEO
    contract fields.
    """

    signature: list[object] = []
    for entity_id in HAEO_ENTITY_IDS:
        record = states.get(entity_id)
        signature.extend(
            (
                entity_id,
                record.get("state") if record else None,
                record.get("last_reported") if record else None,
                _attributes(record).get("last_run") if record else None,
                _forecast_signature(entity_id, record),
            )
        )
    return tuple(signature)


def _forecast_signature(entity_id: str, record: Mapping[str, Any] | None) -> object:
    forecast = _forecast(record)
    if forecast is None:
        return None
    if entity_id == OPTIMIZER_HORIZON:
        return tuple(item.get("time") for item in forecast)
    if entity_id in (SOLAX_ACTIVE_POWER, DEYE_ACTIVE_POWER):
        return tuple((item.get("time"), item.get("value")) for item in forecast)
    return None


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value):
        return None
    return float(value)
