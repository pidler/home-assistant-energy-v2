"""Observer/publisher integration for Phase 5B; it never executes proposals."""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Protocol

from .deye_state import DeyeOperatingState, DeyeStateAdapter, DeyeStateConfig
from .executor.engine import ExecutorInputs, ExecutorResult, evaluate_executor
from .executor.enums import (
    ExecutionState,
    FailureReason,
    PhysicalRole,
    PlannerIntentType,
    SafetyAction,
    TransitionPhase,
)
from .executor.models import CapabilitySnapshot, ExecutionContext, PhysicalRoleVector, PlannerIntent
from .executor.reconciliation import PersistedExecutorMetadata
from .executor.safety import (
    NumericTelemetry,
    PhysicalStateSnapshot,
    ReadinessEvidence,
    RollingExportEvidence,
    SafetyConfig,
    SafetyDecision,
)
from .executor.transition import TransitionConfig
from .executor_state_store import ExecutorStateStore, StoredShadowState

OUTPUTS = {
    "state": "sensor.energy_v2_phase5b_executor_state",
    "reason": "sensor.energy_v2_phase5b_executor_reason",
    "proposal": "sensor.energy_v2_phase5b_executor_proposal",
    "epoch": "sensor.energy_v2_phase5b_executor_epoch",
}
_MAX_TEXT = 240
_PLANNER_CONFIDENCE = {"LOW", "MEDIUM", "HIGH"}
_PLANNER_FIELDS = {
    "schema_version",
    "publication_id",
    "plan_id",
    "intent_id",
    "intent_type",
    "target_w",
    "slot_start",
    "slot_end",
    "deadline",
    "created_at",
    "hypothetical",
    "degraded",
    "confidence",
    "reason_code",
    "reason",
}
_CAPABILITY_FIELDS = (
    "solax_mode1_dispatch_verified",
    "solax_mode5_hold_verified",
    "solax_timeout_verified",
    "deye_start_verified",
    "deye_hold_verified",
    "deye_bounded_export_verified",
)
DEFAULT_ENTITY_MAP = {
    "solax_soc": "sensor.solax_battery_capacity",
    "solax_battery_power": "sensor.solax_battery_power_charge",
    "solax_inverter_power": "sensor.solax_inverter_power",
    "solax_pv_power": "sensor.solax_pv_power_total",
    "pcc": "sensor.solax_measured_power",
    "solax_charger_mode": "select.solax_charger_use_mode",
    "solax_manual_mode": "select.solax_manual_mode_select",
    "solax_operating_state": "sensor.solax_run_mode",
    "solax_remote_control": "sensor.solax_modbus_power_control",
    # No live independent SolaX fault source is verified. None is explicit and fail-closed.
    "solax_fault": None,
    "deye_soc": "sensor.deye_battery",
    # Raw DEYE telemetry: positive=discharge, negative=charge; normalized below.
    "deye_battery_power": "sensor.deye_battery_power",
    "deye_inverter_power": "sensor.deye_power",
    "deye_device_state": "sensor.deye_device_state",
    "deye_device_fault": "sensor.deye_device_fault",
    "deye_connection": "binary_sensor.deye_connection",
    "deye_switch": "switch.deye",
    "deye_work_mode": "select.deye_work_mode",
    "deye_time_of_use": "select.deye_time_of_use",
    "deye_export_surplus": "switch.deye_export_surplus",
    "deye_grid_charging": "switch.deye_battery_grid_charging",
    "execution_enabled": "input_boolean.energy_v2_control_shadow_enabled",
    "writer_conflicts": "input_text.energy_v2_active_conflicts",
    "rolling_export_w": "input_number.energy_v2_rolling_15min_export_w",
    "rolling_export_covered_s": "input_number.energy_v2_export_window_covered_s",
    "planner_intent": "sensor.energy_v2_phase5a_executor_intent",
    "planner_status": "sensor.energy_v2_phase5a_status",
}


class StateReader(Protocol):
    def get_state(self, entity_id: str, **kwargs: Any) -> Any: ...


class DiagnosticPublisher(Protocol):
    def set_state(self, entity_id: str, *, state: str, attributes: dict[str, object]) -> None: ...


class AdapterInputError(ValueError):
    """Bounded fail-closed adapter error raised before authoritative inputs exist."""


@dataclass(frozen=True)
class RuntimeConfig:
    persistence_path: str
    entity_map: Mapping[str, str | None] | None = None
    cadence_s: float = 10.0
    shadow_only: bool = True
    output_entities: Mapping[str, str] | None = None
    telemetry_max_age_s: float = 60.0
    control_max_age_s: float = 30.0
    planner_max_age_s: float = 60.0
    allow_last_updated_fallback: bool = False
    capabilities: Mapping[str, bool] | None = None
    capability_verified_at: str | None = None

    def __post_init__(self) -> None:
        positive = (self.cadence_s, self.telemetry_max_age_s, self.control_max_age_s, self.planner_max_age_s)
        if not self.shadow_only or not all(isfinite(value) and value > 0 for value in positive):
            raise ValueError("runtime must remain shadow-only with positive cadence")
        if (
            not isinstance(self.persistence_path, str)
            or not self.persistence_path.strip()
            or len(self.persistence_path) > 1024
            or "\x00" in self.persistence_path
        ):
            raise ValueError("persistence_path must be bounded nonempty text")
        if not isinstance(self.allow_last_updated_fallback, bool):
            raise ValueError("allow_last_updated_fallback must be bool")
        outputs = OUTPUTS if self.output_entities is None else self.output_entities
        if not isinstance(outputs, Mapping) or dict(outputs) != OUTPUTS:
            raise ValueError("publication allowlist must contain only Phase 5B diagnostic sensors")
        entities = self.entity_map
        if not isinstance(entities, Mapping) or set(entities) != set(DEFAULT_ENTITY_MAP):
            raise ValueError("phase5b_shadow.entity_map must contain the complete read-only input contract")
        if not all(
            (key == "solax_fault" and entity is None) or isinstance(entity, str) and bool(entity)
            for key, entity in entities.items()
        ):
            raise ValueError("read-only entity IDs must be nonempty strings; solax_fault may be explicit None")
        configured_entities = tuple(entity for entity in entities.values() if entity is not None)
        if len(set(configured_entities)) != len(configured_entities):
            raise ValueError("read-only entity IDs must be unique")
        capabilities = dict(self.capabilities or {})
        if not set(capabilities) <= set(_CAPABILITY_FIELDS) or not all(
            isinstance(value, bool) for value in capabilities.values()
        ):
            raise ValueError("capabilities must contain only strict verified capability flags")
        capabilities = {name: capabilities.get(name, False) for name in _CAPABILITY_FIELDS}
        verified = _config_timestamp(self.capability_verified_at)
        if self.capability_verified_at is not None and verified is None:
            raise ValueError("capability_verified_at must be timezone-aware UTC-compatible text")
        if any(capabilities.values()) and verified is None:
            raise ValueError("positive capabilities require capability_verified_at")
        object.__setattr__(self, "output_entities", dict(outputs))
        object.__setattr__(self, "entity_map", dict(entities))
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True)
class TimestampEvidence:
    source: datetime | None
    report: datetime | None
    updated: datetime | None
    changed: datetime | None
    provenance: str

    @property
    def freshness(self) -> datetime | None:
        if self.provenance.startswith("INVALID"):
            return None
        return self.source or self.report or self.updated


