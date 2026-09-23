"""Explicit conversion boundary from planner-shaped data to executor intents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Protocol

from .enums import FailureReason, PlannerIntentType
from .models import PlannerIntent

SLOT_DURATION = timedelta(minutes=15)


class BatterySlotLike(Protocol):
    charge_from_pv_kwh: float
    discharge_to_load_kwh: float
    discharge_to_export_kwh: float
    planned_power_w: float


class PlannerSlotLike(Protocol):
    timestamp: datetime
    battery_plans: Mapping[str, BatterySlotLike]
    reason_code: str
    reason: str


@dataclass(frozen=True)
class IntentConversion:
    intent: PlannerIntent | None
    reasons: tuple[FailureReason, ...] = ()


def convert_slot_to_intent(
    slot: PlannerSlotLike,
    *,
    plan_id: str,
    created_at: datetime,
    confidence: str,
    hypothetical: bool,
    deadline: datetime | None = None,
    epsilon_kwh: float = 1e-9,
) -> IntentConversion:
    """Convert semantic energy fields; power sign never selects direction."""
    if not plan_id or not isfinite(epsilon_kwh) or epsilon_kwh < 0:
        return IntentConversion(None, (FailureReason.UNSUPPORTED_INTENT,))
    start = slot.timestamp
    if start.tzinfo is None or start.utcoffset() is None:
        return IntentConversion(None, (FailureReason.MISSING_TELEMETRY,))
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        return IntentConversion(None, (FailureReason.MISSING_TELEMETRY,))
    end = start + SLOT_DURATION
    due = deadline or end
    active: list[tuple[str, PlannerIntentType, float]] = []
    for battery, plan in slot.battery_plans.items():
        components = (
            plan.charge_from_pv_kwh,
            plan.discharge_to_load_kwh,
            plan.discharge_to_export_kwh,
        )
        if not all(isfinite(value) and value >= 0 for value in components):
            return IntentConversion(None, (FailureReason.TELEMETRY_INCOHERENT,))
        if not isfinite(plan.planned_power_w):
            return IntentConversion(None, (FailureReason.TELEMETRY_INCOHERENT,))
        charge = plan.charge_from_pv_kwh
        discharge = plan.discharge_to_load_kwh + plan.discharge_to_export_kwh
        power = abs(plan.planned_power_w)
        if charge > epsilon_kwh and discharge > epsilon_kwh:
            return IntentConversion(None, (FailureReason.AMBIGUOUS_PLANNER_SLOT,))
        if charge > epsilon_kwh:
            active.append((battery, _intent_type(battery, True), power))
        elif discharge > epsilon_kwh:
            active.append((battery, _intent_type(battery, False), power))
    if len(active) > 1:
        return IntentConversion(None, (FailureReason.AMBIGUOUS_PLANNER_SLOT,))
    if not active:
        kind, target = PlannerIntentType.NORMAL, None
    else:
        battery, kind, target = active[0]
        if kind is None or target <= 0:
            return IntentConversion(None, (FailureReason.UNSUPPORTED_INTENT,))
    try:
        return IntentConversion(
            PlannerIntent(
                plan_id=plan_id,
                intent_id=f"{plan_id}:{start.astimezone(timezone.utc).isoformat()}:{kind.value}",  # noqa: UP017
                slot_start=start,
                slot_end=end,
                intent_type=kind,
                target_w=target,
                deadline=due,
                hypothetical=hypothetical,
                created_at=created_at,
                reason_code=slot.reason_code,
                reason=slot.reason,
                confidence=confidence,
            )
        )
    except ValueError:
        return IntentConversion(None, (FailureReason.INVALID_INTENT,))


def validate_planner_intent(intent: PlannerIntent, now: datetime) -> tuple[FailureReason, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        return (FailureReason.MISSING_TELEMETRY,)
    if now >= intent.deadline:
        return (FailureReason.COMMAND_EXPIRED,)
    return ()


def _intent_type(battery: str, charging: bool) -> PlannerIntentType | None:
    if not isinstance(battery, str):
        return None
    prefix = battery.upper()
    if prefix == "SOLAX":
        return PlannerIntentType.CHARGE_SOLAX if charging else PlannerIntentType.DISCHARGE_SOLAX
    if prefix == "DEYE":
        return PlannerIntentType.CHARGE_DEYE if charging else PlannerIntentType.DISCHARGE_DEYE
    return None
