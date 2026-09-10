"""Pure power allocation and anti-transfer simulation for Phase 4."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite

from .models import BatteryAction, BatteryCommand, BatteryId, CommandStatus, SiteCommand


@dataclass(frozen=True)
class BatteryAvailability:
    battery: BatteryId
    available: bool
    soc_pct: float | None
    min_soc_pct: float = 10.0
    max_soc_pct: float = 100.0
    max_charge_power_w: float = 0.0
    max_discharge_power_w: float = 0.0
    measured_power_w: float | None = None

    def discharge_limit_w(self) -> float:
        if not self.available or self.soc_pct is None or self.soc_pct <= self.min_soc_pct:
            return 0.0
        return max(self.max_discharge_power_w, 0.0)

    def charge_limit_w(self) -> float:
        if not self.available or self.soc_pct is None or self.soc_pct >= self.max_soc_pct:
            return 0.0
        return max(self.max_charge_power_w, 0.0)


@dataclass(frozen=True)
class AllocationParameters:
    operational_export_limit_w: float = 9_800.0
    slew_limit_w_per_tick: float = 2_000.0
    zero_flow_tolerance_w: float = 300.0
    zero_flow_confirmation_s: float = 10.0


@dataclass(frozen=True)
class AllocationResult:
    site_command: SiteCommand
    deye: BatteryCommand
    solax: BatteryCommand
    status: CommandStatus
    requested_grid_target_w: float
    allowed_grid_target_w: float
    required_battery_power_w: float
    saturation_reason: str
    anti_transfer_state: str
    break_before_make_state: str


def validate_battery_command(command: BatteryCommand) -> tuple[str, ...]:
    reasons: list[str] = []
    if not isfinite(command.target_power_w):
        reasons.append("target power must be finite")
    if command.action is BatteryAction.HOLD and command.target_power_w != 0:
        reasons.append("HOLD requires zero target power")
    if command.action is BatteryAction.CHARGE and command.target_power_w <= 0:
        reasons.append("CHARGE requires positive battery power")
    if command.action is BatteryAction.DISCHARGE and command.target_power_w >= 0:
        reasons.append("DISCHARGE requires negative battery power")
    if not 0 <= command.min_soc_pct <= command.max_soc_pct <= 100:
        reasons.append("SOC limits must satisfy 0 <= min <= max <= 100")
    return tuple(reasons)


def validate_anti_transfer(deye: BatteryCommand, solax: BatteryCommand, *, transfer_allowed: bool) -> tuple[str, ...]:
    if transfer_allowed:
        return ()
    if deye.target_power_w < 0 < solax.target_power_w:
        return ("DEYE discharge plus SolaX charge is forbidden",)
    if solax.target_power_w < 0 < deye.target_power_w:
        return ("SolaX discharge plus DEYE charge is forbidden",)
    return ()


@dataclass
class ShadowPowerAllocator:
    parameters: AllocationParameters = field(default_factory=AllocationParameters)
    _last_simulated_power: dict[BatteryId, float] = field(
        default_factory=lambda: {BatteryId.DEYE: 0.0, BatteryId.SOLAX: 0.0}
    )
    _pending_direction: dict[BatteryId, int] = field(default_factory=dict)
    _zero_since: dict[BatteryId, datetime] = field(default_factory=dict)

    def allocate(
        self,
        command: SiteCommand,
        *,
        now: datetime,
        site_load_w: float,
        pv_power_w: float,
        deye: BatteryAvailability,
        solax: BatteryAvailability,
        budget_power_w: float | None = None,
    ) -> AllocationResult:
        if now >= command.expires_at:
            return self._empty(command, CommandStatus.EXPIRED, "Site command expired")
        if not all(isfinite(value) for value in (site_load_w, pv_power_w, command.grid_target_w)):
            return self._empty(command, CommandStatus.FAULT, "Non-finite allocator input")

        export_cap = min(command.max_export_w, self.parameters.operational_export_limit_w)
        if budget_power_w is not None:
            export_cap = min(export_cap, max(budget_power_w, 0.0))
        allowed_grid_target = min(command.grid_target_w, export_cap)
        saturation: list[str] = []
        if allowed_grid_target != command.grid_target_w:
            saturation.append(f"grid target clamped to {allowed_grid_target:.0f} W export budget")

        required_discharge_w = allowed_grid_target + site_load_w - max(pv_power_w, 0.0)
        if required_discharge_w >= 0:
            deye_w, solax_w = self._allocate_discharge(required_discharge_w, command, deye, solax)
            supplied = -deye_w - solax_w
            if supplied + 1e-6 < required_discharge_w:
                saturation.append(f"battery discharge shortfall {required_discharge_w - supplied:.0f} W")
        else:
            charge_required_w = -required_discharge_w
            if allowed_grid_target < 0 and not command.grid_charge_allowed:
                charge_required_w = max(pv_power_w - site_load_w, 0.0)
                saturation.append("grid charging forbidden")
            deye_w, solax_w = self._allocate_charge(charge_required_w, command, deye, solax)
            supplied = deye_w + solax_w
            if supplied + 1e-6 < charge_required_w:
                saturation.append(f"battery charge shortfall {charge_required_w - supplied:.0f} W")

        deye_command = self._battery_command(command, deye, deye_w)
        solax_command = self._battery_command(command, solax, solax_w)
        validation = (*validate_battery_command(deye_command), *validate_battery_command(solax_command))
        anti_transfer = validate_anti_transfer(deye_command, solax_command, transfer_allowed=command.transfer_allowed)
        if validation or anti_transfer:
            return self._empty(command, CommandStatus.BLOCKED, "; ".join((*validation, *anti_transfer)))

        deye_command, deye_break = self._simulate_break_before_make(deye_command, deye.measured_power_w, now)
        solax_command, solax_break = self._simulate_break_before_make(solax_command, solax.measured_power_w, now)
        break_states = tuple(state for state in (deye_break, solax_break) if state)
        status = (
            CommandStatus.BREAK_BEFORE_MAKE
            if break_states
            else CommandStatus.SATURATED
            if saturation
            else CommandStatus.READY
        )
        return AllocationResult(
            command,
            deye_command,
            solax_command,
            status,
            command.grid_target_w,
            allowed_grid_target,
            -required_discharge_w,
            "; ".join(saturation),
            "BLOCKED" if anti_transfer else "CLEAR",
            "; ".join(break_states) or "CLEAR",
        )

    def _allocate_discharge(
        self,
        required_w: float,
        command: SiteCommand,
        deye: BatteryAvailability,
        solax: BatteryAvailability,
    ) -> tuple[float, float]:
        first, second = (deye, solax) if command.preferred_battery is BatteryId.DEYE else (solax, deye)
        first_w = min(required_w, first.discharge_limit_w())
        second_w = min(max(required_w - first_w, 0.0), second.discharge_limit_w())
        values = {first.battery: -first_w, second.battery: -second_w}
        return values[BatteryId.DEYE], values[BatteryId.SOLAX]

    def _allocate_charge(
        self,
        required_w: float,
        command: SiteCommand,
        deye: BatteryAvailability,
        solax: BatteryAvailability,
    ) -> tuple[float, float]:
        first, second = (deye, solax) if command.preferred_battery is BatteryId.DEYE else (solax, deye)
        first_w = min(required_w, first.charge_limit_w())
        second_w = min(max(required_w - first_w, 0.0), second.charge_limit_w())
        values = {first.battery: first_w, second.battery: second_w}
        return values[BatteryId.DEYE], values[BatteryId.SOLAX]

    def _battery_command(self, site: SiteCommand, availability: BatteryAvailability, target_w: float) -> BatteryCommand:
        action = BatteryAction.HOLD
        if target_w > 0:
            action = BatteryAction.CHARGE
        elif target_w < 0:
            action = BatteryAction.DISCHARGE
        return BatteryCommand(
            command_id=f"{site.command_id}:{availability.battery.value}",
            site_command_id=site.command_id,
            battery=availability.battery,
            action=action,
            target_power_w=target_w,
            min_soc_pct=availability.min_soc_pct,
            max_soc_pct=availability.max_soc_pct,
            grid_charge_allowed=site.grid_charge_allowed,
            export_allowed=site.grid_target_w > 0,
            expires_at=site.expires_at,
        )

    def _simulate_break_before_make(
        self, command: BatteryCommand, measured_w: float | None, now: datetime
    ) -> tuple[BatteryCommand, str]:
        desired = _direction(command.target_power_w)
        previous = _direction(self._last_simulated_power[command.battery])
        pending = self._pending_direction.get(command.battery)
        if pending is None and previous and desired and previous != desired:
            self._pending_direction[command.battery] = desired
            self._zero_since.pop(command.battery, None)
            self._last_simulated_power[command.battery] = 0.0
            return replace(command, action=BatteryAction.HOLD, target_power_w=0.0), (
                f"{command.battery.value}:RAMP_TO_ZERO"
            )
        if pending is not None:
            if measured_w is not None and abs(measured_w) <= self.parameters.zero_flow_tolerance_w:
                zero_since = self._zero_since.setdefault(command.battery, now)
                if (now - zero_since).total_seconds() >= self.parameters.zero_flow_confirmation_s:
                    self._pending_direction.pop(command.battery, None)
                    self._zero_since.pop(command.battery, None)
                    self._last_simulated_power[command.battery] = command.target_power_w
                    return command, ""
            else:
                self._zero_since.pop(command.battery, None)
            self._last_simulated_power[command.battery] = 0.0
            return replace(command, action=BatteryAction.HOLD, target_power_w=0.0), (
                f"{command.battery.value}:WAIT_ZERO"
            )
        limited = _slew(
            self._last_simulated_power[command.battery],
            command.target_power_w,
            self.parameters.slew_limit_w_per_tick,
        )
        self._last_simulated_power[command.battery] = limited
        if limited != command.target_power_w:
            action = (
                BatteryAction.CHARGE if limited > 0 else BatteryAction.DISCHARGE if limited < 0 else BatteryAction.HOLD
            )
            return replace(command, action=action, target_power_w=limited), (f"{command.battery.value}:SLEW_LIMITED")
        return command, ""

    def _empty(self, command: SiteCommand, status: CommandStatus, reason: str) -> AllocationResult:
        unavailable = BatteryAvailability(BatteryId.DEYE, False, None)
        deye = self._battery_command(command, unavailable, 0.0)
        solax = self._battery_command(command, replace(unavailable, battery=BatteryId.SOLAX), 0.0)
        return AllocationResult(
            command,
            deye,
            solax,
            status,
            command.grid_target_w,
            0.0,
            0.0,
            reason,
            "CLEAR",
            "CLEAR",
        )


def _direction(power_w: float) -> int:
    return 1 if power_w > 0 else -1 if power_w < 0 else 0


def _slew(previous: float, target: float, limit: float) -> float:
    if limit <= 0 or abs(target - previous) <= limit:
        return target
    return previous + limit if target > previous else previous - limit
