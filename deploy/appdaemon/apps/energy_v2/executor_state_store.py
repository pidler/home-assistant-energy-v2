"""Bounded, atomic persistence for the shadow executor runtime only."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
MAX_PERSISTED_BYTES = 4096
MAX_TEXT = 160


@dataclass(frozen=True)
class StoredShadowState:
    execution_epoch: int
    last_state: str
    active_plan_id: str | None = None
    active_intent_id: str | None = None
    rehydration_reason: str = ""
    last_evaluation_at: str | None = None

    def __post_init__(self) -> None:
        if self.execution_epoch < 0 or not isinstance(self.last_state, str) or not self.last_state:
            raise ValueError("stored state requires epoch and state")
        for name in ("last_state", "active_plan_id", "active_intent_id", "rehydration_reason"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT):
                raise ValueError(f"{name} must be bounded text")
        if self.last_evaluation_at is not None:
            if not isinstance(self.last_evaluation_at, str) or len(self.last_evaluation_at) > MAX_TEXT:
                raise ValueError("last_evaluation_at must be bounded text")
            parsed = datetime.fromisoformat(self.last_evaluation_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("last_evaluation_at must be timezone-aware")


class ExecutorStateStore:
    """JSON store which never restores transition or command authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> StoredShadowState | None:
        if not self.path.exists():
            return None
        try:
            payload = self.path.read_bytes()
            if len(payload) > MAX_PERSISTED_BYTES:
                raise ValueError("persistence payload is too large")
            raw = json.loads(payload.decode("utf-8"))
            if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported persistence schema")
            return StoredShadowState(
                execution_epoch=_epoch(raw.get("execution_epoch")),
                last_state=_required_text(raw.get("last_state"), "last_state"),
                active_plan_id=_text(raw.get("active_plan_id")),
                active_intent_id=_text(raw.get("active_intent_id")),
                rehydration_reason=_required_text(raw.get("rehydration_reason", ""), "rehydration_reason"),
                last_evaluation_at=_text(raw.get("last_evaluation_at")),
            )
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid shadow persistence: {error}") from error

    def save(self, state: StoredShadowState) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "execution_epoch": state.execution_epoch,
            "last_state": state.last_state,
            "active_plan_id": state.active_plan_id,
            "active_intent_id": state.active_intent_id,
            "rehydration_reason": state.rehydration_reason,
            "last_evaluation_at": state.last_evaluation_at,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PERSISTED_BYTES:
            raise ValueError("persistence payload is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded.decode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise ValueError("optional text must be nonempty and bounded")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT or (name == "last_state" and not value):
        raise ValueError(f"{name} must be bounded text")
    return value


def _epoch(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("execution_epoch must be a nonnegative integer")
    return value
