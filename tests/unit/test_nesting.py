"""Nesting / duplicate-feed inference from aligned measurements.

Every expectation here is derived from a synthetic series whose wiring is known
by construction.  The point of the module is that it never states a wiring fact:
it returns *candidates*, and these tests check both that it finds the real
relationship and that it refuses the tempting wrong ones.

Two design rules are load-bearing and are tested explicitly:

* inference is done on **measurement nodes** (a CT plus its declared partner),
  never on a single leg, because comparing legs compares different conductors;
* a **load's** level is not measurable -- it is contained by every panel while
  idle -- so loads are never proposed and are reported as ``not_inferable``.
"""

from __future__ import annotations

import math
import random

import pytest
from wattmosaic_engine.electrical.nesting import (
    ACTIVITY_MIN_W,
    DUPLICATE_RATIO_MAX,
    DUPLICATE_REL_DIFF_MAX,
    build_nodes,
    closure_fraction,
    containment,
    duplicate_evidence,
    infer_nesting,
)

MAIN = {"ct_type": "main"}
BRANCH = {"ct_type": "branch"}
LOAD = {"ct_type": "dedicated_device"}


def _noisy(base: float, n: int, *, seed: int = 7, jitter: float = 0.02) -> list[float]:
    rng = random.Random(seed)
    return [base * (1.0 + rng.uniform(-jitter, jitter)) for _ in range(n)]


def _added(a: list[float], b: list[float]) -> list[float]:
    return [x + y for x, y in zip(a, b, strict=True)]


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #
def test_containment_holds_when_the_child_never_exceeds_the_parent():
    parent = [1000.0] * 10
    child = [500.0] * 10
    fraction, n = containment(parent, child)
    assert n == 10
    assert fraction == 1.0


def test_containment_fails_when_the_child_exceeds_the_parent():
    parent = [1000.0] * 10
    child = [1500.0] * 10
    fraction, _ = containment(parent, child)
    assert fraction == 0.0


def test_containment_skips_instants_below_the_parent_floor():
    """At a few watts the comparison is noise, so the instant is skipped."""
    parent = [50.0, 1000.0]
    child = [49.0, 500.0]
    fraction, n = containment(parent, child)
    assert n == 1
    assert fraction == 1.0


def test_containment_of_nothing_is_zero_not_one():
    """No comparable instants must never read as "contained"."""
    fraction, n = containment([1.0], [1.0])
    assert (fraction, n) == (0.0, 0)


def test_misaligned_series_are_rejected():
    with pytest.raises(ValueError):
        containment([1.0, 2.0], [1.0])


def test_non_finite_samples_are_skipped_not_treated_as_contained():
    outer = [math.nan, 1000.0, 2000.0]
    inner = [0.0, 100.0, math.inf]
    fraction, n = containment(outer, inner)
    assert n == 1
    assert fraction == 1.0


# --------------------------------------------------------------------------- #
# Duplicate evidence
# --------------------------------------------------------------------------- #
def test_duplicate_evidence_detects_one_wire_measured_twice():
    wire = _noisy(2400.0, 200)
    other = _noisy(2412.0, 200, seed=11)
    evidence = duplicate_evidence(wire, other)
    assert evidence is not None
    assert evidence.activity_ratio >= 0.99
    assert evidence.rel_diff < 0.02
    assert evidence.ratio < 1.02


def test_duplicate_evidence_is_absent_without_joint_activity():
    """Two idle channels agreeing about nothing is the absence of evidence."""
    idle_a = _noisy(4.0, 100, seed=3)
    idle_b = _noisy(5.0, 100, seed=4)
    assert duplicate_evidence(idle_a, idle_b) is None


def test_two_loads_active_at_different_times_are_not_a_duplicate():
    """Each is live alone, so the pair is almost never active *together*.

    This is the guard the old mutual-containment test lacked.  The distinction
    matters: when one of the pair is idle, two unrelated channels agree to the
    watt on everything, so a containment-style test saw every idle pair as one
    wire.  Here both series are individually active half the time, and agree
    exactly whenever they overlap -- only ``activity_ratio`` rejects them.
    """
    n = 400
    # They overlap for exactly 20 % of their combined active time, and agree
    # perfectly whenever they do, so rel_diff and ratio both look like one wire.
    a = [2400.0 if i % 2 == 0 else 0.0 for i in range(n)]
    b = [2400.0 if i % 4 in (1, 2) else 0.0 for i in range(n)]
    evidence = duplicate_evidence(a, b)
    assert evidence is not None, "there *is* joint activity, so this is a real test"
    assert evidence.rel_diff < 0.01, "they agree perfectly while both are live"
    assert evidence.ratio < 1.05
    assert evidence.activity_ratio < 0.5, "but they are rarely live at once"


