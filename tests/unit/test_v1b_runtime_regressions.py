"""Focused regressions for the engine defects V1b closed.

* group revisions are persisted through their lifecycle (CONFIRMED, ``closed_at``)
  and a CONFIRMED group records the subjects it touched, so I4's history sees it;
* a confirmed relocation proposes a new mapping candidate from evidence;
* the steady pass measures single-subject endpoints (and exclusive established
  groups) from their reader, so a device that never steps is published;
* I5 pins a MEASURED estimate's bounds to its direct reading (round-off and a
  ``[0, 0]`` absolute posterior alike);
* HA events cited by an estimate resolve through provenance;
* a snapshot whose content returns to an earlier state still swaps;
* P4 opens a window when the pre-window had to be extended backwards.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    ConfigurationChangeKind,
    EventGroupStatus,
    HAEvent,
    MeasurementEvent,
    MeasurementType,
    ReaderMeasurement,
    StepClass,
    VersionTriple,
)
from wattmosaic_engine.core.runtime import EvidenceHistory
from wattmosaic_engine.electrical.operator import build_observation_operator, clear_operator_cache
from wattmosaic_engine.electrical.topology_change import InferredChange
from wattmosaic_engine.inference.orchestrator import infer_steady
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.realtime.grouping import GroupingEngine
from wattmosaic_engine.residual.reader_residual import UnknownPriorModel
from wattmosaic_engine.runtime.fakes import FakeMapping, FakeModelSet, FakeTopology
from wattmosaic_engine.runtime.interfaces import HeldReading
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository
from wattmosaic_engine.versioning.service import VersionService

from tools.simulator import build_hb17

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VERSIONS = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="m")


def _step(reader: str, onset_lo: datetime, delta_w: float = 45.0) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{onset_lo.isoformat()}", reader_id=reader, delta_w=delta_w,
        sigma_w=1.0, z_score=8.0, step_class=StepClass.SUSTAINED, onset_lo=onset_lo,
        onset_hi=onset_lo + timedelta(seconds=20), pre_level_w=100.0,
        post_level_w=100.0 + delta_w, quality_score=0.9, detected_at=onset_lo + timedelta(seconds=40),
    )


def _ha(entity: str, at: datetime) -> HAEvent:
    return HAEvent.make(entity_id=entity, observed_at=at, received_at=at, from_state="off",
                        to_state="on", linked=True)


def _reading(reader: str, value: float, at: datetime, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader, observed_at=at, received_at=at, active_power_w=value, phase="l1",
        leg=None, voltage_reference_v=None, measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=1000, calibration_version="t@1", measurement_basis="instantaneous",
        quality_flags=[], sequence=seq,
    )


def _hb17_core(devices: dict[str, str] | None = None) -> tuple[EngineCore, list[datetime]]:
    observation = build_hb17(17, minutes=2).observation_view()
    now = [T0]
    db = Database(":memory:")
    core = EngineCore(EngineConfig(database_path=":memory:", ha_token="x"), db, clock=lambda: now[0])
    core._wire_v3_services()
    core.rebind_version_service(VersionService.from_hb17(observation, devices=devices, db=db))
    triple = core.version_service.current()
    core.grouping_engine = GroupingEngine(
        versions=triple, repository=db.repos.evidence,
        topology=core.version_service.topology_view(triple.topology_version_id),
        ring=core.candidate_ring, persist_ha_event=core._persist_ha_event,
        on_ready=core.on_group_ready, on_revised=core.on_group_ready,
    )
    return core, now


def _link(core: EngineCore, entity: str, target: str) -> None:
    core.db.repos.entities.add_entity_link(
        {"entity_id": entity, "target_kind": "device", "target_id": target, "role": "state",
         "valid_from": T0 - timedelta(days=1), "declared_by": "user"}
    )


# --------------------------------------------------------------------------- #
# (a) lifecycle persistence and subject history
# --------------------------------------------------------------------------- #


def test_confirmed_transition_and_closed_at_are_persisted(tmp_path: Any) -> None:
    repo = EvidenceRepository(Database(tmp_path / "g.db"))
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_reader_step(_step("ct_a", T0), now=T0)
    (group,) = engine.groups()
    assert repo.latest_group(group.group_id).status == EventGroupStatus.MATCHING
    later = T0 + timedelta(minutes=5)
    engine.tick(later)
    stored = repo.latest_group(group.group_id)
    assert stored.status == EventGroupStatus.CONFIRMED
    assert stored.closed_at == later
    assert stored.revision == 1  # a lifecycle transition is not a new revision


def test_confirmed_group_subjects_become_identifiability_history(tmp_path: Any) -> None:
    repo = EvidenceRepository(Database(tmp_path / "h.db"))
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_reader_step(_step("ct_a", T0), now=T0)
    (group,) = engine.groups()
    engine.tick(T0 + timedelta(minutes=5))
    history = EvidenceHistory(repo)
    assert history.events_touching(["lamp_1", "lamp_2"], T0 - timedelta(hours=1)) == []
    repo.put_group_subjects(
        group.group_id, 1,
        [("device", "lamp_1", 45.0, 1.0, "ct_a"), ("device", "lamp_2", 45.0, 1.0, "ct_a")],
    )
    (row,) = history.events_touching(["lamp_1", "lamp_2"], T0 - timedelta(hours=1))
    assert row.group_id == group.group_id
    assert set(row.subject_ids) == {"lamp_1", "lamp_2"}
    # Idempotent.
    assert repo.put_group_subjects(group.group_id, 1, [("device", "lamp_1", 45.0, 1.0, "ct_a")]) == 0


def test_runtime_records_subjects_of_a_confirmed_published_group() -> None:
    core, now = _hb17_core({"lamp": "ct_a"})
    _link(core, "light.lamp", "lamp")
    core.grouping_engine.on_ha_event(_ha("light.lamp", T0), now=T0)
    core.grouping_engine.on_reader_step(_step("ct_a", T0 - timedelta(seconds=10)), now=T0)
    now[0] = T0 + timedelta(minutes=5)
    core.tick_groups(now[0])
    while (group := core.groups_ready.get_nowait()) is not None:
        bundle = core._build_input_bundle(group)
        core._publish_group_result(bundle["group"], core._run_infer_group(bundle), bundle)
    (row,) = EvidenceHistory(core.db.repos.evidence).events_touching(["lamp"], T0 - timedelta(hours=1))
    assert row.subject_ids == ("lamp",)
    # The HA event the result cites is persisted and resolvable.
    (event,) = core.grouping_engine.ha_events(row.group_id)
    record = core.ha_event_record(event.event_id)
    assert record is not None and record.entity_id == "light.lamp"


# --------------------------------------------------------------------------- #
# (b) relocation candidate
# --------------------------------------------------------------------------- #


def test_relocation_proposes_the_plugs_subject_on_the_new_reader() -> None:
    core, _now = _hb17_core()  # default: tv and lights on ct_a
    _link(core, "switch.tv_plug", "tv")
    core.grouping_engine.on_ha_event(_ha("switch.tv_plug", T0), now=T0)
    core.grouping_engine.on_reader_step(_step("ct_plug", T0 - timedelta(seconds=5), 117.0), now=T0)
    change = InferredChange(
        ConfigurationChangeKind.SMART_PLUG_RELOCATION, "plug:ct_plug", from_id="ct_a", to_id="ct_c"
    )
    edges = core._relocation_candidate_mapping(change)
    assert edges is not None
    pairs = {(e.subject_id, e.endpoint_id): e for e in edges}
    assert ("tv", "ct_a") not in pairs
    assert ("lights", "ct_a") in pairs  # an unrelated subject is not moved
    new = pairs[("tv", "ct_c")]
    assert str(getattr(new.lifecycle, "value", new.lifecycle)) == "provisional"
    assert new.declared_by == "inference"
    # Without evidence naming the plug's subject nothing is proposed.
    bare, _ = _hb17_core()
    assert bare._relocation_candidate_mapping(change) is None


# --------------------------------------------------------------------------- #
# (c) steady pass, I5 bounds, snapshot ids, P4 window
# --------------------------------------------------------------------------- #


def test_steady_pass_publishes_a_single_subject_endpoint_that_never_steps() -> None:
    core, now = _hb17_core({"tv": "ct_plug", "lamp": "ct_a", "heater": "ct_c"})
    core.db.repos.entities.upsert_endpoint(
        {"endpoint_id": "ct_plug", "kind": "smart_plug", "entity_id": None, "scope": "single_subject",
         "scope_status": "confirmed", "scope_subject_id": "tv", "enabled": True, "created_at": T0}
    )
    for tick in range(1, 6):
        now[0] = T0 + timedelta(seconds=15 * tick)
        asyncio.run(core.on_reading(_reading("ct_plug", 3.2, now[0], tick)))
        asyncio.run(core.on_reading(_reading("ct_a", 300.0, now[0], 100 + tick)))
        asyncio.run(core.on_reading(_reading("ct_c", 400.0, now[0], 200 + tick)))
    core._steady_pass()
    estimates = {e.subject_id: e for e in core.snapshot_v3().device_estimates}
    # Circuit C maps one subject but is a circuit CT, not a declared
    # single-subject endpoint: its 400 W is never published as the heater's.
    assert set(estimates) == {"tv"}
    tv = estimates["tv"]
    assert str(tv.status) == "measured" and tv.point_w == pytest.approx(3.2)
    assert tv.bounds.lower_w == tv.bounds.upper_w == tv.point_w
    calls = core.infer_steady_calls
    core._steady_pass()  # unchanged level: no re-publish
    assert core.infer_steady_calls == calls


def test_i5_pins_measured_bounds_to_the_direct_reading() -> None:
    clear_operator_cache()
    operator = build_observation_operator(
        FakeTopology.from_tree({"plug": {}}, at=T0), FakeMapping.from_pairs({("device", "tv"): "plug"}),
        None, ("plug",), (("device", "tv"),), T0,
    )

    class _Empty:
        def groups(self) -> dict:
            return {}

        def identities(self) -> dict:
            return {}

    for value in (3.228464, 149.945758, 0.1 + 0.2):
        result = infer_steady(
            {"plug": HeldReading(value=value, held_since=T0, last_confirmed_at=T0, state="held")},
            operator, FakeModelSet.fixed({}), UnknownPriorModel(), _Empty(), VERSIONS, lambda: T0,
            declared_single_subject={("plug", "tv")},
        )
        (tv,) = result.device_estimates
        assert str(tv.status) == "measured"
        assert tv.point_w == value
        assert tv.bounds.lower_w == tv.bounds.upper_w == value


def test_snapshot_returning_to_an_earlier_state_still_swaps(tmp_path: Any) -> None:
    from wattmosaic_engine.contracts import (
        Bounds,
        ConfidenceVectorV3,
        DeviceEstimate,
        EstimateStatus,
        JointInferenceResult,
    )
    from wattmosaic_engine.core.snapshot import SnapshotAssembler

    db = Database(tmp_path / "s.db")
    assembler = SnapshotAssembler(db.repos.inference, clock=lambda: T0)

    def result(point: float) -> JointInferenceResult:
        estimate = DeviceEstimate(
            subject_kind="device", subject_id="tv", point_w=point, bounds=Bounds(basis="posterior"),
            status=EstimateStatus.ESTIMATED, identifiability="identifiable", method="joint_absolute",
            source_reader_ids=["plug"], evidence_refs=[], versions=VERSIONS, updated_at=T0,
            confidence=ConfidenceVectorV3(),
        )
        return JointInferenceResult(device_estimates=[estimate], group_estimates=[], unknown_estimates=[],
                                    candidates=[], matches={}, residual_w_by_reader={},
                                    versions=VERSIONS, computed_at=T0,
                                    status=EventGroupStatus.CONFIRMED)

    a = assembler.on_result(result(3.0))
    b = assembler.on_result(result(120.0))
    a2 = assembler.on_result(result(3.0))
    assert assembler.current is a2
    assert a2.device_estimates[0].point_w == 3.0
    assert len({a.snapshot_id, b.snapshot_id, a2.snapshot_id}) == 3
    assert assembler.on_result(result(3.0)).snapshot_id == a2.snapshot_id  # unchanged: same id


def test_window_opens_when_the_pre_window_is_extended_backwards() -> None:
    core, now = _hb17_core()
    # Sparse samples: 120 s apart, so the 30 s pre-window holds none.
    for tick in range(1, 4):
        now[0] = T0 + timedelta(seconds=120 * tick)
        asyncio.run(core.on_reading(_reading("ct_a", 300.0, now[0], tick)))
    window = core.window_collector.on_detected(_step("ct_a", T0 + timedelta(seconds=400)), at=now[0])
    assert window is not None
