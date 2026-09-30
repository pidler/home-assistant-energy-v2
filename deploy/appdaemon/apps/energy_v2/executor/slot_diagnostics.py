"""Allowlisted Home Assistant helper projection for Phase 6 diagnostics."""

from __future__ import annotations

from datetime import datetime

from .physical_commands import PhysicalExecutionGates
from .slot_executor import SlotExecutionSession


def helper_projection(
    session: SlotExecutionSession,
    gates: PhysicalExecutionGates,
    *,
    hold_reason: str = "",
    last_successful_restore: datetime | None = None,
) -> dict[str, object]:
    values = session.diagnostics()
    projection: dict[str, object] = {
        "input_select.energy_v2_slot_executor_state": values["state"],
        "input_select.energy_v2_current_inverter_owner": values["owner"],
        "input_number.energy_v2_haeo_deye_target_kw": values["deye_target_kw"],
        "input_number.energy_v2_haeo_solax_target_kw": values["solax_target_kw"],
        "input_number.energy_v2_deye_target_energy_kwh": values["deye_target_kwh"],
        "input_number.energy_v2_deye_delivered_energy_kwh": values["deye_delivered_kwh"],
        "input_number.energy_v2_deye_remaining_energy_kwh": values["deye_remaining_kwh"],
        "input_number.energy_v2_solax_target_energy_kwh": values["solax_target_kwh"],
        "input_number.energy_v2_solax_delivered_energy_kwh": values["solax_delivered_kwh"],
        "input_number.energy_v2_solax_remaining_energy_kwh": values["solax_remaining_kwh"],
        "input_text.energy_v2_slot_hold_reason": hold_reason[:255],
        "input_text.energy_v2_slot_rollback_reason": str(values["rollback_reason"])[:255],
    }
    if last_successful_restore is not None:
        if last_successful_restore.tzinfo is None or last_successful_restore.utcoffset() is None:
            raise ValueError("last_successful_restore must be timezone-aware")
        projection["input_datetime.energy_v2_last_successful_restore"] = last_successful_restore.isoformat()
    # These two helpers are user-owned gates and are intentionally never included in write projection.
    assert "input_boolean.energy_v2_physical_execution_enabled" not in projection
    assert "input_select.energy_v2_execution_authority" not in projection
    _ = gates
    return projection
