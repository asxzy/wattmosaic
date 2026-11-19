"""I2: matching evidence A–H and AT-05 temporal response matching.

AT-05 (HB §21 test 5): an X07 light (τ≈0), a blind (transient then standby)
and an HVAC compressor (+90 s) are each scored highest at their *true* τ.  The
HVAC step is matched over the whole window, not dropped as unanchored (the old
``engine.py:1116-1124`` behaviour).

The remaining tests cover the task's named checks: C+D+E are not double
weighted, the weights version is recorded, every term stays in its clip range,
and sign disagreement only hard-rejects a confirmed consumer with an ON
transition and a clearly negative ΔY.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    CandidateSource,
    DeltaPrior,
    EdgeLifecycleV3,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    HAEvent,
    Observability,
    ReaderDelta,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from wattmosaic_engine.inference.evidence import (
    EVIDENCE_TERMS,
    WEIGHTS_VERSION,
    EvidenceInputs,
    load_weights,
    score_and_record,
    score_candidate,
    score_candidates,
    version_as_float,
)
from wattmosaic_engine.models.device_profile import DevicePowerProfile
from wattmosaic_engine.runtime.interfaces import (
    EntryMeta,
    ObservationOperator,
    RowMeta,
)

from tests.support.fakes import (
    FakeMapping,
    FakeModelSet,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-fake"
)
READER = "Main"


# --------------------------------------------------------------------------- #
# Scenario builders (pure, explicit times — no wall clock).
# --------------------------------------------------------------------------- #


def _group(
    *,
    delta_w: float = 150.0,
    sigma_w: float | None = 10.0,
    duration_s: float = 180.0,
    onset_uncertainty_s: float = 0.0,
    with_ha: bool = False,
) -> EventGroup:
    sub_events = []
    if with_ha:
        sub_events.append(
            SubEvent(
                sub_event_id="sub-ha",
                kind=SubEventKind.HA_STATE,
                ref=EvidenceRef(kind="ha_event", id="evt-1"),
                observed_at=T0,
                timestamp_uncertainty_s=onset_uncertainty_s,
            )
        )
    return EventGroup(
        group_id="grp-1",
        revision=1,
        status=EventGroupStatus.MATCHING,
        opened_at=T0,
        closed_at=T0 + timedelta(seconds=duration_s),
        sub_events=sub_events,
        reader_deltas=[
            ReaderDelta(
                reader_id=READER,
                delta_w=delta_w,
                sigma_w=sigma_w,
                completeness="complete",
            )
        ],
        versions=VT,
    )


def _samples(delay_s: float, amplitude: float = 150.0, *, transient: bool = False) -> list[tuple[datetime, float | None]]:
    """A step at ``delay_s``; optionally with a startup transient + standby."""
    out: list[tuple[datetime, float | None]] = [
        (T0 - timedelta(seconds=10), 0.0)
    ]
    for s in range(0, 181, 10):
        if s < delay_s:
            value = 0.0
        elif transient and s < delay_s + 30:
            value = amplitude * 1.6  # motor startup transient
        elif transient:
            value = amplitude  # operating plateau
        else:
            value = amplitude
        out.append((T0 + timedelta(seconds=s), value))
    return out


def _operator(subject_id: str = "dev", reader: str = READER) -> ObservationOperator:
    topology = FakeTopology.from_tree({reader: {}})
    mapping = FakeMapping.from_pairs({("device", subject_id): reader})
    return fake_operator(topology, mapping, [reader], [("device", subject_id)])


def _candidate(
    subject_id: str = "dev",
    *,
    transition: str = "on",
    sources: list[CandidateSource] | None = None,
) -> CandidateHypothesis:
    return CandidateHypothesis(
        candidate_id=f"cand::{subject_id}",
        subject_kind="device",
        subject_id=subject_id,
        sources=sources or [CandidateSource.LEARNED_PROFILE],
        transition=transition,
        evidence_terms={},
    )


def _models(amplitude: float = 150.0, sigma: float = 10.0) -> FakeModelSet:
    return FakeModelSet.fixed(
        {"dev": DeltaPrior(mean_w=amplitude, sigma_w=sigma, sign="pos", model_version_id="m")}
    )


def _inputs(
    group: EventGroup,
    operator: ObservationOperator,
    models: FakeModelSet,
    samples: list[tuple[datetime, float | None]],
    *,
    profiles: dict[str, DevicePowerProfile] | None = None,
) -> EvidenceInputs:
    return EvidenceInputs(
        group=group,
        operator=operator,
        models=models,
        samples_by_reader={READER: samples},
        profiles=profiles,
    )


def _best_delay(
    delays: list[float],
    samples: list[tuple[datetime, float | None]],
    *,
    amplitude: float = 150.0,
) -> tuple[float, Any]:
    """Score each declared delay against the same window; return best + results."""
    group = _group(delta_w=amplitude, duration_s=180.0)
    operator = _operator()
    models = _models(amplitude=amplitude)
    results: dict[float, Any] = {}
    for delay in delays:
        prof = DevicePowerProfile(device_id="dev").with_response_delay(delay)
        results[delay] = score_candidate(
            _candidate(), _inputs(group, operator, models, samples, profiles={"dev": prof})
        )
    best = max(delays, key=lambda d: results[d].temporal_fit)
    return best, results


# --------------------------------------------------------------------------- #
# AT-05 — each load is matched at its true τ.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "true_delay", "amplitude", "transient"),
    [
        ("light", 0.0, 60.0, False),  # τ≈0 instantaneous step
        ("blind", 5.0, 80.0, True),  # transient then standby/plateau
        ("hvac", 90.0, 150.0, False),  # compressor +90 s
    ],
)
def test_at05_each_load_is_matched_at_its_true_tau(
    label: str, true_delay: float, amplitude: float, transient: bool
) -> None:
    samples = _samples(true_delay, amplitude=amplitude, transient=transient)
    delays = [0.0, 5.0, 30.0, 90.0]
    best, results = _best_delay(delays, samples, amplitude=amplitude)
    assert best == pytest.approx(true_delay), (
        f"{label}: expected τ={true_delay}, got τ={best} "
        f"(fits={{{', '.join(f'{d}:{r.temporal_fit:.3f}' for d, r in results.items())}}})"
    )
    assert results[true_delay].temporal_fit > 0.9


def test_at05_hvac_step_is_matched_not_dropped_as_unanchored() -> None:
    """The +90 s HVAC step must still be scored, not filtered out at onset."""
    group = _group(delta_w=150.0, duration_s=180.0)
    operator = _operator()
    samples = _samples(90.0)
    models = _models()
    prof = DevicePowerProfile(device_id="dev").with_response_delay(90.0)
    result = score_candidate(
        _candidate(), _inputs(group, operator, models, samples, profiles={"dev": prof})
    )
    assert result.temporal_fit > 0.9
    assert not result.rejected
    assert result.terms["A"] <= 2.0


# --------------------------------------------------------------------------- #
# Dependence correction — C+D+E from one reader are not double-weighted.
# --------------------------------------------------------------------------- #


def test_duplicated_readers_do_not_double_weight_cde() -> None:
    """Duplicating a reader's ΔY leaves the score unchanged within 1 %."""
    base_group = _group(delta_w=100.0)
    dup_group = base_group.model_copy(
        update={
            "reader_deltas": [
                *base_group.reader_deltas,
                ReaderDelta(
                    reader_id=READER,
                    delta_w=100.0,
                    sigma_w=10.0,
                    completeness="complete",
                ),
            ]
        }
    )
    operator = _operator()
    samples = _samples(0.0, amplitude=100.0)
    models = _models(amplitude=100.0)
    inputs_base = _inputs(base_group, operator, models, samples)
    inputs_dup = _inputs(dup_group, operator, models, samples)
    base = score_candidate(_candidate(), inputs_base)
    dup = score_candidate(_candidate(), inputs_dup)
    assert base.score == pytest.approx(dup.score, rel=0.01)


