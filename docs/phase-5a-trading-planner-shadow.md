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
| Whole-site load history | Prepared from SolaX AC + DEYE AC - grid | Authoritative measured W; grid is positive export, negative import |
| DEYE SOC | `sensor.deye_battery` | Percent |
| SolaX SOC | `sensor.solax_battery_capacity` | Percent |

The price parser consumes timestamp keys directly and never duplicates already-quarter-hour values. The horizon is
all common future timestamps; it is not fixed to 96 slots. Solcast average kW points are converted to two 15-minute
kWh values. Thus `sum(power_kw * 0.5 h)` equals the resulting 15-minute energy sum.

The authoritative input is the same production-validated whole-site relationship used by Phase 4:

`L_site = sensor.solax_inverter_power + sensor.deye_power - sensor.solax_measured_power`

The grid convention is positive for export and negative for import. `sensor.solax_house_load` is not an authoritative
site-load source because it does not cover the complete AC-coupled system. The load adapter accepts prepared
whole-site history and builds a median quarter-hour-of-day profile, split into weekday/weekend buckets. When
sufficient history is supplied it reports `MEASURED_MODEL`. Sparse history is `SIMPLE_BASELINE`; no history uses an
explicit configurable fallback and reports `FALLBACK`. Phase 5A does not fetch recorder history itself.

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
| Charge/discharge power | 10 kW / 10 kW | 10 kW / 10 kW |
| Limit status | `MODEL_ASSUMPTION` | `MODEL_ASSUMPTION` |
| Charge/discharge efficiency | 95% / 95% | 95% / 95% |
| Role | `TRADING_BATTERY` | `HOUSE_RESERVE_BATTERY` |
| Default terminal soft reserve | 10% | 10% |

The 10 kW values are provisional `MODEL_ASSUMPTION` inputs, not confirmed or conservative physical limits. Charge
and discharge limits are independently configurable for each battery. A value may be labelled
`CONFIRMED_PHYSICAL_LIMIT` only after supporting evidence or controlled capability tests exist. The status and actual
per-battery values are shown in the rendered plan summary.

## Battery-role and SOC checkpoint policy

DEYE is the trading battery. Its physical, economic and default terminal soft floors are all 10%, so the planner may
discharge it to 10% when the solved economics justify that action. It has no house-load evening checkpoint.

SolaX is the house-reserve battery. Its physical and generic terminal soft floors remain 10%, while operational
protection is expressed by timestamped `SocCheckpoint` constraints rather than an artificial horizon-edge reserve:

- the configurable evening checkpoint targets 30% SOC;
- the normal candidate disables SolaX trading export during the configured morning window but permits discharge to
  whole-site load down to the physical floor;
- a candidate exception may additionally export using a 15% trading floor within the configured morning window;
- the exception candidate has a hard 30% recovery checkpoint at the configured deadline.

The evening 30% is energy intended for forecast overnight house consumption, not a morning floor. After the evening
checkpoint, the LP may discharge SolaX to supply whole-site load. Morning SOC is therefore whatever remains after
the modeled overnight trajectory, subject to the physical 10% floor.

No policy time is silently invented. The caller supplies local `datetime.time` values and the planning-slot timezone
is used. A time between quarter-hour boundaries maps deterministically to the first state timestamp at or after that
time on the same local date. Checkpoints outside the visible horizon are not generated.

An evening target that is optimistically reachable is a hard operational constraint. If initial SOC, available PV,
duration or charge power make it unreachable, it becomes an explicit highly penalized soft checkpoint so the LP
remains feasible and reports `EVENING_RESERVE_SHORTFALL` with target, projected SOC and shortfall.

### Conditional morning candidate selection

The planner solves two deterministic candidates using the same objective:

1. normal policy: SolaX trading export is blocked from morning-window start through recovery deadline, while
   discharge to house load remains available down to the physical 10% floor;
2. trading exception: SolaX starts at its actual SOC after overnight house consumption, may additionally export down
   to a 15% trading floor inside the morning window, may not export during recovery, and must reach 30% by deadline.

Before candidate 2 is solved, a conservative feasibility gate calculates per-slot usable PV surplus as
`max(PV forecast - whole-site load forecast, 0)`, caps it by the SolaX charge-power limit, and applies charge
efficiency. It must conservatively be sufficient for a full 15% to 30% recovery, but it does not impose or assume a
30% SOC at morning start. This gate is not the final proof: the candidate's
hard recovery checkpoint is then solved inside the complete LP, so PV allocation, DEYE competition, load, export
and all power constraints remain effective. Candidate 2 is selected only when it is feasible and has strictly higher
objective value. Otherwise the normal 30% policy remains selected.

The result exposes battery roles, active per-slot SOC floors, checkpoint target/projected/shortfall values, candidate
feasibility and selection, usable recovery energy, projected SOC at the deadline and expected recovery time.

## Terminal value

For each battery, one stored kWh at the horizon edge is valued from an estimate at that edge:

`median sell price in the final 3 hours × discharge efficiency × terminal_value_factor`

The edge window is configurable and an explicit continuation-price estimate may be supplied. The deterministic
median prevents an attractive opportunity earlier in the horizon from being incorrectly treated as value available
after the horizon. The default factor is 0.60, with a 1 CZK/kWh floor.

The 10% role-policy default is explicitly a configurable **HEURISTIC SOFT RESERVE**, not a physical minimum and not
an economically derived guarantee. It coincides with the 10% physical floor by default; SolaX house protection is
provided by operational checkpoints. Missing soft reserve is penalized from the same
edge price estimate. Unlike a hard constraint, this remains feasible when initial SOC is below the target and PV
cannot restore it. `PlannerResult` separately returns physical minimum SOC, soft target SOC/kWh, actual terminal
SOC/stored kWh, and actual shortfall; it never describes an unmet target as energy actually reserved.

## Rolling re-optimization

`replan_reasons()` requests a new solve for any of:

- a new price revision, including D+1 publication;
- 15 minutes since the previous solve;
- at least 15% PV forecast change;
- at least 3 percentage points SOC deviation from plan;
- at least 20% measured load deviation from forecast.

The function only identifies triggers. No automatic scheduler was added.

## Explainability and manual comparison

Every slot has an action and a reason generated after solving. The context includes current SOC/stored energy,
terminal SOC, soft target and shortfall, actual future planned battery exports, relevant prices, and forecast PV
before that export. A higher future price alone is not enough to claim energy is being held for it.

`render_text()` emits the complete table plus significant action transitions; `render_json()` emits a machine-readable
representation. `PlanComparison` and `compare_plans()` report raw grid cashflow, terminal stored-energy difference,
terminal-value adjustment, and comparable economic value. Both plans must use the same terminal valuation policy.

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
