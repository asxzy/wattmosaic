"""Z4: live reader residuals reach the snapshot; model activation advances the set.

* After a steady pass the published snapshot carries the T3 per-reader residual
  points, and ``reconciliation.residual_w`` is the topology root's residual
  (HB §12.1).  A missing root residual publishes ``None``, never 0 W (HB §18).
* An event-path publish refreshes the same points against the event estimates.
* Activating a power model advances ``VersionTriple.model_set_id`` (HB §13) for
  the next snapshot and for event groups formed afterwards, and a restart on the
  same database reports the same, content-derived id.

Reader and subject names are deliberately unlike the HB §17 house.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    ConfigurationEvent,
    DataQualityReport,
    DeviceEstimate,
    EdgeEvidence,
    EdgeLifecycleV3,
    EstimateStatus,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    FixedSpec,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    LevelStat,
    MappingEdge,
    MeasurementBasis,
    MeasurementEvent,
    MeasurementType,
    ModelBasis,
    ModelClass,
    Observability,
    PowerLevelV3,
    PowerModelVersion,
    ReaderMeasurement,
    ReaderResidualPoint,
    ReconciliationV3,
    RelationType,
    StepClass,
    TopologyEdgeV3,
    UnknownEstimate,
    VersionTriple,
)
from wattmosaic_engine.contracts import model_set_id as make_model_set_id
from wattmosaic_engine.core.snapshot import AssembleInputs, SnapshotAssembler
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 3, 1, tzinfo=UTC)
FEED = "feed_l1"
FEED_2 = "feed_l2"
KITCHEN = "kitchen_ckt"
DISHWASHER = "dishwasher"


def _topo_edge(edge_id: str, parent: str, child: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id, relation=RelationType.CONTAINS, source_endpoint_id=parent,
        target_endpoint_id=child, lifecycle=EdgeLifecycleV3.CONFIRMED, declared_by="user",
        beta=1.0, beta_basis="unity", evidence=EdgeEvidence(support=8, independent_causes=2),
        valid_from=T0, pinned=True,
    )


def _map_edge(edge_id: str, subject: str, endpoint: str) -> MappingEdge:
    return MappingEdge(
        mapping_edge_id=edge_id, subject_kind="device", subject_id=subject, endpoint_id=endpoint,
        observability=Observability.DIRECT, lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="migration", evidence=EdgeEvidence(support=8, independent_causes=2),
        valid_from=T0,
    )


def _reading(reader: str, value: float, at: datetime, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader, observed_at=at, received_at=at, active_power_w=value, phase="l1",
        leg=None, voltage_reference_v=None, measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=1000, calibration_version="t@1", measurement_basis="instantaneous",
        quality_flags=[], sequence=seq,
    )


def _core(path: Path, now: list[datetime]) -> EngineCore:
    db = Database(path)
    core = EngineCore(EngineConfig(database_path=str(path), ha_token="x"), db, clock=lambda: now[0])
    core._wire_v3_services()
    return core


def _declare(core: EngineCore, roots: tuple[str, ...]) -> None:
    """Promote a topology (each root contains the kitchen circuit) and a mapping."""
    versions = core.version_service
    event = versions.record_configuration_event(
        ConfigurationEvent(
            event_id="evt-z4", kind="smart_plug_relocation", detection="declared",
            status="confirmed", affected_endpoint_ids=[], affected_edge_ids=[],
            affected_mapping_edge_ids=[], created_at=T0,
        )
    )
    edges = [_topo_edge(f"te-{i}", root, KITCHEN) for i, root in enumerate(roots[:1])]
    edges += [_topo_edge(f"te-x{i}", root, f"{root}_spare") for i, root in enumerate(roots[1:])]
    versions.promote_topology(versions.propose_topology(edges, event), "declared")
    versions.promote_mapping(
        versions.propose_mapping([_map_edge("me-1", DISHWASHER, KITCHEN)], event), "declared"
    )


def _feed(core: EngineCore, now: list[datetime], levels: dict[str, list[float]]) -> None:
    """Report every reader each 15 s, continuing from the current instant."""
    start = now[0]
    ticks = len(next(iter(levels.values())))
    for tick in range(ticks):
        now[0] = start + timedelta(seconds=15 * (tick + 1))
        for offset, (reader, values) in enumerate(sorted(levels.items())):
            seq = 100_000 * offset + int((now[0] - T0).total_seconds())
            asyncio.run(core.on_reading(_reading(reader, values[tick], now[0], seq)))


def _stepped(before: float, after: float) -> list[float]:
    return [before] * 19 + [after] * 10


def _model_version(subject: str, number: int) -> PowerModelVersion:
    return PowerModelVersion(
        version_id=f"mdl-{subject}-{number}", subject_kind="device", subject_id=subject,
        number=number, model_class=ModelClass.FIXED, basis=ModelBasis.DELTA,
        spec=FixedSpec(levels={PowerLevelV3.ACTIVE: LevelStat(median_w=1800.0, n=5)}),
        evidence_refs=[EvidenceRef(kind="event_group", id=f"grp-{number}")],
        trainer_version="test", created_at=T0,
    )


# --------------------------------------------------------------------------- #
# Residuals.
# --------------------------------------------------------------------------- #


def test_steady_pass_publishes_the_root_residual_and_per_reader_points(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    _declare(core, (FEED,))
    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    core._steady_pass()

    snapshot = core.snapshot_v3()
    assert snapshot is not None
    points = {p.reader_id: p for p in snapshot.residual_estimates}
    assert set(points) == {FEED, KITCHEN}
    # R_root = Y_root - beta*Y_child (no subject is mapped directly on the feed).
    assert points[FEED].residual_w == pytest.approx(2400.0 - 2150.0)
    # Nothing explains the kitchen circuit yet: its whole level is residual.
    assert points[KITCHEN].residual_w == pytest.approx(2150.0)
    triple = core.version_service.current()
    assert {p.topology_version_id for p in points.values()} == {triple.topology_version_id}
    assert snapshot.reconciliation.residual_w == pytest.approx(points[FEED].residual_w)
    assert snapshot.reconciliation.main_w == pytest.approx(2400.0)


def test_event_publish_refreshes_residuals_against_the_event_estimates(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    _declare(core, (FEED,))
    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    core._steady_pass()

    triple = core.version_service.current()
    estimate = DeviceEstimate(
        subject_kind="device", subject_id=DISHWASHER, point_w=1750.0,
        bounds=Bounds(lower_w=1700.0, upper_w=1800.0), status=EstimateStatus.ESTIMATED,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE, method=InferenceMethod.JOINT_DELTA,
        confidence=ConfidenceVectorV3(), source_reader_ids=[KITCHEN], evidence_refs=[],
        versions=triple, updated_at=now[0],
    )
    result = JointInferenceResult(
        event_group_id="grp-z4", event_group_revision=1, status=EventGroupStatus.CONFIRMED,
        device_estimates=[estimate], group_estimates=[], unknown_estimates=[], candidates=[],
        matches={}, residual_w_by_reader={KITCHEN: 0.0}, versions=triple, computed_at=now[0],
    )
    group = EventGroup(
        group_id="grp-z4", revision=1, status=EventGroupStatus.CONFIRMED, opened_at=now[0],
        versions=triple,
    )
    core._publish_group_result(group, result, {"operator": None})

    snapshot = core.snapshot_v3()
    points = {p.reader_id: p.residual_w for p in snapshot.residual_estimates}
    assert points[KITCHEN] == pytest.approx(2150.0 - 1750.0)
    assert points[FEED] == pytest.approx(2400.0 - 2150.0)
    assert snapshot.reconciliation.residual_w == pytest.approx(250.0)
    assert snapshot.reconciliation.identified_w == pytest.approx(1750.0)


def test_a_missing_root_residual_publishes_none(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    _declare(core, (FEED, FEED_2))  # two roots; the second never reports
    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    core._steady_pass()

    snapshot = core.snapshot_v3()
    points = {p.reader_id: p.residual_w for p in snapshot.residual_estimates}
    assert points[FEED] == pytest.approx(250.0)
    assert points[FEED_2] is None  # no reading: unknown, never 0 W
    assert snapshot.reconciliation.residual_w is None
    assert snapshot.reconciliation.main_w is None


def _assemble(unknowns: list[UnknownEstimate]) -> ReconciliationV3:
    versions = VersionTriple(topology_version_id="topo-9", mapping_version_id="map-9", model_set_id="mset-z")
    result = JointInferenceResult(
        device_estimates=[], group_estimates=[], unknown_estimates=unknowns, candidates=[],
        matches={}, residual_w_by_reader={}, versions=versions, computed_at=T0,
        status=EventGroupStatus.CONFIRMED,
    )
    assembler = SnapshotAssembler(type("S", (), {"put_snapshot": lambda self, s: True})(), lambda: T0)
    point = ReaderResidualPoint(
        reader_id=FEED, at=T0, residual_w=250.0, topology_version_id="topo-9",
        basis=MeasurementBasis.UNKNOWN,
    )
    snapshot = assembler.on_result(
        result,
        inputs=AssembleInputs(
            result=result, held_readings={FEED: {"value": 2400.0}},
            quality=DataQualityReport(timestamp=T0), residual_points=[point],
            root_reader_ids=(FEED,),
        ),
    )
    return snapshot.reconciliation


def test_balance_error_is_computable_once_every_term_exists() -> None:
    # Known, empty device/group sets are a real 0 W (HB §18: absent is None,
    # an empty sum is not absent).
    recon = _assemble([UnknownEstimate(scope_reader_id=FEED, point_w=150.0)])
    assert recon.identified_w == 0.0
    assert recon.group_w == 0.0
    assert recon.unknown_w == pytest.approx(150.0)
    assert recon.residual_w == pytest.approx(250.0)
    assert recon.balance_error_w == pytest.approx(2400.0 - 0.0 - 0.0 - 150.0 - 250.0)
    # Z6/HB §18: a known, empty set of unknown estimates is a real 0 W (just
    # like identified_w/group_w); absence of any estimate is not "unknown".
    # Unknown is None only when an estimate itself is unconstrained.
    assert _assemble([]).unknown_w == 0.0
    assert _assemble([]).balance_error_w == pytest.approx(2400.0 - 0.0 - 0.0 - 0.0 - 250.0)


# --------------------------------------------------------------------------- #
# Model set.
# --------------------------------------------------------------------------- #


def test_activation_advances_the_model_set_and_survives_a_restart(tmp_path: Path) -> None:
    now = [T0]
    path = tmp_path / "z4.db"
    core = _core(path, now)
    _declare(core, (FEED,))
    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    core._steady_pass()
    before = core.snapshot_v3().versions.model_set_id
    assert before == make_model_set_id([])

    registry = core.model_registry_v3
    version = registry.propose(_model_version(DISHWASHER, 1))
    registry.activate(version.version_id)
    expected = make_model_set_id([version.version_id])
    assert core.version_service.current().model_set_id == expected
    # Groups formed from now on (event records, matrix writes) carry it too.
    assert core.grouping_engine.versions.model_set_id == expected

    # The next snapshot reports the new set.
    _feed(core, now, {FEED: [3000.0] * 8, KITCHEN: [2700.0] * 8})
    core._steady_pass()
    snapshot = core.snapshot_v3()
    assert snapshot.versions.model_set_id == expected != before
    assert snapshot.reconciliation.main_w == pytest.approx(3000.0)
    assert snapshot.reconciliation.residual_w == pytest.approx(300.0)
    stored = core.db.repos.inference.latest_snapshot()
    assert stored is not None and stored["model_set_id"] == expected

    # A restart on the same DB derives the same id from the persisted log.
    restarted = _core(path, now)
    assert restarted.version_service.current().model_set_id == expected
    assert restarted.version_service.model_version_id(DISHWASHER) == expected


def test_rollback_restores_the_earlier_model_set(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    registry = core.model_registry_v3
    first = registry.propose(_model_version(DISHWASHER, 1))
    registry.activate(first.version_id)
    one = core.version_service.current().model_set_id
    now[0] = T0 + timedelta(minutes=1)
    second = registry.propose(_model_version(DISHWASHER, 2))
    registry.activate(second.version_id)
    two = core.version_service.current().model_set_id
    assert two == make_model_set_id([second.version_id]) != one
    now[0] = T0 + timedelta(minutes=2)
    assert registry.rollback(DISHWASHER) == first.version_id
    assert core.version_service.current().model_set_id == one


# --------------------------------------------------------------------------- #
# Z4b: one D1 change-listener refreshes the grouping triple.
# --------------------------------------------------------------------------- #


def _step(reader: str, at: datetime, delta_w: float = 1750.0) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{at.isoformat()}", reader_id=reader, delta_w=delta_w,
        sigma_w=1.0, z_score=8.0, step_class=StepClass.SUSTAINED, onset_lo=at,
        onset_hi=at + timedelta(seconds=20), pre_level_w=400.0, post_level_w=400.0 + delta_w,
        quality_score=0.9, detected_at=at + timedelta(seconds=40),
    )


def test_topology_and_mapping_promotions_restamp_new_groups_and_records(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    startup = core.grouping_engine.versions
    _declare(core, (FEED,))  # D1 configuration event -> topology + mapping promotion
    triple = core.version_service.current()
    assert triple.topology_version_id != startup.topology_version_id
    assert triple.mapping_version_id != startup.mapping_version_id
    assert core.grouping_engine.versions == triple

    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    group = core.grouping_engine.on_reader_step(_step(KITCHEN, now[0]), now=now[0])
    assert group.versions == triple
    bundle = core._build_input_bundle(group)
    result = core._run_infer_group(bundle)
    core._publish_group_result(bundle["group"], result, bundle)
    assert result.versions == triple
    # Read the persisted record's triple from its stored result (decoding the
    # whole record would trip an unrelated point-bounds validator on read).
    rows = core.db._conn.execute(
        "SELECT result_json FROM event_inference_records WHERE event_group_id=?",
        (group.group_id,),
    ).fetchall()
    assert [json.loads(row[0])["versions"] for row in rows] == [triple.model_dump(mode="json")]


def test_user_pin_and_rollback_restamp_the_grouping_triple(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    _declare(core, (FEED,))
    declared = core.version_service.current().topology_version_id

    now[0] = T0 + timedelta(minutes=5)
    pinned = core.pin_topology_edge("te-0")["version_id"]
    assert core.version_service.current().topology_version_id == pinned != declared
    assert core.grouping_engine.versions.topology_version_id == pinned

    now[0] = T0 + timedelta(minutes=10)
    rolled = core.version_service.rollback("topology", declared)
    assert core.grouping_engine.versions.topology_version_id == rolled
    assert core.grouping_engine.versions == core.version_service.current()


# --------------------------------------------------------------------------- #
# Z4b: design §L7 — a model promotion triggers reconciliation.
# --------------------------------------------------------------------------- #


def test_model_promotion_enqueues_l7_reconciliation(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    core.drain_background()
    runs: list[tuple[str, str]] = []
    original = core.reconciler.run

    def spy(window, triple, reason, **kwargs):  # noqa: ANN001, ANN202
        runs.append((reason, triple.model_set_id))
        return original(window, triple, reason, **kwargs)

    core.reconciler.run = spy
    registry = core.model_registry_v3
    version = registry.propose(_model_version(DISHWASHER, 1))
    registry.activate(version.version_id)
    core.drain_background()
    assert ("model_promotion", make_model_set_id([version.version_id])) in runs
    assert core.last_model_set_change["model_set_id"] == make_model_set_id([version.version_id])


# --------------------------------------------------------------------------- #
# Z4b: live residual points are persisted to the S19 series.
# --------------------------------------------------------------------------- #


def test_live_residual_points_are_served_from_the_series(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    _declare(core, (FEED,))
    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    core._steady_pass()

    (feed,) = core.reader_residual_points(FEED)
    assert feed.residual_w == pytest.approx(250.0)
    assert feed.topology_version_id == core.version_service.current().topology_version_id
    (kitchen,) = core.reader_residual_points(KITCHEN)
    assert kitchen.residual_w == pytest.approx(2150.0)

    # An unchanged re-publish adds no row (change-point series).
    reading = core.reader_state.held(FEED, now[0])
    views = core._steady_views()
    core._steady_publish(FEED, reading, (), None, views, now[0])
    assert len(core.reader_residual_points(FEED)) == 1

    # A changed level is a new point.
    _feed(core, now, {FEED: [3000.0] * 8, KITCHEN: [2700.0] * 8})
    core._steady_pass()
    series = core.reader_residual_points(FEED)
    assert [p.residual_w for p in series] == pytest.approx([250.0, 300.0])


def test_two_publishes_at_one_instant_upsert_last_wins(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    repo = db.repos.residual

    def point(value: float) -> ReaderResidualPoint:
        return ReaderResidualPoint(
            reader_id=KITCHEN, at=T0, residual_w=value, topology_version_id="topo-2",
            basis=MeasurementBasis.UNKNOWN,
        )

    assert repo.upsert_points([point(400.0)]) == 1
    assert repo.upsert_points([point(-35.0)]) == 1
    (row,) = repo.series(KITCHEN)
    assert row["residual_w"] == pytest.approx(-35.0)  # signed, never clamped


# --------------------------------------------------------------------------- #
# Z4b: known-empty sums are 0 W in live data.
# --------------------------------------------------------------------------- #


def test_live_snapshot_reports_known_empty_sums_as_zero(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path / "z4.db", now)
    _declare(core, (FEED,))
    _feed(core, now, {FEED: _stepped(600.0, 2400.0), KITCHEN: _stepped(400.0, 2150.0)})
    core._steady_pass()
    recon = core.snapshot_v3().reconciliation
    assert recon.identified_w == 0.0  # nothing identified yet: a real 0 W
    assert recon.group_w == 0.0


# --------------------------------------------------------------------------- #
# Z4b: as_of(t) reports the model set active at t.
# --------------------------------------------------------------------------- #


def test_as_of_reports_the_historical_model_set(tmp_path: Path) -> None:
    now = [T0]
    path = tmp_path / "z4.db"
    core = _core(path, now)
    empty = make_model_set_id([])
    registry = core.model_registry_v3
    now[0] = T0 + timedelta(hours=1)
    first = registry.propose(_model_version(DISHWASHER, 1))
    registry.activate(first.version_id)
    now[0] = T0 + timedelta(hours=2)
    second = registry.propose(_model_version(DISHWASHER, 2))
    registry.activate(second.version_id)

    versions = core.version_service
    assert versions.as_of(T0 + timedelta(minutes=30)).model_set_id == empty
    assert versions.as_of(T0 + timedelta(hours=1, minutes=30)).model_set_id == make_model_set_id(
        [first.version_id]
    )
    assert versions.as_of(T0 + timedelta(hours=3)).model_set_id == make_model_set_id(
        [second.version_id]
    )
    # Restart-safe: derived from the persisted, clock-stamped event log.
    restarted = _core(path, now)
    assert restarted.version_service.as_of(
        T0 + timedelta(hours=1, minutes=30)
    ).model_set_id == make_model_set_id([first.version_id])
