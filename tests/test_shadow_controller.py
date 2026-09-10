from __future__ import annotations

from dataclasses import replace
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


def sample(
    value: float,
    name: str,
    *,
    at: datetime = NOW,
    quality: TelemetryQuality = TelemetryQuality.VALID,
) -> NumericTelemetrySample:
    fresh = quality is TelemetryQuality.VALID
    return NumericTelemetrySample(value, at, 0 if fresh else 60, fresh, quality, name)


def telemetry(
    *,
    at: datetime = NOW,
    grid: float = 0,
    solax_battery: float = 0,
    deye_battery: float = 0,
    solax_pv: float = 0,
    deye_pv: float = 0,
):
    return ControlTelemetrySnapshot(
        at,
        sample(82, "sensor.solax_inverter_power", at=at),
        sample(9797, "sensor.deye_power", at=at),
        sample(grid, "sensor.solax_measured_power", at=at),
        sample(solax_battery, "sensor.solax_battery_power", at=at),
        sample(deye_battery, "sensor.deye_battery_power", at=at),
        sample(90, "sensor.solax_soc", at=at),
        sample(80, "sensor.deye_soc", at=at),
        sample(solax_pv, "sensor.solax_pv_power", at=at),
        sample(deye_pv, "sensor.deye_pv_power", at=at),
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
        deye=availability(BatteryId.DEYE),
        solax=availability(BatteryId.SOLAX),
    )
    assert result.load.load_w == 550
    assert result.grid_actual_w == 9329
    assert result.allocation.deye.target_power_w == -3550


def test_runtime_transfer_requires_ten_second_confirmation() -> None:
    core = ShadowControlCore()
    results = []
    for seconds in (0, 5, 11):
        at = NOW + timedelta(seconds=seconds)
        results.append(
            core.evaluate(
                telemetry(at=at, solax_battery=1000, deye_battery=-1000),
                command(),
                deye=availability(BatteryId.DEYE, -1000),
                solax=availability(BatteryId.SOLAX, 1000),
            )
        )
    assert results[0].command_status is CommandStatus.UNVERIFIED
    assert results[1].command_status is CommandStatus.UNVERIFIED
    assert all("TRANSFER_SUSPECTED" in result.runtime_anti_transfer_state for result in results[:2])
    assert results[2].command_status is CommandStatus.FAULT
    assert "DEYE battery appears to discharge while SolaX charges" in results[2].runtime_anti_transfer_state
    assert results[2].allocation.deye.target_power_w == 0
    assert results[2].allocation.solax.target_power_w == 0
    assert results[2].allocation.break_before_make_state == "RUNTIME_TRANSFER_RAMP_DOWN"


def test_total_pv_combines_validated_solax_and_deye_inputs() -> None:
    core = ShadowControlCore(allocator=ShadowPowerAllocator(AllocationParameters(slew_limit_w_per_tick=50_000)))
    result = core.evaluate(
        telemetry(solax_pv=4000, deye_pv=1500),
        command(),
        deye=availability(BatteryId.DEYE),
        solax=availability(BatteryId.SOLAX),
    )
    assert result.total_pv_power_w == 5500
    assert result.pv_quality is TelemetryQuality.VALID


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
    data = replace(data, whole_site_grid_power=stale)
    core = ShadowControlCore()
    result = core.evaluate(
        data,
        command(),
        deye=availability(BatteryId.DEYE),
        solax=availability(BatteryId.SOLAX),
    )
    assert result.command_status is CommandStatus.FAULT
    assert result.load.load_w is None
    assert core.trailing_tracker.samples == []
