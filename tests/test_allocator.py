from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from apps.energy_v2.allocator import (
    AllocationParameters,
    BatteryAvailability,
    ShadowPowerAllocator,
    validate_anti_transfer,
    validate_battery_command,
)
from apps.energy_v2.models import BatteryAction, BatteryCommand, BatteryId, CommandStatus, SiteCommand

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def site(**overrides: object) -> SiteCommand:
    data = dict(
        command_id="site-1",
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        grid_target_w=3000.0,
        max_export_w=9800.0,
        transfer_allowed=False,
        preferred_battery=BatteryId.DEYE,
        grid_charge_allowed=False,
    )
    data.update(overrides)
    return SiteCommand(**data)


def battery(battery_id: BatteryId, **overrides: object) -> BatteryAvailability:
    data = dict(
        battery=battery_id,
        available=True,
        soc_pct=50.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        max_charge_power_w=5000.0,
        max_discharge_power_w=5000.0,
        measured_power_w=0.0,
    )
    data.update(overrides)
    return BatteryAvailability(**data)


def allocator() -> ShadowPowerAllocator:
    return ShadowPowerAllocator(AllocationParameters(slew_limit_w_per_tick=50_000))


def allocate(command: SiteCommand | None = None, **kwargs: object):
    return allocator().allocate(
        command or site(),
        now=NOW,
        site_load_w=float(kwargs.pop("site_load_w", 0)),
        pv_power_w=float(kwargs.pop("pv_power_w", 0)),
        deye=kwargs.pop("deye", battery(BatteryId.DEYE)),
        solax=kwargs.pop("solax", battery(BatteryId.SOLAX)),
        **kwargs,
    )


def test_deye_covers_whole_discharge_target() -> None:
    result = allocate()
    assert result.deye.target_power_w == -3000
    assert result.solax.target_power_w == 0


def test_deye_saturates_and_solax_gets_residual() -> None:
    result = allocate(deye=battery(BatteryId.DEYE, max_discharge_power_w=2000))
    assert result.deye.target_power_w == -2000
    assert result.solax.target_power_w == -1000


def test_deye_unavailable_uses_solax() -> None:
    result = allocate(deye=battery(BatteryId.DEYE, available=False))
    assert result.deye.target_power_w == 0
    assert result.solax.target_power_w == -3000


def test_solax_unavailable_is_ok_when_deye_covers_target() -> None:
    result = allocate(solax=battery(BatteryId.SOLAX, available=False))
    assert result.status is CommandStatus.READY
    assert result.deye.target_power_w == -3000


def test_both_unavailable_reports_saturation() -> None:
    result = allocate(
        deye=battery(BatteryId.DEYE, available=False),
        solax=battery(BatteryId.SOLAX, available=False),
    )
    assert result.status is CommandStatus.SATURATED
    assert "shortfall" in result.saturation_reason


def test_export_target_above_9800_is_clamped() -> None:
    result = allocate(site(grid_target_w=12_000), budget_power_w=20_000)
    assert result.allowed_grid_target_w == 9800
    assert result.status is CommandStatus.SATURATED


def test_minimum_soc_blocks_discharge() -> None:
    result = allocate(deye=battery(BatteryId.DEYE, soc_pct=10), solax=battery(BatteryId.SOLAX, soc_pct=10))
    assert result.deye.target_power_w == result.solax.target_power_w == 0
    assert result.status is CommandStatus.SATURATED


def test_maximum_soc_blocks_charge() -> None:
    result = allocate(
        site(grid_target_w=0),
        pv_power_w=4000,
        deye=battery(BatteryId.DEYE, soc_pct=100),
        solax=battery(BatteryId.SOLAX, soc_pct=100),
    )
    assert result.deye.target_power_w == result.solax.target_power_w == 0
    assert result.status is CommandStatus.SATURATED


def test_expired_command_is_rejected() -> None:
    result = allocate(site(expires_at=NOW))
    assert result.status is CommandStatus.EXPIRED


def test_invalid_action_sign_is_rejected() -> None:
    command = BatteryCommand("x", "s", BatteryId.DEYE, BatteryAction.CHARGE, -100, 10, 100, False, True, NOW)
    assert "CHARGE requires positive" in validate_battery_command(command)[0]


def test_both_transfer_directions_are_blocked() -> None:
    base = BatteryCommand("x", "s", BatteryId.DEYE, BatteryAction.DISCHARGE, -1000, 10, 100, False, True, NOW)
    other = BatteryCommand("y", "s", BatteryId.SOLAX, BatteryAction.CHARGE, 1000, 10, 100, False, True, NOW)
    assert validate_anti_transfer(base, other, transfer_allowed=False)
    assert validate_anti_transfer(
        replace(base, battery=BatteryId.SOLAX),
        replace(other, battery=BatteryId.DEYE),
        transfer_allowed=False,
    )


def test_break_before_make_waits_for_confirmed_zero_flow() -> None:
    control = allocator()
    discharge = control.allocate(
        site(grid_target_w=1000),
        now=NOW,
        site_load_w=0,
        pv_power_w=0,
        deye=battery(BatteryId.DEYE, measured_power_w=-1000),
        solax=battery(BatteryId.SOLAX),
    )
    assert discharge.deye.target_power_w == -1000
    reverse = control.allocate(
        site(grid_target_w=0),
        now=NOW + timedelta(seconds=1),
        site_load_w=0,
        pv_power_w=1000,
        deye=battery(BatteryId.DEYE, measured_power_w=0),
        solax=battery(BatteryId.SOLAX),
    )
    assert reverse.status is CommandStatus.BREAK_BEFORE_MAKE
    assert reverse.deye.action is BatteryAction.HOLD
    control.allocate(
        site(grid_target_w=0),
        now=NOW + timedelta(seconds=2),
        site_load_w=0,
        pv_power_w=1000,
        deye=battery(BatteryId.DEYE, measured_power_w=0),
        solax=battery(BatteryId.SOLAX),
    )
    resumed = control.allocate(
        site(grid_target_w=0),
        now=NOW + timedelta(seconds=13),
        site_load_w=0,
        pv_power_w=1000,
        deye=battery(BatteryId.DEYE, measured_power_w=0),
        solax=battery(BatteryId.SOLAX),
    )
    assert resumed.deye.action is BatteryAction.CHARGE


def test_missing_power_feedback_cannot_confirm_zero_flow() -> None:
    control = allocator()
    control.allocate(
        site(grid_target_w=1000),
        now=NOW,
        site_load_w=0,
        pv_power_w=0,
        deye=battery(BatteryId.DEYE, measured_power_w=-1000),
        solax=battery(BatteryId.SOLAX),
    )
    control.allocate(
        site(grid_target_w=0),
        now=NOW + timedelta(seconds=1),
        site_load_w=0,
        pv_power_w=1000,
        deye=battery(BatteryId.DEYE, measured_power_w=None),
        solax=battery(BatteryId.SOLAX),
    )
    still_waiting = control.allocate(
        site(grid_target_w=0),
        now=NOW + timedelta(seconds=20),
        site_load_w=0,
        pv_power_w=1000,
        deye=battery(BatteryId.DEYE, measured_power_w=None),
        solax=battery(BatteryId.SOLAX),
    )
    assert still_waiting.status is CommandStatus.BREAK_BEFORE_MAKE
    assert still_waiting.deye.action is BatteryAction.HOLD
    assert "WAIT_ZERO" in still_waiting.break_before_make_state
