"""AT-09: measurement covariance is accounted for across nested readers.

HB §21 test 9: one load is observed by three nested readers (Circuit A ⊂
Subpanel ⊂ Main).  The posterior variance must be **≥ 0.9× the single-reader
variance** — i.e. the solver must not assume the three observations are
independent and divide the variance by three — and the load must be counted
once.

Both covariance regimes are exercised:

* conservative weighting (``σ̃ = σ·√k`` from ``RowMeta.sigma_inflation`` /
  shared support) when no Σ is estimable;
* GLS with an estimated Σ from historical residuals (``covariance.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import CandidateHypothesis
from wattmosaic_engine.inference.covariance import (
    MIN_SHARED_EVENTS,
    ReaderCovariance,
    estimate_reader_covariance,
)
from wattmosaic_engine.inference.joint import solve_joint, solve_joint_detailed
from wattmosaic_engine.runtime.fakes import FakeMapping, FakeTopology, fake_operator

CLOSED_BY = "I3"

T0 = datetime(2026, 1, 1, tzinfo=UTC)

NESTED_TREE = {"Main": {"Subpanel": {"Circuit A": {}}}}
NESTED_READERS = ("Main", "Subpanel", "Circuit A")
LOAD = ("device", "dryer")
PAIRS = {LOAD: "Circuit A"}
LOAD_W = 1800.0
SIGMA_W = 25.0


def _operator(readers: tuple[str, ...] = NESTED_READERS):
    return fake_operator(
        FakeTopology.from_tree(NESTED_TREE),
        FakeMapping.from_pairs(PAIRS),
        readers,
        (LOAD,),
        at=T0,
    )


def _candidates() -> list[CandidateHypothesis]:
    return [
        CandidateHypothesis(
            candidate_id="cand-dryer",
            subject_kind="device",
            subject_id="dryer",
            sources=["ha_state"],
            transition="on",
            evidence_terms={},
        )
    ]


def _covariance(correlation: float = 0.95) -> ReaderCovariance:
    """Σ for nested readers: they share the load, so they are correlated."""
    matrix = np.full((3, 3), correlation * SIGMA_W**2)
    np.fill_diagonal(matrix, SIGMA_W**2)
    return ReaderCovariance(reader_ids=NESTED_READERS, matrix=matrix, n_events=200)


def _single_reader_variance() -> float:
    solution = solve_joint(_operator(("Circuit A",)), {"Circuit A": LOAD_W}, {"Circuit A": SIGMA_W})
    assert solution.cov is not None
    return float(solution.cov[0, 0])


def test_at09_conservative_weighting_keeps_the_single_reader_variance() -> None:
    single = _single_reader_variance()
    solution = solve_joint(
        _operator(),
        {"Main": LOAD_W, "Subpanel": LOAD_W, "Circuit A": LOAD_W},
        {"Main": SIGMA_W, "Subpanel": SIGMA_W, "Circuit A": SIGMA_W},
    )
    assert solution.cov is not None
    variance = float(solution.cov[0, 0])
    assert variance >= 0.9 * single
    # The naive independent assumption would give ~ single/3 (actually /9 here,
    # because the anchor weighting is the same in both cases).
    assert variance > single / 2.0


def test_at09_gls_with_estimated_covariance_keeps_the_single_reader_variance() -> None:
    single = _single_reader_variance()
    solution = solve_joint(
        _operator(),
        {"Main": LOAD_W, "Subpanel": LOAD_W, "Circuit A": LOAD_W},
        {"Main": SIGMA_W, "Subpanel": SIGMA_W, "Circuit A": SIGMA_W},
        covariance=_covariance(),
    )
    assert solution.cov is not None
    variance = float(solution.cov[0, 0])
    assert variance >= 0.9 * single


def test_at09_the_load_is_counted_once() -> None:
    report = solve_joint_detailed(
        _operator(),
        {"Main": LOAD_W, "Subpanel": LOAD_W, "Circuit A": LOAD_W},
        {"Main": SIGMA_W, "Subpanel": SIGMA_W, "Circuit A": SIGMA_W},
        _candidates(),
    )
    assert float(report.solution.x[0]) == pytest.approx(LOAD_W, abs=2.0)
    # Not 3 × 1800 W: the three rows describe one physical load.
    assert float(report.solution.x[0]) < 2 * LOAD_W
    # No unknown column, so nothing is double-billed to ΔU either.
    assert report.solution.u.size == 0


def test_at09_more_readers_never_lower_the_variance_a_lot() -> None:
    """Monotonic in the conservative direction: 1 -> 3 readers is flat-ish."""
    single = _single_reader_variance()
    two = solve_joint(
        _operator(("Main", "Circuit A")),
        {"Main": LOAD_W, "Circuit A": LOAD_W},
        {"Main": SIGMA_W, "Circuit A": SIGMA_W},
    )
    three = solve_joint(
        _operator(),
        {"Main": LOAD_W, "Subpanel": LOAD_W, "Circuit A": LOAD_W},
        {"Main": SIGMA_W, "Subpanel": SIGMA_W, "Circuit A": SIGMA_W},
    )
    assert two.cov is not None and three.cov is not None
    assert float(two.cov[0, 0]) >= 0.9 * single
    assert float(three.cov[0, 0]) >= 0.9 * single


def test_at09_covariance_is_estimated_from_historical_residuals() -> None:
    rng = np.random.default_rng(9)
    n_events = MIN_SHARED_EVENTS + 60
    shared = rng.normal(0.0, 1.0, size=(n_events, 1))
    private = rng.normal(0.0, 0.3, size=(n_events, 3))
    residuals = SIGMA_W * (0.95 * shared + 0.31 * private)
    estimate = estimate_reader_covariance(residuals, NESTED_READERS)
    assert estimate is not None
    assert estimate.n_events == n_events
    sigma = np.sqrt(np.diag(estimate.matrix))
    correlation = estimate.matrix / np.outer(sigma, sigma)
    # Nested readers are strongly correlated, which is the whole point.
    off_diagonal = correlation[~np.eye(3, dtype=bool)]
    assert np.all(off_diagonal > 0.5)


def test_at09_a_singular_covariance_falls_back_to_conservative_weights() -> None:
    """When GLS cannot be built the solver must not fall back to independence."""
    single = _single_reader_variance()
    # A covariance that does not cover the rows cannot be used.
    partial = ReaderCovariance(
        reader_ids=("Main", "Subpanel"),
        matrix=np.eye(2) * SIGMA_W**2,
        n_events=100,
    )
    report = solve_joint_detailed(
        _operator(),
        {"Main": LOAD_W, "Subpanel": LOAD_W, "Circuit A": LOAD_W},
        {"Main": SIGMA_W, "Subpanel": SIGMA_W, "Circuit A": SIGMA_W},
        _candidates(),
        covariance=partial,
    )
    assert "gls_unavailable_fallback_conservative" in report.warnings
    assert report.solution.cov is not None
    assert float(report.solution.cov[0, 0]) >= 0.9 * single
