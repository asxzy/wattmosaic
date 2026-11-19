"""Tests for I1 candidate generation and topology-aware filtering.

Run with::

    .venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_candidates.py

The tests build against the K08 runtime fakes where they fit and against small,
explicit test doubles for capabilities the fakes don't expose (mapping-edge
lifecycle, reader-to-reader path edges).  Production code never imports fakes.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from wattmosaic_engine.contracts import (
    CandidateSource,
    EdgeLifecycleV3,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    RelationType,
    SubEvent,
    SubEventKind,
    TopologyEdgeV3,
    UnknownLoadProfileV3,
    VersionTriple,
)
from wattmosaic_engine.inference import (
    MAX_CANDIDATES,
    CandidateSet,
    generate_candidates,
)
from wattmosaic_engine.inference.topology_filter import (
    PROVISIONAL_FLAG,
    evaluate_topology,
)
from wattmosaic_engine.runtime.interfaces import MappingView, ObservationOperator

from tests.support.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeModelSet,
    FakeTopology,
    fake_operator,
)

NOW = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
VERSIONS = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


# --------------------------------------------------------------------------- #
# Helpers — small, explicit test doubles.
# --------------------------------------------------------------------------- #


def _ref(kind: str, identifier: str) -> EvidenceRef:
    return EvidenceRef(kind=kind, id=identifier)  # type: ignore[arg-type]


def _group(
    *,
    readers: Sequence[str] = ("Circuit A",),
    sub_events: Sequence[SubEvent] = (),
    group_id: str = "eg-1",
    opened_at: datetime = NOW,
) -> EventGroup:
    deltas = []
    for i, reader in enumerate(readers):
        from wattmosaic_engine.contracts import ReaderDelta

        deltas.append(
            ReaderDelta(
                reader_id=reader,
                delta_w=120.0 + i,
                sigma_w=3.0,
                completeness="complete",
                evidence_ref=_ref("event_evidence", f"evd-{reader}"),
            )
        )
    return EventGroup(
        group_id=group_id,
        revision=1,
        status=EventGroupStatus.DETECTED,
        opened_at=opened_at,
        sub_events=list(sub_events),
        reader_deltas=deltas,
        versions=VERSIONS,
    )


def _operator(
    readers: Sequence[str] = ("Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C"),
    subjects: Sequence[tuple[str, str]] = (),
    topology: FakeTopology | None = None,
    mapping: MappingView | None = None,
) -> ObservationOperator:
    topo = topology or FakeTopology.from_tree(HB17_TREE)
    mp = mapping or FakeMapping.from_pairs({s: "Circuit A" for s in subjects})
    return fake_operator(topo, mp, readers, subjects, at=NOW)


def _profile(profile_id: str, scope: str) -> UnknownLoadProfileV3:
    return UnknownLoadProfileV3(
        profile_id=profile_id,
        scope_endpoint_id=scope,
        lifecycle="candidate",
        signature={"delta_w_median": 120.0},
        event_count=6,
        independent_event_count=5,
        first_seen=NOW - timedelta(days=2),
        last_seen=NOW,
    )


class LifecycleMapping(FakeMapping):
    """FakeMapping that also reports a subject's mapping-edge lifecycle."""

    def __init__(self, pairs: dict[tuple[str, str], str], lifecycle: EdgeLifecycleV3) -> None:
        super().__init__(MappingProxyType(pairs))
        self._lifecycle = lifecycle

    def edge_lifecycle(self, subject: tuple[str, str]) -> EdgeLifecycleV3 | None:
        return self._lifecycle if subject in self.pairs else None


class PathTopology(FakeTopology):
    """FakeTopology that can report edge lists on a reader-to-reader path."""

    def __init__(self, spec: dict[str, Any], path_edges: dict[tuple[str, str], list[TopologyEdgeV3]]):
        super().__init__(FakeTopology.from_tree(spec).edges_all)
        self._path_edges = path_edges

    def path_edges(self, source: str, target: str) -> Sequence[TopologyEdgeV3]:
        return self._path_edges.get((source, target), [])


