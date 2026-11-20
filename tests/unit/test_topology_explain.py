"""M1 — explain-away and multi-child step matching (design §5 T5).

These tests use neutral reader ids and plain floats: the module under test is
pure, so no clock, no database and no ``runtime.fakes`` are involved.
"""

from __future__ import annotations

import math

import pytest
from wattmosaic_engine.electrical.topology_explain import (
    MAX_SUBSET_CANDIDATES,
    MAX_SUBSET_SIZE,
    ChildStep,
    best_subset,
    classify_with_explanation,
    match_tolerance,
    residual_after_known,
    verify_known,
)
from wattmosaic_engine.electrical.topology_inference import (
    _SIGMA_FACTOR,
    MATCH_RELATIVE_TOLERANCE,
    _classify_step,
)

SIGMA = 2.0


def step(reader_id: str, delta: float, sigma: float = SIGMA) -> ChildStep:
    return ChildStep(reader_id=reader_id, delta=delta, sigma=sigma)


def verdicts(parent_move: float, unknown: list[ChildStep], *, sigma_p: float = SIGMA) -> dict:
    return classify_with_explanation(parent_move, sigma_p, known=[], unknown=unknown)


# --------------------------------------------------------------------------- #
# Constants are imported, never copied.
# --------------------------------------------------------------------------- #


def test_constants_come_from_the_ledger_module() -> None:
    assert MAX_SUBSET_SIZE == 3
    assert MAX_SUBSET_CANDIDATES == 8
    # The tolerances are the ledger's own numbers, not a second copy of them.
    assert match_tolerance(0.0, [], 1.0) == pytest.approx(_SIGMA_FACTOR)
    assert match_tolerance(100.0, [], 0.0) == pytest.approx(
        MATCH_RELATIVE_TOLERANCE * 100.0
    )


# --------------------------------------------------------------------------- #
# Example 1 -- one child explains the parent step.
# --------------------------------------------------------------------------- #


def test_example_1_single_child_explains_the_parent() -> None:
    assert verdicts(100.0, [step("y", 100.0)]) == {"y": "support"}


def test_example_1_returns_the_subset_of_readers() -> None:
    assert best_subset(100.0, SIGMA, [step("y", 100.0)]) == ("y",)


# --------------------------------------------------------------------------- #
# Example 2 -- several children together explain the parent step.
# --------------------------------------------------------------------------- #


def test_example_2_two_children_split_the_parent_step() -> None:
    assert verdicts(100.0, [step("y", 30.0), step("z", 70.0)]) == {
        "y": "support",
        "z": "support",
    }


def test_example_2_with_a_distractor_explains_only_the_matching_pair() -> None:
    result = verdicts(100.0, [step("y", 30.0), step("z", 70.0), step("w", 400.0)])
    assert result == {"y": "support", "z": "support", "w": None}


def test_example_2_negative_steps_match_too() -> None:
    assert verdicts(-100.0, [step("y", -30.0), step("z", -70.0)]) == {
        "y": "support",
        "z": "support",
    }


def test_example_2_a_single_member_stays_neutral() -> None:
    # Neither child alone explains a 100 W parent move.
    assert verdicts(100.0, [step("y", 30.0), step("z", 70.0), step("q", 500.0)]) == {
        "y": "support",
        "z": "support",
        "q": None,
    }


# --------------------------------------------------------------------------- #
# Explain-away of already-known children.
# --------------------------------------------------------------------------- #


def test_known_child_is_subtracted_before_matching_the_unknown() -> None:
    result = classify_with_explanation(
        150.0, SIGMA, known=[step("a", 100.0)], unknown=[step("b", 50.0)]
    )
    assert result == {"b": "support"}


def test_known_children_explaining_everything_make_the_unknown_a_conflict() -> None:
    result = classify_with_explanation(
        100.0, SIGMA, known=[step("a", 100.0)], unknown=[step("b", 60.0)]
    )
    assert result == {"b": "conflict"}


