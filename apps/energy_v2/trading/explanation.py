from __future__ import annotations

from .models import TradingAction, TradingSlotInput


def explain_slot(
    slot: TradingSlotInput,
    *,
    action: TradingAction,
    battery_export_kwh: float,
    future_peak: float | None,
    price_percentile: float,
) -> str:
    if action is TradingAction.CHARGE_FROM_PV:
        later = f"; a later sell price reaches {future_peak:.2f} CZK/kWh" if future_peak is not None else ""
        return f"PV surplus is stored instead of sold at {slot.sell_price_czk_per_kwh:.2f} CZK/kWh{later}"
    if action is TradingAction.EXPORT:
        source = "battery/PV energy" if battery_export_kwh > 1e-6 else "PV surplus"
        return (
            f"Export {source}; price is at percentile {price_percentile:.0f} of the visible horizon "
            "and terminal reserves remain protected"
        )
    if action is TradingAction.IMPORT_FOR_LOAD:
        return "Grid supplies residual house load; summer policy forbids importing energy to charge batteries"
    if future_peak is not None and future_peak > slot.sell_price_czk_per_kwh + 0.01:
        return (
            f"Energy held for a later peak at {future_peak:.2f} CZK/kWh, "
            f"above the current {slot.sell_price_czk_per_kwh:.2f} CZK/kWh"
        )
    return "Hold: no economically superior safe action after losses, cycling cost and terminal value"


def price_percentile(prices: tuple[float, ...], value: float) -> float:
    if not prices:
        return 0.0
    return 100.0 * sum(price <= value for price in prices) / len(prices)
