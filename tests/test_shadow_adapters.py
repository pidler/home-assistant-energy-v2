from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.energy_v2.adapters.deye import DeyeShadowAdapter, DeyeShadowCapabilities
from apps.energy_v2.adapters.solax import SolaxShadowAdapter, SolaxShadowStrategy
from apps.energy_v2.models import BatteryAction, BatteryCommand, BatteryId

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def command(battery: BatteryId, power: float) -> BatteryCommand:
    action = BatteryAction.CHARGE if power > 0 else BatteryAction.DISCHARGE if power < 0 else BatteryAction.HOLD
    return BatteryCommand("c", "s", battery, action, power, 10, 100, False, True, NOW + timedelta(seconds=30))


def test_deye_requested_allowed_predicted_actual_are_separate() -> None:
    result = DeyeShadowAdapter(DeyeShadowCapabilities(max_discharge_power_w=5000)).translate(
        command(BatteryId.DEYE, -7000),
        -4200,
    )
    assert result.requested_power_w == -7000
    assert result.allowed_power_w == -5000
    assert result.simulated_power_w == -5000
    assert result.measured_actual_power_w == -4200
    assert result.saturated


def test_deye_tou_power_is_documented_as_ceiling() -> None:
    result = DeyeShadowAdapter().translate(command(BatteryId.DEYE, -3000), None)
    assert "not assumed exact" in result.reason
    assert ("work_mode", "Export First") in result.proposed_settings


def test_solax_grid_trim_is_diagnostic_and_has_no_trigger() -> None:
    adapter = SolaxShadowAdapter(SolaxShadowStrategy.GRID_TRIM)
    result = adapter.translate(command(BatteryId.SOLAX, -1000), -800, grid_error_w=200)
    assert result.requested_power_w == -1000
    assert ("whole_site_grid_error_w", "200") in result.proposed_settings
    assert ("trigger", "NOT_CALLED") in result.proposed_settings
    assert not hasattr(adapter, "call_service")
    assert not hasattr(adapter, "write_register")


def test_shadow_adapter_sources_have_no_physical_write_api() -> None:
    root = Path(__file__).resolve().parents[1] / "apps" / "energy_v2" / "adapters"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for forbidden in ("call_service", "write_register", "write_registers", "remotecontrol_trigger", "import appdaemon"):
        assert forbidden not in source