class PathMapping(LifecycleMapping):
    def __init__(
        self,
        pairs: dict[tuple[str, str], str],
        lifecycle: EdgeLifecycleV3,
        topology: PathTopology,
    ) -> None:
        super().__init__(pairs, lifecycle)
        self.topology = topology

    def path_edges(self, source: str, target: str) -> Sequence[TopologyEdgeV3]:
        return self.topology.path_edges(source, target)


def _edge(source: str, target: str, relation: RelationType, lifecycle: EdgeLifecycleV3) -> TopologyEdgeV3:
    from wattmosaic_engine.contracts import EdgeEvidence

    return TopologyEdgeV3(
        edge_id=f"e::{source}->{target}::{relation.value}::{lifecycle.value}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=lifecycle,
        declared_by="inference",
        evidence=EdgeEvidence(),
        valid_from=NOW - timedelta(days=1),
        pinned=False,
    )


# --------------------------------------------------------------------------- #
# AT-10 — property test (seeded random groups).
# --------------------------------------------------------------------------- #


def test_at10_every_candidate_set_has_an_unknown_for_each_affected_scope() -> None:
    rng = random.Random(20261118)
    reader_pool = ["Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C"]
    models = FakeModelSet.fixed({})

    for _ in range(150):
        affected = tuple(sorted(rng.sample(reader_pool, rng.randint(1, 4))))
        group = _group(readers=affected)
        mapping = FakeMapping.from_pairs({("device", "dev-x"): affected[0]})
        operator = _operator(subjects=[("device", "dev-x")], mapping=mapping)
        result = generate_candidates(group, operator, models, mapping)
        scopes = {c.subject_id for c in result.unknown_candidates}
        assert scopes == set(affected)
        assert all(c.subject_kind == "unknown_scope" for c in result.unknown_candidates)
        assert all(
            CandidateSource.UNKNOWN_SCOPE in c.sources for c in result.unknown_candidates
        )
        # Every candidate set (not just the unknown list) contains them.
        assert scopes.issubset({c.subject_id for c in result.candidates})


# --------------------------------------------------------------------------- #
# HB §17.7 fridge step with no HA event.
# --------------------------------------------------------------------------- #


def test_fridge_step_without_ha_yields_profile_and_unknown_scope() -> None:
    group = _group(readers=("Circuit B",))
    mapping = FakeMapping.from_pairs({})
    operator = _operator(subjects=[], mapping=mapping)
    fridge = _profile("ulp-fridge", "Circuit B")
    result = generate_candidates(
        group,
        operator,
        FakeModelSet.fixed({}),
        mapping,
        unknown_profiles={"Circuit B": [fridge]},
    )

    profile_cands = [
        c for c in result.candidates if CandidateSource.UNKNOWN_PROFILE in c.sources
    ]
    assert len(profile_cands) == 1
    assert profile_cands[0].subject_id == "ulp-fridge"
    unknown_cands = [c for c in result.candidates if c.subject_kind == "unknown_scope"]
    assert [c.subject_id for c in unknown_cands] == ["Circuit B"]
    assert {c.subject_id for c in result.candidates} == {"ulp-fridge", "Circuit B"}


# --------------------------------------------------------------------------- #
# Topology filtering: provisional kept/flagged, confirmed rejected with reason.
# --------------------------------------------------------------------------- #


