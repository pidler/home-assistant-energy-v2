# home-assistant-energy-v2

Passive Home Assistant/AppDaemon controller development for ENERGY V2.

Current stable release:

- `v0.1.0-shadow`: phase 1 passive shadow controller validated in production without physical
  inverter control.

Current development:

- phase 2 summer flow monitoring: passive diagnostics for current SolaX, DEYE, grid, PV, and house
  power flows.
- aggregate export-limit diagnostics using a time-weighted rolling 15-minute average.
- read-only Lovelace dashboard YAML for phase 2 review.
- grid authority is `sensor.solax_measured_power`, where positive means export and negative means
  import for the whole connection.
- battery power is normalized internally as positive charging and negative discharging for both
  SolaX and DEYE; DEYE uses existing `sensor.battery_power_otoceny`.
- phase 4 adds a strictly shadow-only whole-site load model, export budgets, DEYE-first power
  allocator, anti-transfer simulation and inverter-specific diagnostic adapters.
- phase 4 telemetry uses production-derived SolaX/DEYE freshness windows and source-health
  inference so unchanged SOC and healthy exact-zero PV/grid states are not falsely rejected.
- phase 5A adds an isolated, computation-only 15-minute LP trading planner with terminal value,
  rolling-replan triggers and human-readable reasons; it is not scheduled or deployed.

Safety boundary:

- no physical SolaX or DEYE control is implemented,
- `execute_mode()` remains blocked,
- Grid Charge is not implemented,
- ledger helpers are not written,
- legacy `energy_trading_*` entities and automations are not changed by this project phase.
- the repository change does not update the already deployed production dashboard.

See [`docs/phase-5a-trading-planner-shadow.md`](docs/phase-5a-trading-planner-shadow.md) for the
planner's units, audited inputs, constraints and safety boundary.

Confirmed phase 2 export parameters:

- operational target: 9,800 W aggregate export,
- permitted average: 10,000 W over 15 minutes,
- instantaneous excursions are warnings, not automatic faults.

Useful checks:

```bash
python -m compileall apps tests scripts
ruff check .
ruff format --check .
pytest
python scripts/verify_deployment_files.py
python scripts/verify_homeassistant_package.py
python scripts/verify_dashboard.py
```
