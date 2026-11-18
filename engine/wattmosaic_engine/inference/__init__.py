"""Inference package (contract 3.0): candidates, matching, joint solve, groups.

Wave-3/4 tasks own the modules in here:

* I1 ``candidates`` / ``topology_filter``
* I2 ``temporal`` / ``evidence``
* I3 ``joint`` / ``covariance``
* I4 ``identifiability`` / ``bounds``
* I5 ``estimates`` / ``confidence_v3``
* I6 ``matching`` / ``records``
* I7 ``orchestrator``

The modules are pure: they consume contracts and ``runtime`` types, take
injected inputs, and never import ``runtime.fakes`` or touch a database.  Only
I1's stable entry points are re-exported here; import other stages from their
modules (``from wattmosaic_engine.inference.identifiability import ...``) so a
partially landed wave stays importable.
"""

from __future__ import annotations

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
