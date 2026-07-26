# home-assistant-energy-v2

Passive Home Assistant/AppDaemon controller development for ENERGY V2.

Current stable release:

- `v0.1.0-shadow`: phase 1 passive shadow controller validated in production without physical
  inverter control.

Current development:

- phase 2 summer flow monitoring: passive diagnostics for current SolaX, DEYE, grid, PV, and house
  power flows.

Safety boundary:

- no physical SolaX or DEYE control is implemented,
- `execute_mode()` remains blocked,
- Grid Charge is not implemented,
- ledger helpers are not written,
- legacy `energy_trading_*` entities and automations are not changed by this project phase.

Useful checks:

```bash
python -m compileall apps tests scripts
ruff check .
ruff format --check .
pytest
python scripts/verify_deployment_files.py
python scripts/verify_homeassistant_package.py
```
