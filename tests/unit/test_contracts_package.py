"""X01: contracts.py is now a package facade with a stable public surface.

The split is a pure refactor: every name importable from the old single-file
module must remain importable from ``wattmosaic_engine.contracts``.  New
contracts are discovered through each submodule's ``SCHEMA_EXPORTS`` list.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import (
    entities,
    foundation,
    inference,
    matrix,
    measurement,
    power_models,
    topology,
)

# Snapshot of ``dir(wattmosaic_engine.contracts)`` on the unmodified tree
# (contracts.py at 27373aa).  A package adds a handful of inherent attributes
# (``__path__`` and the submodule names); every *other* name must be present.
LEGACY_PUBLIC_NAMES = frozenset(
    (
        "Any",
        "BaseModel",
        "CONTRACT_VERSION",
        "CTChannel",
        "CTType",
        "ChangeDetection",
        "ChangeStatus",
        "Circuit",
        "CircuitClass",
        "ConfidenceDimension",
        "ConfidenceTier",
        "ConfidenceVector",
        "ConfigDict",
        "ConfigurationChangeEvent",
        "ConfigurationChangeKind",
        "ControllerConfiguration",
        "ControllerStateMapping",
        "ControllerStateRole",
        "DataQualityReport",
        "DeviceConfiguration",
        "DeviceConfigurationSet",
        "DeviceEvent",
        "DeviceModelParameters",
        "DeviceModelVersion",
        "DeviceReaderMappingCell",
        "DeviceState",
        "DeviceTopology",
        "DeviceType",
        "ENGINE_VERSION",
        "EdgeLifecycle",
        "Enum",
        "ErrorResponse",
        "EventEvidenceRecord",
        "EventIsolation",
        "ExpectedPower",
        "Field",
        "HaConnectivity",
        "HaEntityBinding",
        "HaEntityRole",
        "HealthResponse",
        "InferenceRecord",
        "InferenceResult",
        "IsolationStatus",
        "Iterable",
        "LearningSample",
        "LightConfiguration",
        "LightType",
        "Literal",
        "LoadShape",
        "MappingCellStatus",
        "MappingObservability",
        "Measurement",
        "MeasurementBasis",
        "MeasurementFactor",
        "Model",
        "ModelStatus",
        "ModelType",
        "MonitoringLevel",
        "OperatingStage",
        "Phase",
        "Polarity",
        "PowerBehavior",
        "PowerLevel",
        "PowerState",
        "QualityStatus",
        "Reconciliation",
        "RelationKind",
        "ResidualBaselineChange",
        "ResidualDecompositionRecord",
        "ResidualKind",
        "ResidualPower",
        "SampleRejection",
        "ServiceCommand",
        "ServiceResult",
        "SnapshotResponse",
        "Source",
        "StageStatus",
        "StorageHealthReport",
        "TIER_RANK",
        "Topology",
        "TopologyEdge",
        "TopologyRelation",
        "TopologyStatus",
        "TopologyVersion",
        "UTC",
        "UnknownLoadLifecycle",
        "UnknownLoadProfile",
        "UnknownResidualEvent",
        "annotations",
        "datetime",
        "derived_residual_kind",
        "field_validator",
        "model_validator",
        "power_state_for",
        "utcnow",
        "validate_factor_intervals",
    )
)

# Private names that tests import directly.
LEGACY_PRIVATE_NAMES = frozenset({"_document", "_reject_parent_cycle"})

# Dunder names present in both the old module and the package.
MODULE_DUNDER_NAMES = frozenset(
    {
        "__annotations__",
        "__builtins__",
        "__cached__",
        "__doc__",
        "__file__",
        "__loader__",
        "__name__",
        "__package__",
        "__spec__",
    }
)

# Attributes that a package necessarily has but the old module did not.
PACKAGE_ONLY_NAMES = frozenset(
    {
        "__path__",
        "entities",
        "foundation",
        "inference",
        "legacy",
        "matrix",
        "measurement",
        "power_models",
        "topology",
    }
)


def test_every_legacy_name_is_importable_from_the_package() -> None:
    """Every name in the old module's ``dir()`` is still available."""
    for name in LEGACY_PUBLIC_NAMES | LEGACY_PRIVATE_NAMES:
        assert hasattr(contracts, name), f"contracts facade lost {name!r}"


def test_package_surface_extras_come_only_from_v3_submodules() -> None:
    """Facade extras are exactly the v3 submodule exports (K00..K06).

    X01 wrote this as "no extras"; that held only while the submodules were
    empty seams.  As the wave-1 contract tasks fill them, their ``__all__``
    names are star-imported here by design.  The stronger invariant is that
    *every* extra name is declared by a v3 submodule -- nothing appears by
    accident -- and no v3 name shadows a legacy name.
    """
    allowed = (
        LEGACY_PUBLIC_NAMES
        | LEGACY_PRIVATE_NAMES
        | MODULE_DUNDER_NAMES
        | PACKAGE_ONLY_NAMES
    )
    v3_names: set[str] = set()
    for module in (foundation, entities, measurement, topology, inference, matrix, power_models):
        v3_names.update(module.__all__)
    extras = frozenset(dir(contracts)) - allowed
    assert extras == v3_names, (
        f"facade extras {sorted(extras)} do not match v3 exports {sorted(v3_names)}"
    )
    assert not (v3_names & LEGACY_PUBLIC_NAMES), "a v3 export shadows a legacy name"


def test_legacy_private_names_are_the_real_objects() -> None:
    from wattmosaic_engine.contracts import legacy

    assert contracts._document is legacy._document
    assert contracts._reject_parent_cycle is legacy._reject_parent_cycle


@pytest.mark.parametrize(
    "module",
    [foundation, entities, measurement, topology, inference, matrix, power_models],
)
def test_submodule_exports_are_self_consistent(module: ModuleType) -> None:
    """``__all__`` names resolve; exported models are registered in SCHEMA_EXPORTS.

    Written generically so wave-1 tasks can fill their seam (K00..K06) without
    ever editing this test: an empty seam passes trivially, a filled one must be
    internally consistent.  The facade-surface test above enforces that no v3
    export shadows a legacy name.
    """
    for name in module.__all__:
        assert hasattr(module, name), f"{module.__name__}.__all__ lists missing {name!r}"
    for _stem, model in module.SCHEMA_EXPORTS:
        assert model.__name__ in module.__all__, (
            f"{module.__name__} exports a schema model absent from __all__"
        )
    assert hasattr(module, "SCHEMA_EXPORTS")


def test_schema_exports_discovery_finds_an_injected_dummy_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exporter discovers SCHEMA_EXPORTS from any submodule via monkeypatch."""

    class DummyModel:
        """Stand-in for a future contract model."""

    sentinel = ("injected_dummy_model", DummyModel)
    monkeypatch.setattr(foundation, "SCHEMA_EXPORTS", [sentinel])

    exporter = importlib.import_module("tools.dev.export_schemas")
    collected = exporter.collect_models()

    assert collected["injected_dummy_model"] is DummyModel
