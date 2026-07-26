from __future__ import annotations

from datetime import datetime

from .models import Mode, PlannerDecision


def compact_reasons(reasons: tuple[str, ...], max_len: int = 255) -> str:
    text = "; ".join(reasons)
    return text[:max_len]


def format_decision(decision: PlannerDecision, max_len: int = 255) -> str:
    text = f"{decision.mode.value}: {decision.reason} (confidence={decision.confidence})"
    return text[:max_len]


def format_conflicts(conflicts: tuple[str, ...], max_len: int = 255) -> str:
    return ", ".join(conflicts)[:max_len]


def heartbeat_value() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def mode_value(mode: Mode) -> str:
    return mode.value

