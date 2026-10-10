"""ENERGY V3 domain foundation and read-only shadow adapters."""

from .controller import decide
from .execution_proposal import (
    ControlledInverter,
    CounterpartSafeState,
    ExecutionProposal,
    ExecutionReadiness,
    ProposalBlocker,
    ProtocolSetpoint,
    execution_proposal,
)
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
    "ControlledInverter",
    "CounterpartSafeState",
    "CurrentTarget",
    "Decision",
    "DecisionReason",
    "DecisionState",
    "DeyeCapabilities",
    "ExportAuthorization",
    "ExecutionProposal",
    "ExecutionReadiness",
    "HaeoError",
    "HaeoTargetResult",
    "SafetyConfig",
    "SafetySnapshot",
    "SolaxCapabilities",
    "V3Capabilities",
    "TelemetryResult",
    "TelemetryStatus",
    "ProposalBlocker",
    "ProtocolSetpoint",
    "current_target_from_states",
    "decide",
    "execution_proposal",
    "safety_snapshot_from_states",
]