def test_provisional_incompatible_candidate_is_kept_and_flagged() -> None:
    subject = ("device", "dev-light")
    topo = PathTopology(
        HB17_TREE,
        {("Circuit A", "Circuit B"): [_edge("Circuit B", "Circuit A", RelationType.SHARED_LOAD, EdgeLifecycleV3.PROVISIONAL)]},
    )
    mapping = PathMapping({subject: "Circuit A"}, EdgeLifecycleV3.CONFIRMED, topo)
    operator = fake_operator(
        topo, mapping, ["Main", "Subpanel", "Circuit A", "Circuit B"], [subject], at=NOW
    )
    verdict = evaluate_topology(subject, ("Circuit B",), operator, mapping)
    assert verdict.action == "soft_keep"
    assert not verdict.filtered
    assert verdict.soft_flag

    group = _group(readers=("Circuit B",))
    result = generate_candidates(group, operator, FakeModelSet.fixed({}), mapping)
    light = next(c for c in result.candidates if c.subject_id == "dev-light")
    assert PROVISIONAL_FLAG in light.evidence_terms
    assert not light.filtered


def test_confirmed_incompatible_candidate_is_rejected_with_a_reason() -> None:
    subject = ("device", "dev-light")
    topo = PathTopology(
        HB17_TREE,
        {("Circuit A", "Circuit B"): [_edge("Circuit B", "Circuit A", RelationType.SHARED_LOAD, EdgeLifecycleV3.CONFIRMED)]},
    )
    mapping = PathMapping({subject: "Circuit A"}, EdgeLifecycleV3.CONFIRMED, topo)
    operator = fake_operator(
        topo, mapping, ["Main", "Subpanel", "Circuit A", "Circuit B"], [subject], at=NOW
    )
    verdict = evaluate_topology(subject, ("Circuit B",), operator, mapping)
    assert verdict.action == "reject"
    assert verdict.filtered
    assert verdict.reason and "confirmed" in verdict.reason.lower()

    group = _group(readers=("Circuit B",))
    result = generate_candidates(group, operator, FakeModelSet.fixed({}), mapping)
    assert all(c.subject_id != "dev-light" for c in result.candidates)
    rejected = [c for c in result.rejected if c.subject_id == "dev-light"]
    assert len(rejected) == 1
    assert rejected[0].filtered and rejected[0].filter_reason


def test_unknown_scope_never_rejected_even_with_confirmed_barrier() -> None:
    topo = PathTopology(HB17_TREE, {})
    mapping = PathMapping({}, EdgeLifecycleV3.CONFIRMED, topo)
    operator = fake_operator(
        topo, mapping, ["Main", "Subpanel", "Circuit A", "Circuit B"], [], at=NOW
    )
    verdict = evaluate_topology(("unknown_scope", "Circuit A"), ("Circuit B",), operator, mapping)
    assert verdict.action == "keep"


# --------------------------------------------------------------------------- #
# Source 8 — delayed HVAC compressor.
# --------------------------------------------------------------------------- #


def test_delayed_hvac_compressor_from_90s_earlier_group_is_a_candidate() -> None:
    subject = ("device", "hvac-compressor")
    mapping = FakeMapping.from_pairs({subject: "Circuit A"})
    operator = _operator(subjects=[subject], mapping=mapping)
    group = _group(readers=("Circuit A",), opened_at=NOW)
    opened_earlier = NOW - timedelta(seconds=90)
    result = generate_candidates(
        group,
        operator,
        FakeModelSet.fixed({}),
        mapping,
        pending_delayed=[(subject, opened_earlier)],
    )
    cand = next(c for c in result.candidates if c.subject_id == "hvac-compressor")
    assert CandidateSource.DELAYED_RESPONSE in cand.sources


def test_stale_pending_delayed_response_is_not_a_candidate() -> None:
    subject = ("device", "hvac-compressor")
    mapping = FakeMapping.from_pairs({subject: "Circuit A"})
    operator = _operator(subjects=[subject], mapping=mapping)
    group = _group(readers=("Circuit A",), opened_at=NOW)
    result = generate_candidates(
        group,
        operator,
        FakeModelSet.fixed({}),
        mapping,
        pending_delayed={subject: NOW - timedelta(hours=2)},
    )
    cand = next(c for c in result.candidates if c.subject_id == "hvac-compressor")
    assert CandidateSource.DELAYED_RESPONSE not in cand.sources