def test_a_flat_parent_is_a_conflict_for_a_clearly_stepping_child() -> None:
    assert verdicts(1.0, [step("y", 100.0)]) == {"y": "conflict"}


def test_a_child_inside_the_parent_noise_is_neutral() -> None:
    # The parent did not move beyond its noise and the child barely moved.
    assert verdicts(1.0, [step("y", 3.0)]) == {"y": None}


def test_residual_after_known_combines_noise_in_quadrature() -> None:
    residual, sigma = residual_after_known(
        150.0, 3.0, [step("a", 100.0, sigma=4.0)], beta=1.0
    )
    assert residual == pytest.approx(50.0)
    assert sigma == pytest.approx(5.0)


def test_residual_after_known_applies_beta_to_deltas_and_sigmas() -> None:
    residual, sigma = residual_after_known(100.0, 3.0, [step("a", 50.0, sigma=4.0)], beta=2.0)
    assert residual == pytest.approx(0.0)
    assert sigma == pytest.approx(math.hypot(3.0, 8.0))


def test_residual_after_known_without_known_children_is_the_parent_move() -> None:
    assert residual_after_known(42.0, 2.0, []) == (pytest.approx(42.0), pytest.approx(2.0))


# --------------------------------------------------------------------------- #
# Ambiguity -- two equal legs must not both claim one parent.
# --------------------------------------------------------------------------- #


def test_ambiguity_equal_legs_claim_nothing() -> None:
    assert verdicts(1500.0, [step("h1", 1500.0), step("h2", 1500.0)]) == {
        "h1": None,
        "h2": None,
    }


def test_ambiguity_does_not_fire_for_a_clear_winner() -> None:
    # 30 + 70 is the only subset of size two that fits 100.
    assert best_subset(100.0, SIGMA, [step("y", 30.0), step("z", 70.0)]) == ("y", "z")


def test_ambiguity_between_different_sizes_prefers_the_smaller_one() -> None:
    # One child alone explains the move; a two-child subset fits only loosely.
    assert best_subset(100.0, SIGMA, [step("a", 100.0), step("b", 60.0)]) == ("a",)


# --------------------------------------------------------------------------- #
# Subset search details.
# --------------------------------------------------------------------------- #


def test_no_match_returns_none() -> None:
    assert best_subset(1.0, SIGMA, [step("a", 500.0)]) is None


def test_a_parent_move_inside_its_noise_is_never_explained() -> None:
    assert best_subset(5.0, 2.0, [step("a", 5.0)]) is None


def test_zero_delta_candidates_are_ignored() -> None:
    assert best_subset(100.0, SIGMA, [step("a", 100.0), step("b", 0.0)]) == ("a",)


def test_only_the_largest_candidates_are_considered() -> None:
    big = [step(f"r{index:02d}", 200.0 + index) for index in range(MAX_SUBSET_CANDIDATES)]
    small = step("small", 100.0)
    # A 100 W parent move: "small" alone would match, but it is only the ninth
    # largest step in the window and the search is bounded to the largest eight.
    assert len(big) + 1 > MAX_SUBSET_CANDIDATES
    assert best_subset(100.0, SIGMA, [*big, small]) is None
    # It is considered when the pool is small enough.
    assert best_subset(100.0, SIGMA, [small]) == ("small",)


def test_subset_size_is_capped() -> None:
    # Four equal members would sum to the parent move, but max size is three.
    members = [step(f"m{index}", 25.0) for index in range(4)]
    assert best_subset(100.0, SIGMA, members) is None
    assert best_subset(100.0, SIGMA, members, max_size=4) == ("m0", "m1", "m2", "m3")


def test_subset_members_are_returned_sorted() -> None:
    assert best_subset(100.0, SIGMA, [step("z", 30.0), step("a", 70.0)]) == ("a", "z")


def test_max_size_below_one_never_matches() -> None:
    assert best_subset(100.0, SIGMA, [step("a", 100.0)], max_size=0) is None


