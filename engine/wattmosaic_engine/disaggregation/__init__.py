"""Load disaggregation helpers retained after the Z2 cutover.

The legacy absolute solver (``DisaggregationEngine``), ambiguity/group-bounds
logic, per-member model split and step credit were removed with the 5 s solve
path.  The remaining modules are pure, standalone helpers still consumed by the
audit/association tooling and by historical replay; their public names are
re-exported here so existing imports keep resolving.
"""

from __future__ import annotations

from wattmosaic_engine.disaggregation.association import (
    AssociationConfig,
    AssociationFinder,
    Observation,
    persist,
)
from wattmosaic_engine.disaggregation.circuits import (
    CircuitClassifier,
    CircuitConfig,
)
from wattmosaic_engine.disaggregation.circuits import (
    explain as explain_circuit,
)
from wattmosaic_engine.disaggregation.circuits import (
    persist as persist_circuits,
)
from wattmosaic_engine.disaggregation.isolation import (
    EventIsolator,
    IsolationConfig,
    IsolationWindow,
    PhysicalDeviceEvent,
    SampleCoverage,
    clean_attribution_inputs,
    physical_device_events,
    split_attribution,
)
from wattmosaic_engine.disaggregation.mapping import (
    CONFIRM_MIN_EVENTS,
    MappingCoverage,
    MappingEvidence,
    MappingMatrix,
    build_mapping_matrix,
)

__all__ = [
    "AssociationConfig",
    "AssociationFinder",
    "CONFIRM_MIN_EVENTS",
    "CircuitClassifier",
    "CircuitConfig",
    "EventIsolator",
    "IsolationConfig",
    "IsolationWindow",
    "MappingCoverage",
    "MappingEvidence",
    "MappingMatrix",
    "Observation",
    "PhysicalDeviceEvent",
    "SampleCoverage",
    "clean_attribution_inputs",
    "explain_circuit",
    "build_mapping_matrix",
    "persist",
    "persist_circuits",
    "physical_device_events",
    "split_attribution",
]