def _config_timestamp(raw: object) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def utc_timestamp(raw: object, now: datetime | None = None) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, str | int | float | datetime):
        return None
    try:
        if isinstance(raw, datetime):
            value = raw
        elif isinstance(raw, int | float):
            value = datetime.fromtimestamp(float(raw), UTC)
        else:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    value = value.astimezone(UTC)
    return None if now is not None and value > now else value


def timestamp_evidence(raw: object, now: datetime, *, allow_updated: bool = False) -> TimestampEvidence:
    if not isinstance(raw, dict):
        return TimestampEvidence(None, None, None, None, "MISSING")
    attributes = raw.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    source_key = next((key for key in ("source_timestamp", "timestamp", "last_seen") if key in attributes), None)
    source = utc_timestamp(attributes.get(source_key), now) if source_key else None
    report_present = "last_reported" in raw
    report = utc_timestamp(raw.get("last_reported"), now) if report_present else None
    updated = utc_timestamp(raw.get("last_updated"), now) if allow_updated else None
    changed = utc_timestamp(raw.get("last_changed"), now)
    if source_key is not None:
        provenance = f"attributes.{source_key}" if source is not None else "INVALID_SOURCE"
        if source is None:
            return TimestampEvidence(None, report, updated, changed, provenance)
    elif report_present:
        provenance = "last_reported" if report is not None else "INVALID_LAST_REPORTED"
        if report is None:
            return TimestampEvidence(None, None, updated, changed, provenance)
    elif allow_updated:
        provenance = "last_updated" if updated is not None else "INVALID_LAST_UPDATED"
    else:
        provenance = "MISSING"
    return TimestampEvidence(source, report, updated, changed, provenance)


def telemetry(raw: object, now: datetime, *, allow_updated: bool = False) -> tuple[NumericTelemetry, TimestampEvidence]:
    evidence = timestamp_evidence(raw, now, allow_updated=allow_updated)
    if not isinstance(raw, dict):
        return NumericTelemetry(None, receipt_timestamp=now, coherent=False), evidence
    try:
        value = float(raw.get("state"))
        if not isfinite(value):
            value = None
    except (TypeError, ValueError):
        value = None
    valid_stamp = evidence.freshness
    coherent = value is not None and valid_stamp is not None and not evidence.provenance.startswith("INVALID")
    sample = NumericTelemetry(
        value,
        source_timestamp=evidence.source,
        report_timestamp=evidence.report or (evidence.updated if evidence.source is None else None),
        receipt_timestamp=now,
        coherent=coherent,
    )
    return sample, evidence


def _state(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("state")
    if not isinstance(value, str) or value.strip().lower() in {"", "unknown", "unavailable", "none", "null"}:
        return None
    return value.strip()


def _boolean(raw: object) -> bool | None:
    value = _state(raw)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"on", "true", "yes", "1", "enabled", "normal", "ok", "connected"}:
        return True
    if normalized in {"off", "false", "no", "0", "disabled", "clear", "disconnected"}:
        return False
    return None


def _fresh(at: datetime | None, now: datetime, max_age_s: float) -> bool:
    return at is not None and 0 <= (now - at).total_seconds() <= max_age_s


def _bounded(value: object, limit: int = _MAX_TEXT) -> str:
    return str(value).replace("\n", " ")[:limit]


def _new_epoch(previous: int | None) -> int:
    for _attempt in range(8):
        candidate = secrets.randbits(63)
        if previous is None or candidate != previous:
            return candidate
    assert previous is not None
    return (previous + 1) % (1 << 63)


