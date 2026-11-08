"""Negative verification: remove each guard, prove its test fails.

A guard is only proven if *its own* test fails when the guard is gone.  This
harness therefore does three things a naive version gets wrong:

* it clears ``__pycache__`` and sets ``PYTHONDONTWRITEBYTECODE`` so a removed
  guard is never masked by stale bytecode;
* it disables the pytest cache, which otherwise skips re-reading a changed file;
* it requires the test to fail for the *expected* reason by asserting the test
  passes on the restored file immediately afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/asxzy/src/wattmosaic")
SRC = ROOT / "engine/wattmosaic_engine/electrical/nesting.py"
TEST_FILE = "tests/unit/test_nesting.py"

#: (source file, test file) for guards that live outside the inference module.
EXTRA_SRC = ROOT / "custom_components/wattmosaic/flow_logic.py"
EXTRA_TESTS = "tests/unit/test_nesting_wizard.py"

ENV = dict(os.environ)
ENV["PYTHONDONTWRITEBYTECODE"] = "1"


def purge_cache() -> None:
    for path in ROOT.rglob("__pycache__"):
        if ".venv" in path.parts:
            continue
        shutil.rmtree(path, ignore_errors=True)


def run(test: str, test_file: str = TEST_FILE) -> int:
    purge_cache()
    proc = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "pytest",
            test_file,
            "-q",
            "-p",
            "no:cacheprovider",
            "-k",
            test,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=ENV,
    )
    return proc.returncode


CASES = [
    # (guard, old, new, test)
    (
        "activity_ratio_floor",
        "if evidence.activity_ratio < DUPLICATE_ACTIVITY_RATIO_MIN:",
        "if False:",
        "test_a_duplicate_is_not_reported_without_joint_activity",
    ),
    (
        "rel_diff_ceiling",
        "if evidence.rel_diff > DUPLICATE_REL_DIFF_MAX:",
        "if False:",
        "test_a_duplicate_is_not_reported_when_the_readings_disagree_instant_by_instant",
    ),
    (
        "ratio_ceiling",
        "if evidence.ratio > DUPLICATE_RATIO_MAX:",
        "if False:",
        "test_a_duplicate_is_not_reported_when_one_feed_is_a_consistent_multiple",
    ),
    (
        "declared_pair_exclusion",
        "if any(_declared_pair(x, y, channels) for x in node_a.members for y in node_b.members):",
        "if False:",
        "test_a_one_sided_declared_pair_is_never_collapsed",
    ),
    (
        "declared_parent_suppression",
        'if any(str(channels.get(m, {}).get("parent_ct_id") or "") for m in child_node.members):',
        "if False:",
        "test_a_declared_parent_is_not_re_proposed",
    ),
    (
        "main_only_child",
        "if child_node.ct_type not in _NESTABLE_CHILD_TYPES:",
        "if False:",
        "test_a_load_is_never_proposed_as_a_nested_child",
    ),
    (
        "excitation_floor",
        "if parent_peak <= 0.0 or peak / parent_peak < excitation_min_ratio:",
        "if False:",
        "test_an_idle_feed_is_never_nested_under_the_whole_home",
    ),
    (
        "containment_min",
        "if comparable == 0 or fraction < containment_min:",
        "if comparable == 0:",
        "test_a_child_that_exceeds_its_parent_is_not_nested",
    ),
    (
        "closure_rejection",
        "if closure is not None and closure[1] and closure[0] < closure_min:",
        "if False:",
        "test_a_parent_over_explained_by_the_child_is_rejected",
    ),
    (
        "reciprocal_pair_required",
        'if paired and paired in channels and str(channels[paired].get("paired_ct_id") or "") == ct_id:',
        "if paired and paired in channels:",
        "test_a_non_reciprocal_pair_is_not_grouped",
    ),
    (
        "leg_count_exactly_two",
        "if len(legs) == 2:",
        "if len(legs) >= 1:",
        "test_a_lone_leg_is_not_grouped_with_anything",
    ),
    (
        "node_sums_legs",
        "total += float(raw)\n                    seen = True",
        "total = float(raw)\n                    seen = True",
        "test_a_node_reading_is_the_sum_of_its_members",
    ),
    (
        "closure_skips_unknown",
        "if not _finite(p) or not _finite(a):\n            continue",
        "if not _finite(p):\n            continue\n        a = a if _finite(a) else 0.0",
        "test_closure_skips_instants_where_the_accounted_sum_is_unknown",
    ),
    (
        "containment_min_outer_floor",
        "if o < min_outer_w:\n            continue",
        "if False:\n            continue",
        "test_containment_skips_instants_below_the_parent_floor",
    ),
]

EXTRA_CASES = [
    (
        "declared_fact_suppresses_alias",
        'if channel.get("paired_ct_id") or channel.get("parent_ct_id"):',
        "if False:",
        "test_a_declared_fact_suppresses_the_alias_suggestion",
    ),
    (
        "alias_self_cycle_guard",
        """    candidates = alias_candidates(channel)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if candidate == ct_id or candidate in descendant_ids(ct_id, channels):
        return None
    return candidate""",
        """    candidates = alias_candidates(channel)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    return candidate""",
        "test_an_alias_never_suggests_itself_or_a_descendant",
    ),
    (
        "alias_not_read_from_parents",
        'raw = meta.get("alias_candidates")',
        'raw = meta.get("nesting_candidates")',
        "test_a_parent_proposal_is_not_read_as_an_alias",
    ),
]

# No guard is currently unverifiable.  If one becomes so, record it here with the
# reason rather than letting it pass silently.
STRUCTURAL: dict[str, str] = {}


def main() -> int:
    original = SRC.read_text()
    results: list[tuple[str, str, str]] = []
    try:
        for name, old, new, test in CASES:
            if old not in original:
                results.append((name, "PATTERN-MISSING", test))
                continue
            SRC.write_text(original.replace(old, new, 1))
            try:
                removed = run(test)
            finally:
                SRC.write_text(original)
            restored = run(test)
            if removed == 0:
                results.append((name, "STILL-PASSES-BAD", test))
            elif restored != 0:
                results.append((name, "TEST-UNSTABLE", test))
            else:
                results.append((name, "FAILS-AS-REQUIRED", test))
        extra_original = EXTRA_SRC.read_text()
        try:
            for name, old, new, test in EXTRA_CASES:
                if old not in extra_original:
                    results.append((name, "PATTERN-MISSING", test))
                    continue
                EXTRA_SRC.write_text(extra_original.replace(old, new, 1))
                try:
                    removed = run(test, EXTRA_TESTS)
                finally:
                    EXTRA_SRC.write_text(extra_original)
                restored = run(test, EXTRA_TESTS)
                if removed == 0:
                    results.append((name, "STILL-PASSES-BAD", test))
                elif restored != 0:
                    results.append((name, "TEST-UNSTABLE", test))
                else:
                    results.append((name, "FAILS-AS-REQUIRED", test))
        finally:
            EXTRA_SRC.write_text(extra_original)
    finally:
        SRC.write_text(original)
        purge_cache()

    bad = 0
    print(f"{'guard':34s} {'result':22s} test")
    for name, res, test in results:
        mark = "OK " if res == "FAILS-AS-REQUIRED" else "XX "
        if res != "FAILS-AS-REQUIRED":
            bad += 1
        print(f"{mark}{name:32s} {res:22s} {test}")
    print()
    for name, why in STRUCTURAL.items():
        print(f"-- {name}: structurally unreachable, not verifiable: {why}")
    print(
        f"\n{len(results) - bad}/{len(results)} guards negatively verified "
        f"(+{len(STRUCTURAL)} recorded as structural)"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
