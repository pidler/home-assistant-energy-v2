from __future__ import annotations

from datetime import datetime

from .models import AppStatus, Mode, PlannerDecision


def compact_reasons(reasons: tuple[str, ...], max_len: int = 255) -> str:
    ordered_reasons = tuple(sorted(dict.fromkeys(reasons)))
    text = "; ".join(ordered_reasons)
    if len(text) <= max_len:
        return text

    suffix = f"; +{len(ordered_reasons)} total"
    if len(suffix) >= max_len:
        return text[:max_len]
    return text[: max_len - len(suffix)] + suffix


def format_decision(decision: PlannerDecision, max_len: int = 255) -> str:
    text = f"{decision.mode.value}: {decision.reason} (confidence={decision.confidence})"
    return text[:max_len]


def format_conflicts(conflicts: tuple[str, ...], max_len: int = 255) -> str:
    return ", ".join(conflicts)[:max_len]


def heartbeat_value() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def mode_value(mode: Mode) -> str:
    return mode.value


def app_status_value(status: AppStatus) -> str:
    return status.value
