# Phase 5A: trading planner shadow

## Scope and isolation

Phase 5A is a computation-only rolling planner. It does not import the AppDaemon application, create a
`SiteCommand`, call Home Assistant, or publish any inverter setting. The existing Phase 4 controller does not
import this package. The package may therefore be exercised in tests and from its CLI without changing production.

The implementation is under `apps/energy_v2/trading/`. Its deployment mirror exists only because the repository
verifies all application source mirrors; no Phase 5A runtime configuration or scheduler has been added.

## Audited production inputs

The read-only audit on 2026-09-11 found:

| Input | Production source | Shape |
| --- | --- | --- |
| Buy price | `sensor.current_buy_electricity_price_15min` | Timestamp-keyed 15-minute CZK/kWh values for today and D+1 |
| Sell price | `sensor.current_sell_electricity_price_15min` | Timestamp-keyed 15-minute CZK/kWh values for today and D+1 |
| PV | `sensor.solcast_pv_forecast_forecast_today` and `_tomorrow` | `detailedForecast`, 30-minute average kW points |
| Load history | `sensor.solax_house_load` | Measured W; no separate forecast entity was found |
| DEYE SOC | `sensor.deye_battery` | Percent |
| SolaX SOC | `sensor.solax_battery_capacity` | Percent |

The price parser consumes timestamp keys directly and never duplicates already-quarter-hour values. The horizon is
all common future timestamps; it is not fixed to 96 slots. Solcast average kW points are converted to two 15-minute
kWh values. Thus `sum(power_kw * 0.5 h)` equals the resulting 15-minute energy sum.

The load adapter supports a median quarter-hour-of-day profile, split into weekday/weekend buckets. When sufficient
history is supplied it reports `MEASURED_MODEL`. Sparse history is `SIMPLE_BASELINE`; no history uses an explicit
configurable fallback and reports `FALLBACK`. Phase 5A does not fetch recorder history itself.

## Units and signs

- Price: CZK/kWh.
- Power: W in configuration and output; positive battery power means charging, negative means discharging.
- Energy: kWh.
- Slot duration: exactly 0.25 h. For example, 4,000 W for one slot is 1 kWh.
- Grid import/export are non-negative kWh per slot.

## Linear optimization model

PuLP/CBC solves a deterministic linear program. Per slot it models PV to load, PV to each battery, PV export, each
battery to load/export, and grid import for residual load. Battery stored energy is a state variable.

Constraints include:

- PV allocation cannot exceed the PV forecast;
- load must be supplied by PV, battery discharge, or grid import;
- battery SOC, charge power, discharge power, and efficiencies;
- each battery's soft terminal-reserve target and explicit shortfall variable;
- whole-site export no greater than 9.8 kW (2.45 kWh per quarter hour);
- grid energy can serve load but has no path to battery charging.

The LP validates the normal retail tariff invariant `buy_price >= sell_price`. This makes simultaneous import/export
strictly dominated without adding binary variables. A violated tariff is rejected instead of allowing artificial
same-slot grid arbitrage; supporting such a tariff would require an explicit MILP import/export direction variable.

The objective maximizes export revenue minus import cost, a 0.02 CZK/kWh throughput penalty, and a tiny
0.0001 CZK/kWh SolaX discharge tie-break, plus terminal stored-energy value. The SolaX term only resolves equivalent
solutions; it does not override economics. The positive throughput penalty prevents degenerate simultaneous
charge/discharge cycles.

Default battery model for review:

| Parameter | DEYE | SolaX |
| --- | ---: | ---: |
| Nominal capacity | 32 kWh | 24 kWh |
| Minimum / maximum SOC | 10% / 100% | 10% / 100% |
| Conservative charge/discharge power | 10 kW / 10 kW | 10 kW / 10 kW |
| Charge/discharge efficiency | 95% / 95% | 95% / 95% |
| Default terminal reserve | 20% | 20% |

Power limits remain conservative model parameters until controlled physical capability tests establish better values.

## Terminal value

For each battery, one stored kWh at the horizon edge is valued at:

`best visible sell price × discharge efficiency × terminal_value_factor`

The default factor is 0.60, with a conservative 1 CZK/kWh floor so an all-negative-price horizon cannot make the LP
unbounded or assign nonsensical negative continuation value. A configurable terminal SOC reserve (20% by default)
is a soft target. Missing reserve is
penalized at 1.05 times the best visible discharge value, so the planner does not dump it merely because the horizon
ends. Unlike a hard constraint, this remains feasible when the real initial SOC is below 20% and forecast PV cannot
restore it. The CZK/kWh continuation value, reserve target and shortfall are all returned in `PlannerResult`.

## Rolling re-optimization

`replan_reasons()` requests a new solve for any of:

- a new price revision, including D+1 publication;
- 15 minutes since the previous solve;
- at least 15% PV forecast change;
- at least 3 percentage points SOC deviation from plan;
- at least 20% measured load deviation from forecast.

The function only identifies triggers. No automatic scheduler was added.

## Explainability and manual comparison

Every slot has an action and generated reason. `render_text()` emits the complete table plus significant action
transitions; `render_json()` emits a machine-readable representation. `PlanComparison` and `compare_plans()` retain
net values and terminal SOC trajectories for a future user-entered manual plan.

Run the synthetic renderer locally with:

```bash
python -m apps.energy_v2.trading.cli
python -m apps.energy_v2.trading.cli --json
```

## Explicit non-goals

- no production deployment or AppDaemon schedule;
- no Phase 4 command publication;
- no grid charging or winter strategy;
- no Home Assistant service or Modbus writes;
- no automatic use of the production price, forecast, or SOC entities yet.
