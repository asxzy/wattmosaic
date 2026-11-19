"""Static gate: the cut-over removed the legacy engine paths (Z2 / Z2b).

The three-loop runtime is the only runtime.  Nothing under ``engine/`` may
still define or import the symbols that implemented the removed path: the
5 s full solve (:class:`DisaggregationEngine`), the greedy step credit, the
per-member model split / 0 W placeholder, and the old pointer-based topology
and model-interpreter helpers.

This is a *structural* gate: it reads source with ``ast`` rather than importing
modules, so a dead-but-importable module cannot keep the old behaviour alive
by accident.  Only identifier bindings (class/function definitions, imports and
module/class-level assignments) count: string literals such as a provenance
key in a historical migration are data, not a live code path.  Two locations
are deliberately excluded because they are owned by later tasks or are
historical data rewrites rather than production code paths:

* ``contracts/legacy.py`` and the contract facades: the legacy *contracts*
  (including ``branch_ct_ids`` / ``parent_ct_id``) are removed by Z1, not Z2;
* ``storage/migrations/``: migrations read legacy column/field names to
  rewrite historical rows and are intentionally frozen.

Z2b additionally forbids the *modules* that implemented the retained legacy
helpers (``electrical/models.py``, ``electrical/panel_tree.py``,
``electrical/nesting.py`` and ``disaggregation/configuration.py``).  A module
is allowed to survive only when its *sole* production importers are the
legacy contract facade (``contracts/legacy.py``) or a frozen storage migration
(``storage/migrations/``) — those are rewritten by Z1, not by this cutover.
The allowlist is explicit below so a new importer cannot quietly keep a module
alive.

Z1 extends the gate with contract-3.0.0 symbols that are genuinely gone from
the production tree (``PowerState`` and its ``power_state_for`` derivation) and
pins the cut-over version across engine and integration.  The remaining legacy
contract *models* stay reachable through the facade until the unowned tests that
import them are ported by their owners; see the task's open issues.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "wattmosaic_engine"

#: Symbols that must not be reachable as live definitions/imports under engine/.
FORBIDDEN = frozenset(
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
        # Z2b: renamed dodges from Z2 must not survive under another spelling.
        "_power_for_params",
        "_infer_model_type",
        "parent_endpoint_id",
        # Z1: contract 3.0.0 removes the legacy provenance/power-state axis
        # entirely (architecture.md §5).  The v3 surface uses EstimateStatus,
        # IdentifiabilityStatus and PowerLevelV3 instead.
        "PowerState",
        "power_state_for",
    }
)

#: Historical/data-rewrite sources where field *names* are data, not code.
EXCLUDED = (
    "contracts/legacy.py",
    "storage/migrations/",
)


def _excluded(path: Path) -> bool:
    rel = path.relative_to(ENGINE).as_posix()
    return any(rel == e or rel.startswith(e) for e in EXCLUDED)


def _bound_names(tree: ast.Module) -> set[str]:
    """Names bound at module or class scope (definitions, imports, fields)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.add(alias.asname or alias.name)
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


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        if "__pycache__" in path.parts or _excluded(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits = _bound_names(tree) & FORBIDDEN
        for name in sorted(hits):
            out.append(f"{path.relative_to(ROOT).as_posix()}: {name}")
    return out


def test_no_legacy_symbols_are_reachable() -> None:
    """No production module may define or import a removed legacy symbol."""
    offenders = _offenders()
    assert not offenders, "legacy engine paths are still reachable:\n" + "\n".join(offenders)


#: Legacy helper modules Z2b retires.  Each value is the *explicit* allowlist
#: of importers permitted to keep a module alive: only the Z1-owned legacy
#: contract facade and frozen storage migrations.  Any other importer — even a
#: sibling module in the same package — keeps the module forbidden.
REMOVED_MODULES: dict[str, frozenset[str]] = {
    "electrical/models.py": frozenset({"contracts/legacy.py", "storage/migrations/"}),
    "electrical/panel_tree.py": frozenset({"contracts/legacy.py", "storage/migrations/"}),
    "electrical/nesting.py": frozenset({"contracts/legacy.py", "storage/migrations/"}),
    "disaggregation/configuration.py": frozenset(
        {"contracts/legacy.py", "storage/migrations/"}
    ),
}


def _module_name(rel: str) -> str:
    """Importable module path for a source file relative to ENGINE."""
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return "wattmosaic_engine" + (("." + ".".join(parts)) if parts else "")


def _importers_of(module: str) -> set[str]:
    """Relative paths under ENGINE importing exactly ``module`` (AST-based).

    Reading source rather than importing modules means a dead-but-importable
    module cannot hide from the gate.  A submodule import is a distinct target
    and is ignored; the removed module never imports itself.
    """
    want = _module_name(module)
    found: set[str] = set()
    for path in sorted(ENGINE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(ENGINE).as_posix()
        if rel == module:
            continue
        source_module = _module_name(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == want for alias in node.names):
                    found.add(rel)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.level == 0:
                    target = ".".join([source_module, node.module])
                else:
                    base = source_module.split(".")
                    target = ".".join([*base[: max(1, len(base) - node.level)], node.module])
                if target == want:
                    found.add(rel)
    return found


def _is_allowed_importer(rel: str, allowed: frozenset[str]) -> bool:
    return any(rel == a or rel.startswith(a) for a in allowed)


def test_removed_legacy_modules_are_deleted() -> None:
    """Retired legacy helper modules may exist only for Z1/storage consumers."""
    offenders: list[str] = []
    for module, allowed in REMOVED_MODULES.items():
        if not (ENGINE / module).exists():
            continue
        unexpected = sorted(
            imp for imp in _importers_of(module) if not _is_allowed_importer(imp, allowed)
        )
        if unexpected:
            offenders.append(
                f"{module}: imported by {', '.join(unexpected)} "
                f"(allowed only by: {', '.join(sorted(allowed))})"
            )
    assert not offenders, "legacy helper modules are still alive:\n" + "\n".join(offenders)


def test_contract_version_is_cut_over_to_3_0_0() -> None:
    """Z1: contract 3.0.0 is the single version across engine and integration.

    The legacy contract module no longer pins 2.4.0; the authoritative constant
    lives in ``contracts/foundation.py`` and is mirrored by the HA integration.
    """
    from wattmosaic_engine import contracts
    from wattmosaic_engine.contracts import foundation, legacy

    assert contracts.CONTRACT_VERSION == "3.0.0"
    assert foundation.CONTRACT_VERSION == "3.0.0"
    assert legacy.CONTRACT_VERSION == "3.0.0"

    # The integration mirror is loaded from source (HA is not installed here).
    import importlib.util
    import sys

    const_path = ROOT / "custom_components" / "wattmosaic" / "const.py"
    spec = importlib.util.spec_from_file_location("wm_z1_const", const_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.CONTRACT_VERSION == contracts.CONTRACT_VERSION


def test_no_legacy_names_in_zl() -> None:
    """ZL (models + learning + aggregation + model/learning repositories).

    Contract 3.0 removed the legacy power-model classes, the sample-based
    learning pipeline and the pointer-based contracts.  No file in the ZL area
    may define or import any of those names; the surviving read adapters for
    archived model rows use local duck-typed value objects instead.
    """
    legacy_names = frozenset(
        {
            "CTChannel",
            "DeviceTopology",
            "Topology",
            "branch_ct_ids",
            "parent_ct_id",
            "paired_ct_id",
            "alias_of_ct_id",
            "Source",
            "PowerState",
            "ConfidenceTier",
            "TopologyStatus",
            "RelationKind",
            "MappingObservability",
            "MappingCellStatus",
            "InferenceResult",
            "Reconciliation",
            "DeviceReaderMappingCell",
            "DeviceModelVersion",
            "DeviceModelParameters",
            "LearningSample",
            "Measurement",
            "DeviceEvent",
            "EventEvidenceRecord",
            "ConfigurationChangeEvent",
            "TopologyEdge",
            "TopologyVersion",
            "UnknownLoadProfile",
            "PowerLevel",
            "ConfidenceVector",
        }
    )
    area = (
        ENGINE / "models",
        ENGINE / "learning",
        ENGINE / "aggregation",
    )
    files = [p for root in area for p in root.rglob("*.py")]
    files += [
        ENGINE / "storage" / "repositories" / "models.py",
        ENGINE / "storage" / "repositories" / "learning.py",
    ]
    offenders: list[str] = []
    for path in files:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits = _bound_names(tree) & legacy_names
        for name in sorted(hits):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}: {name}")
    assert not offenders, "ZL area still references legacy names:\n" + "\n".join(offenders)
# --------------------------------------------------------------------------- #
# ZT: topology / disaggregation area — no legacy contract names.
# --------------------------------------------------------------------------- #
#
# Contract 3.0 removes the legacy contract models and enums.  Every file owned
# by the topology/disaggregation area must name the v3 contracts (or the neutral
# migration seam ``electrical/_declared_shapes.py``) instead.  The seam is the
# single documented bridge that still imports the retired models so the
# read-only projections stay byte-identical; ZC rewrites only that file at the
# final cutover.  It is excluded from this area-scoped assertion exactly as
# ``contracts/legacy.py`` is excluded from the global gate above.

ZT_AREA_FILES = (
    "electrical/topology.py",
    "electrical/factors.py",
    "electrical/hierarchy.py",
    "electrical/main_power.py",
    "electrical/topology_change.py",
    "electrical/topology_inference.py",
    "electrical/__init__.py",
    "disaggregation/association.py",
    "disaggregation/audit.py",
    "disaggregation/auto_placement.py",
    "disaggregation/circuits.py",
    "disaggregation/mapping.py",
    "disaggregation/reconcile.py",
    "disaggregation/tiers.py",
    "disaggregation/group_fit.py",
    "disaggregation/isolation.py",
    "disaggregation/_shared.py",
    "device_configuration.py",
    "device_links.py",
    "mapping/display_tree.py",
    "versioning/__init__.py",
    "versioning/service.py",
    "versioning/workflow.py",
    "core/topology_api.py",
    "core/discovery_api.py",
    "api/routes/topology.py",
    "storage/repositories/topology.py",
    "storage/repositories/associations.py",
)

#: Retired contract names eliminated from this area at contract 3.0.
ZT_LEGACY_NAMES = frozenset(
    {
        "CTChannel",
        "DeviceTopology",
        "Topology",
        "branch_ct_ids",
        "parent_ct_id",
        "paired_ct_id",
        "alias_of_ct_id",
        "Source",
        "PowerState",
        "ConfidenceTier",
        "TopologyStatus",
        "RelationKind",
        "MappingObservability",
        "MappingCellStatus",
        "InferenceResult",
        "Reconciliation",
        "DeviceReaderMappingCell",
        "DeviceModelVersion",
        "DeviceModelParameters",
        "LearningSample",
        "Measurement",
        "DeviceEvent",
        "EventEvidenceRecord",
        "ConfigurationChangeEvent",
        "TopologyEdge",
        "TopologyVersion",
        "UnknownLoadProfile",
        "PowerLevel",
        "ConfidenceVector",
    }
)


def test_no_legacy_names_in_topology_disaggregation() -> None:
    """No topology/disaggregation area file binds a retired contract name."""
    offenders: list[str] = []
    for rel in ZT_AREA_FILES:
        path = ENGINE / rel
        if not path.exists():
            continue  # a module deleted in a later cutover cannot offend
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _bound_names(tree) & ZT_LEGACY_NAMES
        for name in sorted(hits):
            offenders.append(f"{rel}: {name}")
    assert not offenders, "legacy contract names remain in the area:\n" + "\n".join(
        offenders
    )
