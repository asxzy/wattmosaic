"""Declared-data shapes for the retained read-only topology projections.

Contract 3.0 removes the legacy contract *models* from ``contracts.legacy``.
A handful of pure, read-time projections in the topology/disaggregation area
still operate on the *shape* of a declared pointer tree (channels, devices,
typed relations, and the evidence/source vocabularies that graded an attributed
watt value).  Their v3 replacements — ``MeasurementEndpoint``, ``TopologyEdgeV3``
and ``MappingEdge`` — carry the same information under different names.

This module is the single migration seam for that transition.  It is the only
file in the area that still binds a retired contract name, so the cutover task
only has to change this one import block.  Every consumer in the area imports a
neutral name from here, which keeps the projections readable and behaviourally
identical while the storage and HTTP surfaces have already moved to 3.0.
"""

from __future__ import annotations

from wattmosaic_engine.contracts.legacy import (
    ConfidenceTier as EvidenceGrade,
)
from wattmosaic_engine.contracts.legacy import (
    ConfidenceVector as EvidenceVector,
)
from wattmosaic_engine.contracts.legacy import (
    ConfigurationChangeEvent as ConfigEventDecl,
)
from wattmosaic_engine.contracts.legacy import (
    CTChannel as ChannelDecl,
)
from wattmosaic_engine.contracts.legacy import (
    DeviceEvent as HaDeviceEvent,
)
from wattmosaic_engine.contracts.legacy import (
    DeviceModelParameters as ModelParamsDecl,
)
from wattmosaic_engine.contracts.legacy import (
    DeviceModelVersion as ModelVersionDecl,
)
from wattmosaic_engine.contracts.legacy import (
    DeviceReaderMappingCell as MappingCellDecl,
)
from wattmosaic_engine.contracts.legacy import (
    DeviceTopology as DeviceDecl,
)
from wattmosaic_engine.contracts.legacy import (
    InferenceResult as AttributedValue,
)
from wattmosaic_engine.contracts.legacy import (
    LearningSample as TrainingSample,
)
from wattmosaic_engine.contracts.legacy import (
    MappingCellStatus as CellStateDecl,
)
from wattmosaic_engine.contracts.legacy import (
    MappingObservability as ObservabilityDecl,
)
from wattmosaic_engine.contracts.legacy import (
    Measurement as ReadingDecl,
)
from wattmosaic_engine.contracts.legacy import (
    PowerLevel as PowerTierDecl,
)
from wattmosaic_engine.contracts.legacy import (
    Reconciliation as BalanceReport,
)
from wattmosaic_engine.contracts.legacy import (
    RelationKind as RelationKindDecl,
)
from wattmosaic_engine.contracts.legacy import (
    Source as ValueSource,
)
from wattmosaic_engine.contracts.legacy import (
    Topology as DeclaredTree,
)
from wattmosaic_engine.contracts.legacy import (
    TopologyEdge as EdgeDecl,
)
from wattmosaic_engine.contracts.legacy import (
    TopologyStatus as EdgeState,
)
from wattmosaic_engine.contracts.legacy import (
    TopologyVersion as TreeVersionDecl,
)
from wattmosaic_engine.contracts.legacy import (
    UnknownLoadProfile as UnknownLoadDecl,
)

__all__ = [
    "AttributedValue",
    "BalanceReport",
    "CellStateDecl",
    "ChannelDecl",
    "ConfigEventDecl",
    "DeclaredTree",
    "DeviceDecl",
    "EdgeDecl",
    "EdgeState",
    "EvidenceGrade",
    "EvidenceVector",
    "HaDeviceEvent",
    "MappingCellDecl",
    "ModelParamsDecl",
    "ModelVersionDecl",
    "ObservabilityDecl",
    "PowerTierDecl",
    "ReadingDecl",
    "RelationKindDecl",
    "TrainingSample",
    "TreeVersionDecl",
    "UnknownLoadDecl",
    "ValueSource",
]
