"""Load disaggregation: event detection, confidence, inference orchestration."""

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
from wattmosaic_engine.disaggregation.confidence import (
    ConfidenceBreakdown,
    ConfidenceInputs,
    compute_confidence,
    event_isolation_score,
)
from wattmosaic_engine.disaggregation.engine import DisaggregationEngine, InferenceBatch
from wattmosaic_engine.disaggregation.events import (
    DEFAULT_K_MAD,
    MAD_TO_SIGMA,
    StepChange,
    detect_changes,
    detect_step,
    mad_sigma,
    median,
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
    "DEFAULT_K_MAD",
    "MAD_TO_SIGMA",
    "AssociationConfig",
    "AssociationFinder",
    "CONFIRM_MIN_EVENTS",
    "CircuitClassifier",
    "CircuitConfig",
    "ConfidenceBreakdown",
    "ConfidenceInputs",
    "DisaggregationEngine",
    "EventIsolator",
    "InferenceBatch",
    "IsolationConfig",
    "IsolationWindow",
    "MappingCoverage",
    "MappingEvidence",
    "MappingMatrix",
    "Observation",
    "PhysicalDeviceEvent",
    "SampleCoverage",
    "StepChange",
    "clean_attribution_inputs",
    "compute_confidence",
    "detect_changes",
    "detect_step",
    "event_isolation_score",
    "explain_circuit",
    "mad_sigma",
    "median",
    "build_mapping_matrix",
    "persist",
    "persist_circuits",
    "physical_device_events",
    "split_attribution",
]
