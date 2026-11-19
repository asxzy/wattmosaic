"""AT-10 — unknown-load alternatives remain available during matching (HB §21).

Closes: I1. 100 % of candidate sets contain an unknown_scope candidate
(property test), and a weak known candidate loses to unknown.

The invented ``generate_candidates(seed=...)`` / ``rank_candidates(...)`` surface
named by the original K07 sketch never landed.  This closed test drives I1's real
``generate_candidates(group, operator, models, mapping)`` (seeded HB §17 groups,
as in ``tests/unit/test_candidates.py``) for the property test, and the shipped I3
joint solver + I6 ``classify_match`` decision rule for the weak-known case.  The
fixture plumbing is rewritten; the AT-10 assertions are preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    CandidateSource,
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    MatchState,
    ReaderDelta,
    VersionTriple,
)
from wattmosaic_engine.inference.candidates import generate_candidates  # real I1 surface
from wattmosaic_engine.inference.joint import solve_joint_detailed, unknown_share  # real I3
from wattmosaic_engine.inference.matching import classify_match  # real I6 decision rule
from wattmosaic_engine.runtime.fakes import (
    FakeMapping,
    FakeModelSet,
    FakeTopology,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import ObservationOperator, UnknownPrior

CLOSED_BY = "I1"

pytestmark = [pytest.mark.acceptance]

NOW = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
VT = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1")
READERS = ("Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C")


def _group(seed: int, readers) -> EventGroup:
    """A seeded, deterministic event group affecting ``readers``."""
    deltas = [
        ReaderDelta(reader_id=r, delta_w=100.0 + i, sigma_w=3.0, completeness="complete")
        for i, r in enumerate(readers)
    ]
    return EventGroup(
        group_id=f"eg-seed-{seed}",
        revision=1,
        status=EventGroupStatus.DETECTED,
        opened_at=NOW,
        reader_deltas=deltas,
        versions=VT,
    )


def _operator(subjects) -> ObservationOperator:
    topo = FakeTopology.from_tree(
        {"Main": {"Subpanel": {"Circuit A": {}, "Circuit B": {}}, "Circuit C": {}}}
    )
    mapping = FakeMapping.from_pairs({s: "Circuit A" for s in subjects})
    return fake_operator(topo, mapping, READERS, subjects, at=NOW)


def test_unknown_alternative_always_available_and_can_win() -> None:
    """Property test + ranking: unknown_scope survives and can beat a weak known."""
    # 100 seeded groups: every candidate set keeps its unknown_scope alternative.
    for seed in range(100):
        # A deterministic 1–4 reader subset from the seed (the K08 fakes need no
        # RNG state: the same seed always yields the same affected readers).
        affected = tuple(r for i, r in enumerate(READERS) if (seed >> i) & 1) or ("Main",)
        group = _group(seed, affected)
        subjects = [("device", "dev-x")]
        result = generate_candidates(
            group, _operator(subjects), FakeModelSet.fixed({}),
            FakeMapping.from_pairs({subjects[0]: "Circuit A"}),
        )
        assert result.candidates, f"empty candidate set for seed {seed}"
        unknown = [c for c in result.candidates if CandidateSource.UNKNOWN_SCOPE in c.sources]
        assert unknown, f"seed {seed} lost its unknown_scope alternative"
        assert set(affected).issubset({c.subject_id for c in unknown})
        assert all(c.subject_kind == "unknown_scope" for c in unknown)

    # Ranking: a weak known candidate with a negligible prior cannot claim a
    # strong observed step; the unknown alternative takes most of it and the
    # known candidate is rejected by the real I6 decision rule.
    known = CandidateHypothesis(
        candidate_id="cand-weak",
        subject_kind="device",
        subject_id="weak-known",
        sources=[CandidateSource.HA_STATE],
        transition="on",
        evidence_terms={},
    )
    unknown = CandidateHypothesis(
        candidate_id="cand-unknown",
        subject_kind="unknown_scope",
        subject_id="Main",
        sources=[CandidateSource.UNKNOWN_SCOPE],
        transition=None,
        evidence_terms={},
    )
    topo = FakeTopology.from_tree({"Main": {}})
    mapping = FakeMapping.from_pairs({("device", "weak-known"): "Main"})
    base = fake_operator(topo, mapping, ["Main"], [("device", "weak-known")], at=NOW)
    operator = ObservationOperator(
        H=base.H,
        rows=base.rows,
        subjects=base.subjects,
        entries=base.entries,
        unknown_scopes=(("Main", (0,)),),
        soft_alternatives=base.soft_alternatives,
        topology_version_id=base.topology_version_id,
        mapping_version_id=base.mapping_version_id,
        at=base.at,
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0},
        {"Main": 2.0},
        [known, unknown],
        {"weak-known": DeltaPrior(mean_w=1.0, sigma_w=100.0, sign="pos", model_version_id="m")},
        # The unknown competitor is deliberately broad (σ = 500 W): a weak known
        # prior must not claim a 100 W step it cannot explain.
        {"Main": UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=500.0, lambda_u=0.5)},
    )
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    share = unknown_share(operator, report.solution, {"Main": 100.0})["Main"]
    assert share is not None and share > 0.5  # unknown wins the attribution
    decision = classify_match(share=share, identifiable=True, unknown_share=share)
    assert decision in (MatchState.PROVISIONAL, MatchState.CONFIRMED)
    weak_decision = classify_match(
        share=abs(float(values["weak-known"])) / 100.0,
        identifiable=True,
        unknown_share=share,
    )
    assert weak_decision == MatchState.REJECTED  # the weak known candidate loses
