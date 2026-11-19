"""D4 — snapshot assembly (HB §12.1), persistence and the v3 HTTP reads.

Integration tests over the real S17 SQLite repositories and the real aiohttp
app.  The DB fixture is the genuine pre-v17 schema built by the v17 unit suite;
``Database`` migrates it to the current version on open.  No legacy engine path
is imported, and ``runtime.fakes`` is used only as test input.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from wattmosaic_engine.api.facade import StubFacade
from wattmosaic_engine.api.server import create_app
from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DataQualityReport,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EventGroupStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    InferenceSnapshot,
    JointInferenceResult,
    MeasurementBasis,
    ReaderResidualPoint,
    ReconciliationV3,
    VersionTriple,
)
from wattmosaic_engine.core.snapshot import SnapshotAssembler
from wattmosaic_engine.runtime.interfaces import HeldReading
from wattmosaic_engine.storage.database import Database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_v017_results import _build_pre_migration_db  # noqa: E402

T0 = datetime(2026, 11, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "snapshot_v3.db"
    _build_pre_migration_db(path)
    database = Database(path)
    try:
        yield database
    finally:
        database.close()


def _estimate(
    subject_id: str,
    *,
    point_w: float | None = 60.0,
    status: EstimateStatus = EstimateStatus.ESTIMATED,
    ident: IdentifiabilityStatus = IdentifiabilityStatus.IDENTIFIABLE,
    method: InferenceMethod = InferenceMethod.JOINT_DELTA,
    group_id: str | None = None,
    readers: list[str] | None = None,
    refs: list[EvidenceRef] | None = None,
    updated_at: datetime = T0,
) -> DeviceEstimate:
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point_w,
        bounds=Bounds(lower_w=0.0, upper_w=100.0, basis="non_negativity"),
        status=status,
        identifiability=ident,
        group_id=group_id,
        method=method,
        model_version_id="mdl-test",
        confidence=ConfidenceVectorV3(measurement_quality=0.8),
        source_reader_ids=readers or ["Circuit A"],
        evidence_refs=refs or [EvidenceRef(kind="observation", id="Circuit A")],
        versions=VT,
        updated_at=updated_at,
    )


def _group(group_id: str = "group-1", *, members: tuple[str, ...] = ("lamp-a", "lamp-b")) -> DeviceGroupEstimate:
    return DeviceGroupEstimate(
        group_id=group_id,
        member_ids=list(members),
        point_w=45.0,
        bounds=Bounds(lower_w=0.0, upper_w=100.0, basis="group_sum"),
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        source_reader_ids=["Main"],
        evidence_refs=[EvidenceRef(kind="event_group", id=group_id)],
        versions=VT,
        updated_at=T0,
    )


def _result(
    estimates: list[DeviceEstimate],
    groups: list[DeviceGroupEstimate] | None = None,
    *,
    group_id: str | None = "eg-1",
) -> JointInferenceResult:
    return JointInferenceResult(
        event_group_id=group_id,
        event_group_revision=1,
        status=EventGroupStatus.CONFIRMED,
        device_estimates=estimates,
        group_estimates=groups or [],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={"Main": 500.0},
        versions=VT,
        computed_at=T0,
    )


def _assembler(db: Database) -> SnapshotAssembler:
    return SnapshotAssembler(db.repos.inference, clock=lambda: T0)


def _held(*readers: str) -> dict[str, HeldReading]:
    return {
        reader: HeldReading(
            value=float(index + 1) * 10.0,
            held_since=T0,
            last_confirmed_at=T0,
            state="held",
        )
        for index, reader in enumerate(readers)
    }


# --------------------------------------------------------------------------- #
# Assembly + persistence.
# --------------------------------------------------------------------------- #


def test_on_result_builds_hb121_snapshot(db: Database) -> None:
    assembler = _assembler(db)
    snapshot = assembler.on_result(
        _result(
            [_estimate("dev-1")],
            [_group()],
        ),
        _held("Circuit A", "Main"),
        DataQualityReport(timestamp=T0),
        residual_points=[
            ReaderResidualPoint(
                reader_id="Main", at=T0, residual_w=500.0,
                topology_version_id="topo-1", basis=MeasurementBasis.CT_MEASURED,
            )
        ],
    )
    assert snapshot.snapshot_id.startswith("snap-")
    assert snapshot.versions == VT
    assert snapshot.observed_at == T0
    assert [e.subject_id for e in snapshot.device_estimates] == ["dev-1"]
    assert [g.group_id for g in snapshot.group_estimates] == ["group-1"]
    assert len(snapshot.reader_measurements) == 2
    assert snapshot.reader_measurements[0].active_power_w == 10.0
    assert snapshot.reconciliation.main_w == 500.0
    assert snapshot.reconciliation.identified_w == 60.0
    assert snapshot.reconciliation.group_w == 45.0
    # Publish is copy-on-write: the published object is the exact object returned.
    assert assembler.current is snapshot


def test_merge_per_subject_latest_result_wins(db: Database) -> None:
    assembler = _assembler(db)
    first = assembler.on_result(_result([_estimate("dev-a", point_w=10.0)]))
    second = assembler.on_result(
        _result([_estimate("dev-b", point_w=20.0), _estimate("dev-a", point_w=30.0)])
    )
    by_id = {e.subject_id: e.point_w for e in second.device_estimates}
    assert by_id == {"dev-a": 30.0, "dev-b": 20.0}
    assert first is not second


def test_stale_sources_and_unresolved_events(db: Database) -> None:
    held = _held("Circuit A")
    held["Main"] = HeldReading(None, T0, T0, "stale")
    result = _result([_estimate("dev-1")], group_id="eg-unresolved")
    result.status = EventGroupStatus.UNRESOLVED
    snapshot = _assembler(db).on_result(result, held)
    assert "Main" in snapshot.stale_sources
    assert "eg-unresolved" in snapshot.unresolved_events


def test_unchanged_state_writes_no_new_snapshot_row(db: Database) -> None:
    assembler = _assembler(db)
    result = _result([_estimate("dev-1")])
    first = assembler.on_result(result)
    count_after_first = db._conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0]
    # Identical content at a different clock still must not write a new row.
    assembler_2 = SnapshotAssembler(db.repos.inference, clock=lambda: datetime(2026, 11, 2, tzinfo=UTC))
    second = assembler_2.on_result(result)
    count_after_second = db._conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0]
    assert count_after_second == count_after_first == 1
    assert first.snapshot_id == second.snapshot_id


def test_changed_state_writes_a_new_snapshot_row(db: Database) -> None:
    assembler = _assembler(db)
    assembler.on_result(_result([_estimate("dev-1", point_w=10.0)]))
    assembler.on_result(_result([_estimate("dev-1", point_w=20.0)]))
    count = db._conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0]
    assert count == 2


def test_restart_loads_identical_snapshot(db: Database) -> None:
    """A fresh assembler loads the last persisted snapshot (HB §15)."""
    assembler = _assembler(db)
    built = assembler.on_result(_result([_estimate("dev-1")], [_group()]))
    # Simulate a restart: a brand-new assembler with no in-memory state.
    restarted = _assembler(db)
    loaded = restarted.load_latest()
    assert loaded is not None
    assert loaded.snapshot_id == built.snapshot_id
    assert loaded.versions == built.versions
    assert [e.subject_id for e in loaded.device_estimates] == [
        e.subject_id for e in built.device_estimates
    ]
    # The pre-restart snapshot equals the post-restart one via the v3 facade.
    class Facade(StubFacade):
        def snapshot_v3(self):
            return restarted.current

    async def scenario() -> None:
        app = create_app(Facade(), api_token="")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/snapshot?v=3")
            assert resp.status == 200
            body = await resp.json()
            assert body["snapshot_id"] == built.snapshot_id

    asyncio.run(scenario())


def test_snapshot_payload_round_trips_through_storage(db: Database) -> None:
    """Every evidence ref on a persisted estimate still resolves after load."""
    assembler = _assembler(db)
    estimate = _estimate(
        "dev-1",
        refs=[EvidenceRef(kind="observation", id="Circuit A")],
    )
    built = assembler.on_result(_result([estimate]), _held("Circuit A"))
    loaded = _assembler(db).load_latest()
    assert loaded is not None
    assert built == loaded
    assert loaded.device_estimates[0].evidence_refs == estimate.evidence_refs


# --------------------------------------------------------------------------- #
# HTTP v3 surface.
# --------------------------------------------------------------------------- #


class V3Facade(StubFacade):
    """A stub that serves a fixed v3 snapshot for the new read paths."""

    def __init__(self, snapshot: InferenceSnapshot) -> None:
        super().__init__()
        self.snapshot_data = snapshot

    def snapshot_v3(self):
        return self.snapshot_data

    def devices_v3(self):
        return list(self.snapshot_data.device_estimates)

    def device_v3(self, device_id: str):
        for estimate in self.snapshot_data.device_estimates:
            if estimate.subject_id == device_id:
                return estimate
        from wattmosaic_engine.api.facade import NotFound
        raise NotFound(device_id)

    def groups_v3(self):
        return list(self.snapshot_data.group_estimates)

    def group_v3(self, group_id: str):
        for group in self.snapshot_data.group_estimates:
            if group.group_id == group_id:
                return group
        from wattmosaic_engine.api.facade import NotFound
        raise NotFound(group_id)

    def provenance(self, kind: str, identifier: str):
        from wattmosaic_engine.contracts import EvidenceRef
        from wattmosaic_engine.provenance import ProvenanceResolver

        resolver = ProvenanceResolver(self.snapshot_data)
        if kind in ("device", "component"):
            return resolver.for_device(self.device_v3(identifier))
        if kind == "group":
            return resolver.for_group(self.group_v3(identifier))
        value = resolver.lookup.resolve(EvidenceRef(kind=kind, id=identifier))  # type: ignore[arg-type]
        if value is None:
            from wattmosaic_engine.api.facade import NotFound
            raise NotFound(f"{kind}:{identifier}")
        return value


def _v3_snapshot() -> InferenceSnapshot:
    member = _estimate(
        "lamp-a",
        point_w=None,
        status=EstimateStatus.UNKNOWN,
        ident=IdentifiabilityStatus.NON_IDENTIFIABLE,
        method=InferenceMethod.NONE,
        group_id="group-1",
    )
    group = _group()
    return InferenceSnapshot(
        snapshot_id="snap-http",
        observed_at=T0,
        computed_at=T0,
        versions=VT,
        reader_measurements=[],
        device_estimates=[member],
        group_estimates=[group],
        residual_estimates=[],
        reconciliation=ReconciliationV3(main_w=100.0),
        system_quality=DataQualityReport(timestamp=T0),
        stale_sources=[],
        unresolved_events=[],
    )


def test_v3_routes_serve_snapshot_devices_groups_and_provenance() -> None:
    snapshot = _v3_snapshot()

    async def scenario() -> None:
        app = create_app(V3Facade(snapshot), api_token="")
        async with TestClient(TestServer(app)) as client:
            # /snapshot?v=3 serves the v3 shape; /snapshot stays legacy.
            resp = await client.get("/api/v1/snapshot?v=3")
            assert resp.status == 200
            body = await resp.json()
            assert body["snapshot_id"] == "snap-http"
            assert body["versions"]["topology_version_id"] == "topo-1"

            resp = await client.get("/api/v1/snapshot")
            assert resp.status == 200
            legacy = await resp.json()
            assert "results" in legacy  # legacy shape unchanged

            resp = await client.get("/api/v1/devices?v=3")
            assert resp.status == 200
            devices = await resp.json()
            assert [e["subject_id"] for e in devices] == ["lamp-a"]

            resp = await client.get("/api/v1/groups")
            assert resp.status == 200
            groups = await resp.json()
            assert [g["group_id"] for g in groups] == ["group-1"]

            resp = await client.get("/api/v1/groups/group-1")
            assert resp.status == 200
            group = await resp.json()
            assert group["point_w"] == 45.0

            resp = await client.get("/api/v1/provenance/device/lamp-a")
            assert resp.status == 200
            provenance = await resp.json()
            assert provenance["status"] == "unknown"
            assert provenance["identifiability"] == "non_identifiable"
            assert provenance["bounds"]["lower_w"] == 0.0

            resp = await client.get("/api/v1/provenance/group/group-1")
            assert resp.status == 200
            group_prov = await resp.json()
            assert group_prov["point_w"] == 45.0

    asyncio.run(scenario())


def test_group_member_has_no_individual_value_in_any_v3_route() -> None:
    """A group member never acquires a number in any v3 payload."""
    snapshot = _v3_snapshot()

    async def scenario() -> None:
        app = create_app(V3Facade(snapshot), api_token="")
        async with TestClient(TestServer(app)) as client:
            for path in (
                "/api/v1/devices?v=3",
                "/api/v1/snapshot?v=3",
                "/api/v1/provenance/device/lamp-a",
            ):
                resp = await client.get(path)
                assert resp.status == 200, path
                text = await resp.text()
                body = json.loads(text)
                if path.endswith("lamp-a"):
                    assert body["point_w"] is None
                elif isinstance(body, list):
                    for estimate in body:
                        if estimate.get("subject_id") == "lamp-a":
                            assert estimate["point_w"] is None
                else:
                    for estimate in body.get("device_estimates", []):
                        if estimate.get("subject_id") == "lamp-a":
                            assert estimate["point_w"] is None

    asyncio.run(scenario())
