"""L4 — device-attributed learning observations from immutable event groups.

Pure tests: contracts are built directly, no DB and no wall clock (every
timestamp is injected).  The event-group producers (P5) are not merged yet, so
observations are built straight from :class:`EventGroup` / :class:`JointInferenceResult`
contracts, exactly as the task requires.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    ModelBasis,
    ReaderDelta,
    VersionTriple,
)
from wattmosaic_engine.contracts.inference import JointInferenceResult
from wattmosaic_engine.contracts.measurement import EventGroup, EventGroupStatus
from wattmosaic_engine.learning.observations import (
    build_group_observations,
    build_observations,
    observation_for_estimate,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _estimate(
    subject_id: str,
    *,
    point_w: float | None,
    readers: list[str],
    ident: IdentifiabilityStatus = IdentifiabilityStatus.IDENTIFIABLE,
    status: EstimateStatus = EstimateStatus.ESTIMATED,
    group_id: str | None = None,
    provisional: bool = False,
) -> DeviceEstimate:
    if point_w is None:
        status = EstimateStatus.UNKNOWN
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point_w,
        bounds=Bounds(),
        status=status,
        identifiability=ident,
        group_id=group_id,
        level=None,
        method=(
        InferenceMethod.NONE
        if point_w is None
        else (
            InferenceMethod.JOINT_DELTA
            if status is EstimateStatus.ESTIMATED
            else InferenceMethod.DIRECT_READING
        )
    ),
        allocation_rule=None,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=readers,
        evidence_refs=[EvidenceRef(kind="event_evidence", id=f"evd-{subject_id}")],
        versions=VT,
        updated_at=T0,
        provisional=provisional,
    )


def _group_estimate(point_w: float | None = 250.0) -> DeviceGroupEstimate:
    return DeviceGroupEstimate(
        group_id="grp-lights",
        member_ids=["light-a", "light-b"],
        point_w=point_w,
        bounds=Bounds(),
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        source_reader_ids=["ct-row"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )


def _result(estimates: list[DeviceEstimate], groups: list[DeviceGroupEstimate] | None = None) -> JointInferenceResult:
    return JointInferenceResult(
        result_id="res-test",
        event_group_id="grp-1",
        event_group_revision=1,
        status=EventGroupStatus.CONFIRMED,
        device_estimates=estimates,
        group_estimates=groups if groups is not None else [],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={},
        versions=VT,
        computed_at=T0,
    )


def _group(deltas: list[ReaderDelta] | None = None) -> EventGroup:
    return EventGroup(
        group_id="grp-1",
        revision=1,
        status=EventGroupStatus.CONFIRMED,
        opened_at=T0,
        sub_events=[],
        reader_deltas=deltas or [ReaderDelta(reader_id="ct-row", delta_w=50.0, completeness="complete")],
        grouping_reasons=[],
        versions=VT,
    )


# --------------------------------------------------------------------------- #
# build_observations
# --------------------------------------------------------------------------- #


def test_one_observation_per_subject_endpoint_with_a_delta() -> None:
    result = _result(
        [
            _estimate("dev-a", point_w=50.0, readers=["ct-row"]),
            _estimate("dev-b", point_w=70.0, readers=["ct-row"]),
        ]
    )
    obs = build_observations(result, _group(), VT, clock=lambda: T0)
    assert len(obs) == 2
    assert {(o.subject_id, o.endpoint_id) for o in obs} == {
        ("dev-a", "ct-row"),
        ("dev-b", "ct-row"),
    }
    assert [o.delta_w for o in obs] == [50.0, 70.0]
    assert all(o.level_w is None for o in obs)
    assert all(o.basis == ModelBasis.DELTA for o in obs)


def test_delta_basis_on_shared_circuit_carries_only_device_delta_not_row_level() -> None:
    """Two subjects on one row: each observation holds its own ΔP, never the row level."""
    result = _result(
        [
            _estimate("light-a", point_w=40.0, readers=["ct-row"]),
            _estimate("light-b", point_w=60.0, readers=["ct-row"]),
        ]
    )
    group = _group([ReaderDelta(reader_id="ct-row", delta_w=100.0, completeness="complete")])
    obs = build_observations(result, group, VT, clock=lambda: T0)
    assert {o.delta_w for o in obs} == {40.0, 60.0}
    assert 100.0 not in {o.delta_w for o in obs}
    assert all(o.level_w is None for o in obs)
    assert all(o.basis == ModelBasis.DELTA for o in obs)


def test_level_only_for_a_confirmed_single_subject_endpoint() -> None:
    anchored = _estimate(
        "tv",
        point_w=3.0,
        readers=["tv-plug"],
        status=EstimateStatus.MEASURED,
    )
    result = _result([anchored])
    obs = build_observations(
        result,
        _group([ReaderDelta(reader_id="tv-plug", delta_w=3.0, completeness="complete")]),
        VT,
        clock=lambda: T0,
        single_subject_endpoints={"tv-plug": "tv"},
    )
    assert len(obs) == 1
    assert obs[0].basis == ModelBasis.ABSOLUTE_ANCHOR
    assert obs[0].level_w == 3.0
    assert obs[0].delta_w == 3.0

    # Without confirmation the level is withheld even for a direct-looking estimate.
    shared = build_observations(
        _result([_estimate("tv", point_w=3.0, readers=["ct-row"])]),
        _group(),
        VT,
        clock=lambda: T0,
        single_subject_endpoints={"ct-row": "other-device"},
    )
    assert shared[0].level_w is None
    assert shared[0].basis == ModelBasis.DELTA


def test_non_identifiable_members_produce_no_per_device_observation() -> None:
    """AT-07 learning side: the 250 W group yields zero per-device observations."""
    result = _result(
        [
            _estimate(
                "light-a",
                point_w=None,
                readers=["ct-row"],
                ident=IdentifiabilityStatus.NON_IDENTIFIABLE,
                group_id="grp-lights",
            ),
            _estimate(
                "light-b",
                point_w=None,
                readers=["ct-row"],
                ident=IdentifiabilityStatus.NON_IDENTIFIABLE,
                group_id="grp-lights",
            ),
        ],
        [_group_estimate(250.0)],
    )
    obs = build_observations(result, _group(), VT, clock=lambda: T0)
    assert obs == []

    group_obs = build_group_observations(result, VT, clock=lambda: T0)
    assert len(group_obs) == 1
    assert group_obs[0].attributes["group_only"] is True
    assert group_obs[0].delta_w == 250.0
    assert group_obs[0].level_w is None


def test_deterministic_ids_and_replay_produces_no_duplicates() -> None:
    result = _result([_estimate("dev-a", point_w=50.0, readers=["ct-row"])])
    first = build_observations(result, _group(), VT, clock=lambda: T0)
    second = build_observations(result, _group(), VT, clock=lambda: T0 + __import__("datetime").timedelta(days=1))
    assert [o.observation_id for o in first] == [o.observation_id for o in second]

    from wattmosaic_engine.storage.database import Database

    db = Database(":memory:")
    try:
        assert [db.repos.learning.put_observation(o) for o in first] == [True]
        assert [db.repos.learning.put_observation(o) for o in second] == [False]
        assert len(db.repos.learning.observations("dev-a")) == 1
    finally:
        db.close()


def test_id_changes_with_group_revision_or_topology_version() -> None:
    result = _result([_estimate("dev-a", point_w=50.0, readers=["ct-row"])])
    a = build_observations(result, _group(), VT, clock=lambda: T0)[0]
    b = build_observations(result, _group(), VT, clock=lambda: T0)[0]
    assert a.observation_id == b.observation_id
    assert a.event_group_ref.id == "grp-1"
    assert a.attributes["topology_version_id"] == VT.topology_version_id


def test_provisional_group_yields_provisional_observations() -> None:
    result = _result([_estimate("dev-a", point_w=50.0, readers=["ct-row"], provisional=True)])
    obs = observation_for_estimate(
        result,
        result.device_estimates[0],
        "ct-row",
        VT,
        clock=lambda: T0,
    )
    assert obs.status == "provisional"


def test_missing_point_w_is_not_replaced_by_zero() -> None:
    result = _result([_estimate("dev-a", point_w=None, readers=["ct-row"])])
    assert build_observations(result, _group(), VT, clock=lambda: T0) == []


def test_three_argument_task_signature_is_supported_and_deterministic() -> None:
    """The task specifies ``build_observations(result, group, versions)``."""
    result = _result([_estimate("dev-a", point_w=50.0, readers=["ct-row"])])
    group = _group()
    first = build_observations(result, group, VT)
    second = build_observations(result, group, VT)
    assert len(first) == 1
    assert first[0].observation_id == second[0].observation_id
    assert first[0].created_at == second[0].created_at == T0
