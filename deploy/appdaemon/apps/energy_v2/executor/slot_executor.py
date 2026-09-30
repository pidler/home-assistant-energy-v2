"""Energy-budget slot orchestration layered above the Phase 5B safety engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite

from .enums import ExecutionState, FailureReason, PlannerIntentType
from .haeo_plan import BatteryEnergyBudget, CurrentSlotPlan
from .models import PlannerIntent

_EPSILON_KWH = 0.001


@dataclass(frozen=True)
class EnergyAccount:
    target_kwh: float
    delivered_kwh: float = 0.0
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        if not all(isfinite(value) and value >= 0 for value in (self.target_kwh, self.delivered_kwh, self.elapsed_s)):
            raise ValueError("energy account values must be finite and nonnegative")

    @property
    def remaining_kwh(self) -> float:
        return max(0.0, self.target_kwh - self.delivered_kwh)

    @property
    def complete(self) -> bool:
        return self.remaining_kwh <= _EPSILON_KWH

    @property
    def average_power_w(self) -> float:
        return 0.0 if self.elapsed_s <= 0 else self.delivered_kwh * 3_600_000 / self.elapsed_s

    def integrate(self, matching_power_w: float, elapsed_s: float) -> EnergyAccount:
        if not isfinite(matching_power_w) or not isfinite(elapsed_s) or elapsed_s < 0:
            raise ValueError("power and elapsed time must be finite; elapsed time must be nonnegative")
        moved = max(0.0, matching_power_w) * elapsed_s / 3_600_000
        return EnergyAccount(
            self.target_kwh, min(self.target_kwh, self.delivered_kwh + moved), self.elapsed_s + elapsed_s
        )


@dataclass(frozen=True)
class SlotSafetyStatus:
    plan_fresh: bool = True
    telemetry_fresh: bool = True
    export_limit_clear: bool = True
    soc_floor_clear: bool = True
    cross_transfer_clear: bool = True
    command_readback_ok: bool = True

    def failure(self) -> FailureReason | None:
        checks = (
            (self.plan_fresh, FailureReason.HAEO_PLAN_STALE),
            (self.telemetry_fresh, FailureReason.STALE_TELEMETRY),
            (self.export_limit_clear, FailureReason.PCC_EXPORT_LIMIT),
            (self.soc_floor_clear, FailureReason.INVALID_INTENT),
            (self.cross_transfer_clear, FailureReason.CROSS_BATTERY_FLOW),
            (self.command_readback_ok, FailureReason.READBACK_MISMATCH),
        )
        return next((reason for passed, reason in checks if not passed), None)


@dataclass(frozen=True)
class SlotExecutorDecision:
    state: ExecutionState
    owner: str | None
    intent: PlannerIntent | None
    reason: FailureReason | None


@dataclass
class SlotExecutionSession:
    """Select one owner at a time; existing Phase 5B validates every emitted intent."""

    state: ExecutionState = ExecutionState.IDLE
    plan: CurrentSlotPlan | None = None
    owner: str | None = None
    accounts: dict[str, EnergyAccount] = field(default_factory=dict)
    last_sample_at: datetime | None = None
    rollback_reason: FailureReason | None = None

    def load_plan(self, plan: CurrentSlotPlan, now: datetime) -> None:
        self._aware(now)
        if not plan.slot_start <= now.astimezone(UTC) < plan.slot_end:
            raise ValueError("current time must be inside the HAEO slot")
        self.plan = plan
        self.owner = None
        self.accounts = {battery: EnergyAccount(budget.magnitude_kwh) for battery, budget in plan.budgets.items()}
        self.last_sample_at = now.astimezone(UTC)
        self.rollback_reason = None
        self.state = ExecutionState.PREPARE

    def record_power(self, now: datetime, *, solax_power_w: float, deye_power_w: float) -> None:
        self._aware(now)
        if not all(isfinite(value) for value in (solax_power_w, deye_power_w)):
            raise ValueError("battery powers must be finite")
        now = now.astimezone(UTC)
        if self.last_sample_at is None:
            self.last_sample_at = now
            return
        elapsed_s = (now - self.last_sample_at).total_seconds()
        if elapsed_s < 0:
            raise ValueError("power observations must be chronological")
        self.last_sample_at = now
        if self.owner is None or self.plan is None:
            return
        budget = self.plan.budgets[self.owner]
        measured = deye_power_w if self.owner == "DEYE" else solax_power_w
        matching_power = measured * budget.direction
        self.accounts[self.owner] = self.accounts[self.owner].integrate(matching_power, elapsed_s)
        if self.accounts[self.owner].complete:
            self.owner = None
            self.state = ExecutionState.PREPARE

    def decide(self, now: datetime, safety: SlotSafetyStatus) -> SlotExecutorDecision:
        self._aware(now)
        now = now.astimezone(UTC)
        if self.plan is None:
            return SlotExecutorDecision(ExecutionState.IDLE, None, None, None)
        if now >= self.plan.slot_end:
            self.owner = None
            self.state = ExecutionState.COMPLETE
            return SlotExecutorDecision(self.state, None, None, FailureReason.ENERGY_BUDGET_COMPLETE)
        failure = safety.failure()
        if failure is not None:
            return self.request_rollback(failure)
        active = [
            budget
            for battery, budget in self.plan.budgets.items()
            if not self.accounts[battery].complete and budget.direction != 0
        ]
        directions = {budget.direction for budget in active}
        if len(directions) > 1:
            self.owner = None
            self.state = ExecutionState.HOLD
            return SlotExecutorDecision(self.state, None, None, FailureReason.CROSS_BATTERY_FLOW)
        if not active:
            self.owner = None
            self.state = ExecutionState.COMPLETE
            return SlotExecutorDecision(self.state, None, None, FailureReason.ENERGY_BUDGET_COMPLETE)
        if self.owner is None:
            self.owner = self._select_owner(active)
        budget = self.plan.budgets[self.owner]
        self.state = ExecutionState.EXECUTE_DEYE if self.owner == "DEYE" else ExecutionState.EXECUTE_SOLAX
        intent = self._intent(budget, now)
        return SlotExecutorDecision(self.state, self.owner, intent, None)

    def request_rollback(self, reason: FailureReason) -> SlotExecutorDecision:
        self.owner = None
        self.rollback_reason = reason
        self.state = ExecutionState.ROLLBACK
        return SlotExecutorDecision(self.state, None, None, reason)

    def diagnostics(self) -> dict[str, object]:
        plan = self.plan
        values: dict[str, object] = {
            "state": self.state.value,
            "owner": self.owner or "NONE",
            "plan_id": plan.plan_id if plan else None,
            "rollback_reason": self.rollback_reason.value if self.rollback_reason else "",
        }
        for battery in ("DEYE", "SOLAX"):
            budget = plan.budgets[battery] if plan else None
            account = self.accounts.get(battery, EnergyAccount(0.0))
            prefix = battery.lower()
            values.update(
                {
                    f"{prefix}_target_kw": budget.target_power_kw if budget else 0.0,
                    f"{prefix}_target_kwh": account.target_kwh,
                    f"{prefix}_delivered_kwh": account.delivered_kwh,
                    f"{prefix}_remaining_kwh": account.remaining_kwh,
                    f"{prefix}_average_power_w": account.average_power_w,
                }
            )
        return values

    @staticmethod
    def _select_owner(active: list[BatteryEnergyBudget]) -> str:
        # Highest HAEO economic priority wins. DEYE is only the equal-value tie-breaker.
        selected = sorted(active, key=lambda item: (-item.economic_priority, item.battery != "DEYE"))[0]
        return selected.battery

    def _intent(self, budget: BatteryEnergyBudget, now: datetime) -> PlannerIntent:
        assert self.plan is not None
        charging = budget.direction < 0
        intent_type = {
            ("DEYE", False): PlannerIntentType.DISCHARGE_DEYE,
            ("DEYE", True): PlannerIntentType.CHARGE_DEYE,
            ("SOLAX", False): PlannerIntentType.DISCHARGE_SOLAX,
            ("SOLAX", True): PlannerIntentType.CHARGE_SOLAX,
        }[(budget.battery, charging)]
        return PlannerIntent(
            plan_id=self.plan.plan_id,
            intent_id=f"{self.plan.plan_id}:{self.plan.slot_start.isoformat()}:{budget.battery}",
            slot_start=self.plan.slot_start,
            slot_end=self.plan.slot_end,
            intent_type=intent_type,
            target_w=abs(budget.target_power_kw) * 1000,
            deadline=self.plan.slot_end,
            hypothetical=False,
            created_at=now,
            reason_code="HAEO_ENERGY_BUDGET",
            reason=f"execute remaining {self.accounts[budget.battery].remaining_kwh:.6f} kWh",
            confidence="HAEO",
        )

    @staticmethod
    def _aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
