from __future__ import annotations

from dataclasses import dataclass

from .models import TradingAction, TradingSlotInput


@dataclass(frozen=True)
class ExplanationContext:
    current_soc_pct: dict[str, float]
    current_stored_kwh: dict[str, float]
    terminal_soc_pct: dict[str, float]
    terminal_reserve_target_kwh: dict[str, float]
    terminal_reserve_shortfall_kwh: dict[str, float]
    future_planned_battery_export_kwh: float
    future_export_price_czk_per_kwh: float | None
    pv_before_future_export_kwh: float


def explain_slot(
    slot: TradingSlotInput,
    *,
    action: TradingAction,
    battery_export_kwh: float,
    price_percentile: float,
    context: ExplanationContext,
) -> str:
    state = ", ".join(
        f"{name} {context.current_soc_pct[name]:.1f}%/{context.current_stored_kwh[name]:.2f} kWh"
        for name in context.current_soc_pct
    )
    shortfall = sum(context.terminal_reserve_shortfall_kwh.values())
    target = sum(context.terminal_reserve_target_kwh.values())
    future_export = context.future_planned_battery_export_kwh
    future_price = context.future_export_price_czk_per_kwh
    if slot.is_guard_only:
        if action is TradingAction.CHARGE_FROM_PV:
            activity = "PV surplus may charge batteries"
        elif action is TradingAction.IMPORT_FOR_LOAD:
            activity = "grid supplies load that PV and available battery energy cannot cover"
        else:
            activity = "PV and batteries balance forecast house load"
        return (
            "GUARD ONLY: price is unavailable, so trading export and grid-to-battery charging are blocked; "
            f"{activity}; current {state}"
        )
    if action is TradingAction.CHARGE_FROM_PV:
        if future_export > 1e-6 and future_price is not None:
            return (
                f"PV surplus is stored at {slot.sell_price_czk_per_kwh:.2f} CZK/kWh; solved plan later exports "
                f"{future_export:.2f} battery kWh at up to {future_price:.2f} CZK/kWh after "
                f"{context.pv_before_future_export_kwh:.2f} kWh forecast PV; current {state}"
            )
        return (
            f"PV surplus is stored; no later battery export is scheduled, terminal soft target is {target:.2f} kWh "
            f"with {shortfall:.2f} kWh shortfall; current {state}"
        )
    if action is TradingAction.EXPORT:
        source = "battery/PV energy" if battery_export_kwh > 1e-6 else "PV surplus"
        reserve_status = (
            "heuristic soft terminal reserve target is achieved"
            if shortfall <= 1e-6
            else f"heuristic soft terminal reserve has {shortfall:.2f} kWh shortfall"
        )
        return (
            f"Export {source}; price is at percentile {price_percentile:.0f} of the visible horizon "
            f"and {reserve_status}; current {state}"
        )
    if action is TradingAction.IMPORT_FOR_LOAD:
        return f"Grid supplies residual whole-site load; summer policy forbids grid battery charging; current {state}"
    if (
        future_export > 1e-6
        and future_price is not None
        and slot.sell_price_czk_per_kwh is not None
        and future_price > slot.sell_price_czk_per_kwh + 0.01
    ):
        return (
            f"Solved plan holds energy now and later exports {future_export:.2f} battery kWh at up to "
            f"{future_price:.2f} CZK/kWh versus {slot.sell_price_czk_per_kwh:.2f} now; forecast PV before that "
            f"export is {context.pv_before_future_export_kwh:.2f} kWh; current {state}"
        )
    return (
        f"Hold: solved plan schedules no battery export now or at a higher later price; terminal SOC "
        f"{_format_values(context.terminal_soc_pct, '%')}, soft target {target:.2f} kWh, shortfall "
        f"{shortfall:.2f} kWh; current {state}"
    )


def price_percentile(prices: tuple[float, ...], value: float) -> float:
    if not prices:
        return 0.0
    return 100.0 * sum(price <= value for price in prices) / len(prices)


def _format_values(values: dict[str, float], unit: str) -> str:
    return ", ".join(f"{name} {value:.1f}{unit}" for name, value in values.items())
