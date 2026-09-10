from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.energy_v2.allocator import AllocationParameters, BatteryAvailability, ShadowPowerAllocator
from apps.energy_v2.models import (
    BatteryId,
    CommandStatus,
    ControlTelemetrySnapshot,
    NumericTelemetrySample,
    SiteCommand,
    TelemetryQuality,
)
from apps.energy_v2.shadow_controller import ShadowControlCore

NOW = datetime(2026, 9, 10, 16, 17, tzinfo=UTC)


def sample(value: float, name: str) -> NumericTelemetrySample:
    return NumericTelemetrySample(value, NOW, 0, True, TelemetryQuality.VALID, name)


def telemetry(*, grid: float = 0, solax_battery: float = 0, deye_battery: float = 0):
    return ControlTelemetrySnapshot(
        NOW,
        sample(82, "sensor.solax_inverter_power"),
        sample(9797, "sensor.deye_power"),
        sample(grid, "sensor.solax_measured_power"),
        sample(solax_battery, "sensor.solax_battery_power"),
        sample(deye_battery, "sensor.deye_battery_power"),
        sample(90, "sensor.solax_soc"),
        sample(80, "sensor.deye_soc"),
    )


def availability(battery: BatteryId, measured: float = 0) -> BatteryAvailability:
    return BatteryAvailability(battery, True, 80, 10, 100, 5000, 5000, measured)


def command() -> SiteCommand:
    return SiteCommand("site", NOW, NOW + timedelta(seconds=30), 3000)


def test_shadow_controller_uses_whole_site_grid_and_deye_first() -> None:
    core = ShadowControlCore(allocator=ShadowPowerAllocator(AllocationParameters(slew_limit_w_per_tick=50_000)))
    result = core.evaluate(
        telemetry(grid=9329),
        command(),
        pv_power_w=0,
        deye=availability(BatteryId.DEYE),
        solax=availability(BatteryId.SOLAX),
    )
    assert result.load.load_w == 550
    assert result.grid_actual_w == 9329
    assert result.allocation.deye.target_power_w == -3550


def test_runtime_transfer_is_fault_diagnostic_only() -> None:
    core = ShadowControlCore()
    result = core.evaluate(
        telemetry(solax_battery=1000, deye_battery=-1000),
        command(),
        pv_power_w=0,
        deye=availability(BatteryId.DEYE, -1000),
        solax=availability(BatteryId.SOLAX, 1000),
    )
    assert result.command_status is CommandStatus.FAULT
    assert "DEYE discharge plus SolaX charge" in result.runtime_anti_transfer_state
    assert result.allocation.deye.target_power_w == 0
    assert result.allocation.solax.target_power_w == 0
    assert result.allocation.break_before_make_state == "RUNTIME_TRANSFER_RAMP_DOWN"


def test_stale_grid_measurement_faults_shadow_evaluation() -> None:
    data = telemetry()
    stale = NumericTelemetrySample(
        0,
        NOW - timedelta(seconds=60),
        60,
        False,
        TelemetryQuality.STALE,
        "sensor.solax_measured_power",
    )
    data = ControlTelemetrySnapshot(
        data.sampled_at,
        data.solax_inverter_power,
        data.deye_inverter_power,
        stale,
        data.solax_battery_power,
        data.deye_battery_power,
        data.solax_soc,
        data.deye_soc,
    )
    core = ShadowControlCore()
    result = core.evaluate(
        data,
        command(),
        pv_power_w=0,
        deye=availability(BatteryId.DEYE),
        solax=availability(BatteryId.SOLAX),
    )
    assert result.command_status is CommandStatus.FAULT
    assert result.load.load_w is None
    assert core.trailing_tracker.samples == []