def test_a_feed_reporting_a_consistent_multiple_is_not_a_duplicate():
    """A x2 circuit multiplier is one wire reported twice, not two wires.

    ``rel_diff`` cannot separate this: a doubled reading differs by exactly 50 %
    of the larger at every instant, which is the same *shape* as a real alias
    disagreeing by a few percent.  The median-magnitude ratio is what tells them
    apart, and collapsing it would silently discard the multiplier.
    """
    full = _noisy(2400.0, 200, seed=31)
    half = [x / 2.0 for x in full]
    evidence = duplicate_evidence(full, half)
    assert evidence is not None
    assert evidence.ratio > DUPLICATE_RATIO_MAX


def test_two_feeds_that_merely_track_each_other_are_not_a_duplicate():
    """Two different wires can move together yet disagree on magnitude.

    Constructed so ``activity_ratio`` and ``ratio`` both pass: both are always
    active and their medians match.  Only the per-instant relative difference
    rejects them, which is the case a nested sub-panel actually presents.
    """
    rng = random.Random(41)
    common = [800.0 + rng.uniform(-300.0, 300.0) for _ in range(400)]
    # Same median, same activity, but independent per-instant variation.
    other = [800.0 + rng.uniform(-300.0, 300.0) for _ in range(400)]
    evidence = duplicate_evidence(common, other)
    assert evidence is not None
    assert evidence.activity_ratio >= 0.5
    assert evidence.ratio <= DUPLICATE_RATIO_MAX
    assert evidence.rel_diff > DUPLICATE_REL_DIFF_MAX


# --------------------------------------------------------------------------- #
# Closure
# --------------------------------------------------------------------------- #
def test_closure_holds_when_the_parent_covers_its_loads_and_child():
    parent = [3000.0] * 10
    accounted = [2600.0] * 10
    fraction, n, median = closure_fraction(parent, accounted)
    assert fraction == 1.0
    assert n == 10
    assert median == pytest.approx(400.0)


def test_closure_fails_when_the_parent_is_over_explained():
    parent = [1000.0] * 10
    accounted = [1500.0] * 10
    fraction, n, _ = closure_fraction(parent, accounted)
    assert n == 10
    assert fraction == 0.0


def test_closure_skips_instants_where_the_accounted_sum_is_unknown():
    """A missing own-load sample must be skipped, never substituted as zero."""
    parent = [3000.0, 3000.0]
    accounted = [None, 2000.0]
    fraction, n, _ = closure_fraction(parent, accounted)
    assert n == 1
    assert fraction == 1.0


# --------------------------------------------------------------------------- #
# Measurement nodes
# --------------------------------------------------------------------------- #
def test_a_declared_double_pole_pair_is_one_node():
    channels = {
        "load_ct1": {"ct_type": "branch", "phase": "l1", "paired_ct_id": "load_ct2"},
        "load_ct2": {"ct_type": "branch", "phase": "l2", "paired_ct_id": "load_ct1"},
    }
    nodes = build_nodes(channels)
    assert len(nodes) == 1
    node = next(iter(nodes.values()))
    assert node.members == ("load_ct1", "load_ct2")
    assert node.grouping == "declared_pair"
    assert node.is_assumed is False


def test_a_split_phase_panel_is_one_node():
    """Two legs of a panel feed are one meter point, not two."""
    channels = {
        "panel_l1": {"ct_type": "main", "phase": "l1"},
        "panel_l2": {"ct_type": "main", "phase": "l2"},
    }
    nodes = build_nodes(channels)
    assert len(nodes) == 1
    node = next(iter(nodes.values()))
    assert node.members == ("panel_l1", "panel_l2")
    assert node.grouping == "name_stem"
    # The grouping is an assumption read off the channel ids, and says so.
    assert node.is_assumed is True


def test_a_non_reciprocal_pair_is_not_grouped():
    """A one-sided declaration is not a declaration."""
    channels = {
        "load_ct1": {"ct_type": "branch", "paired_ct_id": "load_ct2"},
        "load_ct2": {"ct_type": "branch", "paired_ct_id": None},
    }
    nodes = build_nodes(channels)
    assert len(nodes) == 2


