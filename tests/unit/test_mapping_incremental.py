"""M2: event-driven incremental updater (HB §17 step 6, design §8).

Only the affected cells are recomputed: for HB §17 step 6, the six light×reader
cells plus the group cell.  Mapping versions bump only when a CONFIRMED match
introduces a new association; contribution changes alone never do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    CandidateHypothesis,
    CandidateSource,
    ConfidenceVectorV3,
    DeviceEstimate,
    DeviceGroupEstimate,
    EdgeLifecycleV3,
    EstimateStatus,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    MappingEdge,
    MatchState,
    VersionTriple,
)
from wattmosaic_engine.mapping import MatrixStore, MatrixUpdater

from tests.support.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
READERS = ["Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C"]
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _clock(start: datetime | None = None) -> Any:
    current = start or T0

    def read() -> datetime:
        nonlocal current
        now = current
        current = current + timedelta(seconds=1)
        return now

    return read


def _estimate(
    subject_id: str,
    point_w: float | None = 45.0,
    *,
    group_id: str | None = None,
    ident: IdentifiabilityStatus = IdentifiabilityStatus.IDENTIFIABLE,
) -> DeviceEstimate:
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point_w,
        bounds=Bounds(lower_w=None if point_w is None else 0.0, upper_w=None),
        status=EstimateStatus.ESTIMATED if point_w is not None else EstimateStatus.UNKNOWN,
        identifiability=ident,
        group_id=group_id,
        method=InferenceMethod.JOINT_DELTA if point_w is not None else InferenceMethod.NONE,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=["Circuit A"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )


def _result(
    estimates: list[DeviceEstimate],
    *,
    groups: list[DeviceGroupEstimate] | None = None,
    candidates: list[CandidateHypothesis] | None = None,
    matches: dict[str, MatchState] | None = None,
    group_id: str | None = None,
) -> JointInferenceResult:
    return JointInferenceResult(
        result_id="res-test",
        event_group_id=group_id,
        event_group_revision=1,
        status=EventGroupStatus.CONFIRMED,
        device_estimates=estimates,
        group_estimates=groups or [],
        unknown_estimates=[],
        candidates=candidates or [],
        matches=matches or {},
        residual_w_by_reader={},
        versions=VT,
        computed_at=T0,
    )


def _operator() -> Any:
    mapping = FakeMapping.from_pairs(
        {("device", "light-1"): "Circuit A", ("device", "light-2"): "Circuit A"}
    )
    return fake_operator(
        FakeTopology.from_tree(HB17_TREE),
        mapping,
        READERS,
        [("device", "light-1"), ("device", "light-2")],
    )


def test_only_affected_cells_change_hb17_step6() -> None:
    """The six light×reader cells (2×3) change; nothing else is touched."""
    store = MatrixStore(clock=_clock())
    updater = MatrixUpdater(store, clock=_clock())
    result = _result([_estimate("light-1"), _estimate("light-2", 10.0)])
    delta = updater.apply(result, _operator())

    # A light mapped to Circuit A enters Circuit A, Subpanel and Main.
    assert len(delta.upserted) == 6
    assert {key[1] for key in delta.upserted} == {"light-1", "light-2"}
    assert {key[2] for key in delta.upserted} == {"Circuit A", "Subpanel", "Main"}
    # Circuit B / C are untouched.
    assert not any(key[2] in ("Circuit B", "Circuit C") for key in delta.upserted)


def test_group_cell_is_updated_when_group_is_in_result() -> None:
    store = MatrixStore(clock=_clock())
    updater = MatrixUpdater(store, clock=_clock())
    estimates = [
        _estimate("light-1", None, group_id="grp-1", ident=IdentifiabilityStatus.NON_IDENTIFIABLE),
        _estimate("light-2", None, group_id="grp-1", ident=IdentifiabilityStatus.NON_IDENTIFIABLE),
    ]
    group = DeviceGroupEstimate(
        group_id="grp-1",
        member_ids=["light-1", "light-2"],
        point_w=55.0,
        bounds=Bounds(lower_w=0.0, upper_w=55.0, basis="group_sum"),
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        source_reader_ids=["Circuit A"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )
    delta = updater.apply(_result(estimates, groups=[group]), _operator())
    # HB §17 step 6: six light×reader cells plus one group-level cell.
    assert len(delta.upserted) == 7
    group_keys = [key for key in delta.upserted if key[0] == "group"]
    assert group_keys == [("group", "grp-1", "Circuit A")]


def test_history_keeps_old_values_after_contribution_update() -> None:
    store = MatrixStore(clock=_clock())
    updater = MatrixUpdater(store, clock=_clock())
    updater.apply(_result([_estimate("light-1", 45.0)]), _operator())
    old_history = store.history(("device", "light-1", "Circuit A"))
    assert old_history[-1].power.estimated_power_w == 45.0

    updater.apply(_result([_estimate("light-1", 60.0)]), _operator())
    history = store.history(("device", "light-1", "Circuit A"))
    assert [h.power.estimated_power_w for h in history] == [45.0, 60.0]


# --------------------------------------------------------------------------- #
# Mapping version evolution: only CONFIRMED new associations bump the version.
# --------------------------------------------------------------------------- #


class _RecordingVersions:
    def __init__(self) -> None:
        self.triple = VT
        self.proposed: list[list[MappingEdge]] = []
        self.promoted: list[str] = []

    def current(self) -> VersionTriple:
        return self.triple

    def mapping_view(self, version_id: str) -> Any:
        class View:
            def subjects_on(self, reader_id: str) -> tuple[Any, ...]:
                return ()

            def most_specific_reader(self, subject: Any) -> str | None:
                return "Circuit A"

        return View()

    def propose_mapping(self, edges: Any, change_event_id: Any) -> str:
        self.proposed.append(list(edges))
        return "map-2"

    def promote_mapping(self, version_id: str, reason: str) -> None:
        self.promoted.append(version_id)
        self.triple = VersionTriple(
            topology_version_id="topo-1", mapping_version_id=version_id, model_set_id="mset-1"
        )


def _confirmed_result(new_reader: str) -> JointInferenceResult:
    est = _estimate("light-1", 45.0)
    est.source_reader_ids = [new_reader]
    candidate = CandidateHypothesis(
        candidate_id="cand-1",
        subject_kind="device",
        subject_id="light-1",
        sources=[CandidateSource.HA_STATE],
        evidence_terms={},
    )
    return _result(
        [est],
        candidates=[candidate],
        matches={"cand-1": MatchState.CONFIRMED},
        group_id="evt-1",
    )


def test_confirmed_new_association_bumps_mapping_version() -> None:
    versions = _RecordingVersions()
    store = MatrixStore(clock=_clock())
    updater = MatrixUpdater(store, versions, clock=_clock())
    updater.apply(_confirmed_result("Circuit B"), _operator())
    assert len(versions.proposed) == 1
    assert versions.promoted == ["map-2"]
    edge = versions.proposed[0][-1]
    assert edge.endpoint_id == "Circuit B"
    assert edge.lifecycle == EdgeLifecycleV3.CONFIRMED


def test_contribution_change_alone_does_not_bump_mapping_version() -> None:
    versions = _RecordingVersions()
    store = MatrixStore(clock=_clock())
    updater = MatrixUpdater(store, versions, clock=_clock())
    updater.apply(_result([_estimate("light-1", 45.0)]), _operator())
    updater.apply(_result([_estimate("light-1", 60.0)]), _operator())
    assert versions.proposed == []
    assert versions.promoted == []