def test_best_subset_ignores_non_finite_candidates() -> None:
    assert best_subset(100.0, SIGMA, [step("a", math.nan)]) is None
    assert best_subset(100.0, SIGMA, [step("a", 100.0), step("b", math.inf)]) == ("a",)


# --------------------------------------------------------------------------- #
# Agreement with the single-pair ledger classifier.
# --------------------------------------------------------------------------- #


def test_agrees_with_classify_step_on_singletons() -> None:
    cases = [
        (100.0, 100.0),  # support
        (1.0, 100.0),  # conflict
        (300.0, 100.0),  # neutral: the parent moved by another amount
        (100.0, -100.0),  # neutral: opposite direction
        (2.0, 3.0),  # neutral: both inside the noise
    ]
    for parent_move, child_delta in cases:
        expected = _classify_step(
            parent_move, child_delta, sigma_p=SIGMA, sigma_c=SIGMA
        )
        (child,) = verdicts(parent_move, [step("y", child_delta)]).values()
        assert child == expected, (parent_move, child_delta)


# --------------------------------------------------------------------------- #
# verify_known -- edge health for confirmed children.
# --------------------------------------------------------------------------- #


def test_verify_known_confirms_both_children_of_a_consistent_parent() -> None:
    result = verify_known(150.0, SIGMA, known=[step("a", 100.0), step("b", 50.0)], unknown=[])
    assert result == {"a": "support", "b": "support"}


def test_verify_known_marks_the_child_the_parent_stopped_following() -> None:
    result = verify_known(50.0, SIGMA, known=[step("a", 100.0), step("b", 50.0)], unknown=[])
    assert result == {"a": "conflict", "b": "support"}


def test_verify_known_is_neutral_when_the_parent_moved_elsewhere() -> None:
    result = verify_known(300.0, SIGMA, known=[step("a", 100.0), step("b", 50.0)], unknown=[])
    assert result == {"a": None, "b": None}


def test_verify_known_reports_only_the_known_children() -> None:
    result = verify_known(
        150.0, SIGMA, known=[step("a", 100.0)], unknown=[step("b", 50.0)]
    )
    assert result == {"a": "support"}


def test_verify_known_of_nothing_is_empty() -> None:
    assert verify_known(150.0, SIGMA, known=[], unknown=[step("b", 50.0)]) == {}


def test_verify_known_does_not_explain_away_more_than_the_parent_moved() -> None:
    # A child cannot carry more than its parent (KCL): the over-claiming A is
    # not subtracted from the move, so the honest B keeps its support.
    result = verify_known(100.0, SIGMA, known=[step("a", 300.0), step("b", 100.0)], unknown=[])
    assert result == {"a": "conflict", "b": "support"}


def test_verify_known_partial_explanation_still_checks_each_child() -> None:
    result = verify_known(100.0, SIGMA, known=[step("a", 60.0), step("b", 40.0)], unknown=[])
    assert result == {"a": "support", "b": "support"}


def test_verify_known_flags_a_child_stepping_the_other_way() -> None:
    # An export step is not part of an import move: the parent cannot be
    # following it, so the edge collects conflict -- and it is never explained
    # away from the honest sibling's share of the move.
    result = verify_known(100.0, SIGMA, known=[step("a", -80.0), step("b", 100.0)], unknown=[])
    assert result == {"a": "conflict", "b": "support"}


# --------------------------------------------------------------------------- #
# Performance -- the subset enumeration stays cheap.
# --------------------------------------------------------------------------- #


def test_eight_candidates_thousand_calls_under_two_seconds() -> None:
    import time

    candidates = [step(f"r{index}", 10.0 * (index + 1)) for index in range(8)]
    began = time.perf_counter()
    for _ in range(1000):
        best_subset(120.0, SIGMA, candidates, max_size=3)
    elapsed = time.perf_counter() - began
    assert elapsed < 2.0, elapsed
