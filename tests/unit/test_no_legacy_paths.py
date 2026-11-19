"""Static gate: contract 3.0.0 has no legacy engine paths or legacy contracts.

The three-loop runtime is the only runtime and the v3 contracts are the only
contracts.  This gate reads source with ``ast`` rather than importing modules, so
a dead-but-importable module cannot keep the old behaviour alive by accident,
and it closes the evasions earlier cut-overs used:

* binding a retired contract model under any name (an import, an alias, a
  definition, an annotation target);
* reaching it as an attribute of the ``contracts`` package or of a
  ``contracts.legacy`` module (``contracts.legacy.Topology``), through
  ``getattr(..., "Topology")``, or by naming the ``contracts.legacy`` module in
  a string;
* keeping a retired module alive at a new import site.

Scope is every Python file under ``engine/`` and ``custom_components/``.  The
only exclusion is ``engine/wattmosaic_engine/storage/migrations/``: migrations
1-24 read the legacy *tables and columns* by name as SQL strings while upgrading
historical databases (they run before migration 25 archives those tables), so
those strings are data, not live code paths.  Even there, no migration may
import or reference a legacy contract *model*.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "wattmosaic_engine"
INTEGRATION = ROOT / "custom_components" / "wattmosaic"

#: Removed engine symbols (Z2): the 5 s full solve and its helpers.
REMOVED_SYMBOLS = frozenset(
    {
        "DisaggregationEngine",
        "power_w_is_placeholder",
        "_group_bounds",
        "_detect_ambiguity",
        "StepTracker",
        "branch_ct_ids",
        "parent_ct_id",
        "expected_power_w",
        "infer_model_type",
        "inference_interval_s",
        "_power_for_params",
        "_infer_model_type",
        "parent_endpoint_id",
        "run_inference_once",
        "discover_candidate_topology",
        "derive_mapping_matrix",
        "build_mapping_matrix",
        "row_to_inference_result",
        "row_to_reconciliation",
    }
)

#: Every contract-2 model, enum and helper removed at contract 3.0.0
#: (architecture.md §5).  None may be bound or referenced anywhere in scope.
LEGACY_CONTRACT_NAMES = frozenset(
    {
        "CTChannel",
        "CTType",
        "Circuit",
        "CircuitClass",
        "ConfidenceDimension",
        "ConfidenceTier",
        "ConfidenceVector",
        "ConfigurationChangeEvent",
        "ControllerConfiguration",
        "ControllerStateMapping",
        "ControllerStateRole",
        "DeviceConfiguration",
        "DeviceConfigurationSet",
        "DeviceEvent",
        "DeviceModelParameters",
        "DeviceModelVersion",
        "DeviceReaderMappingCell",
        "DeviceState",
        "DeviceTopology",
        "DeviceType",
        "EdgeLifecycle",
        "EventEvidenceRecord",
        "EventIsolation",
        "ExpectedPower",
        "HaConnectivity",
        "HaEntityBinding",
        "InferenceRecord",
        "InferenceResult",
        "IsolationStatus",
        "LearningSample",
        "LightConfiguration",
        "LightType",
        "MappingCellStatus",
        "MappingObservability",
        "Measurement",
        "ModelStatus",
        "ModelType",
        "MonitoringLevel",
        "Phase",
        "Polarity",
        "PowerBehavior",
        "PowerLevel",
        "PowerState",
        "Reconciliation",
        "RelationKind",
        "ResidualBaselineChange",
        "ResidualDecompositionRecord",
        "ResidualKind",
        "ResidualPower",
        "SampleRejection",
        "SnapshotResponse",
        "Source",
        "StorageHealthReport",
        "TIER_RANK",
        "Topology",
        "TopologyEdge",
        "TopologyRelation",
        "TopologyStatus",
        "TopologyVersion",
        "UnknownLoadLifecycle",
        "UnknownLoadProfile",
        "UnknownResidualEvent",
        "derived_residual_kind",
        "power_state_for",
    }
)

#: Retired modules: none may exist (relative to ``engine/wattmosaic_engine``).
DELETED_MODULES = (
    "contracts/legacy.py",
    "electrical/_declared_shapes.py",
    "electrical/models.py",
    "electrical/panel_tree.py",
    "electrical/nesting.py",
    "electrical/hierarchy.py",
    "electrical/topology.py",
    "electrical/main_power.py",
    "electrical/matrix.py",
    "electrical/direct.py",
    "disaggregation/configuration.py",
    "disaggregation/mapping.py",
    "disaggregation/audit.py",
    "disaggregation/isolation.py",
    "disaggregation/association.py",
    "disaggregation/circuits.py",
    "disaggregation/reconcile.py",
    "disaggregation/_shared.py",
    "disaggregation/tiers.py",
    "device_configuration.py",
    "device_links.py",
    "models/base.py",
    "models/serde.py",
    "models/view.py",
    "models/residual_power.py",
    "learning/sample_collector.py",
    "core/rows.py",
    "backend/inference.py",
    "backend/audit.py",
    "backend/reanalysis.py",
    "backend/batch.py",
    "storage/repositories/topology.py",
    "storage/repositories/devices.py",
    "storage/repositories/associations.py",
    "api/routes/topology.py",
)

#: Legacy storage tables archived read-only by migration 25.
LEGACY_TABLES = (
    "ct_channels",
    "devices",
    "topology_versions",
    "topology_relations",
    "device_model_versions",
    "device_models",
    "learning_samples",
    "residual_power",
    "device_events",
    "ha_entity_bindings",
)

MIGRATIONS = "storage/migrations/"


def _files() -> list[Path]:
    out: list[Path] = []
    for root in (ENGINE, INTEGRATION):
        out.extend(p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_migration(path: Path) -> bool:
    return path.is_relative_to(ENGINE) and path.relative_to(ENGINE).as_posix().startswith(
        MIGRATIONS
    )


def _bound_names(tree: ast.Module) -> set[str]:
    """Names bound anywhere: definitions, imports (and aliases), assignments."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.add(alias.name)
                if alias.asname:
                    found.add(alias.asname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.add(target.id)
    return found


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _legacy_references(tree: ast.Module, *, in_contracts: bool = True) -> list[str]:
    """Every way a module can reach the legacy contract surface.

    ``in_contracts`` says whether a relative ``from .legacy import`` resolves to
    ``contracts.legacy`` (it does inside the contracts package; inside
    ``storage/migrations`` it names the frozen ``migrations/legacy.py``).
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            relative_legacy = in_contracts and node.level == 1 and (
                module == "legacy" or (not module and any(a.name == "legacy" for a in node.names))
            )
            if relative_legacy or module.endswith("contracts.legacy") or (
                module.endswith("contracts") and any(a.name == "legacy" for a in node.names)
            ):
                hits.append(f"import of contracts.legacy (line {node.lineno})")
            for alias in node.names:
                if alias.name in LEGACY_CONTRACT_NAMES:
                    hits.append(f"import of {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("contracts.legacy"):
                    hits.append(f"import of {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node) or ""
            if node.attr == "legacy" and (_dotted(node.value) or "").endswith("contracts"):
                hits.append(f"attribute {dotted} (line {node.lineno})")
            base = _dotted(node.value) or ""
            if node.attr in LEGACY_CONTRACT_NAMES and (
                base.endswith("contracts") or base.endswith("legacy")
            ):
                hits.append(f"attribute {dotted} (line {node.lineno})")
        elif isinstance(node, ast.Call) and _dotted(node.func) == "getattr" and len(node.args) >= 2:
            name = node.args[1]
            if isinstance(name, ast.Constant) and name.value in LEGACY_CONTRACT_NAMES | {"legacy"}:
                hits.append(f"getattr(..., {name.value!r}) (line {node.lineno})")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and ("contracts.legacy" in node.value or "contracts/legacy" in node.value)
        ):
            hits.append(f"string naming contracts.legacy (line {node.lineno})")
    return hits


def test_no_removed_engine_symbols_are_bound() -> None:
    """No production module may define or import a removed engine symbol."""
    offenders: list[str] = []
    for path in _files():
        if _is_migration(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in sorted(_bound_names(tree) & REMOVED_SYMBOLS):
            offenders.append(f"{_rel(path)}: {name}")
    assert not offenders, "removed engine symbols are still bound:\n" + "\n".join(offenders)


def test_no_legacy_contract_names_are_bound() -> None:
    """No module binds a retired contract name (import, alias or definition)."""
    offenders: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in sorted(_bound_names(tree) & LEGACY_CONTRACT_NAMES):
            offenders.append(f"{_rel(path)}: {name}")
    assert not offenders, "legacy contract names are still bound:\n" + "\n".join(offenders)


def test_no_legacy_contract_references_by_any_means() -> None:
    """No import, alias, attribute access, getattr or string reaches the legacy surface."""
    offenders: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_contracts = path.is_relative_to(ENGINE / "contracts")
        offenders.extend(
            f"{_rel(path)}: {hit}" for hit in _legacy_references(tree, in_contracts=in_contracts)
        )
    assert not offenders, "legacy contracts are still referenced:\n" + "\n".join(offenders)


def test_contract_facade_exports_no_legacy_name() -> None:
    """The ``contracts`` package exposes only v3 names."""
    from wattmosaic_engine import contracts

    leaked = sorted(name for name in LEGACY_CONTRACT_NAMES if hasattr(contracts, name))
    assert not leaked, f"legacy contract names still exported: {leaked}"
    assert not hasattr(contracts, "legacy")


def test_deleted_modules_do_not_exist() -> None:
    """Every retired module is gone from the tree."""
    present = [rel for rel in DELETED_MODULES if (ENGINE / rel).exists()]
    assert not present, f"retired modules still exist: {present}"


def test_legacy_table_names_appear_only_in_migrations() -> None:
    """Outside migrations, no SQL string names a legacy table by its live name.

    Migration 25 renames them to ``*_legacy`` read-only; a runtime query against
    the old name would fail, and one against the archived name would be a read
    of archived data, which no runtime path may do either.
    """
    import re

    patterns = [
        re.compile(rf"\b(FROM|INTO|UPDATE|JOIN|TABLE)\s+\"?{table}(_legacy)?\b", re.IGNORECASE)
        for table in LEGACY_TABLES
    ]
    offenders: list[str] = []
    for path in _files():
        if _is_migration(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(p.search(node.value) for p in patterns)
            ):
                offenders.append(f"{_rel(path)}:{node.lineno}: {node.value.strip()[:80]}")
    assert not offenders, "legacy tables are still queried at runtime:\n" + "\n".join(offenders)


def test_contract_version_is_cut_over_to_3_0_0() -> None:
    """Contract 3.0.0 is the single version across engine and integration."""
    import importlib.util
    import sys

    from wattmosaic_engine import contracts
    from wattmosaic_engine.contracts import foundation

    assert contracts.CONTRACT_VERSION == "3.0.0"
    assert foundation.CONTRACT_VERSION == "3.0.0"

    const_path = INTEGRATION / "const.py"
    spec = importlib.util.spec_from_file_location("wm_z1_const", const_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.CONTRACT_VERSION == contracts.CONTRACT_VERSION


def test_gate_catches_each_evasion() -> None:
    """The reference detector flags every evasion this gate exists to close."""
    samples = {
        "from .legacy import Model": 1,
        "from ..contracts import legacy as _legacy": 1,
        "from wattmosaic_engine.contracts.legacy import Topology": 2,
        "import wattmosaic_engine.contracts.legacy": 1,
        "x = contracts.legacy.Topology": 2,
        "x = contracts.CTChannel": 1,
        "x = getattr(contracts, 'Topology')": 1,
        "x = 'wattmosaic_engine.contracts.legacy'": 1,
        "from wattmosaic_engine.contracts import Topology as DeclaredTree": 1,
    }
    for source, minimum in samples.items():
        hits = _legacy_references(ast.parse(source))
        assert len(hits) >= minimum, (source, hits)
    aliased = _bound_names(ast.parse("from x import Topology as DeclaredTree"))
    assert "Topology" in aliased and "DeclaredTree" in aliased
