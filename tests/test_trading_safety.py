from __future__ import annotations

from pathlib import Path


def test_trading_package_contains_no_physical_control_calls() -> None:
    root = Path("apps/energy_v2/trading")
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    forbidden = ("call_service", "write_register", "write_registers", "remotecontrol_trigger", "SiteCommand")
    for token in forbidden:
        assert token not in source


def test_appdaemon_runtime_does_not_import_trading_planner() -> None:
    app_source = Path("apps/energy_v2/app.py").read_text(encoding="utf-8")
    assert "energy_v2.trading" not in app_source
    assert "from .trading" not in app_source