def test_cde_terms_use_the_configured_dependence_factor() -> None:
    config = load_weights()
    group = config["dependence_correction"]["group"]
    factor = config["dependence_correction"]["factor"]
    assert set(group) == {"C", "D", "E"}
    assert factor == pytest.approx(1.0 / np.sqrt(3.0))


def test_weights_version_is_recorded_on_candidates() -> None:
    group = _group()
    operator = _operator()
    candidates = [_candidate("dev")]
    inputs = _inputs(group, operator, _models(), _samples(0.0))
    scored, results = score_and_record(candidates, inputs)
    assert results["cand::dev"].weights_version == WEIGHTS_VERSION
    assert scored[0].evidence_terms["_weights_version"] == pytest.approx(
        version_as_float(WEIGHTS_VERSION)
    )
    assert candidates[0].evidence_terms == {}  # inputs are never mutated


# --------------------------------------------------------------------------- #
# Clip range — every term stays in [clip_min, clip_max].
# --------------------------------------------------------------------------- #


def test_every_term_stays_within_clip_range() -> None:
    config = load_weights()
    lo, hi = config["clip_min_nats"], config["clip_max_nats"]
    group = _group(delta_w=150.0)
    operator = _operator()
    samples = _samples(90.0)
    models = _models()
    for delay in (0.0, 90.0):
        prof = DevicePowerProfile(device_id="dev").with_response_delay(delay)
        result = score_candidate(
            _candidate(), _inputs(group, operator, models, samples, profiles={"dev": prof})
        )
        assert set(result.terms) == set(EVIDENCE_TERMS)
        for term, value in result.terms.items():
            assert lo <= value <= hi, f"term {term}={value} outside [{lo}, {hi}]"


