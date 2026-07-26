from __future__ import annotations

from .models import TelemetrySnapshot, ValidationResult


def validate_telemetry(snapshot: TelemetrySnapshot) -> ValidationResult:
    reasons: list[str] = []
    if snapshot.deye_connected is not True:
        reasons.append("DEYE is not connected")
    if snapshot.deye_device_state != "Normal":
        reasons.append(f"DEYE device state is {snapshot.deye_device_state!r}, expected 'Normal'")
    if snapshot.deye_soc_pct is None:
        reasons.append("DEYE SOC is missing")
    if snapshot.solax_soc_pct is None:
        reasons.append("SolaX SOC is missing")
    if snapshot.deye_battery_power_w is None:
        reasons.append("DEYE battery power is missing")
    if snapshot.solax_battery_power_w is None:
        reasons.append("SolaX battery power is missing")
    if snapshot.solax_pv_power_w is None:
        reasons.append("SolaX PV power is missing")
    if snapshot.sell_price is None:
        reasons.append("Sell price is missing")
    return ValidationResult(valid=not reasons, reasons=tuple(reasons))


def find_active_conflicts(
    states: dict[str, str],
    conflicting_entities: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(entity_id for entity_id in conflicting_entities if states.get(entity_id) == "on")


def safe_to_enable(
    telemetry: ValidationResult,
    legacy_enabled: bool,
    current_system_enabled: bool,
    active_conflicts: tuple[str, ...],
    service_mode: bool,
) -> ValidationResult:
    reasons: list[str] = []
    if service_mode:
        reasons.append("Energy V2 service mode is active")
    if not telemetry.valid:
        reasons.extend(telemetry.reasons)
    if legacy_enabled:
        reasons.append("Legacy energy_trading system is enabled")
    if current_system_enabled:
        reasons.append("Current energy_trading new system is enabled")
    if active_conflicts:
        reasons.append("Conflicting automations are active: " + ", ".join(active_conflicts))
    return ValidationResult(valid=not reasons, reasons=tuple(reasons))

