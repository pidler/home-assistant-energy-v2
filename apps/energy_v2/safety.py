from __future__ import annotations

import math

from .models import TelemetrySnapshot, ValidationResult


def validate_telemetry(snapshot: TelemetrySnapshot) -> ValidationResult:
    reasons: list[str] = []
    if snapshot.deye_connected is not True:
        reasons.append("DEYE is not connected")
    deye_device_state = snapshot.deye_device_state.strip() if snapshot.deye_device_state is not None else None
    if deye_device_state != "Normal":
        reasons.append(f"DEYE device state is {snapshot.deye_device_state!r}, expected 'Normal'")
    if snapshot.deye_soc_pct is None:
        reasons.append("DEYE SOC is missing")
    elif not 0 <= snapshot.deye_soc_pct <= 100:
        reasons.append(f"DEYE SOC is outside 0-100 %: {snapshot.deye_soc_pct}")
    if snapshot.solax_soc_pct is None:
        reasons.append("SolaX SOC is missing")
    elif not 0 <= snapshot.solax_soc_pct <= 100:
        reasons.append(f"SolaX SOC is outside 0-100 %: {snapshot.solax_soc_pct}")
    if snapshot.deye_battery_power_w is None:
        reasons.append("DEYE battery power is missing")
    if snapshot.solax_battery_power_w is None:
        reasons.append("SolaX battery power is missing")
    if snapshot.solax_pv_power_w is None:
        reasons.append("SolaX PV power is missing")
    if snapshot.solax_measured_power_w is None:
        reasons.append("SolaX measured grid power is missing")
    if snapshot.solax_inverter_power_w is None:
        reasons.append("SolaX inverter power is missing")
    if snapshot.deye_inverter_power_w is None:
        reasons.append("DEYE inverter power is missing")
    if snapshot.sell_price is None:
        reasons.append("Sell price is missing")
    if snapshot.future_sell_rank is not None and snapshot.future_sell_rank < 1:
        reasons.append(f"Future sell rank is below 1: {snapshot.future_sell_rank}")
    for name, value in _numeric_values(snapshot):
        if value is not None and not math.isfinite(value):
            reasons.append(f"{name} is not finite")
    return ValidationResult(valid=not reasons, reasons=tuple(reasons))


def find_active_conflicts(
    states: dict[str, str],
    conflicting_entities: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(entity_id for entity_id in conflicting_entities if states.get(entity_id) == "on")


def find_missing_entities(
    states: dict[str, object],
    entity_ids: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(entity_id for entity_id in entity_ids if states.get(entity_id) is None)


def safe_to_enable(
    telemetry: ValidationResult,
    legacy_enabled: bool,
    current_system_enabled: bool,
    active_conflicts: tuple[str, ...],
    missing_required_entities: tuple[str, ...],
    missing_conflicting_automations: tuple[str, ...],
    missing_owned_actuators: tuple[str, ...],
    service_mode: bool,
) -> ValidationResult:
    reasons: list[str] = []
    if service_mode:
        reasons.append("Energy V2 service mode is active")
    if missing_required_entities:
        reasons.append("Required entities are missing: " + ", ".join(missing_required_entities))
    if missing_conflicting_automations:
        reasons.append("Conflicting automations are missing: " + ", ".join(missing_conflicting_automations))
    if missing_owned_actuators:
        reasons.append("Owned actuator entities are missing: " + ", ".join(missing_owned_actuators))
    if not telemetry.valid:
        reasons.extend(telemetry.reasons)
    if legacy_enabled:
        reasons.append("Legacy energy_trading system is enabled")
    if current_system_enabled:
        reasons.append("Current energy_trading new system is enabled")
    if active_conflicts:
        reasons.append("Conflicting automations are active: " + ", ".join(active_conflicts))
    return ValidationResult(valid=not reasons, reasons=tuple(reasons))


def _numeric_values(snapshot: TelemetrySnapshot) -> tuple[tuple[str, float | None], ...]:
    return (
        ("SolaX SOC", snapshot.solax_soc_pct),
        ("SolaX battery power", snapshot.solax_battery_power_w),
        ("SolaX PV power", snapshot.solax_pv_power_w),
        ("SolaX house load", snapshot.solax_house_load_w),
        ("SolaX measured grid power", snapshot.solax_measured_power_w),
        ("SolaX measured power L1", snapshot.solax_measured_power_l1_w),
        ("SolaX measured power L2", snapshot.solax_measured_power_l2_w),
        ("SolaX measured power L3", snapshot.solax_measured_power_l3_w),
        ("SolaX grid import", snapshot.solax_grid_import_w),
        ("SolaX grid export", snapshot.solax_grid_export_w),
        ("DEYE SOC", snapshot.deye_soc_pct),
        ("DEYE battery power", snapshot.deye_battery_power_w),
        ("DEYE raw battery power", snapshot.deye_battery_power_raw_w),
        ("DEYE grid power", snapshot.deye_grid_power_w),
        ("DEYE external power", snapshot.deye_external_power_w),
        ("Buy price", snapshot.buy_price),
        ("Sell price", snapshot.sell_price),
        ("Future sell rank", snapshot.future_sell_rank),
        ("SolaX inverter power", snapshot.solax_inverter_power_w),
        ("DEYE inverter power", snapshot.deye_inverter_power_w),
    )
