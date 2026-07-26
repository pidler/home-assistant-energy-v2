from __future__ import annotations

from datetime import datetime

from apps.energy_v2.models import TelemetrySnapshot, ValidationResult
from apps.energy_v2.safety import find_active_conflicts, find_missing_entities, safe_to_enable, validate_telemetry


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


def test_deye_device_state_whitespace_is_valid() -> None:
    result = validate_telemetry(snapshot(deye_device_state=" Normal "))
    assert result.valid


def test_multiple_invalid_telemetry_reasons_are_reported() -> None:
    result = validate_telemetry(
        snapshot(
            deye_connected=False,
            deye_soc_pct=101.0,
            solax_soc_pct=-1.0,
            sell_price=None,
        )
    )
    assert not result.valid
    assert "DEYE is not connected" in result.reasons
    assert "DEYE SOC is outside 0-100 %: 101.0" in result.reasons
    assert "SolaX SOC is outside 0-100 %: -1.0" in result.reasons
    assert "Sell price is missing" in result.reasons


def test_missing_soc_invalid() -> None:
    result = validate_telemetry(snapshot(deye_soc_pct=None))
    assert not result.valid
    assert "DEYE SOC is missing" in result.reasons


def test_solax_soc_below_zero_invalid() -> None:
    result = validate_telemetry(snapshot(solax_soc_pct=-0.1))
    assert not result.valid
    assert any("SolaX SOC is outside" in reason for reason in result.reasons)


def test_deye_soc_above_100_invalid() -> None:
    result = validate_telemetry(snapshot(deye_soc_pct=100.1))
    assert not result.valid
    assert any("DEYE SOC is outside" in reason for reason in result.reasons)


def test_future_sell_rank_below_one_invalid_when_available() -> None:
    result = validate_telemetry(snapshot(future_sell_rank=0.0))
    assert not result.valid
    assert any("Future sell rank is below 1" in reason for reason in result.reasons)


def test_future_sell_rank_unknown_is_optional() -> None:
    result = validate_telemetry(snapshot(future_sell_rank=None))
    assert result.valid


def test_all_safe_valid() -> None:
    assert validate_telemetry(snapshot()).valid


def test_active_conflicts_detected() -> None:
    conflicts = find_active_conflicts(
        {"automation.a": "on", "automation.b": "off"},
        ("automation.a", "automation.b"),
    )
    assert conflicts == ("automation.a",)


def test_missing_entities_detected() -> None:
    missing = find_missing_entities(
        {"sensor.present": {"state": "1"}, "sensor.missing": None},
        ("sensor.present", "sensor.missing"),
    )
    assert missing == ("sensor.missing",)


def test_safe_to_enable_blocks_legacy_system() -> None:
    result = safe_to_enable(
        ValidationResult(True, ()),
        legacy_enabled=True,
        current_system_enabled=False,
        active_conflicts=(),
        missing_required_entities=(),
        missing_conflicting_automations=(),
        missing_owned_actuators=(),
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
        missing_required_entities=(),
        missing_conflicting_automations=(),
        missing_owned_actuators=(),
        service_mode=False,
    )
    assert not result.valid
    assert any("Conflicting" in reason for reason in result.reasons)


def test_safe_to_enable_blocks_missing_required_entity() -> None:
    result = safe_to_enable(
        ValidationResult(True, ()),
        legacy_enabled=False,
        current_system_enabled=False,
        active_conflicts=(),
        missing_required_entities=("sensor.required",),
        missing_conflicting_automations=(),
        missing_owned_actuators=(),
        service_mode=False,
    )
    assert not result.valid
    assert any("Required entities are missing" in reason for reason in result.reasons)


def test_safe_to_enable_blocks_missing_conflicting_automation() -> None:
    result = safe_to_enable(
        ValidationResult(True, ()),
        legacy_enabled=False,
        current_system_enabled=False,
        active_conflicts=(),
        missing_required_entities=(),
        missing_conflicting_automations=("automation.missing",),
        missing_owned_actuators=(),
        service_mode=False,
    )
    assert not result.valid
    assert any("Conflicting automations are missing" in reason for reason in result.reasons)


def test_safe_to_enable_blocks_missing_owned_actuator() -> None:
    result = safe_to_enable(
        ValidationResult(True, ()),
        legacy_enabled=False,
        current_system_enabled=False,
        active_conflicts=(),
        missing_required_entities=(),
        missing_conflicting_automations=(),
        missing_owned_actuators=("select.future_actuator",),
        service_mode=False,
    )
    assert not result.valid
    assert any("Owned actuator entities are missing" in reason for reason in result.reasons)
