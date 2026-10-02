"""Read-only conversion of verified Home Assistant telemetry into V3 evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any

from .models import SafetySnapshot

ENTITY_IDS = {
    "solax_soc": "sensor.solax_battery_capacity",
    "solax_battery_power": "sensor.solax_battery_power_charge",
    "solax_inverter_power": "sensor.solax_inverter_power",
    "solax_mode": "select.solax_charger_use_mode",
    "solax_run_mode": "sensor.solax_run_mode",
    "deye_soc": "sensor.deye_battery",
    "deye_battery_power": "sensor.battery_power_otoceny",
    "deye_battery_power_raw": "sensor.deye_battery_power",
    "deye_inverter_power": "sensor.deye_power",
    "deye_work_mode": "select.deye_work_mode",
    "deye_battery_fault": "binary_sensor.deye_battery_fault",
    "deye_battery_alarm": "binary_sensor.deye_battery_alarm",
    "deye_device_fault": "sensor.deye_device_fault",
    "pcc_power": "sensor.solax_measured_power",
}
TELEMETRY_ENTITY_IDS = tuple(ENTITY_IDS.values())


class TelemetryStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class TelemetryResult:
    snapshot: SafetySnapshot
    status: TelemetryStatus
    issues: tuple[str, ...]
    observed_modes: Mapping[str, str]


def safety_snapshot_from_states(
    states: Mapping[str, Mapping[str, Any] | None],
    *,
    now: datetime,
    solax_max_age: timedelta = timedelta(seconds=60),
    deye_max_age: timedelta = timedelta(seconds=30),
    normalized_power_tolerance_w: float = 2.0,
) -> TelemetryResult:
    """Build fail-closed safety evidence without inferring physical health."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    issues: list[str] = []
    required = {key: states.get(entity_id) for key, entity_id in ENTITY_IDS.items()}
    unavailable = [key for key, record in required.items() if not _available(record)]
    if unavailable:
        issues.extend(f"{key}:unavailable" for key in unavailable)

    solax_soc = _number(required["solax_soc"])
    solax_power = _number(required["solax_battery_power"])
    deye_soc = _number(required["deye_soc"])
    deye_power = _number(required["deye_battery_power"])
    deye_raw = _number(required["deye_battery_power_raw"])
    pcc_signed = _number(required["pcc_power"])
    for key, value in (
        ("solax_soc", solax_soc),
        ("solax_battery_power", solax_power),
        ("deye_soc", deye_soc),
        ("deye_battery_power", deye_power),
        ("deye_battery_power_raw", deye_raw),
        ("pcc_power", pcc_signed),
    ):
        if value is None and key not in unavailable:
            issues.append(f"{key}:invalid")
    if solax_soc is not None and not 0 <= solax_soc <= 100:
        issues.append("solax_soc:out_of_range")
        solax_soc = None
    if deye_soc is not None and not 0 <= deye_soc <= 100:
        issues.append("deye_soc:out_of_range")
        deye_soc = None
    if deye_power is not None and deye_raw is not None and abs(deye_power + deye_raw) > normalized_power_tolerance_w:
        issues.append("deye_battery_power:normalization_mismatch")
        deye_power = None

    solax_fresh = _fresh(required["solax_inverter_power"], now, solax_max_age) and _fresh(
        required["pcc_power"], now, solax_max_age
    )
    deye_fresh = _fresh(required["deye_battery_power_raw"], now, deye_max_age) and _fresh(
        required["deye_inverter_power"], now, deye_max_age
    )
    if not solax_fresh:
        issues.append("solax_telemetry:stale")
    if not deye_fresh:
        issues.append("deye_telemetry:stale")

    # No verified SolaX fault entity exists. None is deliberate fail-closed evidence.
    solax_fault: bool | None = None
    issues.append("solax_fault:evidence_missing")
    deye_fault = _deye_fault(required, issues)
    status = _status(issues)
    snapshot = SafetySnapshot(
        telemetry_fresh=solax_fresh and deye_fresh,
        solax_soc=solax_soc,
        deye_soc=deye_soc,
        pcc_export_w=max(pcc_signed, 0.0) if pcc_signed is not None else None,
        export_authorization=None,
        solax_available=all(
            _available(required[key])
            for key in ("solax_soc", "solax_battery_power", "solax_inverter_power", "solax_mode", "solax_run_mode")
        ),
        deye_available=all(_available(record) for key, record in required.items() if key.startswith("deye_")),
        solax_fault=solax_fault,
        deye_fault=deye_fault,
        measured_solax_battery_power_w=solax_power,
        measured_deye_battery_power_w=deye_power,
        writer_conflict=False,
    )
    return TelemetryResult(
        snapshot=snapshot,
        status=status,
        issues=tuple(dict.fromkeys(issues)),
        observed_modes={
            "solax_mode": _state(required["solax_mode"]),
            "solax_run_mode": _state(required["solax_run_mode"]),
            "deye_work_mode": _state(required["deye_work_mode"]),
        },
    )


def _deye_fault(required: Mapping[str, Mapping[str, Any] | None], issues: list[str]) -> bool | None:
    values = (
        _state(required["deye_battery_fault"]),
        _state(required["deye_battery_alarm"]),
        _state(required["deye_device_fault"]),
    )
    if (
        values[0] not in {"on", "off"}
        or values[1] not in {"on", "off"}
        or values[2]
        in {
            "unknown",
            "unavailable",
            "",
        }
    ):
        issues.append("deye_fault:evidence_invalid")
        return None
    return values[0] == "on" or values[1] == "on" or values[2] != "OK"


def _status(issues: list[str]) -> TelemetryStatus:
    if any(issue.endswith(":unavailable") for issue in issues):
        return TelemetryStatus.MISSING
    if any(issue.endswith(":invalid") or "mismatch" in issue or "out_of_range" in issue for issue in issues):
        return TelemetryStatus.INVALID
    if any(issue.endswith(":stale") for issue in issues):
        return TelemetryStatus.STALE
    return TelemetryStatus.FRESH


def _number(record: Mapping[str, Any] | None) -> float | None:
    if not record:
        return None
    try:
        value = float(record.get("state"))
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _state(record: Mapping[str, Any] | None) -> str:
    return str(record.get("state", "unavailable")) if record else "unavailable"


def _available(record: Mapping[str, Any] | None) -> bool:
    return record is not None and _state(record) not in {"unknown", "unavailable", "none", ""}


def _fresh(record: Mapping[str, Any] | None, now: datetime, max_age: timedelta) -> bool:
    if not _available(record):
        return False
    raw = record.get("last_updated") if record else None
    if not isinstance(raw, str):
        return False
    try:
        updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None or updated.utcoffset() is None:
        return False
    age = now - updated
    return timedelta(0) <= age <= max_age