def decode_planner_intent(
    raw: object,
    now: datetime,
    *,
    max_age_s: float,
    allow_updated: bool = False,
) -> PlannerIntent | None:
    if not isinstance(raw, dict):
        raise ValueError("PLANNER_INTENT_MISSING")
    evidence = timestamp_evidence(raw, now, allow_updated=allow_updated)
    if not _fresh(evidence.freshness, now, max_age_s):
        raise ValueError("PLANNER_INTENT_STALE")
    attributes = raw.get("attributes")
    schema = attributes.get("schema_version") if isinstance(attributes, dict) else None
    if not isinstance(attributes, dict) or isinstance(schema, bool) or schema != 1:
        raise ValueError("PLANNER_INTENT_SCHEMA")
    if set(attributes) - _PLANNER_FIELDS:
        raise ValueError("PLANNER_INTENT_ATTRIBUTES")
    kind_raw = attributes.get("intent_type", raw.get("state"))
    published_value = raw.get("state")
    published_kind = published_value.strip() if isinstance(published_value, str) and published_value.strip() else None
    if not isinstance(kind_raw, str):
        raise ValueError("PLANNER_INTENT_UNSUPPORTED")
    normalized_kind = kind_raw.upper()
    if normalized_kind != "NONE":
        try:
            kind = PlannerIntentType(normalized_kind)
        except ValueError as error:
            raise ValueError("PLANNER_INTENT_UNSUPPORTED") from error
    else:
        kind = None
    if published_kind is None or published_kind.upper() != normalized_kind:
        raise ValueError("PLANNER_INTENT_INCOHERENT")
    if attributes.get("degraded") is not False:
        raise ValueError("PLANNER_INTENT_DEGRADED")
    if attributes.get("hypothetical") is not False:
        raise ValueError("PLANNER_INTENT_HYPOTHETICAL")
    confidence = attributes.get("confidence")
    if not isinstance(confidence, str) or confidence not in _PLANNER_CONFIDENCE:
        raise ValueError("PLANNER_INTENT_CONFIDENCE")
    publication_id = attributes.get("publication_id")
    plan_id = attributes.get("plan_id")
    intent_id = attributes.get("intent_id")
    if not all(
        isinstance(value, str) and value.strip() == value and 0 < len(value) <= 160
        for value in (publication_id, plan_id, intent_id)
    ):
        raise ValueError("PLANNER_INTENT_IDS")
    timestamps = {
        name: _config_timestamp(attributes.get(name)) for name in ("slot_start", "slot_end", "deadline", "created_at")
    }
    if any(value is None for value in timestamps.values()):
        raise ValueError("PLANNER_INTENT_TIMESTAMP")
    start, end, deadline, created = (timestamps[name] for name in ("slot_start", "slot_end", "deadline", "created_at"))
    assert start is not None and end is not None and deadline is not None and created is not None
    if created > now:
        raise ValueError("PLANNER_INTENT_TIMESTAMP")
    if end <= start or not start <= deadline <= end:
        raise ValueError("PLANNER_INTENT_TIMESTAMP")
    if not start <= now < end or now >= deadline:
        raise ValueError("PLANNER_INTENT_EXPIRED")
    reason_code = attributes.get("reason_code")
    reason = attributes.get("reason")
    if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 80:
        raise ValueError("PLANNER_INTENT_REASON")
    if not isinstance(reason, str) or not reason or len(reason) > 160:
        raise ValueError("PLANNER_INTENT_REASON")
    target_raw = attributes.get("target_w")
    if normalized_kind == "NONE":
        if target_raw is not None:
            raise ValueError("PLANNER_INTENT_TARGET")
        return None
    assert kind is not None
    if kind is PlannerIntentType.NORMAL:
        if target_raw is not None:
            raise ValueError("PLANNER_INTENT_TARGET")
        target: float | None = None
    else:
        try:
            if isinstance(target_raw, bool):
                raise TypeError
            target = float(target_raw)
        except (TypeError, ValueError) as error:
            raise ValueError("PLANNER_INTENT_TARGET") from error
        if not isfinite(target) or target <= 0:
            raise ValueError("PLANNER_INTENT_TARGET")
    try:
        return PlannerIntent(
            plan_id=plan_id,
            intent_id=intent_id,
            slot_start=start,
            slot_end=end,
            intent_type=kind,
            target_w=target,
            deadline=deadline,
            hypothetical=False,
            created_at=created,
            reason_code=reason_code,
            reason=reason,
            confidence=confidence,
        )
    except ValueError as error:
        raise ValueError("PLANNER_INTENT_CONTRACT") from error


def decode_authoritative_planner_intent(
    raw: object,
    status_raw: object,
    now: datetime,
    *,
    max_age_s: float,
    allow_updated: bool = False,
) -> PlannerIntent | None:
    """Decode only an explicitly committed, generation-coherent Phase 5A publication."""
    if not isinstance(status_raw, dict):
        raise ValueError("PLANNER_STATUS_MISSING")
    status_evidence = timestamp_evidence(status_raw, now, allow_updated=allow_updated)
    if status_evidence.freshness is None:
        raise ValueError("PLANNER_STATUS_MISSING")
    if not _fresh(status_evidence.freshness, now, max_age_s):
        raise ValueError("PLANNER_STATUS_STALE")

    status = _state(status_raw)
    if status is None:
        raise ValueError("PLANNER_STATUS_INVALID")
    normalized_status = status.upper()
    if normalized_status == "UPDATING":
        raise ValueError("PLANNER_STATUS_UPDATING")
    if normalized_status in {"DEGRADED", "LAST_KNOWN", "ERROR", "FAILED"}:
        raise ValueError("PLANNER_STATUS_DEGRADED")
    if normalized_status != "ADVISORY":
        raise ValueError("PLANNER_STATUS_INVALID")

    status_attributes = status_raw.get("attributes")
    if not isinstance(status_attributes, dict):
        raise ValueError("PLANNER_STATUS_INVALID")
    if status_attributes.get("valid") is not True or status_attributes.get("committed") is not True:
        raise ValueError("PLANNER_STATUS_INVALID")
    committed_at = _config_timestamp(status_attributes.get("committed_at"))
    if committed_at is None:
        raise ValueError("PLANNER_STATUS_INVALID")
    if not _fresh(committed_at, now, max_age_s):
        raise ValueError("PLANNER_STATUS_STALE")

    intent_attributes = raw.get("attributes") if isinstance(raw, dict) else None
    if not isinstance(intent_attributes, dict):
        raise ValueError("PLANNER_PUBLICATION_INCOHERENT")
    status_identity = tuple(status_attributes.get(name) for name in ("publication_id", "plan_id", "intent_id"))
    intent_identity = tuple(intent_attributes.get(name) for name in ("publication_id", "plan_id", "intent_id"))
    if not all(
        isinstance(value, str) and value.strip() == value and 0 < len(value) <= 160
        for value in (*status_identity, *intent_identity)
    ):
        raise ValueError("PLANNER_PUBLICATION_INCOHERENT")
    if status_identity != intent_identity:
        raise ValueError("PLANNER_PUBLICATION_INCOHERENT")

    intent = decode_planner_intent(raw, now, max_age_s=max_age_s, allow_updated=allow_updated)
    intent_evidence = timestamp_evidence(raw, now, allow_updated=allow_updated)
    assert intent_evidence.freshness is not None
    if not intent_evidence.freshness <= committed_at <= status_evidence.freshness:
        raise ValueError("PLANNER_PUBLICATION_INCOHERENT")
    return intent


