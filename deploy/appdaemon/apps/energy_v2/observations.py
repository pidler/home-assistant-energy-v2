"""Timestamp parsing for cached HA state: reading a cache is not an observation."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_timestamp(value: object, now: datetime) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed > now:
            return None
        return parsed
    except (ValueError, TypeError, OverflowError):
        return None


def change_time(raw: object, now: datetime) -> datetime | None:
    if not isinstance(raw, dict):
        return None
    return parse_timestamp(raw.get("last_changed") or raw.get("last_updated"), now)


def transition_time(raw: object, now: datetime) -> datetime | None:
    """Actual state-change evidence; metadata/report updates are not transitions."""
    if not isinstance(raw, dict):
        return None
    return parse_timestamp(raw.get("last_changed"), now)


def observation_time(raw: object, now: datetime) -> datetime | None:
    if not isinstance(raw, dict):
        return None
    # A malformed explicit report must not be hidden by a different timestamp.
    key = next((key for key in ("last_reported", "last_updated", "last_changed") if key in raw), None)
    reported = parse_timestamp(raw.get(key), now) if key else None
    changed = change_time(raw, now)
    if reported is not None and changed is not None and reported < changed:
        return None
    return reported


def communication_time(raw: object, now: datetime) -> datetime | None:
    """Honor an explicit connection receipt timestamp as well as HA observation time."""
    observed = observation_time(raw, now)
    if not isinstance(raw, dict) or observed is None:
        return None
    attributes = raw.get("attributes", {})
    if not isinstance(attributes, dict) or "timestamp" not in attributes:
        return observed
    try:
        received = datetime.fromtimestamp(float(attributes["timestamp"]), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return min(observed, received) if received <= now else None
