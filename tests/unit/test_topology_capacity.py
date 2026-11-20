"""Absolute-value capacity and paired (240 V) leg assignment (M2).

Two children inside a parent can never need more power than the parent
carries, and the two legs of a 240 V load sit under different parent legs.
These tests pin the pure helpers that wave 2 will call from topology
inference.
"""

from __future__ import annotations

import random
import time

from wattmosaic_engine.electrical.topology_capacity import (
    CAPACITY_MAX_VIOLATION,
    assign_parents,
    capacity_violation,
    fits,
    paired_legs,
)


def _never_nested(_a: str, _b: str) -> bool:
    return False


# --------------------------------------------------------------------------- #
# capacity_violation / fits
# --------------------------------------------------------------------------- #
def test_capacity_violation_ignores_absent_samples() -> None:
    parent = [100.0, 100.0, None]
    children = [[60.0, 60.0, 1.0], [30.0, 50.0, 1.0]]

    # 90 <= 135 and 110 <= 135; the third sample has an absent parent.
    assert capacity_violation(parent, children) == (0.0, 2)


def test_capacity_violation_flags_children_larger_than_the_parent() -> None:
    parent = [100.0] * 5
    children = [[200.0] * 5, [100.0] * 5]

    violation, comparable = capacity_violation(parent, children)

    assert comparable == 5
    assert violation == 1.0
    assert not fits(parent, children)


def test_capacity_violation_without_comparable_samples_is_neutral() -> None:
    assert capacity_violation([None, None], [[10.0, 20.0]]) == (0.0, 0)
    # An absent child makes the sample incomparable, never 0 W.
    assert capacity_violation([100.0], [[None]]) == (0.0, 0)
    # No comparable evidence is not evidence against.
    assert fits([None], [[500.0]])


def test_capacity_violation_compares_only_the_common_prefix() -> None:
    assert capacity_violation([100.0, 100.0, 100.0], [[40.0]]) == (0.0, 1)


def test_capacity_violation_allows_a_small_share_of_skewed_samples() -> None:
    parent = [100.0] * 100
    children = [[300.0] + [90.0] * 99]  # one bad sample out of 100

    violation, comparable = capacity_violation(parent, children)

    assert comparable == 100
    assert violation == 0.01
    assert violation <= CAPACITY_MAX_VIOLATION
    assert fits(parent, children)


def test_capacity_violation_respects_explicit_tolerance() -> None:
    parent = [100.0]
    children = [[160.0]]  # 160 <= 130 + 0.30 * 100 = 160 -> not a violation

    assert capacity_violation(parent, children, tol_w=30.0, rel=0.30) == (0.0, 1)
    assert capacity_violation(parent, children) == (1.0, 1)


