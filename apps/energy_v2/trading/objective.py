from __future__ import annotations

from .models import BatteryParameters, PlannerConfig, TradingSlotInput


def terminal_values(
    slots: tuple[TradingSlotInput, ...],
    batteries: tuple[BatteryParameters, ...],
    config: PlannerConfig,
) -> dict[str, float]:
    """Return a conservative continuation value for one stored kWh."""

    best_sell = max(slot.sell_price_czk_per_kwh for slot in slots)
    return {
        battery.name: max(
            config.terminal_value_floor_czk_per_kwh,
            best_sell * battery.discharge_efficiency * config.terminal_value_factor,
        )
        for battery in batteries
    }


def terminal_reserve_shortfall_costs(
    slots: tuple[TradingSlotInput, ...],
    batteries: tuple[BatteryParameters, ...],
    config: PlannerConfig,
) -> dict[str, float]:
    """Value missing reserve above the best visible discharge opportunity."""

    best_sell = max(slot.sell_price_czk_per_kwh for slot in slots)
    return {
        battery.name: max(
            config.terminal_value_floor_czk_per_kwh,
            best_sell * battery.discharge_efficiency,
        )
        * config.terminal_reserve_shortfall_factor
        for battery in batteries
    }
