from __future__ import annotations

from math import ceil
from statistics import median

from .models import BatteryParameters, PlannerConfig, TradingSlotInput


def continuation_price(slots: tuple[TradingSlotInput, ...], config: PlannerConfig) -> float:
    """Estimate value beyond the horizon from prices nearest its right edge.

    An explicit estimate wins. Otherwise the median sell price in the final
    configured lookback window is used. Prices earlier in the horizon cannot
    inflate this edge estimate.
    """

    if config.terminal_continuation_price_czk_per_kwh is not None:
        return config.terminal_continuation_price_czk_per_kwh
    priced_slots = tuple(slot for slot in slots if not slot.is_guard_only)
    if not priced_slots:
        raise ValueError("continuation price requires an economic horizon")
    lookback_slots = max(1, ceil(config.terminal_price_lookback_hours / config.slot_hours))
    return float(median(slot.sell_price_czk_per_kwh for slot in priced_slots[-lookback_slots:]))  # type: ignore[arg-type]


def terminal_values(
    slots: tuple[TradingSlotInput, ...],
    batteries: tuple[BatteryParameters, ...],
    config: PlannerConfig,
) -> dict[str, float]:
    """Return the discounted edge-based continuation value of stored energy."""

    edge_price = continuation_price(slots, config)
    return {
        battery.name: max(
            config.terminal_value_floor_czk_per_kwh,
            edge_price * battery.discharge_efficiency * config.terminal_value_factor,
        )
        for battery in batteries
    }


def terminal_reserve_shortfall_costs(
    slots: tuple[TradingSlotInput, ...],
    batteries: tuple[BatteryParameters, ...],
    config: PlannerConfig,
) -> dict[str, float]:
    """Penalize missing heuristic reserve using the same edge estimate."""

    edge_price = continuation_price(slots, config)
    return {
        battery.name: max(
            config.terminal_value_floor_czk_per_kwh,
            edge_price * battery.discharge_efficiency,
        )
        * config.terminal_reserve_shortfall_factor
        for battery in batteries
    }
