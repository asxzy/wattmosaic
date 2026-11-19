"""Public topology-inference surface (T5).

Re-exports the pure engine from :mod:`wattmosaic_engine.electrical.topology_inference`
under the stable name the acceptance suite imports (``infer_topology``).
The engine implementation lives in ``electrical``; this is a thin facade so
callers do not need to know the internal package layout.
"""

from __future__ import annotations

from wattmosaic_engine.electrical.topology_inference import (
    CONTAINMENT_MIN,
    MIN_INDEPENDENT_CAUSES,
    DeltaObservation,
    TopologyEvidence,
    TopologyInferenceView,
    detect_same_conductor,
    infer_topology,
)

__all__ = [
    "CONTAINMENT_MIN",
    "MIN_INDEPENDENT_CAUSES",
    "DeltaObservation",
    "TopologyEvidence",
    "TopologyInferenceView",
    "detect_same_conductor",
    "infer_topology",
]
