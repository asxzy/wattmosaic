"""Static gate: refuse a hard-coded two-second sampling cadence.

Spec ``00-project-overview.md`` §"cadence" records the audited CT
state-update cadence as a median of ~15.7 s, and states that no event window,
sampling grid or quality threshold may assume 2 s.  A two-second constant is
therefore only ever legitimate when it is *not* a cadence assumption, and the
two-second cadence assumption itself must be visible at review time -- it is a
property of the source text, so it is checked statically rather than at runtime.

Rule
----
Walk every ``engine/**/*.py`` file as an AST and flag an ``Assign`` or
``AnnAssign`` whose

* target is a plain ``Name`` (not an attribute, subscript or tuple), and
* value is the numeric literal ``2.0`` / ``2000.0`` -- i.e. a
  ``Constant`` and not a ``BinOp``, so midpoint arithmetic such as
  ``(a + b) / 2.0`` is never read as a cadence; and
* target name belongs to the cadence vocabulary: it contains ``cadence``,
  ``sample_interval``, ``sampling_interval``, ``poll_interval``,
  ``update_interval``, ``report_interval``, ``sample_period``,
  ``sampling_period`` or ``flush_interval`` (case-insensitive), **and** it is
  a seconds-valued name.  A ``_S`` suffix, or a ``_seconds``/``_ms``/``_hz``
  suffix, makes the unit explicit; a bare ``cadence``/``*interval`` name with
  no unit suffix is also accepted because it is unambiguous.

A name outside that vocabulary (``max_age_factor``, ``tolerance_w``, ...) or a
count of *things* rather than seconds (``MIN_CADENCE_INTERVALS``, which counts
gaps) cannot be a cadence and is ignored.

Allowlist
---------
A flagged name may carry a reason in :data:`ALLOWLIST`.  The reason is the
audit: it states why the two seconds are a batching/multiplicity/independence
constant and not a measurement cadence.  A name that is not allowlisted fails.

Exit status is 1 with a named ``file:line`` for every refusal, 0 otherwise.

Usage::

    python tools/dev/check_cadence.py [--root PATH] [--quiet]

This checker is standalone; it does not import ``tools/dev/verify.py`` and is
not wired into it.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"

#: The two-second values that would encode the refused cadence assumption.
#: ``2000.0``/``2000`` covers the millisecond spelling of the same assumption.
FORBIDDEN_LITERALS = {2.0, 2000.0}

#: Case-insensitive substrings that make a name part of the cadence vocabulary.
#: A name must match one of these to be considered at all.
CADENCE_NAME_MARKERS = (
    "cadence",
    "sample_interval",
    "sampling_interval",
    "poll_interval",
    "update_interval",
    "report_interval",
    "sample_period",
    "sampling_period",
    "flush_interval",
)

#: Suffixes that state the unit is seconds (or a frequency derived from it).
#: A cadence name without one of these must be a bare ``cadence``/``*interval``
#: form, which is unambiguous; ``MIN_CADENCE_INTERVALS`` is a count of gaps and
#: carries neither the vocabulary nor a unit, so it is not a seconds value.
SECONDS_SUFFIXES = ("_s", "_seconds", "_ms", "_hz")


def _denotes_seconds(name: str) -> bool:
    """True when ``name`` is a seconds-valued cadence name, not a count of them."""
    lowered = name.lower()
    if lowered.endswith(SECONDS_SUFFIXES):
        return True
    # No explicit unit: accept the unambiguous bare forms only.
    return lowered.endswith(("cadence", "interval", "period"))

#: Names that legitimately hold two seconds for a reason other than a sampling
#: cadence.  Each entry is the reason recorded at review time; an empty reason
#: is refused by the checker so the allowlist cannot be used to silence a
#: finding without an audit.
ALLOWLIST: dict[str, str] = {
    "SAMPLE_FLUSH_INTERVAL_S": "writer batching interval in main.py; how often the persist queue is flushed, not a CT sampling cadence",
    "MIN_PAIR_GAP_S": "minimum separation for two transitions of one entity to count as independent in ingestion/correlation.py; event independence, not a sampling cadence",
    "before_multiplier": "multiplier applied to an already-observed cadence in electrical/window.py; the cadence itself is derived, this only scales the window",
}


def _literal_value(node: ast.expr) -> float | int | None:
    """The value of a numeric ``Constant`` node, or ``None`` for anything else.

    ``BinOp``/``Call``/``Name`` values return ``None``: the gate reads only a
    literal constant, so arithmetic such as ``(a + b) / 2.0`` is not a cadence
    assumption.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    return None


def _is_forbidden_cadence_target(target: ast.expr, value: ast.expr) -> bool:
    """True when ``target = value`` is a two-second cadence-vocabulary literal."""
    if not isinstance(target, ast.Name):
        return False
    literal = _literal_value(value)
    if literal is None or literal not in FORBIDDEN_LITERALS:
        return False
    lowered = target.id.lower()
    if not any(marker in lowered for marker in CADENCE_NAME_MARKERS):
        return False
    return _denotes_seconds(target.id)


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line, name, reason_or_empty)`` for every refused assignment."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if _is_forbidden_cadence_target(target, value):
                findings.append((node.lineno, target.id, ALLOWLIST.get(target.id, "")))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: %(default)s)")
    parser.add_argument("--quiet", action="store_true", help="suppress the pass message")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    engine = root / "engine"
    if not engine.is_dir():
        print(f"error: no engine directory under {root}", file=sys.stderr)
        return 1

    refused: list[tuple[str, int, str]] = []
    for path in sorted(engine.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for line, name, reason in scan_file(path):
            if not reason:
                refused.append((rel, line, name))

    if refused:
        for rel, line, name in refused:
            print(f"{rel}:{line}: hard-coded two-second cadence assumption in {name!r}")
        print(
            f"\n{len(refused)} hard-coded two-second cadence assumption(s). "
            "Derive the cadence from the observed history, or record a reason in "
            "ALLOWLIST in tools/dev/check_cadence.py.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print("check_cadence: no hard-coded two-second cadence assumption found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
