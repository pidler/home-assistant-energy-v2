from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from .models import BatteryParameters, PhysicalLimitStatus, PlannerInput, TradingSlotInput
from .planner import plan_trading_schedule
from .renderer import render_json, render_text


def synthetic_example() -> PlannerInput:
    start = datetime(2026, 9, 11, 17, 0, tzinfo=UTC)
    prices = (3.0, 4.0, 6.0, 5.0, 2.0, 2.0, 5.5, 4.0)
    pv = (0.2, 0.0, 0.0, 0.0, 1.5, 2.0, 0.2, 0.0)
    slots = tuple(
        TradingSlotInput(start + timedelta(minutes=15 * index), 8.0, price, pv[index], 0.25)
        for index, price in enumerate(prices)
    )
    return PlannerInput(
        slots=slots,
        batteries=(
            BatteryParameters(
                "DEYE",
                32,
                max_charge_power_w=10_000,
                max_discharge_power_w=10_000,
                power_limit_status=PhysicalLimitStatus.MODEL_ASSUMPTION,
            ),
            BatteryParameters(
                "SolaX",
                24,
                max_charge_power_w=10_000,
                max_discharge_power_w=10_000,
                power_limit_status=PhysicalLimitStatus.MODEL_ASSUMPTION,
            ),
        ),
        initial_soc_pct={"DEYE": 60, "SolaX": 60},
        generated_at=start,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a computation-only ENERGY V2 Phase 5A plan")
    parser.add_argument("--json", action="store_true", help="render JSON instead of a text table")
    args = parser.parse_args()
    result = plan_trading_schedule(synthetic_example())
    print(render_json(result) if args.json else render_text(result))


if __name__ == "__main__":
    main()