def test_a_lone_leg_is_not_grouped_with_anything():
    """A panel feed published as only one leg is one meter point on its own.

    The stem rule must require *both* legs: grouping a single ``_l1`` would
    invent a second leg that was never measured.
    """
    channels = {"panel_l1": {"ct_type": "main", "phase": "l1"}}
    nodes = build_nodes(channels)
    assert len(nodes) == 1
    node = next(iter(nodes.values()))
    assert node.members == ("panel_l1",)
    assert node.grouping == "single"
    assert node.is_assumed is False


def test_three_legs_sharing_a_stem_are_not_a_panel():
    """A family of circuits that share a name is not one meter point."""
    channels = {
        "lights_ct1": {"ct_type": "main"},
        "lights_ct2": {"ct_type": "main"},
        "lights_ct3": {"ct_type": "main"},
    }
    nodes = build_nodes(channels)
    assert len(nodes) == 3


# --------------------------------------------------------------------------- #
# Whole-picture inference
# --------------------------------------------------------------------------- #
def _house():
    """service -> panel -> sub-panel, plus loads at each level.

    Every number is constructed so the wiring is unambiguous: each child is a
    fraction of its own parent and the parents add up exactly.
    """
    n = 200
    sub_loads = _noisy(400.0, n, seed=1)
    sub_other = _noisy(120.0, n, seed=4)
    sub_feed = _added(sub_loads, sub_other)
    panel_loads = _noisy(900.0, n, seed=2)
    panel_feed = _added(panel_loads, sub_feed)
    service_loads = _noisy(700.0, n, seed=3)
    service = _added(service_loads, panel_feed)
    return {
        "service": service,
        "service_load": service_loads,
        "panel": panel_feed,
        "panel_load": panel_loads,
        "sub": sub_feed,
        "sub_load": sub_loads,
        "sub_other": sub_other,
    }


def test_three_level_feed_nesting_is_proposed():
    series = _house()
    channels = {
        "service": MAIN,
        "panel": MAIN,
        "sub": MAIN,
        "service_load": LOAD,
        "panel_load": LOAD,
        "sub_load": LOAD,
    }
    candidates = infer_nesting(
        channels,
        series,
        container_ct_ids=["service", "panel", "sub"],
        direct_loads={
            "service": series["service_load"],
            "panel": series["panel_load"],
            "sub": series["sub_other"],
        },
    )
    nested = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    assert nested["sub"] == "panel", "the sub-panel hangs off the panel, not the service"
    assert nested["panel"] == "service"


def test_a_level_of_nesting_is_chosen_by_the_tightest_closure():
    """The parent the child explains most tightly is the one it hangs off.

    A grandparent always has the child's siblings left over, so its median
    remainder is larger.  This is what places a candidate at a *level*.
    """
    series = _house()
    channels = {"service": MAIN, "panel": MAIN, "sub": MAIN}
    candidates = infer_nesting(
        channels,
        series,
        container_ct_ids=["service", "panel", "sub"],
        direct_loads={
            "service": series["service_load"],
            "panel": series["panel_load"],
            "sub": series["sub_other"],
        },
    )
    by_child = {c.child_ct_id: c for c in candidates if c.kind == "nested"}
    assert by_child["sub"].parent_ct_id == "panel"
    assert by_child["sub"].evidence["closure_median_remainder_w"] == pytest.approx(
        0.0, abs=1.0
    )


def test_a_parent_over_explained_by_the_child_is_rejected():
    """A parent whose own loads already account for it cannot contain the child.

    Containment alone would accept this pair: the child never exceeds the parent.
    What rules it out is that the parent's *own* metered loads plus the child
    exceed the parent at every instant, so the child cannot be behind it.
    """
    n = 100
    service = _noisy(3000.0, n, seed=81)
    child = _noisy(2000.0, n, seed=82)  # contained, well excited
    candidates = infer_nesting(
        {"service": MAIN, "child": MAIN},
        {"service": service, "child": child},
        container_ct_ids=["service", "child"],
        # The parent's own loads already total more than the child could fit in.
        direct_loads={"service": _noisy(2000.0, n, seed=83)},
    )
    nested = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    assert "child" not in nested