# --------------------------------------------------------------------------- #
# Sources 4 / 7 / 9 and merging.
# --------------------------------------------------------------------------- #


def test_sources_merge_per_subject_and_are_ranked_by_source_count() -> None:
    rich = ("device", "rich-device")
    poor = ("device", "poor-device")
    mapping = FakeMapping.from_pairs({rich: "Circuit A", poor: "Circuit A"})
    operator = _operator(subjects=[rich, poor], mapping=mapping)

    ha = SubEvent(
        sub_event_id="se-1",
        kind=SubEventKind.HA_STATE,
        ref=_ref("ha_event", "device:rich-device"),
        observed_at=NOW,
        timestamp_uncertainty_s=0.0,
    )
    group = _group(readers=("Circuit A",), sub_events=[ha])
    result = generate_candidates(group, operator, FakeModelSet.fixed({}), mapping)
    rich_cand = next(c for c in result.candidates if c.subject_id == "rich-device")
    assert set(rich_cand.sources) == {
        CandidateSource.HA_STATE,
        CandidateSource.PRIOR_MAPPING,
        CandidateSource.TOPOLOGY_VISIBLE,
    }
    assert rich_cand.evidence_terms["source_count"] == 3.0
    poor_cand = next(c for c in result.candidates if c.subject_id == "poor-device")
    assert set(poor_cand.sources) == {
        CandidateSource.PRIOR_MAPPING,
        CandidateSource.TOPOLOGY_VISIBLE,
    }
    non_unknown = [c for c in result.candidates if c.subject_kind != "unknown_scope"]
    assert non_unknown[0].subject_id == "rich-device"


def test_known_device_group_adds_group_and_member_candidates() -> None:
    mapping = FakeMapping.from_pairs({("device", "lamp-1"): "Circuit A"})
    operator = _operator(subjects=[], mapping=mapping)
    group = _group(readers=("Circuit A",))
    result = generate_candidates(
        group,
        operator,
        FakeModelSet.fixed({}),
        mapping,
        groups={"grp-living": ["device:lamp-1", "device:lamp-2"]},
    )
    assert any(c.subject_kind == "group" and c.subject_id == "grp-living" for c in result.candidates)
    assert any(
        c.subject_id == "lamp-1" and CandidateSource.DEVICE_GROUP in c.sources
        for c in result.candidates
    )
    assert any(c.subject_id == "lamp-2" for c in result.candidates)


# --------------------------------------------------------------------------- #
# Truncation.
# --------------------------------------------------------------------------- #


def test_truncation_is_recorded_and_never_drops_unknown() -> None:
    subjects = [("device", f"dev-{i:03d}") for i in range(MAX_CANDIDATES + 10)]
    mapping = FakeMapping.from_pairs({s: "Circuit A" for s in subjects})
    operator = _operator(subjects=subjects, mapping=mapping)
    group = _group(readers=("Circuit A", "Circuit B"))
    result = generate_candidates(group, operator, FakeModelSet.fixed({}), mapping)

    assert result.truncated == 10
    assert len([c for c in result.candidates if c.subject_kind != "unknown_scope"]) == MAX_CANDIDATES
    assert {c.subject_id for c in result.unknown_candidates} == {"Circuit A", "Circuit B"}
    for unknown in result.unknown_candidates:
        assert unknown in result.candidates
    assert isinstance(result, CandidateSet)


def test_source_count_fits_within_cap_without_truncation() -> None:
    subjects = [("device", f"dev-{i:03d}") for i in range(MAX_CANDIDATES)]
    mapping = FakeMapping.from_pairs({s: "Circuit A" for s in subjects})
    operator = _operator(subjects=subjects, mapping=mapping)
    result = generate_candidates(
        _group(readers=("Circuit A",)), operator, FakeModelSet.fixed({}), mapping
    )
    assert result.truncated == 0