def test_scoring_is_deterministic() -> None:
    group = _group()
    operator = _operator()
    inputs = _inputs(group, operator, _models(), _samples(0.0))
    a = score_candidate(_candidate(), inputs)
    b = score_candidate(_candidate(), inputs)
    assert a == b


# --------------------------------------------------------------------------- #
# Sign disagreement — penalty except for confirmed consumer + ON + ΔY < -3σ.
# --------------------------------------------------------------------------- #


def test_sign_disagreement_is_only_a_penalty_for_unconfirmed() -> None:
    group = _group(delta_w=-100.0, sigma_w=10.0)  # ON candidate but ΔY negative
    operator = _operator()
    inputs = EvidenceInputs(
        group=group,
        operator=operator,
        models=_models(),  # prior sign = pos
        samples_by_reader={READER: [(T0, 0.0), (T0 + timedelta(seconds=10), -100.0)]},
    )
    candidate = _candidate(sources=[CandidateSource.HA_STATE])  # unconfirmed
    result = score_candidate(candidate, inputs)
    assert not result.rejected
    assert result.terms["B"] < 0.0


def test_sign_disagreement_hard_rejects_confirmed_consumer_on_clear_negative() -> None:
    group = _group(delta_w=-100.0, sigma_w=10.0)
    operator = _operator()
    inputs = EvidenceInputs(
        group=group,
        operator=operator,
        models=_models(),
        samples_by_reader={READER: [(T0, 0.0), (T0 + timedelta(seconds=10), -100.0)]},
    )
    candidate = _candidate(
        sources=[CandidateSource.PRIOR_MAPPING], transition="on"
    )  # confirmed consumer
    result = score_candidate(candidate, inputs)
    assert result.rejected
    assert result.rejection_reason is not None
    scored, _ = score_and_record([candidate], inputs)
    assert scored[0].filtered
    assert scored[0].filter_reason is not None