def test_a_load_is_never_proposed_as_a_nested_child():
    """A load's level is not measurable, so the module refuses to guess.

    Measured on the reference installation, a 9 W lighting circuit is contained
    by every panel in the house at 1.000 while idle.  Proposing a parent would
    turn a coin flip into a wiring fact, so every load is reported as
    ``not_inferable`` instead.
    """
    series = _house()
    channels = {
        "service": MAIN,
        "panel": MAIN,
        "sub": MAIN,
        "service_load": LOAD,
        "panel_load": LOAD,
        "sub_load": LOAD,
    }
    report: dict = {}
    candidates = infer_nesting(
        channels,
        series,
        container_ct_ids=["service", "panel", "sub"],
        direct_loads={
            "service": series["service_load"],
            "panel": series["panel_load"],
            "sub": series["sub_other"],
        },
        report=report,
    )
    for candidate in candidates:
        assert channels[candidate.child_ct_id]["ct_type"] == "main"
    not_inferable = {entry["ct_id"] for entry in report["not_inferable"]}
    assert {"service_load", "panel_load", "sub_load"} <= not_inferable


def test_every_candidate_requires_confirmation():
    series = _house()
    channels = {cid: MAIN for cid in ("service", "panel", "sub")}
    for candidate in infer_nesting(
        channels, series, container_ct_ids=["service", "panel", "sub"]
    ):
        assert candidate.requires_confirmation is True
        assert candidate.as_dict()["status"] == "candidate"


def test_an_idle_feed_is_never_nested_under_the_whole_home():
    """A feed drawing a few watts of standby is contained by everything.

    ``peak > 0`` is not enough to count as excitation: the feed must actually
    reach a real fraction of the parent.  Without the excitation rule every
    always-on standby channel would be nested under every panel above it.
    """
    n = 100
    service = _noisy(3000.0, n)
    standby = _noisy(4.0, n, seed=9)  # ~0.13 % of the service, never zero
    assert max(standby) > 0.0, "the peak rule alone must not be what rejects this"
    candidates = infer_nesting(
        {"service": MAIN, "standby": MAIN},
        {"service": service, "standby": standby},
        container_ct_ids=["service", "standby"],
        direct_loads={"service": [0.0] * n},
    )
    assert not [c for c in candidates if c.kind == "nested"]


def test_a_child_that_exceeds_its_parent_is_not_nested():
    """A feed larger than another cannot be fed by it.

    Containment runs in one direction only, so the smaller is *not* proposed as
    a child of the larger -- the larger exceeds it at every instant.
    """
    n = 100
    service = _noisy(1000.0, n)
    larger = _noisy(2000.0, n, seed=5)
    candidates = infer_nesting(
        {"service": MAIN, "larger": MAIN},
        {"service": service, "larger": larger},
        container_ct_ids=["service", "larger"],
    )
    nested = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    assert "larger" not in nested


def test_a_duplicate_is_not_reported_without_joint_activity():
    """At the inference level, rare co-activity is not a duplicate.

    Both feeds are live half the time and agree exactly whenever they overlap,
    so ``rel_diff`` and ``ratio`` both look like one wire.  Only the joint-activity
    floor rejects the pair, which is the whole reason it exists: two circuits that
    are each idle most of the time agree about nothing.
    """
    n = 400
    a = [2400.0 if i % 2 == 0 else 0.0 for i in range(n)]
    b = [2400.0 if i % 4 in (1, 2) else 0.0 for i in range(n)]
    candidates = infer_nesting(
        {"feed_a": MAIN, "feed_b": MAIN},
        {"feed_a": a, "feed_b": b},
        container_ct_ids=["feed_a", "feed_b"],
    )
    assert not [c for c in candidates if c.kind == "duplicate"]


def test_a_duplicate_is_not_reported_when_the_readings_disagree_instant_by_instant():
    """Two different wires move together but never agree to the watt.

    Both are always active and their medians match, so the joint-activity floor
    and the magnitude ratio both pass.  Only the per-instant relative difference
    rejects the pair.
    """
    rng = random.Random(41)
    first = [800.0 + rng.uniform(-300.0, 300.0) for _ in range(400)]
    second = [800.0 + rng.uniform(-300.0, 300.0) for _ in range(400)]
    candidates = infer_nesting(
        {"feed_a": MAIN, "feed_b": MAIN},
        {"feed_a": first, "feed_b": second},
        container_ct_ids=["feed_a", "feed_b"],
    )
    assert not [c for c in candidates if c.kind == "duplicate"]


