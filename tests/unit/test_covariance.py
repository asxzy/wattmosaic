"""I3: covariance handling for nested readers (HB §8.3, design §7, AT-09).

The point of these tests is the *conservative* direction: nested readers must
never buy precision they do not have.  A load seen by three nested CTs must not
end up with a third of the single-reader variance.

Deterministic: no clock, no DB, no global RNG state.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.inference.covariance import (
    MIN_SHARED_EVENTS,
    ReaderCovariance,
    conservative_inflation,
    effective_sigma,
    estimate_reader_covariance,
    gls_whitener,
    posterior_covariance,
)
from wattmosaic_engine.inference.joint import solve_joint
from wattmosaic_engine.runtime.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _operator(
    readers: tuple[str, ...],
    subjects: tuple[tuple[str, str], ...],
    pairs: dict[tuple[str, str], str],
    tree: dict | None = None,
):
    topology = FakeTopology.from_tree(tree or HB17_TREE)
    mapping = FakeMapping.from_pairs(pairs)
    return fake_operator(topology, mapping, readers, subjects, at=T0)


# --------------------------------------------------------------------------- #
# Sample-covariance estimation.
# --------------------------------------------------------------------------- #


def _nested_residuals(
    n_events: int,
    *,
    sigma: float = 20.0,
    correlation: float = 0.0,
    seed: int = 17,
) -> np.ndarray:
    """``n_events x 3`` residuals with a chosen equicorrelation."""
    rng = np.random.default_rng(seed)
    private = rng.normal(0.0, 1.0, size=(n_events, 3))
    common = rng.normal(0.0, 1.0, size=(n_events, 1))
    rho = float(correlation)
    return sigma * (np.sqrt(rho) * common + np.sqrt(1.0 - rho) * private)


def test_covariance_needs_the_minimum_number_of_shared_events() -> None:
    readers = ("Main", "Subpanel", "Circuit A")
    too_few = _nested_residuals(MIN_SHARED_EVENTS - 1)
    assert estimate_reader_covariance(too_few, readers) is None
    enough = estimate_reader_covariance(_nested_residuals(MIN_SHARED_EVENTS), readers)
    assert enough is not None
    assert enough.n_events == MIN_SHARED_EVENTS
    assert enough.reader_ids == readers
    assert enough.matrix.shape == (3, 3)


def test_covariance_uses_only_complete_shared_events() -> None:
    residuals = _nested_residuals(MIN_SHARED_EVENTS + 10)
    residuals[::2, 1] = np.nan  # Subpanel missed every other event
    estimate = estimate_reader_covariance(residuals, ("Main", "Subpanel", "Circuit A"))
    if estimate is None:
        # Half of 40 is 20 shared events: below the minimum, and that is the
        # correct, honest answer.
        assert (MIN_SHARED_EVENTS + 10) // 2 < MIN_SHARED_EVENTS
        return
    assert estimate.n_events <= (MIN_SHARED_EVENTS + 10) // 2 + 1


def test_covariance_is_symmetric_positive_semidefinite() -> None:
    estimate = estimate_reader_covariance(
        _nested_residuals(200, correlation=0.8), ("Main", "Subpanel", "Circuit A")
    )
    assert estimate is not None
    matrix = estimate.matrix
    assert np.allclose(matrix, matrix.T)
    assert np.all(np.linalg.eigvalsh(matrix) >= 0.0)


def test_covariance_recovers_the_equicorrelation() -> None:
    estimate = estimate_reader_covariance(
        _nested_residuals(4000, sigma=20.0, correlation=0.8, seed=3),
        ("a", "b", "c"),
    )
    assert estimate is not None
    matrix = estimate.matrix
    sigma = np.sqrt(np.diag(matrix))
    correlation = matrix / np.outer(sigma, sigma)
    off_diagonal = correlation[~np.eye(3, dtype=bool)]
    assert np.allclose(off_diagonal, 0.8, atol=0.05)
    assert np.allclose(sigma, 20.0, atol=1.0)


def test_covariance_rejects_malformed_input() -> None:
    with pytest.raises(ValueError):
        estimate_reader_covariance(np.zeros((5, 2)), ("a", "b", "c"))
    with pytest.raises(ValueError):
        estimate_reader_covariance(np.zeros(5), ("a",))


# --------------------------------------------------------------------------- #
# Conservative weighting.
# --------------------------------------------------------------------------- #


def test_conservative_inflation_counts_nested_support() -> None:
    operator = _operator(
        ("Main", "Subpanel", "Circuit A", "Circuit B"),
        (("device", "light"),),
        {("device", "light"): "Circuit A"},
    )
    inflation = conservative_inflation(operator)
    # Main, Subpanel and Circuit A all observe the light: k = 3.
    for reader, expected in (("Main", 3), ("Subpanel", 3), ("Circuit A", 3)):
        row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == reader)
        assert inflation[row] == pytest.approx(np.sqrt(expected))
    # Circuit B observes nothing of this subject: k = 1, no inflation.
    row_b = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "Circuit B")
    assert inflation[row_b] == 1.0


def test_conservative_inflation_honours_row_meta_and_takes_the_larger() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "light"),),
        {("device", "light"): "Circuit A"},
    )
    boosted = replace(
        operator,
        rows=(
            replace(operator.rows[0], sigma_inflation=5.0),
            operator.rows[1],
        ),
    )
    inflation = conservative_inflation(boosted)
    assert inflation[0] == 5.0
    assert inflation[1] == pytest.approx(np.sqrt(2.0))


def test_effective_sigma_never_fabricates_a_value() -> None:
    operator = _operator(
        ("Main", "Circuit A", "Circuit B"),
        (("device", "light"),),
        {("device", "light"): "Circuit A"},
    )
    operator = replace(
        operator,
        rows=(
            operator.rows[0],
            replace(operator.rows[1], sigma_w=4.0),
            operator.rows[2],
        ),
    )
    sigma = effective_sigma(operator, {"Main": 10.0}, (0, 1, 2))
    assert sigma[0] == pytest.approx(10.0 * np.sqrt(2.0))
    # Falls back to RowMeta.sigma_w when the caller supplies none.
    assert sigma[1] == pytest.approx(4.0 * np.sqrt(2.0))
    # No sigma anywhere: NaN, never 0 (an unweighted row would dominate).
    assert np.isnan(sigma[2])


# --------------------------------------------------------------------------- #
# GLS whitening.
# --------------------------------------------------------------------------- #


def _known_covariance(correlation: float = 0.9, sigma: float = 20.0) -> ReaderCovariance:
    matrix = np.full((3, 3), correlation * sigma**2)
    np.fill_diagonal(matrix, sigma**2)
    return ReaderCovariance(reader_ids=("Main", "Subpanel", "Circuit A"), matrix=matrix, n_events=100)


def test_gls_whitener_needs_every_reader() -> None:
    covariance = _known_covariance()
    assert gls_whitener(None, ["Main"]) is None
    assert gls_whitener(covariance, ["Main", "Subpanel", "Circuit B"]) is None
    whitener = gls_whitener(covariance, ["Main", "Subpanel", "Circuit A"])
    assert whitener is not None


def test_gls_whitener_whitens_the_covariance() -> None:
    covariance = _known_covariance()
    whitener = gls_whitener(covariance, ["Main", "Subpanel", "Circuit A"])
    assert whitener is not None
    whitened = whitener @ covariance.matrix @ whitener.T
    assert np.allclose(whitened, np.eye(3), atol=1e-8)


def test_gls_does_not_assume_independence_for_nested_readers() -> None:
    """Passing Σ must not shrink the variance below the single-reader one."""
    operator = _operator(
        ("Main", "Subpanel", "Circuit A"),
        (("device", "heater"),),
        {("device", "heater"): "Circuit A"},
    )
    single = solve_joint(operator, {"Circuit A": 100.0}, {"Circuit A": 20.0})
    assert single.cov is not None
    single_variance = float(single.cov[0, 0])
    # A single-subject DIRECT endpoint is an anchor (weight x10), so the
    # reference variance is the anchor-weighted σ², not the raw σ².
    assert single_variance == pytest.approx(400.0 / 100.0, rel=1e-6)

    joint = solve_joint(
        operator,
        {"Main": 100.0, "Subpanel": 100.0, "Circuit A": 100.0},
        {"Main": 20.0, "Subpanel": 20.0, "Circuit A": 20.0},
        covariance=_known_covariance(),
    )
    assert joint.cov is not None
    assert float(joint.cov[0, 0]) >= 0.9 * single_variance


def test_conservative_weighting_alone_never_buys_nested_precision() -> None:
    """Without Σ, σ·√k keeps the triple-reader variance near the single one."""
    operator = _operator(
        ("Main", "Subpanel", "Circuit A"),
        (("device", "heater"),),
        {("device", "heater"): "Circuit A"},
    )
    single = solve_joint(operator, {"Circuit A": 100.0}, {"Circuit A": 20.0})
    assert single.cov is not None
    single_variance = float(single.cov[0, 0])
    joint = solve_joint(
        operator,
        {"Main": 100.0, "Subpanel": 100.0, "Circuit A": 100.0},
        {"Main": 20.0, "Subpanel": 20.0, "Circuit A": 20.0},
    )
    assert joint.cov is not None
    assert float(joint.cov[0, 0]) >= 0.9 * single_variance


# --------------------------------------------------------------------------- #
# Posterior covariance.
# --------------------------------------------------------------------------- #


def test_posterior_covariance_single_reader() -> None:
    covariance = posterior_covariance(np.array([[1.0]]), np.array([1.0 / 20.0]))
    assert covariance is not None
    assert covariance[0, 0] == pytest.approx(400.0)


def test_posterior_covariance_is_none_when_singular() -> None:
    A = np.array([[1.0, 1.0], [1.0, 1.0]])
    assert posterior_covariance(A, np.array([0.05, 0.05])) is None
    # A model prior does not make the data identifiable (DEC-19).
    assert posterior_covariance(A, np.array([0.05, 0.05]), np.array([1.0, 1.0])) is None


def test_posterior_covariance_excludes_rows_with_zero_weight() -> None:
    A = np.array([[1.0], [1.0]])
    covariance = posterior_covariance(A, np.array([1.0 / 20.0, 0.0]))
    assert covariance is not None
    assert covariance[0, 0] == pytest.approx(400.0)


def test_posterior_covariance_rejects_shape_mismatches() -> None:
    with pytest.raises(ValueError):
        posterior_covariance(np.ones(5), np.ones(5))
    with pytest.raises(ValueError):
        posterior_covariance(np.ones((5, 2)), np.ones(4))


def test_covariance_estimates_are_deterministic() -> None:
    residuals = _nested_residuals(120, correlation=0.5)
    first = estimate_reader_covariance(residuals, ("a", "b", "c"))
    second = estimate_reader_covariance(residuals, ("a", "b", "c"))
    assert first is not None and second is not None
    assert np.array_equal(first.matrix, second.matrix)
    first_solve = solve_joint(
        _operator(("Main", "Circuit A"), (("device", "d"),), {("device", "d"): "Circuit A"}),
        {"Main": 10.0, "Circuit A": 10.0},
        {"Main": 5.0, "Circuit A": 5.0},
    )
    second_solve = solve_joint(
        _operator(("Main", "Circuit A"), (("device", "d"),), {("device", "d"): "Circuit A"}),
        {"Main": 10.0, "Circuit A": 10.0},
        {"Main": 5.0, "Circuit A": 5.0},
    )
    assert np.array_equal(first_solve.x, second_solve.x)
    assert np.array_equal(first_solve.cov, second_solve.cov)
