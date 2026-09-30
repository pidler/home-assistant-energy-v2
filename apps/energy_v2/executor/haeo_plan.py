"""Strict HAEO-to-executor contract for one current 15-minute slot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from types import MappingProxyType

from .enums import FailureReason

SLOT_DURATION = timedelta(minutes=15)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if isfinite(result) else None


@dataclass(frozen=True)
class BatteryEnergyBudget:
    """Signed HAEO target and remaining-slot energy budget.

    Positive values discharge the battery; negative values charge it.
    Delivered energy is tracked separately as a positive magnitude.
    """

    battery: str
    target_power_kw: float
    target_energy_kwh: float
    economic_priority: float = 0.0

    def __post_init__(self) -> None:
        if self.battery not in {"DEYE", "SOLAX"}:
            raise ValueError("battery must be DEYE or SOLAX")
        if not all(isfinite(value) for value in (self.target_power_kw, self.target_energy_kwh, self.economic_priority)):
            raise ValueError("budget values must be finite")
        if self.target_power_kw * self.target_energy_kwh < 0:
            raise ValueError("power and energy direction must agree")

    @property
    def magnitude_kwh(self) -> float:
        return abs(self.target_energy_kwh)

    @property
    def direction(self) -> int:
        return 1 if self.target_energy_kwh > 0 else -1 if self.target_energy_kwh < 0 else 0


@dataclass(frozen=True)
class CurrentSlotPlan:
    plan_id: str
    generated_at: datetime
    slot_start: datetime
    slot_end: datetime
    budgets: Mapping[str, BatteryEnergyBudget]
    degraded_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.plan_id or self.slot_end <= self.slot_start:
            raise ValueError("valid plan identity and slot bounds are required")
        for value in (self.generated_at, self.slot_start, self.slot_end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("plan timestamps must be timezone-aware")
        if set(self.budgets) != {"DEYE", "SOLAX"}:
            raise ValueError("both battery budgets are required")
        if not all(isinstance(value, BatteryEnergyBudget) for value in self.budgets.values()):
            raise ValueError("budgets must contain BatteryEnergyBudget values")
        object.__setattr__(self, "budgets", MappingProxyType(dict(self.budgets)))


@dataclass(frozen=True)
class PlanAdaptation:
    slot: CurrentSlotPlan | None
    reasons: tuple[FailureReason, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class HaeoPlanAdapter:
    max_plan_age_s: float = 300.0
    slot_duration: timedelta = SLOT_DURATION

    def __post_init__(self) -> None:
        if not isfinite(self.max_plan_age_s) or self.max_plan_age_s <= 0:
            raise ValueError("max_plan_age_s must be finite and positive")
        if self.slot_duration.total_seconds() <= 0:
            raise ValueError("slot_duration must be positive")

    def adapt(self, raw: object, now: datetime) -> PlanAdaptation:
        if now.tzinfo is None or now.utcoffset() is None:
            return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "now is timezone-naive")
        now = now.astimezone(UTC)
        if not isinstance(raw, Mapping):
            return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "payload is not a mapping")
        plan_id = raw.get("plan_id")
        generated_at = _timestamp(raw.get("generated_at"))
        slots = raw.get("slots")
        if not isinstance(plan_id, str) or not plan_id or generated_at is None or not isinstance(slots, Sequence):
            return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "identity, timestamp, or slots missing")
        age_s = (now - generated_at).total_seconds()
        if age_s < 0 or age_s > self.max_plan_age_s:
            return PlanAdaptation(None, (FailureReason.HAEO_PLAN_STALE,), f"plan age {age_s:.1f}s")
        current: Mapping[str, object] | None = None
        current_start: datetime | None = None
        for candidate in slots:
            if not isinstance(candidate, Mapping):
                return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "slot is not a mapping")
            start = _timestamp(candidate.get("timestamp"))
            if start is None:
                return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "slot timestamp invalid")
            if start <= now < start + self.slot_duration:
                if current is not None:
                    return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "overlapping current slots")
                current, current_start = candidate, start
        if current is None or current_start is None:
            return PlanAdaptation(None, (FailureReason.HAEO_SLOT_MISSING,), "no current HAEO slot")
        remaining_fraction = (
            current_start + self.slot_duration - now
        ).total_seconds() / self.slot_duration.total_seconds()
        budgets: dict[str, BatteryEnergyBudget] = {}
        for battery, prefix in (("DEYE", "deye"), ("SOLAX", "solax")):
            power_kw = _number(current.get(f"{prefix}_target_kw"))
            if power_kw is None:
                return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), f"{prefix}_target_kw invalid")
            full_energy = _number(current.get(f"{prefix}_energy_kwh"))
            if full_energy is None:
                remaining_energy = power_kw * (current_start + self.slot_duration - now).total_seconds() / 3600
            else:
                if power_kw * full_energy < 0:
                    return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), f"{prefix} direction mismatch")
                remaining_energy = full_energy * remaining_fraction
            if power_kw == 0 and abs(remaining_energy) > 1e-9:
                return PlanAdaptation(
                    None,
                    (FailureReason.HAEO_PLAN_MALFORMED,),
                    f"{prefix} nonzero energy has zero power target",
                )
            priority = _number(current.get(f"{prefix}_economic_priority"))
            budgets[battery] = BatteryEnergyBudget(battery, power_kw, remaining_energy, priority or 0.0)
        degraded = raw.get("degraded_inputs", ())
        if (
            not isinstance(degraded, Sequence)
            or isinstance(degraded, str)
            or not all(isinstance(item, str) for item in degraded)
        ):
            return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), "degraded_inputs invalid")
        try:
            return PlanAdaptation(
                CurrentSlotPlan(
                    plan_id,
                    generated_at,
                    current_start,
                    current_start + self.slot_duration,
                    budgets,
                    tuple(degraded),
                )
            )
        except ValueError as error:
            return PlanAdaptation(None, (FailureReason.HAEO_PLAN_MALFORMED,), str(error))