def test_a_duplicate_is_not_reported_when_one_feed_is_a_consistent_multiple():
    """A doubled reading is a circuit multiplier, not one wire.

    Constructed so the relative difference stays *inside* the alias threshold
    while the median magnitudes differ by more than the ratio bound: half the
    instants agree exactly, half differ by 25 %, giving a median disagreement of
    12.5 %.  Only the magnitude-ratio guard rejects this pair, so collapsing it
    would silently discard the user's multiplier.
    """
    n = 400
    a = [200.0 if i % 2 == 0 else 1200.0 for i in range(n)]
    b = [200.0 if i % 2 == 0 else 1600.0 for i in range(n)]
    evidence = duplicate_evidence(a, b)
    assert evidence is not None
    assert evidence.activity_ratio >= 0.5
    assert evidence.rel_diff <= DUPLICATE_REL_DIFF_MAX, "rel_diff alone would allow it"
    assert evidence.ratio > DUPLICATE_RATIO_MAX, "only the ratio bound rejects it"
    candidates = infer_nesting(
        {"feed_a": MAIN, "feed_b": MAIN},
        {"feed_a": a, "feed_b": b},
        container_ct_ids=["feed_a", "feed_b"],
    )
    assert not [c for c in candidates if c.kind == "duplicate"]


def test_two_monitors_on_one_wire_are_a_duplicate_not_a_nesting():
    n = 150
    a = _noisy(2400.0, n, seed=21)
    b = [x * 1.005 for x in a]
    candidates = infer_nesting(
        {"monitor_a": MAIN, "monitor_b": MAIN},
        {"monitor_a": a, "monitor_b": b},
        container_ct_ids=["monitor_a", "monitor_b"],
    )
    duplicates = [c for c in candidates if c.kind == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0].parent_ct_id == "monitor_a"
    assert duplicates[0].child_ct_id == "monitor_b"
    assert not [c for c in candidates if c.kind == "nested"], (
        "the duplicate must not also be proposed as a nested feed"
    )


def test_a_half_scaled_leg_is_not_treated_as_a_duplicate():
    """A x2 measurement of one leg is the same wire at half the value.

    Collapsing it would discard the multiplier, so it must stay a separate
    channel for the user to scale.
    """
    n = 150
    full = _noisy(2400.0, n, seed=31)
    half = [x / 2.0 for x in full]
    candidates = infer_nesting(
        {"full": MAIN, "half": MAIN},
        {"full": full, "half": half},
        container_ct_ids=["full", "half"],
    )
    assert not [c for c in candidates if c.kind == "duplicate"]


def test_a_nested_feed_is_never_reported_as_a_duplicate():
    """Only the child stays inside the parent, so they are not one wire.

    Without the relative-difference and magnitude-ratio guards, every panel feed
    would also look like a duplicate of the feed above it -- and the topology
    would then lose a real sub-panel from the total.
    """
    series = _house()
    channels = {cid: MAIN for cid in ("service", "panel", "sub")}
    candidates = infer_nesting(
        channels,
        series,
        container_ct_ids=["service", "panel", "sub"],
        direct_loads={
            "service": series["service_load"],
            "panel": series["panel_load"],
            "sub": series["sub_other"],
        },
    )
    assert not [c for c in candidates if c.kind == "duplicate"]
    # ...and the relationship it *is* is still found.
    nested = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    assert nested["panel"] == "service"


def test_a_one_sided_declared_pair_is_never_collapsed():
    """A half-declared pair must still not be read as one wire.

    A *reciprocal* pair is one node before the duplicate loop runs, so that case
    cannot exercise the declared-fact exclusion at all.  A one-sided declaration
    is left as two nodes, and the two readings are identical -- without the
    exclusion the inference would collapse a stated leg into an alias and lose
    half of a double-pole circuit.
    """
    n = 200
    leg = _noisy(2400.0, n, seed=51)
    other = [x * 1.01 for x in leg]
    channels = {
        "load_ct1": {"ct_type": "branch", "phase": "l1", "paired_ct_id": "load_ct2"},
        "load_ct2": {"ct_type": "branch", "phase": "l2", "paired_ct_id": None},
    }
    candidates = infer_nesting(
        channels,
        {"load_ct1": leg, "load_ct2": other},
        container_ct_ids=["load_ct1", "load_ct2"],
    )
    assert not [c for c in candidates if c.kind == "duplicate"]


