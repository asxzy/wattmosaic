"""X01: ``wattmosaic_engine.contracts`` is a v3-only package facade.

Contract 3.0.0 deleted the legacy single-file surface.  The facade now exposes
exactly the ``__all__`` names of its v3 submodules (plus the submodules
themselves), and new contracts are discovered through each submodule's
``SCHEMA_EXPORTS`` list.
"""

from __future__ import annotations

import importlib
import importlib.util
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
    service,
    topology,
)

V3_MODULES: tuple[ModuleType, ...] = (
    foundation,
    entities,
    measurement,
    topology,
    inference,
    matrix,
    power_models,
    service,
)

V3_MODULE_NAMES = frozenset(m.__name__.rsplit(".", 1)[-1] for m in V3_MODULES)

# Names that were removed in the 3.0.0 cutover and must not come back.
REMOVED_LEGACY_NAMES = (
    "Topology",
    "CTChannel",
    "DeviceTopology",
    "InferenceResult",
    "Reconciliation",
    "Source",
    "PowerState",
    "ConfidenceTier",
    "TopologyStatus",
    "RelationKind",
    "Measurement",
    "DeviceEvent",
    "LearningSample",
    "DeviceModelVersion",
    "DeviceModelParameters",
    "ModelType",
    "ModelStatus",
    "ExpectedPower",
    "DeviceConfiguration",
    "DeviceConfigurationSet",
    "Circuit",
    "CircuitClass",
    "TopologyRelation",
    "EventIsolation",
    "InferenceRecord",
    "ResidualPower",
    "ResidualKind",
    "HaEntityBinding",
    "MonitoringLevel",
    "PowerBehavior",
    "DeviceType",
    "Phase",
    "Polarity",
    "CTType",
    "SnapshotResponse",
    "ConfidenceVector",
    "PowerLevel",
    "TIER_RANK",
)


def _public_names(module: ModuleType) -> set[str]:
    return {name for name in dir(module) if not name.startswith("_")}


def test_legacy_module_is_gone() -> None:
    assert importlib.util.find_spec("wattmosaic_engine.contracts.legacy") is None
    assert not hasattr(contracts, "legacy")


@pytest.mark.parametrize("name", REMOVED_LEGACY_NAMES)
def test_removed_legacy_names_are_not_exported(name: str) -> None:
    assert not hasattr(contracts, name), f"legacy name {name!r} resurfaced"


def test_facade_surface_is_exactly_the_v3_exports() -> None:
    """Every public facade name is a v3 submodule or a v3 ``__all__`` export."""
    v3_names: set[str] = set()
    for module in V3_MODULES:
        v3_names.update(module.__all__)
    # ``annotations`` is the facade's own ``from __future__ import annotations``.
    assert _public_names(contracts) - V3_MODULE_NAMES - {"annotations"} == v3_names


def test_v3_exports_do_not_collide_across_submodules() -> None:
    seen: dict[str, str] = {}
    for module in V3_MODULES:
        for name in module.__all__:
            assert name not in seen, (
                f"{name!r} exported by both {seen[name]} and {module.__name__}"
            )
            seen[name] = module.__name__


def test_facade_names_are_the_submodule_objects() -> None:
    for module in V3_MODULES:
        for name in module.__all__:
            assert getattr(contracts, name) is getattr(module, name), name


def test_shared_types_moved_to_their_v3_homes() -> None:
    homes = {
        foundation: ("Model", "utcnow", "CONTRACT_VERSION"),
        entities: ("LoadShape", "HaEntityRole"),
        measurement: (
            "MeasurementBasis",
            "QualityStatus",
            "DataQualityReport",
            "MeasurementFactor",
            "validate_factor_intervals",
        ),
        topology: ("ConfigurationChangeKind", "ChangeDetection", "ChangeStatus"),
        power_models: ("OperatingStage", "StageStatus"),
        service: ("HealthResponse", "ErrorResponse", "ServiceCommand", "ServiceResult"),
    }
    for module, names in homes.items():
        for name in names:
            assert name in module.__all__, f"{name} not exported by {module.__name__}"
            obj = getattr(module, name)
            if isinstance(obj, type):
                assert obj.__module__ == module.__name__, name


@pytest.mark.parametrize("module", V3_MODULES, ids=lambda m: m.__name__)
def test_submodule_exports_are_self_consistent(module: ModuleType) -> None:
    """``__all__`` names resolve; exported models are registered in SCHEMA_EXPORTS."""
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