class HomeAssistantExecutorAdapter:
    """Read-only mapping; absent control facts deliberately fail closed."""

    def __init__(self, reader: StateReader, config: RuntimeConfig) -> None:
        self.reader = reader
        self.config = config
        self.entities = dict(config.entity_map)
        self.deye = DeyeStateAdapter(
            DeyeStateConfig(
                feedback_max_age_s=config.control_max_age_s,
                maximum_skew_s=config.control_max_age_s,
            )
        )
        self.last_input_health: dict[str, object] = {}
        self.last_site_load_w: float | None = None
        self.last_planner_status = "NOT_EVALUATED"
        self.last_deye_operating_state = "UNKNOWN"

    def _all(self, key: str) -> dict[str, object]:
        entity = self.entities[key]
        if entity is None:
            return {"state": "unavailable", "attributes": {}}
        raw = self.reader.get_state(entity, attribute="all")
        return raw if isinstance(raw, dict) else {"state": "unavailable", "attributes": {}}

    def _record(self, key: str, raw: object, now: datetime, max_age_s: float) -> TimestampEvidence:
        evidence = timestamp_evidence(raw, now, allow_updated=self.config.allow_last_updated_fallback)
        available = isinstance(raw, dict) and _state(raw) is not None
        fresh = _fresh(evidence.freshness, now, max_age_s)
        if not available:
            status = "UNAVAILABLE"
        elif evidence.provenance.startswith("INVALID"):
            status = "INVALID"
        elif evidence.freshness is None:
            status = "UNAVAILABLE"
        elif not fresh:
            status = "STALE"
        else:
            status = "HEALTHY"
        self.last_input_health[key] = {
            "entity": self.entities[key],
            "provenance": evidence.provenance,
            "timestamp": evidence.freshness.isoformat() if evidence.freshness else None,
            "fresh": fresh,
            "status": status,
        }
        return evidence

    def _set_health(self, key: str, status: str, reason: str | None = None) -> None:
        current = self.last_input_health.get(key)
        details = dict(current) if isinstance(current, dict) else {}
        details["status"] = status
        if reason is not None:
            details["reason"] = _bounded(reason, 120)
        self.last_input_health[key] = details

    def _numeric(self, key: str, now: datetime, *, scale: float = 1.0) -> NumericTelemetry:
        raw = self._all(key)
        sample, evidence = telemetry(raw, now, allow_updated=self.config.allow_last_updated_fallback)
        if sample.value is not None and scale != 1.0:
            sample = NumericTelemetry(
                sample.value * scale,
                source_timestamp=sample.source_timestamp,
                report_timestamp=sample.report_timestamp,
                receipt_timestamp=sample.receipt_timestamp,
                coherent=sample.coherent,
            )
        self._record(key, raw, now, self.config.telemetry_max_age_s)
        if scale == -1.0:
            self.last_input_health[key]["normalization"] = "raw positive discharge -> normalized negative discharge"
        if sample.value is None and self.last_input_health[key]["status"] == "HEALTHY":
            self._set_health(key, "INVALID", "NON_NUMERIC_OR_NONFINITE")
        elif not sample.coherent and self.last_input_health[key]["status"] == "HEALTHY":
            self._set_health(key, "INCOHERENT", "UNTRUSTWORTHY_TELEMETRY")
        if not _fresh(evidence.freshness, now, self.config.telemetry_max_age_s):
            return NumericTelemetry(
                sample.value,
                source_timestamp=sample.source_timestamp,
                report_timestamp=sample.report_timestamp,
                receipt_timestamp=now,
                coherent=False,
            )
        return sample

    def _control(self, key: str, now: datetime) -> tuple[dict[str, object], str | None, TimestampEvidence]:
        raw = self._all(key)
        evidence = self._record(key, raw, now, self.config.control_max_age_s)
        value = _state(raw) if _fresh(evidence.freshness, now, self.config.control_max_age_s) else None
        if value is None and self.last_input_health[key]["status"] == "HEALTHY":
            self._set_health(key, "INVALID", "CONTROL_STATE_UNRECOGNIZED")
        return raw, value, evidence

    def _capabilities(self, now: datetime) -> CapabilitySnapshot:
        flags = dict(self.config.capabilities)
        verified = _config_timestamp(self.config.capability_verified_at)
        expired = any(flags.values()) and not _fresh(verified, now, self.config.control_max_age_s)
        if expired:
            flags = {name: False for name in _CAPABILITY_FIELDS}
            verified = None
        self.last_input_health["capabilities"] = {
            "verified_at": verified.isoformat() if verified else None,
            "positive": tuple(name for name, value in flags.items() if value),
            "status": "DEGRADED" if expired else "HEALTHY",
            "reason": "CAPABILITY_EVIDENCE_STALE" if expired else None,
        }
        return CapabilitySnapshot(**flags, observed_at=verified or datetime.min.replace(tzinfo=UTC))

    @staticmethod
    def _solax_operating_available(value: str | None) -> bool | None:
        # Live-verified vocabulary. This proves operation, never absence of faults.
        return True if (value or "").strip().lower() == "normal mode" else None

    @staticmethod
    def _solax_remote_active(value: str | None) -> bool | None:
        normalized = (value or "").strip().lower()
        if normalized in {"disabled", "off", "inactive"}:
            return False
        active = {
            "enabled power control",
            "enabled grid control",
            "enabled battery control",
            "enabled self use",
            "enabled feedin priority",
            "enabled no discharge",
            "power control",
            "grid control",
            "battery control",
            "self use",
            "feedin priority",
            "no discharge",
        }
        return True if normalized in active else None

    @staticmethod
    def _solax_role(charger: str | None, manual: str | None, remote: bool | None) -> PhysicalRole | None:
        charger_value = (charger or "").lower()
        manual_value = (manual or "").lower()
        # Active Modbus control requires separate target/readback evidence; native/manual
        # selects alone cannot prove its physical role.
        if remote is not False:
            return None
        if charger_value in {"self use", "self use mode"}:
            return PhysicalRole.NATIVE
        if "manual" in charger_value:
            if "force charge" in manual_value:
                return PhysicalRole.CHARGE
            if "force discharge" in manual_value:
                return PhysicalRole.DISCHARGE
            if "stop charge" in manual_value and "discharge" in manual_value:
                return PhysicalRole.HOLD
        return None

    @staticmethod
    def _deye_role(
        operating: DeyeOperatingState,
        grid_charging: bool | None,
        export_surplus: bool | None,
        time_of_use: str | None,
    ) -> PhysicalRole | None:
        if operating is DeyeOperatingState.INTENTIONAL_OFF:
            return PhysicalRole.OFF
        if operating is not DeyeOperatingState.READY or grid_charging is None or export_surplus is None:
            return None
        if grid_charging and export_surplus:
            return None
        if grid_charging:
            return PhysicalRole.CHARGE
        if export_surplus:
            return PhysicalRole.DISCHARGE
        if time_of_use is not None and time_of_use.lower() in {"disabled", "off"}:
            return PhysicalRole.NATIVE
        return None

    def build(self, now: datetime, epoch: int, *, reconciliation_required: bool) -> ExecutorInputs:
        self.last_input_health = {}
        errors: list[str] = []
        samples = {
            key: self._numeric(key, now)
            for key in (
                "solax_soc",
                "solax_battery_power",
                "solax_inverter_power",
                "solax_pv_power",
                "pcc",
                "deye_soc",
                "deye_inverter_power",
                "rolling_export_w",
                "rolling_export_covered_s",
            )
        }
        samples["deye_battery_power"] = self._numeric("deye_battery_power", now, scale=-1.0)
        for key, sample in samples.items():
            if sample.value is None or not sample.coherent:
                errors.append(f"{key}:INVALID_OR_STALE")
        if samples["solax_soc"].value is not None and not 0 <= samples["solax_soc"].value <= 100:
            errors.append("solax_soc:OUT_OF_RANGE")
            self._set_health("solax_soc", "INVALID", "OUT_OF_RANGE")
        if samples["deye_soc"].value is not None and not 0 <= samples["deye_soc"].value <= 100:
            errors.append("deye_soc:OUT_OF_RANGE")
            self._set_health("deye_soc", "INVALID", "OUT_OF_RANGE")
        load_inputs = tuple(samples[key] for key in ("solax_inverter_power", "deye_inverter_power", "pcc"))
        if all(sample.value is not None and sample.coherent for sample in load_inputs):
            self.last_site_load_w = (
                float(samples["solax_inverter_power"].value)
                + float(samples["deye_inverter_power"].value)
                - float(samples["pcc"].value)
            )
            if not isfinite(self.last_site_load_w):
                self.last_site_load_w = None
        else:
            self.last_site_load_w = None
        if self.last_site_load_w is None:
            errors.append("whole_site_load:INVALID")
        self.last_input_health["whole_site_load"] = {
            "formula": "solax_inverter_power + deye_power - solax_measured_power",
            "value_w": self.last_site_load_w,
            "status": "HEALTHY" if self.last_site_load_w is not None else "INVALID",
            "reason": None if self.last_site_load_w is not None else "OPERAND_INVALID_OR_STALE",
        }

        _solax_charger_raw, solax_charger, solax_charger_at = self._control("solax_charger_mode", now)
        _manual_raw, solax_manual, solax_manual_at = self._control("solax_manual_mode", now)
        _operating_raw, solax_operating, solax_operating_at = self._control("solax_operating_state", now)
        _remote_raw, solax_remote, solax_remote_at = self._control("solax_remote_control", now)
        _fault_raw, solax_fault_state, solax_fault_at = self._control("solax_fault", now)
        available = self._solax_operating_available(solax_operating)
        fault = _boolean({"state": solax_fault_state})
        remote_active = self._solax_remote_active(solax_remote)
        if available is None:
            self._set_health("solax_operating_state", "UNPROVEN", "UNKNOWN_OR_STALE_RUN_MODE")
        if fault is None:
            self._set_health("solax_fault", "UNPROVEN", "NO_VERIFIED_FAULT_SOURCE")
        if remote_active is None:
            self._set_health("solax_remote_control", "UNPROVEN", "UNKNOWN_OR_STALE_MODBUS_CONTROL")
        if available is None or fault is None or remote_active is None:
            errors.append("solax_control:UNPROVEN")
        solax_role = self._solax_role(solax_charger, solax_manual, remote_active)
        if solax_role is None:
            errors.append("solax_ownership:UNPROVEN")
        solax_control_times = (
            solax_charger_at.freshness,
            solax_manual_at.freshness,
            solax_operating_at.freshness,
            solax_remote_at.freshness,
            solax_fault_at.freshness,
        )
        solax_control_at = min((value for value in solax_control_times if value is not None), default=None)
        solax_control_fresh = all(_fresh(value, now, self.config.control_max_age_s) for value in solax_control_times)

        deye_raw = {
            name: self._all(key)
            for name, key in (
                ("switch", "deye_switch"),
                ("state", "deye_device_state"),
                ("fault", "deye_device_fault"),
                ("power", "deye_inverter_power"),
                ("connection", "deye_connection"),
            )
        }
        deye_health_keys = {
            "switch": "deye_switch",
            "state": "deye_device_state",
            "fault": "deye_device_fault",
            "power": "deye_inverter_power",
            "connection": "deye_connection",
        }
        deye_evidence = {
            name: self._record(deye_health_keys[name], raw, now, self.config.control_max_age_s)
            for name, raw in deye_raw.items()
        }
        if all(_fresh(item.freshness, now, self.config.control_max_age_s) for item in deye_evidence.values()):
            deye_operating = self.deye.evaluate(deye_raw, now).state
        else:
            deye_operating = DeyeOperatingState.UNAVAILABLE
            errors.append("deye_state:INVALID_OR_STALE")
        self.last_deye_operating_state = deye_operating.value
        _work_raw, _work_mode, deye_work_at = self._control("deye_work_mode", now)
        _tou_raw, time_of_use, deye_tou_at = self._control("deye_time_of_use", now)
        _export_raw, _export, deye_export_at = self._control("deye_export_surplus", now)
        _charge_raw, _charge, deye_charge_at = self._control("deye_grid_charging", now)
        export_surplus = _boolean({"state": _export})
        grid_charging = _boolean({"state": _charge})
        deye_role = self._deye_role(deye_operating, grid_charging, export_surplus, time_of_use)
        if deye_role is None:
            errors.append("deye_ownership:UNPROVEN")
        deye_control_times = (
            deye_evidence["switch"].freshness,
            deye_evidence["state"].freshness,
            deye_evidence["fault"].freshness,
            deye_evidence["connection"].freshness,
            deye_work_at.freshness,
            deye_tou_at.freshness,
            deye_export_at.freshness,
            deye_charge_at.freshness,
        )
        deye_control_at = min((value for value in deye_control_times if value is not None), default=None)
        deye_control_fresh = all(_fresh(value, now, self.config.control_max_age_s) for value in deye_control_times)
        solax_ownership_proven = (
            available is True
            and fault is False
            and remote_active is not None
            and solax_role is not None
            and solax_control_fresh
        )
        deye_ownership_proven = (
            deye_operating is not DeyeOperatingState.UNAVAILABLE and deye_role is not None and deye_control_fresh
        )
        ownership = (
            PhysicalRoleVector(solax_role, deye_role)
            if solax_ownership_proven and deye_ownership_proven and solax_role is not None and deye_role is not None
            else None
        )
        self.last_input_health["physical_ownership"] = {
            "status": "HEALTHY" if ownership is not None else "INCOHERENT",
            "solax": solax_role.value if solax_ownership_proven and solax_role is not None else "UNKNOWN",
            "deye": deye_role.value if deye_ownership_proven and deye_role is not None else "UNKNOWN",
            "solax_remote_control_active": remote_active if solax_ownership_proven else "UNKNOWN",
            "reason": None if ownership is not None else "OWNERSHIP_UNPROVEN",
        }

        _enabled_raw, enabled_state, _enabled_at = self._control("execution_enabled", now)
        execution_enabled = _boolean({"state": enabled_state})
        if execution_enabled is None:
            execution_enabled = False
            errors.append("execution_enabled:UNPROVEN")
        _conflicts_raw, conflicts_raw, _conflicts_at = self._control("writer_conflicts", now)
        conflicts = ()
        conflicts_proven = False
        raw_conflict_state = _conflicts_raw.get("state") if isinstance(_conflicts_raw, dict) else None
        normalized_conflicts = raw_conflict_state.strip().upper() if isinstance(raw_conflict_state, str) else None
        conflict_fresh = _fresh(_conflicts_at.freshness, now, self.config.control_max_age_s)
        if conflict_fresh and normalized_conflicts in {"NONE", "OK", "CLEAR"}:
            conflicts_proven = True
            self._set_health("writer_conflicts", "HEALTHY")
        elif normalized_conflicts in {"NONE", "OK", "CLEAR"}:
            if _conflicts_at.provenance.startswith("INVALID"):
                self._set_health("writer_conflicts", "INVALID", "INVALID_CONFLICT_TIMESTAMP")
            elif _conflicts_at.freshness is not None:
                self._set_health("writer_conflicts", "STALE", "STALE_CONFLICT_EVIDENCE")
            errors.append("writer_conflicts:UNPROVEN")
        elif conflict_fresh and conflicts_raw:
            parsed_conflicts = tuple(
                sorted({item.strip() for item in conflicts_raw.replace(";", ",").split(",") if item.strip()})
            )
            if parsed_conflicts:
                conflicts = parsed_conflicts
                conflicts_proven = True
                self._set_health("writer_conflicts", "HEALTHY")
            else:
                self._set_health("writer_conflicts", "INCOHERENT", "MALFORMED_CONFLICT_LIST")
                errors.append("writer_conflicts:UNPROVEN")
        else:
            errors.append("writer_conflicts:UNPROVEN")

        planner_raw = self._all("planner_intent")
        planner_status_raw = self._all("planner_status")
        self._record("planner_intent", planner_raw, now, self.config.planner_max_age_s)
        self._record("planner_status", planner_status_raw, now, self.config.planner_max_age_s)
        try:
            intent = decode_authoritative_planner_intent(
                planner_raw,
                planner_status_raw,
                now,
                max_age_s=self.config.planner_max_age_s,
                allow_updated=self.config.allow_last_updated_fallback,
            )
            self.last_planner_status = "VALID" if intent is not None else "NO_INTENT"
            self._set_health("planner_intent", "HEALTHY")
            self._set_health("planner_status", "HEALTHY")
        except ValueError as error:
            intent = None
            self.last_planner_status = _bounded(error)
            errors.append(self.last_planner_status)
            planner_health = "STALE" if "STALE" in self.last_planner_status else "INVALID"
            if "MISSING" in self.last_planner_status:
                planner_health = "UNAVAILABLE"
            if self.last_planner_status.startswith("PLANNER_STATUS"):
                self._set_health("planner_status", planner_health, self.last_planner_status)
            else:
                self._set_health("planner_intent", planner_health, self.last_planner_status)

        capability_snapshot = self._capabilities(now)
        if not conflicts_proven:
            raise AdapterInputError("WRITER_CONFLICTS_UNPROVEN")
        if ownership is None:
            unknown = ",".join(
                name
                for name, proven in (
                    ("SOLAX", solax_ownership_proven),
                    ("DEYE", deye_ownership_proven),
                )
                if not proven
            )
            raise AdapterInputError(f"OWNERSHIP_UNPROVEN:{unknown}")
        assert remote_active is not None
        physical = PhysicalStateSnapshot(
            observed_at=now,
            pcc=samples["pcc"],
            solax_battery_power=samples["solax_battery_power"],
            deye_battery_power=samples["deye_battery_power"],
            solax_available=available is True,
            solax_fault=fault is not False,
            solax_native_mode_confirmed=solax_role is PhysicalRole.NATIVE,
            solax_remote_control_active=remote_active,
            deye_state=deye_operating,
            deye_non_owning_confirmed=deye_role in {PhysicalRole.OFF, PhysicalRole.NATIVE},
            ownership=ownership,
            solax_control_source_timestamp=solax_control_at,
            deye_control_source_timestamp=deye_control_at,
            writer_conflicts=conflicts,
            continuity_certain=not reconciliation_required,
        )
        context = ExecutionContext(
            epoch,
            ExecutionState.RECONCILING if reconciliation_required else ExecutionState.PRECHECK,
            now,
            now,
            intent=intent,
            enabled=execution_enabled,
            writer_conflicts=conflicts,
        )
        safety = SafetyConfig(
            30,
            20000,
            50,
            5,
            200,
            4,
            100,
            3,
            SafetyAction.DEFER,
            SafetyAction.ROLLBACK,
            SafetyAction.SAFE_STOP,
            30,
            30,
        )
        phases = {phase: 30 for phase in TransitionPhase if phase is not TransitionPhase.COMPLETE}
        additional = ()
        if errors:
            reason = (
                FailureReason.INVALID_INTENT
                if any(error.startswith("PLANNER_") for error in errors)
                else FailureReason.MISSING_TELEMETRY
            )
            additional = (SafetyDecision(SafetyAction.DEFER, (reason,), details=tuple(sorted(set(errors)))[:20]),)
        rolling_timestamp = samples["rolling_export_w"].freshness_timestamp
        rolling_value = samples["rolling_export_w"].value
        rolling_covered = samples["rolling_export_covered_s"].value
        return ExecutorInputs(
            context=context,
            intent=intent,
            physical=physical,
            capabilities=capability_snapshot,
            safety_config=safety,
            transition_config=TransitionConfig(phases),
            current_roles=ownership,
            now=now,
            transition_id=f"shadow-{epoch}",
            rolling_export=RollingExportEvidence(
                rolling_value,
                rolling_covered if rolling_covered is not None and rolling_covered >= 0 else 0.0,
                rolling_covered is not None and rolling_covered >= 900 and rolling_timestamp is not None,
                rolling_timestamp,
            ),
            readiness_evidence=ReadinessEvidence(
                deye_operating,
                deye_control_at,
                available is True,
                fault is not False,
                solax_control_at,
            ),
            reconciliation_required=reconciliation_required,
            additional_safety=additional,
            contradictions=tuple(sorted(set(errors))),
        )


