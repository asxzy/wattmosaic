"""AT-06: multiple simultaneous devices are jointly inferred (HB §7.7).

HB §7.7's example: Living Room Light and Kitchen Light and the TV change
together; Circuit A reads +45 W, Circuit B +60 W and Main +110 W.

The joint solve must:

* evaluate the whole candidate set at once (never a greedy per-candidate
  credit);
* separate the two circuits;
* attribute the power through the candidates instead of one device taking the
  measured step;
* leave the ~5 W that no candidate explains in ΔU on Main, so a non-HA load in
  the same window is not absorbed by a device (DEC-21).

The test drives the K08 runtime seams (fakes for topology/mapping/residual) and
I3's pure solver.  It is a semantic acceptance test, not an end-to-end HTTP
test; ``I7`` wires the same solver into ``infer_group``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import CandidateHypothesis, DeltaPrior
from wattmosaic_engine.inference.joint import (
    ANCHOR_WEIGHT,
    solve_joint,
    solve_joint_detailed,
    unknown_share,
)
from wattmosaic_engine.runtime.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeModelSet,
    FakeResidual,
    FakeTopology,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import UnknownPrior

T0 = datetime(2026, 1, 1, tzinfo=UTC)

READERS = ("Main", "Circuit A", "Circuit B")
SUBJECTS = (
    ("device", "living-room-light"),
    ("device", "kitchen-light"),
    ("device", "tv"),
)
PAIRS = {
    ("device", "living-room-light"): "Circuit A",
    ("device", "kitchen-light"): "Circuit B",
    ("device", "tv"): "Circuit B",
}

#: Circuit A +45, Circuit B +60, Main +110.
DELTA_Y = {"Main": 110.0, "Circuit A": 45.0, "Circuit B": 60.0}
SIGMA = {"Main": 2.0, "Circuit A": 2.0, "Circuit B": 2.0}


def _operator():
    topology = FakeTopology.from_tree(HB17_TREE)
    mapping = FakeMapping.from_pairs(PAIRS)
    operator = fake_operator(topology, mapping, READERS, SUBJECTS, at=T0)
    # One unknown-scope column per affected reader: HB §7.5 requires an unknown
    # alternative to always compete, so residues are never forced onto devices.
    from wattmosaic_engine.runtime.interfaces import ObservationOperator

    return ObservationOperator(
        H=operator.H,
        rows=operator.rows,
        subjects=operator.subjects,
        entries=operator.entries,
        unknown_scopes=(("Main", (0,)), ("Circuit A", (1,)), ("Circuit B", (2,))),
        soft_alternatives=operator.soft_alternatives,
        topology_version_id=operator.topology_version_id,
        mapping_version_id=operator.mapping_version_id,
        at=operator.at,
    )


def _candidates() -> list[CandidateHypothesis]:
    return [
        CandidateHypothesis(
            candidate_id="cand-living",
            subject_kind="device",
            subject_id="living-room-light",
            sources=["ha_state"],
            transition="on",
            evidence_terms={"A": -0.1, "B": 0.0, "C": -0.2},
        ),
        CandidateHypothesis(
            candidate_id="cand-kitchen",
            subject_kind="device",
            subject_id="kitchen-light",
            sources=["ha_state"],
            transition="on",
            evidence_terms={"A": -0.1, "B": 0.0, "C": -0.2},
        ),
        CandidateHypothesis(
            candidate_id="cand-tv",
            subject_kind="device",
            subject_id="tv",
            sources=["ha_state", "plug"],
            transition="on",
            evidence_terms={"A": -0.3, "B": 0.0, "C": -0.4},
        ),
        CandidateHypothesis(
            candidate_id="cand-unknown-main",
            subject_kind="unknown_scope",
            subject_id="Main",
            sources=["unknown_scope"],
            transition=None,
            evidence_terms={},
        ),
    ]


def _priors() -> dict[str, DeltaPrior]:
    return {
        "living-room-light": DeltaPrior(mean_w=45.0, sigma_w=12.0, sign="pos", model_version_id="m-1"),
        "kitchen-light": DeltaPrior(mean_w=45.0, sigma_w=12.0, sign="pos", model_version_id="m-1"),
        "tv": DeltaPrior(mean_w=15.0, sigma_w=6.0, sign="pos", model_version_id="m-1"),
    }


def _solve():
    models = FakeModelSet.fixed(_priors())
    residual = FakeResidual(
        UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=50.0, lambda_u=1.0)
    )
    # The fakes provide the K08 handoffs; the solver itself only needs H/ΔY/σ.
    assert models.transition_prior("tv", "on", {}) is not None
    assert residual.unknown_prior("Main", T0) is not None
    return solve_joint_detailed(_operator(), DELTA_Y, SIGMA, _candidates(), _priors())


def test_at06_the_circuits_are_separated() -> None:
    report = _solve()
    assert report.solution.converged
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    # Circuit A sees exactly one candidate: its +45 W row fixes it.
    assert values["living-room-light"] == pytest.approx(45.0, abs=1.0)
    # Circuit B has two candidates and one row; their *sum* is fixed at +60.
    assert values["kitchen-light"] + values["tv"] == pytest.approx(60.0, abs=1.0)


def test_at06_about_five_watts_lands_in_unknown_on_main() -> None:
    report = _solve()
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    assert sum(values.values()) == pytest.approx(105.0, abs=1.5)
    unknown_main = float(report.solution.u[0])
    assert unknown_main == pytest.approx(5.0, abs=1.5)
    share = report.unknown_share_by_reader["Main"]
    assert share is not None
    assert 0.0 < share < 0.1


def test_at06_no_greedy_credit_happens() -> None:
    """No single candidate absorbs Main's step; the split is a joint decision."""
    report = _solve()
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    for subject, value in values.items():
        assert abs(value) < 110.0, f"{subject} absorbed the whole measured step"
    # Circuit B's two candidates are both needed: neither is zero nor equal to
    # the full +60 W.
    assert values["kitchen-light"] > 0.0
    assert values["tv"] > 0.0
    # Dropping the kitchen candidate must open a ΔU gap on Circuit B equal to
    # its share: the two candidates were solved together, not credited one by
    # one.
    only_tv = solve_joint_detailed(
        _operator(),
        DELTA_Y,
        SIGMA,
        [c for c in _candidates() if c.subject_id in {"tv", "living-room-light"}],
        _priors(),
    )
    share_with_both = unknown_share(_operator(), report.solution, DELTA_Y)["Circuit B"]
    share_without_kitchen = unknown_share(_operator(), only_tv.solution, DELTA_Y)["Circuit B"]
    assert share_with_both is not None and share_without_kitchen is not None
    assert share_with_both < share_without_kitchen
    assert share_without_kitchen == pytest.approx(values["kitchen-light"] / 60.0, abs=0.05)


