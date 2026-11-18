"""Event-driven candidate generation and topology-aware filtering (I1).

The modules here are pure: they consume K04 contracts and K08 runtime types,
take an injected clock/inputs, and never import ``runtime.fakes`` or touch a
database.  They are the first stage of the new inference pipeline (HB §7).
"""

from .candidates import (
    MAX_CANDIDATES,
    PROVISIONAL_FLAG,
    CandidateSet,
    generate_candidates,
)
from .topology_filter import (
    FilterVerdict,
    evaluate_topology,
)

__all__ = [
    "MAX_CANDIDATES",
    "PROVISIONAL_FLAG",
    "CandidateSet",
    "FilterVerdict",
    "evaluate_topology",
    "generate_candidates",
]
