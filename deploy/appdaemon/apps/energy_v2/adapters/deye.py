"""DEYE diagnostic translation with no physical write capability."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import BatteryAction, BatteryCommand, BatteryId, CommandResult, CommandStatus


@dataclass(frozen=True)
class DeyeShadowCapabilities:
    max_charge_power_w: float = 10_000.0
    max_discharge_power_w: float = 10_000.0
    max_sell_power_w: float = 9_800.0
    max_discharge_current_a: float = 184.0


class DeyeShadowAdapter:
    """Translate an abstract command into proposed DEYE settings only."""

    def __init__(self, capabilities: DeyeShadowCapabilities | None = None) -> None:
        self.capabilities = capabilities or DeyeShadowCapabilities()

    def translate(self, command: BatteryCommand, measured_actual_power_w: float | None) -> CommandResult:
        requested = command.target_power_w
        limit = self.capabilities.max_charge_power_w if requested > 0 else self.capabilities.max_discharge_power_w
        allowed = max(min(requested, limit), -limit)
        saturated = allowed != requested
        settings = self._proposed_settings(command, allowed)
        matches = (
            None
            if measured_actual_power_w is None
            else abs(measured_actual_power_w - allowed) <= max(abs(allowed) * 0.1, 300.0)
        )
        if command.action is BatteryAction.HOLD:
            status = CommandStatus.UNVERIFIED
            reason = "DEYE HOLD strategy is unverified; no physical work mode or register value is proposed"
        elif measured_actual_power_w is None:
            status = CommandStatus.UNVERIFIED
            reason = (
                "Battery-power feedback is unavailable; TOU power is an allowed ceiling, "
                "not assumed exact, and the proposal cannot be verified"
            )
        else:
            status = CommandStatus.SATURATED if saturated else CommandStatus.READY
            reason = "TOU power is an allowed ceiling, not assumed exact battery power"
        return CommandResult(
            command.command_id,
            BatteryId.DEYE,
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

    def _proposed_settings(self, command: BatteryCommand, allowed_w: float) -> tuple[tuple[str, str], ...]:
        if command.action is BatteryAction.DISCHARGE:
            return (
                ("work_mode", "Export First"),
                ("time_of_use", "Enabled"),
                ("tou_power_w", f"{abs(allowed_w):.0f}"),
                ("tou_soc_pct", f"{command.min_soc_pct:.1f}"),
                ("max_sell_power_w", f"{self.capabilities.max_sell_power_w:.0f}"),
                ("max_discharge_power_w", f"{self.capabilities.max_discharge_power_w:.0f}"),
                ("max_discharge_current_a", f"{self.capabilities.max_discharge_current_a:.0f}"),
            )
        if command.action is BatteryAction.CHARGE:
            return (
                ("time_of_use", "Enabled"),
                ("tou_power_w", f"{allowed_w:.0f}"),
                ("tou_soc_pct", f"{command.max_soc_pct:.1f}"),
                ("grid_charge", str(command.grid_charge_allowed).lower()),
            )
        return (("hold_strategy", "UNVERIFIED"),)
