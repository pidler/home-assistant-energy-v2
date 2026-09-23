"""Pure shadow proposal adapters."""

from .base import AdapterOperation, AdapterResult, ShadowControlProposal, ShadowProposalAdapter
from .deye import DeyeShadowAdapter
from .solax import SolaxShadowAdapter

__all__ = [
    "AdapterOperation",
    "AdapterResult",
    "DeyeShadowAdapter",
    "ShadowControlProposal",
    "ShadowProposalAdapter",
    "SolaxShadowAdapter",
]
