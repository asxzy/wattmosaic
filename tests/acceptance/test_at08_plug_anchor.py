"""AT-08: smart-plug measurements act as stronger anchors (HB §8.3, §21 test 8).

A smart plug is a reader whose scope is exactly one device.  Its reading is a
near-hard constraint on that device: the TV's estimate stays within the plug's
σ even when the enclosing circuit is noisy (σ = 20 W), and the circuit noise is
absorbed by the other candidates or by ΔU rather than by the anchored TV.

The test builds the K08 seams directly: the plug is one more row of the
observation operator with a DIRECT entry, and I3 detects it structurally (no
``dedicated_device`` vocabulary — ADR-0034 stays retired).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import CandidateHypothesis, DeltaPrior, Observability
from wattmosaic_engine.inference.joint import ANCHOR_WEIGHT, solve_joint_detailed
from wattmosaic_engine.runtime.fakes import FakeMapping, FakeResidual, FakeTopology, fake_operator
from wattmosaic_engine.runtime.interfaces import ObservationOperator, UnknownPrior

T0 = datetime(2026, 1, 1, tzinfo=UTC)

#: TV on its own plug, plugged into Circuit A (a noisy circuit).
TREE = {"Main": {"Subpanel": {"Circuit A": {"TV plug": {}}}}}
READERS = ("Main", "Subpanel", "Circuit A", "TV plug")
SUBJECTS = (("device", "tv"), ("device", "fridge"), ("device", "hall-lights"))
PAIRS = {
    ("device", "tv"): "TV plug",
    ("device", "fridge"): "Circuit A",
    ("device", "hall-lights"): "Circuit A",
}

#: TV standby 3 W -> 88 W is +85 W, measured on the plug.
TV_DELTA = 85.0
CIRCUIT_NOISE_W = 20.0


def _operator() -> ObservationOperator:
    base = fake_operator(
        FakeTopology.from_tree(TREE),
        FakeMapping.from_pairs(PAIRS),
        READERS,
        SUBJECTS,
        at=T0,
    )
    return ObservationOperator(
        H=base.H,
        rows=base.rows,
        subjects=base.subjects,
        entries=base.entries,
        unknown_scopes=(("Circuit A", (2,)), ("Main", (0,))),
        soft_alternatives=base.soft_alternatives,
        topology_version_id=base.topology_version_id,
        mapping_version_id=base.mapping_version_id,
        at=base.at,
    )


def _candidates() -> list[CandidateHypothesis]:
    return [
        CandidateHypothesis(
            candidate_id="cand-tv",
            subject_kind="device",
            subject_id="tv",
            sources=["ha_state", "plug"],
            transition="on",
            evidence_terms={},
        ),
        CandidateHypothesis(
            candidate_id="cand-fridge",
            subject_kind="device",
            subject_id="fridge",
            sources=["ha_state"],
            transition="on",
            evidence_terms={},
        ),
        CandidateHypothesis(
            candidate_id="cand-lights",
            subject_kind="device",
            subject_id="hall-lights",
            sources=["ha_state"],
            transition="on",
            evidence_terms={},
        ),
    ]


def _solve(circuit_delta: float):
    residual = FakeResidual(
        UnknownPrior(scope_reader_id="Circuit A", mu_w=0.0, sigma_w=40.0, lambda_u=1.0)
    )
    assert residual.unknown_prior("Circuit A", T0) is not None
    # The noisy circuit reads the TV plus fridge plus lights plus noise; only
    # the plug is a clean single-subject anchor.
    delta_y = {
        "Main": circuit_delta,
        "Subpanel": circuit_delta,
        "Circuit A": circuit_delta,
        "TV plug": TV_DELTA,
    }
    sigma = {
        "Main": CIRCUIT_NOISE_W,
        "Subpanel": CIRCUIT_NOISE_W,
        "Circuit A": CIRCUIT_NOISE_W,
        "TV plug": 1.0,
    }
    return solve_joint_detailed(
        _operator(),
        delta_y,
        sigma,
        _candidates(),
        {"tv": DeltaPrior(mean_w=85.0, sigma_w=10.0, sign="pos", model_version_id="m-tv")},
    )


def test_at08_the_plug_row_is_an_anchor() -> None:
    operator = _operator()
    plug_row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "TV plug")
    tv_column = operator.subjects.index(("device", "tv"))
    entry = operator.entries[(plug_row, tv_column)]
    assert entry.observability is Observability.DIRECT
    # It observes exactly one subject, so it is structurally a single-subject
    # endpoint; no separate "dedicated" declaration is needed.
    assert np.count_nonzero(operator.H[plug_row]) == 1


def test_at08_anchor_weight_is_ten_and_sigma_floor_is_half_a_watt() -> None:
    assert ANCHOR_WEIGHT == 10.0
    # A sub-0.5 W plug σ is floored so the anchor stays a measurement rather
    # than an unsatisfiable hard constraint.
    operator = _operator()
    delta_y = {"Main": 90.0, "Subpanel": 90.0, "Circuit A": 90.0, "TV plug": 85.0}
    sigma = {"Main": 20.0, "Subpanel": 20.0, "Circuit A": 20.0, "TV plug": 0.01}
    report = solve_joint_detailed(operator, delta_y, sigma, _candidates())
    plug_row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "TV plug")
    assert report.sigma[plug_row] == pytest.approx(0.5)


def test_at08_tv_stays_within_the_plug_sigma_on_a_noisy_circuit() -> None:
    # The circuit is much noisier than the plug and the total is only loosely
    # consistent with the TV's own 85 W.
    report = _solve(circuit_delta=280.0)
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    assert values["tv"] == pytest.approx(TV_DELTA, abs=1.0)
    # The circuit noise did not move the TV; it landed on the other candidates
    # and/or in ΔU.
    others = values["fridge"] + values["hall-lights"]
    unknown = float(np.sum(report.solution.u))
    assert others + unknown == pytest.approx(280.0 - TV_DELTA, abs=CIRCUIT_NOISE_W)


def test_at08_noise_never_lands_on_the_anchored_tv() -> None:
    """Across a range of noisy circuit readings the TV estimate is stable."""
    estimates = []
    for circuit_delta in (150.0, 200.0, 250.0, 300.0):
        report = _solve(circuit_delta=circuit_delta)
        values = dict(zip(report.subject_ids, report.solution.x, strict=True))
        estimates.append(values["tv"])
    assert max(estimates) - min(estimates) <= 2.0
    for estimate in estimates:
        assert estimate == pytest.approx(TV_DELTA, abs=1.5)


def test_at08_the_plug_anchor_outweighs_the_noisy_circuit_row() -> None:
    """The plug row's effective weight is 10× the circuit row's."""
    report = _solve(circuit_delta=200.0)
    weights = {
        meta.reader_id: 1.0 / report.sigma[i] if np.isfinite(report.sigma[i]) else 0.0
        for i, meta in enumerate(_operator().rows)
    }
    # The plug is a 1 W reader with a ×10 anchor scale: its weight is far above
    # the 20 W circuit rows.
    assert weights["TV plug"] > 5.0 * weights["Circuit A"]


def test_at08_a_silent_plug_leaves_the_tv_unanchored() -> None:
    """A missing plug reading is None, not 0 W: no anchor is fabricated."""
    operator = _operator()
    delta_y = {"Main": 100.0, "Subpanel": 100.0, "Circuit A": 100.0, "TV plug": None}
    sigma = {"Main": 5.0, "Subpanel": 5.0, "Circuit A": 5.0, "TV plug": 1.0}
    report = solve_joint_detailed(operator, delta_y, sigma, _candidates())
    plug_row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "TV plug")
    assert plug_row not in report.solution.active_rows
    assert np.isnan(report.solution.row_residual_w[plug_row])
