"""SolaX diagnostic translation with no trigger or service-call path."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..models import BatteryCommand, BatteryId, CommandResult, CommandStatus, StrEnum


class SolaxShadowStrategy(StrEnum):
    BATTERY_RESIDUAL = "BATTERY_RESIDUAL"
    GRID_TRIM = "GRID_TRIM"


@dataclass(frozen=True)
class SolaxShadowCapabilities:
    max_charge_power_w: float = 12_000.0
    max_discharge_power_w: float = 12_000.0


class SolaxShadowAdapter:
    """Describe a future SolaX command without executing Remote Control."""

    def __init__(
        self,
        strategy: SolaxShadowStrategy = SolaxShadowStrategy.GRID_TRIM,
        capabilities: SolaxShadowCapabilities | None = None,
    ) -> None:
        self.strategy = strategy
        self.capabilities = capabilities or SolaxShadowCapabilities()

    def translate(
        self,
        command: BatteryCommand,
        measured_actual_power_w: float | None,
        *,
        grid_error_w: float = 0.0,
        grid_target_w: float | None = None,
    ) -> CommandResult:
        requested = command.target_power_w
        limit = self.capabilities.max_charge_power_w if requested > 0 else self.capabilities.max_discharge_power_w
        allowed = max(min(requested, limit), -limit)
        saturated = allowed != requested
        matches = (
            None
            if measured_actual_power_w is None
            else abs(measured_actual_power_w - allowed) <= max(abs(allowed) * 0.1, 300.0)
        )
        if self.strategy is SolaxShadowStrategy.GRID_TRIM:
            verified_target = grid_target_w is not None and isfinite(grid_target_w)
            settings_list = [
                ("strategy", self.strategy.value),
                ("remote_control_mode", "Grid Control"),
            ]
            if verified_target:
                settings_list.append(("whole_site_grid_target_w", f"{grid_target_w:.0f}"))
            settings_list.extend(
                (
                    ("whole_site_grid_error_w", f"{grid_error_w:.0f}"),
                    ("grid_target_status", "MODELLED" if verified_target else "UNVERIFIED"),
                    ("trigger", "NOT_CALLED"),
                )
            )
            settings = tuple(settings_list)
            reason = "GRID_TRIM uses an explicit whole-site grid target; translation is diagnostic only"
            if not verified_target:
                reason = "GRID_TRIM whole-site grid target is unavailable; no active-power value is proposed"
        else:
            settings = (
                ("strategy", self.strategy.value),
                ("remote_control_mode", "Battery Control"),
                ("simulated_battery_power_w", f"{allowed:.0f}"),
                ("trigger", "NOT_CALLED"),
            )
            reason = "BATTERY_RESIDUAL keeps the battery-power target distinct from whole-site grid control"
        status = CommandStatus.SATURATED if saturated else CommandStatus.READY
        if measured_actual_power_w is None or (self.strategy is SolaxShadowStrategy.GRID_TRIM and not verified_target):
            status = CommandStatus.UNVERIFIED
        return CommandResult(
            command.command_id,
            BatteryId.SOLAX,
            status,
            requested,
            allowed,
            allowed,
            measured_actual_power_w,
            saturated,
            matches,
            reason,
            settings,
        )