# --------------------------------------------------------------------------- #
# paired_legs
# --------------------------------------------------------------------------- #
def test_paired_legs_finds_one_240v_load() -> None:
    rng = random.Random(7)
    h1: list[float | None] = []
    h2: list[float | None] = []
    unrelated: list[float | None] = []
    for index in range(60):
        on = (index // 5) % 2 == 0
        base = 1500.0 if on else 0.0
        h1.append(base + rng.uniform(-5.0, 5.0))
        h2.append(base + rng.uniform(-5.0, 5.0))
        unrelated.append(120.0 + rng.uniform(-3.0, 3.0))

    pairs = paired_legs({"h1": h1, "h2": h2, "other": unrelated})

    assert len(pairs) == 1
    a, b, r = pairs[0]
    assert (a, b) == ("h1", "h2")
    assert r >= 0.95


def test_paired_legs_ignores_constant_series() -> None:
    flat = [1500.0] * 40
    noisy = [1500.0 + (i % 3) for i in range(40)]

    assert paired_legs({"a": flat, "b": noisy}) == []


def test_paired_legs_requires_activity() -> None:
    idle = [5.0] * 40
    noisy = [5.0 + (i % 3) for i in range(40)]

    assert paired_legs({"a": idle, "b": noisy}) == []


def test_paired_legs_never_puts_a_reader_in_two_pairs() -> None:
    rng = random.Random(11)
    legs = {name: [0.0] * 50 for name in ("a", "b", "c")}
    for index in range(50):
        base = 1500.0 if (index // 5) % 2 == 0 else 0.0
        for name in legs:
            legs[name][index] = base + rng.uniform(-5.0, 5.0)

    pairs = paired_legs(legs)

    assert len(pairs) == 1
    seen = [reader for pair in pairs for reader in pair[:2]]
    assert len(seen) == len(set(seen))


# --------------------------------------------------------------------------- #
# assign_parents: nested reduction
# --------------------------------------------------------------------------- #
def test_nested_candidate_is_reduced_to_the_more_direct_parent() -> None:
    series = {"feed": [1000.0] * 5, "sub": [400.0] * 5, "c": [100.0] * 5}

    chosen = assign_parents(
        {"c": ["feed", "sub"]},
        series,
        levels={"feed": 0.0, "sub": 1.0},
        nested=lambda a, b: a == "feed" and b == "sub",
    )

    assert chosen == {"c": "sub"}


def test_nested_reduction_keeps_sibling_candidates() -> None:
    series = {"l1": [1000.0] * 5, "l2": [1000.0] * 5, "c": [100.0] * 5}

    chosen = assign_parents(
        {"c": ["l1", "l2"]},
        series,
        levels={"l1": 1.0, "l2": 1.0},
        nested=_never_nested,
    )

    assert chosen["c"] in {"l1", "l2"}


# --------------------------------------------------------------------------- #
# assign_parents: paired legs and capacity
# --------------------------------------------------------------------------- #
def _hot_tub() -> dict[str, list[float]]:
    """Two legs of one element, switching together, plus a base load."""
    legs: dict[str, list[float]] = {"h1": [], "h2": []}
    rng = random.Random(3)
    for index in range(60):
        on = (index // 5) % 2 == 0
        base = 1500.0 if on else 0.0
        legs["h1"].append(base + rng.uniform(-4.0, 4.0))
        legs["h2"].append(base + rng.uniform(-4.0, 4.0))
    return legs


def _uneven_legs(high_w: float, low_w: float) -> dict[str, list[float]]:
    """A 240 V element whose two legs share a switch but draw differently."""
    legs: dict[str, list[float]] = {"h1": [], "h2": []}
    rng = random.Random(5)
    for index in range(60):
        on = (index // 5) % 2 == 0
        legs["h1"].append((high_w if on else 0.0) + rng.uniform(-3.0, 3.0))
        legs["h2"].append((low_w if on else 0.0) + rng.uniform(-3.0, 3.0))
    return legs


def test_paired_legs_go_to_different_sibling_parents() -> None:
    legs = _hot_tub()
    other1 = [600.0] * len(legs["h1"])
    series = {
        "h1": legs["h1"],
        "h2": legs["h2"],
        "l1": [400.0 + h + o for h, o in zip(legs["h1"], other1, strict=True)],
        "l2": [250.0 + h for h in legs["h2"]],
    }
    # Leg detection runs on the appliance's own channel candidates, not on the
    # parents above it (a parent that carries a leg correlates with it too).
    pairs = paired_legs({"h1": series["h1"], "h2": series["h2"]})
    assert [pair[:2] for pair in pairs] == [("h1", "h2")]

    chosen = assign_parents(
        {"h1": ["l1", "l2"], "h2": ["l1", "l2"]},
        series,
        levels={"l1": 1.0, "l2": 1.0},
        nested=_never_nested,
        pairs=[("h1", "h2")],
    )

    assert chosen["h1"] != chosen["h2"]


def test_pair_split_is_avoided_even_when_both_parents_could_carry_both_legs() -> None:
    legs = _hot_tub()
    series = {
        "h1": legs["h1"],
        "h2": legs["h2"],
        "l1": [9000.0] * len(legs["h1"]),
        "l2": [9000.0] * len(legs["h2"]),
    }

    chosen = assign_parents(
        {"h1": ["l1", "l2"], "h2": ["l1", "l2"]},
        series,
        levels={"l1": 1.0, "l2": 1.0},
        nested=_never_nested,
        pairs=[("h1", "h2")],
    )

    assert chosen == {"h1": "l1", "h2": "l2"}


def test_only_one_assignment_fits_when_a_parent_is_too_small() -> None:
    legs = _uneven_legs(1800.0, 300.0)
    series = {
        "h1": legs["h1"],
        "h2": legs["h2"],
        # l1 is the bus of the big leg, l2 the bus of the small one: l2 cannot
        # carry h1 at all, so the swapped assignment (h1 -> l2) violates KCL.
        "l1": [50.0 + h for h in legs["h1"]],
        "l2": [20.0 + h for h in legs["h2"]],
    }

    assert not fits(series["l2"], [series["h1"]])
    assert fits(series["l1"], [series["h2"]])
    assert not fits(series["l1"], [series["h1"], series["h2"]])

    chosen = assign_parents(
        {"h1": ["l1", "l2"], "h2": ["l1", "l2"]},
        series,
        levels={"l1": 1.0, "l2": 1.0},
        nested=_never_nested,
        pairs=[("h1", "h2")],
    )

    assert chosen == {"h1": "l1", "h2": "l2"}
    # Capacity alone is decisive here: the pair rule is not what picks this.
    assert (
        assign_parents(
            {"h1": ["l1", "l2"], "h2": ["l1", "l2"]},
            series,
            levels={"l1": 1.0, "l2": 1.0},
            nested=_never_nested,
        )
        == chosen
    )


# --------------------------------------------------------------------------- #
# assign_parents: fixed children, score and determinism
# --------------------------------------------------------------------------- #
def test_fixed_children_count_against_a_parent_capacity() -> None:
    series = {
        "p": [300.0] * 5,
        "q": [1000.0] * 5,
        "c1": [300.0] * 5,
        "c2": [200.0] * 5,
    }

    chosen = assign_parents(
        {"c2": ["p", "q"]},
        series,
        levels={"p": 1.0, "q": 2.0},
        nested=_never_nested,
        fixed={"c1": "p"},
    )

    assert chosen["c2"] == "q"


def test_higher_evidence_score_wins_when_capacity_is_equal() -> None:
    series = {
        "a": [1000.0] * 5,
        "b": [1000.0] * 5,
        "c": [50.0] * 5,
    }

    chosen = assign_parents(
        {"c": ["a", "b"]},
        series,
        levels={"a": 1.0, "b": 1.0},
        nested=_never_nested,
        score={("b", "c"): 3.0},
    )

    assert chosen["c"] == "b"


def test_assignment_is_pure_and_deterministic() -> None:
    series = {
        "a": [1000.0] * 5,
        "b": [1000.0] * 5,
        "c": [50.0] * 5,
        "d": [60.0] * 5,
    }
    candidates = {"c": ["a", "b"], "d": ["a", "b"]}
    levels = {"a": 1.0, "b": 1.0}

    first = assign_parents(candidates, series, levels=levels, nested=_never_nested)
    second = assign_parents(candidates, series, levels=levels, nested=_never_nested)

    assert first == second
    assert candidates == {"c": ["a", "b"], "d": ["a", "b"]}
    assert series["a"] == [1000.0] * 5


def test_children_without_candidates_are_omitted() -> None:
    assert assign_parents({"c": []}, {"c": [1.0]}, levels={}, nested=_never_nested) == {}


# --------------------------------------------------------------------------- #
# assign_parents: large search takes the greedy path
# --------------------------------------------------------------------------- #
def test_large_search_falls_back_to_greedy_quickly() -> None:
    children = [f"c{index:02d}" for index in range(13)]
    parents = ["p0", "p1"]
    series: dict[str, list[float]] = {parent: [5000.0] * 8 for parent in parents}
    for index, child in enumerate(children):
        series[child] = [100.0 + index] * 8
    candidates = {child: list(parents) for child in children}

    started = time.perf_counter()
    chosen = assign_parents(
        candidates,
        series,
        levels={"p0": 1.0, "p1": 1.0},
        nested=_never_nested,
    )
    elapsed = time.perf_counter() - started

    assert set(chosen) == set(children)
    for child in children:
        assert chosen[child] in parents
    assert elapsed < 2.0
    # 2 ** 13 = 8192 combinations would be too many to enumerate; the greedy
    # walk is still deterministic.
    assert chosen == assign_parents(
        candidates, series, levels={"p0": 1.0, "p1": 1.0}, nested=_never_nested
    )
