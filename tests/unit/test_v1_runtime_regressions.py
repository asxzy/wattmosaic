"""Focused regressions for the engine defects V1 found driving HB §17 live.

Each test pins one defect that made the three-loop runtime publish nothing (or
publish something wrong) for the HB §17 house, independent of the walkthrough:

* P5 grouping never put an HA change and the reader steps confirming it in one
  group (design §4 rule 2) and closed the HA group before P3 confirmed the step;
* the event loop inferred the group as it was at its *first* reader step;
* ``_publish_v3_snapshot`` shadowed ``SnapshotMixin._v3_snapshot`` so every
  facade read raised ``TypeError`` after the first publish;
* nothing ever ticked P5's lifecycle (no CONFIRMED/UNRESOLVED in production);
* ``_steady_group`` omitted the required ``versions`` (every steady publish raised);
* the runtime handed the bare S16 repository to I4 as ``IdentifiabilityHistory``;
* HA sub-events never resolved to their linked subjects in I1;
* M2 crashed on MEASURED point bounds (write and ``/matrix`` read);
* ``VersionService.from_hb17`` seeded topology edges outside any version;
* T7's default candidate mapping was empty (a relocation wiped the mapping).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    Bounds,
    EstimateStatus,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    HAEvent,
    MeasurementEvent,
    MeasurementType,
    ReaderDelta,
    ReaderMeasurement,
    StepClass,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from wattmosaic_engine.core.runtime import EvidenceHistory, _steady_group
from wattmosaic_engine.inference.candidates import generate_candidates
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.mapping.projection import _cell_power
from wattmosaic_engine.mapping.service import _decode_power
from wattmosaic_engine.realtime.grouping import GroupingEngine, form_groups
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository
from wattmosaic_engine.versioning.service import VersionService

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VERSIONS = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="m")


def _ha(entity: str, at: datetime, *, ctx: str | None = None) -> HAEvent:
    return HAEvent.make(
        entity_id=entity, observed_at=at, received_at=at, from_state="off", to_state="on",
        context_id=ctx, linked=True,
    )


def _step(reader: str, onset_lo: datetime, delta_w: float = 45.0, width_s: float = 50.0) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{onset_lo.isoformat()}",
        reader_id=reader,
        delta_w=delta_w,
        sigma_w=1.0,
        z_score=8.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=onset_lo,
        onset_hi=onset_lo + timedelta(seconds=width_s),
        pre_level_w=100.0,
        post_level_w=100.0 + delta_w,
        quality_score=0.9,
        detected_at=onset_lo + timedelta(seconds=width_s + 20),
    )


@pytest.fixture()
def repo(tmp_path: Any) -> EvidenceRepository:
    return EvidenceRepository(Database(tmp_path / "groups.db"))


# --------------------------------------------------------------------------- #
# P5 grouping
# --------------------------------------------------------------------------- #


def test_reader_step_joins_the_ha_group_it_overlaps(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_ha_event(_ha("light.a", T0), now=T0)
    engine.on_ha_event(_ha("light.b", T0), now=T0)  # sibling, no shared context id
    engine.on_reader_step(_step("ct_a", T0 - timedelta(seconds=45)), now=T0 + timedelta(seconds=60))
    groups = engine.groups()
    assert len(groups) == 1
    kinds = sorted(str(s.kind) for s in groups[0].sub_events)
    assert kinds == ["ha_state", "ha_state", "reader_step"]
    assert "temporal_overlap" in groups[0].grouping_reasons
    assert {e.entity_id for e in engine.ha_events(groups[0].group_id)} == {"light.a", "light.b"}


def test_ha_change_joins_a_reader_group_delivered_first(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_reader_step(_step("ct_a", T0 - timedelta(seconds=30)), now=T0)
    engine.on_ha_event(_ha("light.a", T0), now=T0)
    assert len(engine.groups()) == 1


def test_distant_ha_change_does_not_join(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_ha_event(_ha("switch.decoy", T0 + timedelta(seconds=600)), now=T0)
    engine.on_reader_step(_step("ct_a", T0), now=T0 + timedelta(seconds=60))
    assert len(engine.groups()) == 2


def test_unresolved_ha_group_is_reopened_by_its_late_confirmed_step(repo: EvidenceRepository) -> None:
    ready: list[EventGroup] = []
    engine = GroupingEngine(versions=VERSIONS, repository=repo, on_ready=ready.append)
    engine.on_ha_event(_ha("light.a", T0), now=T0)
    engine.tick(T0 + timedelta(seconds=61))
    assert engine.groups()[0].status == EventGroupStatus.UNRESOLVED
    # P3 confirms the step only after confirm_s + samples: later than quiet_s.
    engine.on_reader_step(_step("ct_a", T0 - timedelta(seconds=45)), now=T0 + timedelta(seconds=70))
    group = engine.groups()[0]
    assert group.status == EventGroupStatus.MATCHING
    assert group.closed_at is None
    assert ready and ready[-1].group_id == group.group_id


def test_related_reader_far_apart_in_time_is_a_new_group(repo: EvidenceRepository) -> None:
    from tests.support.fakes import FakeTopology

    topology = FakeTopology.from_tree({"Main": {"Sub": {}}}, at=T0)
    engine = GroupingEngine(versions=VERSIONS, repository=repo, topology=topology)
    engine.on_reader_step(_step("Main", T0), now=T0)
    engine.on_reader_step(_step("Sub", T0 + timedelta(minutes=30)), now=T0 + timedelta(minutes=30))
    assert len(engine.groups()) == 2


def test_group_growing_after_ready_is_redelivered(repo: EvidenceRepository) -> None:
    ready: list[EventGroup] = []
    engine = GroupingEngine(versions=VERSIONS, repository=repo, on_ready=ready.append)
    engine.on_reader_step(_step("ct_main", T0), now=T0)
    engine.on_reader_step(_step("ct_sub", T0), now=T0)
    engine.on_reader_step(_step("ct_a", T0 + timedelta(seconds=30)), now=T0 + timedelta(seconds=30))
    assert len(ready) == 3
    assert [len(g.reader_deltas) for g in ready] == [1, 2, 3]
    assert len({g.group_id for g in ready}) == 1


def test_form_groups_unions_ha_and_reader_on_overlap() -> None:
    groups = form_groups(
        ha_events=[_ha("light.a", T0)],
        reader_steps=[_step("ct_a", T0 - timedelta(seconds=10))],
        versions=VERSIONS,
    )
    assert len(groups) == 1


# --------------------------------------------------------------------------- #
# EngineCore runtime seams
# --------------------------------------------------------------------------- #


def _core(tmp_path: Any) -> tuple[EngineCore, Database]:
    db = Database(tmp_path / "rt.db")
    core = EngineCore(EngineConfig(database_path=str(tmp_path / "rt.db"), ha_token="x"), db, clock=lambda: T0)
    core._wire_v3_services()
    return core, db


def _reading(reader: str, value: float, at: datetime, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader, observed_at=at, received_at=at, active_power_w=value, phase="l1",
        leg=None, voltage_reference_v=None, measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=1000, calibration_version="t@1", measurement_basis="instantaneous",
        quality_flags=[], sequence=seq,
    )


def test_snapshot_reads_survive_a_publish(tmp_path: Any) -> None:
    core, db = _core(tmp_path)
    try:
        for tick in range(1, 70):
            asyncio.run(core.on_reading(_reading("ct_a", 250.0, T0 + timedelta(seconds=15 * tick), tick)))
        for tick in range(70, 90):
            asyncio.run(core.on_reading(_reading("ct_a", 300.0, T0 + timedelta(seconds=15 * tick), tick)))
        group = core.groups_ready.get_nowait()
        assert group is not None
        bundle = core._build_input_bundle(group)
        core._publish_group_result(bundle["group"], core._run_infer_group(bundle), bundle)
        # Before the fix this attribute shadowed the method: TypeError here.
        assert callable(core._v3_snapshot)
        snapshot = core.snapshot_v3()
        assert snapshot is core.snapshot_assembler.current
        assert core.devices_v3() == list(snapshot.device_estimates)
    finally:
        db.close()


def test_tick_groups_confirms_and_redelivers(tmp_path: Any) -> None:
    core, db = _core(tmp_path)
    try:
        core.grouping_engine.on_reader_step(_step("ct_a", T0), now=T0)
        assert core.groups_ready.get_nowait() is not None
        changed = core.tick_groups(T0 + timedelta(minutes=5))
        assert [g.status for g in changed] == [EventGroupStatus.CONFIRMED]
        again = core.groups_ready.get_nowait()
        assert again is not None and again.status == EventGroupStatus.CONFIRMED
    finally:
        db.close()


def test_bundle_infers_the_freshest_group_value(tmp_path: Any) -> None:
    core, db = _core(tmp_path)
    try:
        engine = core.grouping_engine
        engine.on_reader_step(_step("ct_main", T0), now=T0)
        first = core.groups_ready.get_nowait()
        engine.on_reader_step(_step("ct_sub", T0), now=T0)
        bundle = core._build_input_bundle(first)
        assert {d.reader_id for d in bundle["group"].reader_deltas} == {"ct_main", "ct_sub"}
    finally:
        db.close()


def test_steady_group_carries_versions() -> None:
    class _Result:
        result_id = "res-1"
        status = EventGroupStatus.CONFIRMED
        versions = VERSIONS

    group = _steady_group(_Result(), T0)  # type: ignore[arg-type]
    assert group.versions == VERSIONS


def test_evidence_history_satisfies_the_identifiability_protocol(repo: EvidenceRepository) -> None:
    from wattmosaic_engine.runtime.interfaces import IdentifiabilityHistory

    history = EvidenceHistory(repo)
    assert isinstance(history, IdentifiabilityHistory)
    assert history.events_touching(["light.a", "light.b"], T0) == []


def test_ha_sub_events_resolve_to_linked_subjects(tmp_path: Any) -> None:
    core, db = _core(tmp_path)
    try:
        db.repos.entities.add_entity_link(
            {"entity_id": "light.a", "target_kind": "device", "target_id": "lamp",
             "role": "state", "valid_from": T0 - timedelta(days=1), "declared_by": "user"}
        )
        engine = core.grouping_engine
        linked, unlinked = _ha("light.a", T0), _ha("switch.unlinked", T0)
        engine.on_ha_event(linked, now=T0)
        engine.on_ha_event(unlinked, now=T0)
        group = engine.groups()[0]
        subjects = core._ha_subjects(group)
        assert subjects == {linked.event_id: (("device", "lamp"),), unlinked.event_id: ()}
    finally:
        db.close()


def test_candidates_use_resolved_subjects_never_opaque_event_ids() -> None:
    from wattmosaic_engine.electrical.operator import build_observation_operator

    from tests.support.fakes import FakeMapping, FakeModelSet, FakeTopology

    operator = build_observation_operator(
        FakeTopology.from_tree({"ct_a": {}}, at=T0),
        FakeMapping.from_pairs({("device", "lamp"): "ct_a"}),
        None, ("ct_a",), (("device", "lamp"),), T0,
    )
    group = EventGroup(
        group_id="g", revision=1, status=EventGroupStatus.MATCHING, opened_at=T0,
        sub_events=[SubEvent(sub_event_id="s1", kind=SubEventKind.HA_STATE,
                             ref=EvidenceRef(kind="ha_event", id="hae-123"),
                             observed_at=T0, timestamp_uncertainty_s=0.0)],
        reader_deltas=[ReaderDelta(reader_id="ct_a", delta_w=45.0, sigma_w=1.0, completeness="complete")],
        versions=VERSIONS,
    )

    class _Mapping:
        def most_specific_reader(self, subject):
            return "ct_a" if subject == ("device", "lamp") else None

        def subjects_on(self, reader):
            return (("device", "lamp"),) if reader == "ct_a" else ()

    models = FakeModelSet.fixed({})
    resolved = generate_candidates(group, operator, models, _Mapping(), ha_subjects={"hae-123": (("device", "lamp"),)})
    ids = {c.subject_id for c in resolved.candidates}
    assert "lamp" in ids and "hae-123" not in ids
    unresolved = generate_candidates(group, operator, models, _Mapping(), ha_subjects={})
    assert "hae-123" not in {c.subject_id for c in unresolved.candidates}


# --------------------------------------------------------------------------- #
# M2 point bounds; D1 seeding; T7 default candidate
# --------------------------------------------------------------------------- #


def test_cell_power_carries_a_point_interval_on_write_and_read() -> None:
    point = Bounds._point(3.0, "posterior")
    power = _cell_power(measurement_status=EstimateStatus.MEASURED, estimated_power_w=3.0, bounds=point)
    assert power.bounds.lower_w == power.bounds.upper_w == 3.0
    stored = power.model_dump(mode="json")
    decoded = _decode_power(stored)
    assert decoded.bounds.lower_w == decoded.bounds.upper_w == 3.0
    # A payload still cannot mint a point through the public constructor.
    with pytest.raises(ValueError):
        Bounds(lower_w=3.0, upper_w=3.0, basis="posterior")


def test_from_hb17_edges_belong_to_the_active_topology_version() -> None:
    from tools.simulator import build_hb17

    svc = VersionService.from_hb17(build_hb17(17, minutes=2).observation_view())
    view = svc.topology_view(svc.current().topology_version_id)
    assert {(e.source_endpoint_id, e.target_endpoint_id) for e in view.edges()} == {
        ("ct_main", "ct_sub"), ("ct_sub", "ct_a"), ("ct_sub", "ct_b"), ("ct_main", "ct_c"),
    }


def test_default_candidate_mapping_keeps_the_current_edges() -> None:
    from wattmosaic_engine.versioning.workflow import ConfigurationWorkflow

    from tools.simulator import build_hb17

    svc = VersionService.from_hb17(build_hb17(17, minutes=2).observation_view())
    view = svc.mapping_view(svc.current().mapping_version_id)
    assert {e.subject_id for e in view.edges()} == {"tv", "lights"}
    # HB §17: the refrigerator has no HA entity, so Circuit B declares no subject.
    assert view.subjects_on("ct_b") == ()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    assert {e.subject_id for e in workflow._current_mapping_edges()} == {"tv", "lights"}
