"""Static gate: the cut-over removed the legacy engine paths (Z2).

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
