from __future__ import annotations

from datetime import datetime

from apps.energy_v2.models import TelemetrySnapshot, ValidationResult
from apps.energy_v2.safety import find_active_conflicts, safe_to_enable, validate_telemetry


def snapshot(**overrides: object) -> TelemetrySnapshot:
    data = dict(
        timestamp=datetime.now(),
        solax_soc_pct=90.0,
        solax_battery_power_w=0.0,
        solax_pv_power_w=3000.0,
        solax_house_load_w=1000.0,
        solax_grid_import_w=0.0,
        solax_grid_export_w=0.0,
        deye_soc_pct=50.0,
        deye_battery_power_w=0.0,
        deye_battery_state="idle",
        deye_grid_power_w=0.0,
        deye_external_power_w=0.0,
        deye_device_state="Normal",
        deye_connected=True,
        buy_price=2.0,
        sell_price=3.5,
        future_sell_rank=5.0,
        deye_grid_charging_enabled=False,
        deye_export_enabled=False,
    )
    data.update(overrides)
    return TelemetrySnapshot(**data)


def test_deye_offline_invalid() -> None:
    result = validate_telemetry(snapshot(deye_connected=False))
    assert not result.valid
    assert any("not connected" in reason for reason in result.reasons)


def test_deye_not_normal_invalid() -> None:
    result = validate_telemetry(snapshot(deye_device_state="Fault"))
    assert not result.valid
    assert any("expected 'Normal'" in reason for reason in result.reasons)


def test_missing_soc_invalid() -> None:
    result = validate_telemetry(snapshot(deye_soc_pct=None))
    assert not result.valid
    assert "DEYE SOC is missing" in result.reasons


def test_all_safe_valid() -> None:
    assert validate_telemetry(snapshot()).valid


def test_active_conflicts_detected() -> None:
    conflicts = find_active_conflicts(
        {"automation.a": "on", "automation.b": "off"},
        ("automation.a", "automation.b"),
    )
    assert conflicts == ("automation.a",)


def test_safe_to_enable_blocks_legacy_system() -> None:
    result = safe_to_enable(
        ValidationResult(True, ()),
        legacy_enabled=True,
        current_system_enabled=False,
        active_conflicts=(),
        service_mode=False,
    )
    assert not result.valid
    assert any("Legacy" in reason for reason in result.reasons)


def test_safe_to_enable_blocks_active_conflict() -> None:
    result = safe_to_enable(
        ValidationResult(True, ()),
        legacy_enabled=False,
        current_system_enabled=False,
        active_conflicts=("automation.a",),
        service_mode=False,
    )
    assert not result.valid
    assert any("Conflicting" in reason for reason in result.reasons)

