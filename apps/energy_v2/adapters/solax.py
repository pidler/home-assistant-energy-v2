"""SolaX diagnostic translation with no trigger or service-call path."""

from __future__ import annotations

from dataclasses import dataclass

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
        mode = "Grid Control" if self.strategy is SolaxShadowStrategy.GRID_TRIM else "Battery Control"
        settings = (
            ("strategy", self.strategy.value),
            ("remote_control_mode", mode),
            ("simulated_active_power_w", f"{allowed:.0f}"),
            ("whole_site_grid_error_w", f"{grid_error_w:.0f}"),
            ("trigger", "NOT_CALLED"),
        )
        return CommandResult(
            command.command_id,
            BatteryId.SOLAX,
            CommandStatus.SATURATED if saturated else CommandStatus.READY,
            requested,
            allowed,
            allowed,
            measured_actual_power_w,
            saturated,
            matches,
            "GRID_TRIM uses whole-site grid feedback; translation is diagnostic only",
            settings,
        )