def test_a_declared_parent_is_not_re_proposed():
    series = _house()
    channels = {
        "service": MAIN,
        "panel": {"ct_type": "main", "parent_ct_id": "service"},
        "sub": {"ct_type": "main", "parent_ct_id": "panel"},
    }
    report: dict = {}
    infer_nesting(
        channels,
        series,
        container_ct_ids=["service", "panel", "sub"],
        report=report,
    )
    suppressed = {entry["ct_id"] for entry in report["suppressed"]}
    assert "panel" in suppressed
    assert "sub" in suppressed


def test_candidates_are_deterministic():
    series = _house()
    channels = {cid: MAIN for cid in ("service", "panel", "sub")}
    first = [
        c.as_dict()
        for c in infer_nesting(channels, series, container_ct_ids=list(channels))
    ]
    second = [
        c.as_dict()
        for c in infer_nesting(channels, series, container_ct_ids=list(channels))
    ]
    assert first == second


def test_a_split_phase_child_is_written_back_leg_to_leg():
    """The candidate is reported per channel, matched on the declared phase.

    Phase is only used to make the written-back link readable; it never decides
    the level, because on the reference installation the phase labels are
    contradicted by the measurements.
    """
    n = 200
    parent_l1 = _noisy(1000.0, n, seed=61)
    parent_l2 = _noisy(1000.0, n, seed=62)
    child_l1 = [x * 0.4 for x in parent_l1]
    child_l2 = [x * 0.4 for x in parent_l2]
    channels = {
        "panel_l1": {"ct_type": "main", "phase": "l1"},
        "panel_l2": {"ct_type": "main", "phase": "l2"},
        "sub_l1": {"ct_type": "main", "phase": "l1"},
        "sub_l2": {"ct_type": "main", "phase": "l2"},
    }
    series = {
        "panel_l1": parent_l1,
        "panel_l2": parent_l2,
        "sub_l1": child_l1,
        "sub_l2": child_l2,
    }
    candidates = infer_nesting(
        channels,
        series,
        container_ct_ids=["panel_l1", "panel_l2", "sub_l1", "sub_l2"],
    )
    nested = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    assert nested["sub_l1"] == "panel_l1"
    assert nested["sub_l2"] == "panel_l2"


def test_a_node_reading_is_the_sum_of_its_members():
    """Both legs of one panel must be summed, never compared separately.

    Comparing ``sub_l1`` against ``panel_l1`` compares different conductors: the
    sub-panel's loads span both legs, so the per-leg form over-explains the leg
    by construction.  Summing both legs is what makes the relationship visible.

    The series are chosen so that overwriting instead of summing is caught: the
    second member of the child node is *larger* than the second member of the
    parent node, so a non-summing implementation sees a child that exceeds its
    parent and proposes nothing.
    """
    n = 200
    panel_l1 = _noisy(1000.0, n, seed=71)
    panel_l2 = _noisy(300.0, n, seed=72)
    sub_l1 = _noisy(500.0, n, seed=73)
    sub_l2 = _noisy(600.0, n, seed=74)
    channels = {
        "panel_l1": {"ct_type": "main", "phase": "l1"},
        "panel_l2": {"ct_type": "main", "phase": "l2"},
        "sub_l1": {"ct_type": "main", "phase": "l1"},
        "sub_l2": {"ct_type": "main", "phase": "l2"},
    }
    series = {
        "panel_l1": panel_l1,
        "panel_l2": panel_l2,
        "sub_l1": sub_l1,
        "sub_l2": sub_l2,
    }
    # Summed, the sub-panel (1100 W) fits inside the panel (1300 W)...
    assert containment(_added(panel_l1, panel_l2), _added(sub_l1, sub_l2))[0] == 1.0
    # ...but on the *last* member alone the child exceeds the parent, which is
    # what an implementation that overwrites instead of adding would see.
    assert containment(panel_l2, sub_l2)[0] < 0.5
    candidates = infer_nesting(
        channels,
        series,
        container_ct_ids=["panel_l1", "panel_l2", "sub_l1", "sub_l2"],
    )
    nested = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    assert nested.get("sub_l1") == "panel_l1"
    assert nested.get("sub_l2") == "panel_l2"


def test_activity_floor_is_above_standby_draw():
    """Two channels at 8 W and 9 W agree about nothing, so 200 W is the floor."""
    assert ACTIVITY_MIN_W >= 100.0
