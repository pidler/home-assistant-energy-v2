"""ENERGY V3 domain foundation and read-only shadow adapters."""

from .controller import decide
from .haeo_adapter import HaeoError, HaeoTargetResult, current_target_from_states
from .models import (
    CapabilityLevel,
    CurrentTarget,
    Decision,
    DecisionReason,
    DecisionState,
    DeyeCapabilities,
    ExportAuthorization,
    SafetyConfig,
    SafetySnapshot,
    SolaxCapabilities,
    V3Capabilities,
)
from .telemetry_adapter import TelemetryResult, TelemetryStatus, safety_snapshot_from_states

__all__ = [
    "CapabilityLevel",
    "CurrentTarget",
    "Decision",
    "DecisionReason",
    "DecisionState",
    "DeyeCapabilities",
    "ExportAuthorization",
    "HaeoError",
    "HaeoTargetResult",
    "SafetyConfig",
    "SafetySnapshot",
    "SolaxCapabilities",
    "V3Capabilities",
    "TelemetryResult",
    "TelemetryStatus",
    "current_target_from_states",
    "decide",
    "safety_snapshot_from_states",
]
