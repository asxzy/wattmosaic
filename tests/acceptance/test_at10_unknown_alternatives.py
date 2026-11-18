"""AT-10 — unknown-load alternatives remain available during matching (HB §21).

Closes: I1. 100 % of candidate sets contain an unknown_scope candidate
(property test), and a weak known candidate loses to unknown.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I1"),
]


def test_unknown_alternative_always_available_and_can_win() -> None:
    """Property test + ranking: unknown_scope survives and can beat a weak known."""
    from wattmosaic_engine.contracts import CandidateSource
    from wattmosaic_engine.inference import generate_candidates, rank_candidates  # (I1)

    for seed in range(100):
        candidates = generate_candidates(seed=seed)
        assert candidates, f"empty candidate set for seed {seed}"
        assert any(
            CandidateSource.UNKNOWN_SCOPE in c.sources for c in candidates
        ), f"seed {seed} lost its unknown_scope alternative"

    ranked = rank_candidates(
        known_quality=0.05, unknown_quality=0.95, candidate_set_seed=7
    )
    assert ranked[0].subject_kind == "unknown_scope"
