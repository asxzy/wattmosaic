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

The gate is extended by Z1 with the remaining contract symbols.
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