class ShadowExecutorRuntime:
    def __init__(
        self,
        adapter: HomeAssistantExecutorAdapter,
        publisher: DiagnosticPublisher,
        config: RuntimeConfig,
    ) -> None:
        self.adapter, self.publisher, self.config = adapter, publisher, config
        self.store = ExecutorStateStore(config.persistence_path)
        self.lock = threading.Lock()
        self.reconciliation_required = True
        self.rehydration_reason = "FIRST_START"
        self.startup_failure_reason: str | None = None
        self.persisted: PersistedExecutorMetadata | None = None
        self.current_state = ExecutionState.RECONCILING
        self.transition_progress = None
        self.last_evaluation_at: datetime | None = None
        self.last_successful_evaluation_at: datetime | None = None
        self.last_publication_failure: dict[str, str] | None = None
        self.persistence_status = "NOT_LOADED"
        prior = None
        try:
            prior = self.store.load()
            if prior is not None:
                self.rehydration_reason = "RESTART_RECONCILIATION"
                self.persisted = PersistedExecutorMetadata(
                    execution_epoch=prior.execution_epoch,
                    active_plan_id=prior.active_plan_id,
                    active_intent_id=prior.active_intent_id,
                )
                self.persistence_status = "LOADED_INFORMATIONAL_ONLY"
            else:
                self.persistence_status = "MISSING_NEW_EPOCH"
        except ValueError:
            self.rehydration_reason = "PERSISTENCE_INVALID"
            self.persistence_status = "INVALID_NEW_EPOCH"
        except OSError as error:
            self.rehydration_reason = "PERSISTENCE_IO_FAILURE"
            self.startup_failure_reason = _bounded(f"PERSISTENCE_LOAD_ERROR:{type(error).__name__}")
            self.persistence_status = "IO_ERROR_NEW_EPOCH"
            prior = None
        self.epoch = _new_epoch(prior.execution_epoch if prior is not None else None)

    def tick(self, now: datetime | None = None) -> ExecutorResult | None:
        if not self.lock.acquire(blocking=False):
            return None
        try:
            current = (now or datetime.now(UTC)).astimezone(UTC)
            self.last_evaluation_at = current
            try:
                inputs = self.adapter.build(current, self.epoch, reconciliation_required=self.reconciliation_required)
                inputs = ExecutorInputs(
                    **{
                        **vars(inputs),
                        "context": ExecutionContext(
                            execution_epoch=self.epoch,
                            state=self.current_state,
                            state_entered_at=current,
                            now=current,
                            active_plan_id=(
                                self.transition_progress.plan.plan_id if self.transition_progress is not None else None
                            ),
                            active_intent_id=(
                                self.transition_progress.plan.intent_id
                                if self.transition_progress is not None
                                else None
                            ),
                            intent=inputs.intent,
                            enabled=inputs.context.enabled,
                            writer_conflicts=inputs.context.writer_conflicts,
                        ),
                        "transition_progress": self.transition_progress,
                        "current_roles": (
                            self.transition_progress.physical_roles
                            if self.transition_progress is not None
                            else inputs.current_roles
                        ),
                        "persisted": self.persisted,
                    }
                )
                result = evaluate_executor(inputs)
                if (
                    self.reconciliation_required
                    and result.reconciliation is not None
                    and result.reconciliation.outcome_state in {ExecutionState.NORMAL, ExecutionState.PRECHECK}
                ):
                    self.reconciliation_required = False
                    self.persisted = None
                self.current_state = result.state
                self.transition_progress = result.transition_progress
                self._persist(result)
                if not self._publish(result, current):
                    reason = (
                        self.last_publication_failure["reason"]
                        if self.last_publication_failure
                        else "PUBLICATION_ERROR"
                    )
                    self._fail_closed(current, reason)
                    return None
                self.last_successful_evaluation_at = current
                return result
            except AdapterInputError as error:
                self._fail_closed(current, _bounded(f"ADAPTER_INPUT_ERROR:{error}"))
                return None
            except Exception as error:
                self._fail_closed(current, f"RUNTIME_INPUT_ERROR:{type(error).__name__}")
                return None
        finally:
            self.lock.release()

    def _persist(self, result: ExecutorResult) -> None:
        transition = result.transition_progress
        self.store.save(
            StoredShadowState(
                self.epoch,
                result.state.value,
                transition.plan.plan_id if transition else None,
                transition.plan.intent_id if transition else None,
                self.rehydration_reason,
                self.last_evaluation_at.isoformat() if self.last_evaluation_at else None,
            )
        )
        self.persistence_status = "SAVED_INFORMATIONAL_ONLY"

    def _persist_failure(self) -> None:
        try:
            self.store.save(
                StoredShadowState(
                    self.epoch,
                    "SAFE_STOP",
                    rehydration_reason="RUNTIME_FAILURE",
                    last_evaluation_at=self.last_evaluation_at.isoformat() if self.last_evaluation_at else None,
                )
            )
            self.persistence_status = "FAILURE_SAVED"
        except (OSError, ValueError):
            self.persistence_status = "SAVE_FAILED"

    def _publish(self, result: ExecutorResult, now: datetime) -> bool:
        proposal = result.proposal
        proposal_data = (
            None
            if proposal is None
            else {
                "device": proposal.battery,
                "action_kind": proposal.operation.value,
                "role": proposal.role.value,
                "effective_target_w": proposal.effective_executor_target_w,
                "shadow_only": True,
            }
        )
        values = {
            "state": result.state.value,
            "reason": ",".join(reason.value for reason in result.reasons)[:_MAX_TEXT] or "NONE",
            "proposal": json.dumps(proposal_data, sort_keys=True, separators=(",", ":")) if proposal_data else "NONE",
            "epoch": str(self.epoch),
        }
        attributes = self._attributes(now, result=result, proposal=proposal_data)
        try:
            self.publisher.set_state(
                self.config.output_entities["state"],
                state="UPDATING",
                attributes={**attributes, "runtime_status": "UPDATING"},
            )
            for key in ("reason", "proposal", "epoch"):
                self.publisher.set_state(self.config.output_entities[key], state=values[key], attributes=attributes)
            self.publisher.set_state(self.config.output_entities["state"], state=values["state"], attributes=attributes)
            self.last_publication_failure = None
            return True
        except Exception as error:
            self.last_publication_failure = {
                "at": now.isoformat(),
                "reason": _bounded(f"PUBLICATION_ERROR:{type(error).__name__}"),
            }
            return False

    def _fail_closed(self, now: datetime, reason: str) -> None:
        self.current_state = ExecutionState.SAFE_STOP
        self.transition_progress = None
        self.reconciliation_required = True
        self._persist_failure()
        self._publish_failure(now, _bounded(reason))

    def _publish_failure(self, now: datetime, reason: str) -> None:
        values = {"state": "SAFE_STOP", "reason": reason[:_MAX_TEXT], "proposal": "NONE", "epoch": str(self.epoch)}
        original_failure = self.last_publication_failure
        try:
            for key, value in values.items():
                self.publisher.set_state(
                    self.config.output_entities[key],
                    state=value,
                    attributes=self._attributes(now, failure_reason=reason),
                )
            self.last_publication_failure = original_failure
        except Exception as error:
            original_reason = original_failure["reason"] if original_failure is not None else None
            failure_reason = f"PUBLICATION_ERROR:{type(error).__name__}"
            self.last_publication_failure = {
                "at": now.isoformat(),
                "reason": _bounded(
                    f"{original_reason}|FAILURE_SNAPSHOT_{failure_reason}" if original_reason else failure_reason
                ),
            }

    def _attributes(
        self,
        now: datetime,
        *,
        result: ExecutorResult | None = None,
        proposal: dict[str, object] | None = None,
        failure_reason: str | None = None,
    ) -> dict[str, object]:
        reasons = failure_reason or (
            ",".join(reason.value for reason in result.reasons) if result is not None else "NONE"
        )
        health_entries = tuple(
            (key, str(value.get("status", "INVALID")))
            for key, value in self.adapter.last_input_health.items()
            if isinstance(value, dict) and value.get("status") != "HEALTHY"
        )
        health_summary = (
            "HEALTHY" if not health_entries else ",".join(f"{status}:{key}" for key, status in health_entries)
        )
        return {
            "shadow_only": True,
            "runtime_status": "FAIL_CLOSED" if failure_reason else "EVALUATED",
            "execution_epoch": self.epoch,
            "executor_state": result.state.value if result is not None else ExecutionState.SAFE_STOP.value,
            "safety_action": result.safety_action.value if result is not None else SafetyAction.SAFE_STOP.value,
            "reason": _bounded(reasons),
            "reconciliation_required": self.reconciliation_required,
            "planner_status": _bounded(self.adapter.last_planner_status),
            "input_health": _bounded(health_summary),
            "proposal_summary": proposal or None,
            "last_evaluation_at": self.last_evaluation_at.isoformat() if self.last_evaluation_at else None,
            "last_successful_evaluation_at": (
                self.last_successful_evaluation_at.isoformat() if self.last_successful_evaluation_at else None
            ),
            "whole_site_load_w": self.adapter.last_site_load_w,
            "persistence_status": self.persistence_status,
            "publication_status": "FAILED" if self.last_publication_failure else "OK",
            "evaluated_at": now.isoformat(),
            "rehydration": self.rehydration_reason,
            "startup_failure_reason": self.startup_failure_reason,
        }