def test_at06_the_load_is_counted_once_in_the_reconciliation() -> None:
    report = _solve()
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    unknown = float(np.sum(report.solution.u))
    # Main = candidates + unknown, once each.  Not 45 + 60 + 45 + 60.
    total = sum(values.values()) + unknown
    assert total == pytest.approx(110.0, abs=1.0)
    assert share_by_reader(report)["Main"] is not None


def share_by_reader(report) -> dict[str, float | None]:
    return unknown_share(_operator(), report.solution, DELTA_Y)


def test_at06_solver_handoff_is_the_k08_joint_solution() -> None:
    operator = _operator()
    solution = solve_joint(operator, DELTA_Y, SIGMA, _candidates(), _priors())
    assert solution.mode == "delta"
    assert solution.converged
    assert solution.x.shape == (3,)
    assert solution.u.shape == (3,)
    # Kitchen light and TV share Circuit B, so their subject block is not
    # identifiable and the covariance is honestly absent (I4 reports the
    # group).  When it *is* available it is N x N over the subjects.
    assert solution.cov is None
    assert solution.active_rows == (0, 1, 2)


def test_at06_acceptance_anchor_weight_is_documented() -> None:
    """AT-08's plug anchor and AT-06's joint solve share one weight constant."""
    assert ANCHOR_WEIGHT == 10.0
