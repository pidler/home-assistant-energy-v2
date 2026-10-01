"""Pure ENERGY V3 domain foundation.

This package deliberately contains no Home Assistant or AppDaemon runtime code.
"""

from .controller import decide
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

__all__ = [
    "CapabilityLevel",
    "CurrentTarget",
    "Decision",
    "DecisionReason",
    "DecisionState",
    "DeyeCapabilities",
    "ExportAuthorization",
    "SafetyConfig",
    "SafetySnapshot",
    "SolaxCapabilities",
    "V3Capabilities",
    "decide",
]