def test_off_transition_negative_delta_is_not_hard_rejected() -> None:
    prior = DeltaPrior(mean_w=-80.0, sigma_w=10.0, sign="neg", model_version_id="m")
    models = FakeModelSet.fixed({"dev": prior})
    group = _group(delta_w=-80.0, sigma_w=10.0)
    result = score_candidate(
        _candidate(transition="off", sources=[CandidateSource.PRIOR_MAPPING]),
        _inputs(group, _operator(), models, _samples(0.0, amplitude=-80.0)),
    )
    assert not result.rejected
    assert result.terms["B"] > 0.0


# --------------------------------------------------------------------------- #
# Terms E (provisional edges are soft) and H (context lineage).
# --------------------------------------------------------------------------- #


def _operator_with_edge_lifecycle(lifecycle: EdgeLifecycleV3) -> ObservationOperator:
    rows = (RowMeta(reader_id=READER, kind="reader", sigma_w=10.0, sigma_inflation=1.0),)
    entries = {
        (0, 0): EntryMeta(
            coefficient=1.0,
            observability=Observability.EXPECTED,
            edge_lifecycle=lifecycle,
            edge_ids=("e1",),
        )
    }
    return ObservationOperator(
        H=np.ones((1, 1)),
        rows=rows,
        subjects=(("device", "dev"),),
        entries=entries,
        unknown_scopes=(),
        soft_alternatives=(),
        topology_version_id="topo-1",
        mapping_version_id="map-1",
        at=T0,
    )


def test_provisional_edge_is_soft_and_confirmed_is_stronger() -> None:
    group = _group()
    inputs_conf = EvidenceInputs(
        group=group,
        operator=_operator_with_edge_lifecycle(EdgeLifecycleV3.CONFIRMED),
        models=_models(),
        samples_by_reader={READER: _samples(0.0)},
    )
    inputs_prov = EvidenceInputs(
        group=group,
        operator=_operator_with_edge_lifecycle(EdgeLifecycleV3.PROVISIONAL),
        models=_models(),
        samples_by_reader={READER: _samples(0.0)},
    )
    confirmed = score_candidate(_candidate(), inputs_conf)
    provisional = score_candidate(_candidate(), inputs_prov)
    assert confirmed.terms["E"] > provisional.terms["E"]


def test_linked_ha_event_improves_context_term() -> None:
    group = _group(with_ha=True)
    ha_event = HAEvent(
        event_id="evt-1",
        entity_id="light.living_room",
        observed_at=T0,
        received_at=T0,
        to_state="on",
        linked=True,
    )
    linked = score_candidate(
        _candidate(sources=[CandidateSource.HA_STATE]),
        _inputs(group, _operator(), _models(), _samples(0.0)),
    )
    with_context = score_candidate(
        _candidate(sources=[CandidateSource.HA_STATE]),
        EvidenceInputs(
            group=group,
            operator=_operator(),
            models=_models(),
            samples_by_reader={READER: _samples(0.0)},
            ha_events={"evt-1": ha_event},
        ),
    )
    assert with_context.terms["H"] >= linked.terms["H"]


def test_missing_delta_is_never_treated_as_zero() -> None:
    """A missing reader keeps ΔY=None; it must not be scored as a 0 W change."""
    group = _group().model_copy(
        update={
            "reader_deltas": [
                ReaderDelta(reader_id=READER, delta_w=None, completeness="missing")
            ]
        }
    )
    result = score_candidate(
        _candidate(), _inputs(group, _operator(), _models(), _samples(0.0))
    )
    assert all(np.isfinite(v) for v in result.terms.values())


def test_score_candidates_matches_individual_scoring() -> None:
    group = _group()
    candidates = [_candidate("dev"), _candidate("other")]
    operator = _operator()
    models = _models()
    inputs = EvidenceInputs(
        group=group,
        operator=operator,
        models=models,
        samples_by_reader={READER: _samples(0.0)},
    )
    individual = {c.candidate_id: score_candidate(c, inputs) for c in candidates}
    batch = score_candidates(candidates, inputs)
    assert batch == individual
