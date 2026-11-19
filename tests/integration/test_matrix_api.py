"""Integration tests for the v2 matrix API and HB §19.4 drill-downs (M3).

Exercises the real HTTP surface (a live ``APIServer`` on an ephemeral port)
over the materialised sparse view, so the routes are proven wired, ETag-aware,
lock-free and contract-shaped:

* contract round-trip (``MatrixPayload`` / ``CellDetail`` / ``ReaderDetail`` /
  ``RowDetail`` validate the JSON the server actually returns);
* ``304`` on an unchanged ``If-None-Match``;
* ``at=`` returns historical cells from the append-only version table;
* a 500×100 synthetic matrix answers within budget with a *bounded* query
  count (asserted with a ``sqlite3`` trace — no N+1, no writer lock);
* the reader drill-down shows residual = measured − Σ known − unknown;
* ``None`` survives JSON as JSON ``null``;
* no writer lock is taken by any matrix read.

Cells are seeded through M2's :class:`MatrixStore` into a real S15-backed
SQLite database, so the tests read exactly what production reads.  The facade
used by HTTP is a thin object that exposes M3's ``matrix_*`` methods; those are
the real :class:`~wattmosaic_engine.core.mapping_api.MappingApiMixin` methods
bound onto a lightweight stand-in with a real ``Database`` and clock.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from wattmosaic_engine.contracts import (
    CellDetail,
    CellEvents,
    CellMapping,
    CellPower,
    CellProvenance,
    CellTimestamps,
    CellVisual,
    ConfidenceVectorV3,
    EdgeEvidence,
    EdgeLifecycleV3,
    EstimateStatus,
    IdentifiabilityStatus,
    MappingCell,
    MappingStatus,
    MatrixPayload,
    Observability,
    ReaderDetail,
    RelationType,
    RowDetail,
    RowKind,
    TopologyEdgeV3,
)
from wattmosaic_engine.core.mapping_api import MappingApiMixin
from wattmosaic_engine.mapping.service import (
    MatrixFilters,
    MatrixService,
    SqliteCellSource,
    cells_hash,
    decode_cell,
    etag_for,
)
from wattmosaic_engine.storage.database import Database

TOKEN = "test-token"
HEADERS = {"X-API-Token": TOKEN}
_ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures / helpers.
# --------------------------------------------------------------------------- #


def _make_cell(
    row_id: str,
    reader_id: str,
    *,
    power_w: float | None,
    at: datetime,
    status: EstimateStatus = EstimateStatus.ESTIMATED,
    row_kind: RowKind = RowKind.DEVICE,
    measured: bool = False,
) -> MappingCell:
    return MappingCell(
        row_kind=row_kind,
        row_id=row_id,
        reader_id=reader_id,
        mapping=CellMapping(
            observability=Observability.DIRECT,
            relation_type=None,
            mapping_status=MappingStatus.CONFIRMED,
            valid_from=at,
        ),
        power=CellPower(measurement_status=status, estimated_power_w=power_w),
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        event_association=CellEvents(matched_event_count=1, confirmed_match_count=1),
        confidence=ConfidenceVectorV3(
            measurement_quality=0.9, event_association=0.8, model_fit=0.7
        ),
        provenance=CellProvenance(
            topology_version_id="topo-1",
            mapping_version_id="map-1",
            model_version_id="mset-1",
        ),
        timestamps=CellTimestamps(
            first_observed_at=at, last_observed_at=at, updated_at=at
        ),
        visual=(
            CellVisual.DIRECTLY_MEASURED
            if measured
            else CellVisual.CONFIRMED_INFERRED
        ),
    )


class MatrixFacade(MappingApiMixin):
    """The real M3 mixin bound onto a stand-in with a real Database."""

    def __init__(self, path: Path, *, clock: Any | None = None) -> None:
        self.db = Database(path)
        self._clock = clock or (lambda: _ORIGIN)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "matrix_api.db"


def _seed(db: Database, cells: list[MappingCell], *, at: datetime = _ORIGIN) -> None:
    mapping_version = cells[0].provenance.mapping_version_id if cells else "map-1"
    db.repos.mapping.upsert_cells(cells, mapping_version_id=mapping_version, at=at)


def _set_active_map(db: Database, version_id: str = "map-1", number: int = 1) -> None:
    """Record an active mapping version so ETag uses a real version id."""
    with db._lock:
        db._conn.execute(
            "INSERT OR IGNORE INTO mapping_versions(version_id, number, status, mapping_edge_ids_json, created_at, digest) "
            "VALUES (?, ?, 'active', '[]', ?, ?)",
            (version_id, number, _ORIGIN.isoformat(), "digest"),
        )


# HB §17 reference house: Main ⊃ Subpanel ⊃ {Circuit A, Circuit B}, Main ⊃
# Circuit C.  Five measurement endpoints, four CONFIRMED CONTAINS edges.
_HB17_READERS = ("Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C")
_HB17_CONTAINS = (
    ("Main", "Subpanel"),
    ("Subpanel", "Circuit A"),
    ("Subpanel", "Circuit B"),
    ("Main", "Circuit C"),
)


def _seed_hb17_topology(db: Database) -> None:
    """Seed the HB17 endpoints and CONTAINS edges via the S13/S14 repositories."""
    for reader_id in _HB17_READERS:
        db.repos.entities.upsert_endpoint(
            dict(
                endpoint_id=reader_id,
                kind="ct",
                display_name=reader_id,
                scope="circuit",
                scope_status="confirmed",
                enabled=1,
                created_at=_ORIGIN,
            )
        )
    edge_ids: list[str] = []
    for parent, child in _HB17_CONTAINS:
        edge_id = f"hb17::{parent}->{child}"
        edge_ids.append(edge_id)
        db.repos.topology_v3.upsert_edge(
            TopologyEdgeV3(
                edge_id=edge_id,
                relation=RelationType.CONTAINS,
                source_endpoint_id=parent,
                target_endpoint_id=child,
                lifecycle=EdgeLifecycleV3.CONFIRMED,
                declared_by="user",
                evidence=EdgeEvidence(),
                valid_from=_ORIGIN,
                pinned=True,
            )
        )
    # The migration backfills an empty active version ("topo-1"); point its
    # edge set at the seeded edges (the repository has no setter for edge ids).
    with db._lock:
        db._conn.execute(
            "UPDATE topology_versions_v3 SET edge_ids_json=? WHERE version_id='topo-1'",
            (json.dumps(edge_ids),),
        )


def _leaf_ids(nodes: Any) -> list[str]:
    out: list[str] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if getattr(node, "kind", None) == "reader":
            out.append(str(node.reader_id))
        else:
            stack.extend(getattr(node, "children", ()) or ())
    return out


def _max_depth(node: Any, depth: int = 1) -> int:
    children = getattr(node, "children", ()) or ()
    return depth if not children else max(_max_depth(child, depth + 1) for child in children)


def _all_rows(nodes: Any) -> list[Any]:
    """Every RowNode, including grouping nodes and nodes with children."""
    out: list[Any] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(getattr(node, "children", ()) or ())
    return out


async def _with_server(facade: Any, fn: Any, *, token: str = TOKEN) -> Any:
    from wattmosaic_engine.api.server import APIServer

    server = APIServer(facade, host="127.0.0.1", port=0, api_token=token)
    await server.start()
    try:
        base = f"http://127.0.0.1:{server.port}/api/v1"
        async with aiohttp.ClientSession() as session:
            return await fn(session, base)
    finally:
        await server.stop()


# --------------------------------------------------------------------------- #
# Contract round-trip + None semantics.
# --------------------------------------------------------------------------- #


def test_matrix_contract_round_trip_and_none_survives_json(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    _seed(
        db,
        [
            _make_cell("dev_a", "reader_1", power_w=120.0, at=_ORIGIN),
            # An UNKNOWN estimate has no point value; None must survive as null.
            _make_cell(
                "dev_unknown",
                "reader_1",
                power_w=None,
                at=_ORIGIN,
                status=EstimateStatus.UNKNOWN,
            ),
        ],
    )
    facade = MatrixFacade(db_path)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/matrix", headers=HEADERS) as resp:
            assert resp.status == 200
            assert resp.headers["X-WattMosaic-Contract-Version"]
            assert resp.headers["ETag"].startswith("map-1-")
            body = await resp.json()
        payload = MatrixPayload.model_validate(body)
        assert payload.mapping_version_id == "map-1"
        assert len(payload.cells) == 2
        points = {c.row_id: c.power.estimated_power_w for c in payload.cells}
        assert points["dev_a"] == 120.0
        assert points["dev_unknown"] is None
        # None survives JSON as null, never as 0.0.
        raw = await (await session.get(f"{base}/matrix", headers=HEADERS)).json()
        unknown = next(c for c in raw["cells"] if c["row_id"] == "dev_unknown")
        assert unknown["power"]["estimated_power_w"] is None

    asyncio.run(_with_server(facade, fn))


def test_drill_downs_validate_as_contracts(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    _seed(
        db,
        [
            _make_cell("dev_a", "reader_1", power_w=120.0, at=_ORIGIN),
            _make_cell("dev_b", "reader_1", power_w=80.0, at=_ORIGIN),
        ],
    )
    facade = MatrixFacade(db_path)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(
            f"{base}/matrix/cells/dev_a/reader_1", headers=HEADERS
        ) as resp:
            assert resp.status == 200
            CellDetail.model_validate(await resp.json())
        async with session.get(f"{base}/matrix/readers/reader_1", headers=HEADERS) as resp:
            assert resp.status == 200
            ReaderDetail.model_validate(await resp.json())
        async with session.get(f"{base}/matrix/rows/dev_a", headers=HEADERS) as resp:
            assert resp.status == 200
            RowDetail.model_validate(await resp.json())

    asyncio.run(_with_server(facade, fn))


# --------------------------------------------------------------------------- #
# ETag / 304.
# --------------------------------------------------------------------------- #


def test_etag_returns_304_on_unchanged_version(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    _seed(db, [_make_cell("dev_a", "reader_1", power_w=100.0, at=_ORIGIN)])
    facade = MatrixFacade(db_path)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/matrix", headers=HEADERS) as resp:
            assert resp.status == 200
            etag = resp.headers["ETag"]
        assert etag.startswith("map-1-")
        async with session.get(
            f"{base}/matrix",
            headers={**HEADERS, "If-None-Match": etag},
        ) as resp:
            assert resp.status == 304
            assert resp.headers["ETag"] == etag
            assert await resp.text() == ""

        # A cell change changes the fingerprint -> the same ETag no longer 304s.
        _seed(
            db,
            [_make_cell("dev_a", "reader_1", power_w=150.0, at=_ORIGIN + timedelta(minutes=1))],
        )
        async with session.get(
            f"{base}/matrix",
            headers={**HEADERS, "If-None-Match": etag},
        ) as resp:
            assert resp.status == 200

    asyncio.run(_with_server(facade, fn))


def test_etag_helpers_are_stable() -> None:
    cell = _make_cell("dev_a", "reader_1", power_w=42.0, at=_ORIGIN)
    digest = cells_hash([cell])
    assert digest == cells_hash([cell])
    assert etag_for("map-7", digest) == f"map-7-{digest}"


# --------------------------------------------------------------------------- #
# Historical at= query.
# --------------------------------------------------------------------------- #


def test_at_query_returns_history(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    t0 = _ORIGIN
    t1 = _ORIGIN + timedelta(hours=1)
    _seed(db, [_make_cell("dev_a", "reader_1", power_w=100.0, at=t0)], at=t0)
    _seed(db, [_make_cell("dev_a", "reader_1", power_w=250.0, at=t1)], at=t1)
    facade = MatrixFacade(db_path)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(
            f"{base}/matrix",
            headers=HEADERS,
            params={"at": (t0 + timedelta(minutes=1)).isoformat()},
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert len(body["cells"]) == 1
        assert body["cells"][0]["power"]["estimated_power_w"] == 100.0

    asyncio.run(_with_server(facade, fn))


# --------------------------------------------------------------------------- #
# Reader drill-down: residual = measured − Σ known − unknown.
# --------------------------------------------------------------------------- #


def test_reader_drill_down_shows_residual_identity(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    now = _ORIGIN
    _seed(
        db,
        [
            _make_cell(
                "dev_measured",
                "reader_1",
                power_w=500.0,
                at=now,
                status=EstimateStatus.MEASURED,
                measured=True,
            ),
            _make_cell("dev_a", "reader_1", power_w=120.0, at=now),
            _make_cell("dev_b", "reader_1", power_w=80.0, at=now),
            _make_cell(
                "prof_1",
                "reader_1",
                power_w=50.0,
                at=now,
                row_kind=RowKind.UNKNOWN_PROFILE,
            ),
        ],
    )
    facade = MatrixFacade(db_path)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(
            f"{base}/matrix/readers/reader_1", headers=HEADERS
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["measured_w"] == 500.0
        assert body["known_contrib_w"] == 200.0
        assert body["unknown_w"] == 50.0
        assert body["residual_w"] == 500.0 - 200.0 - 50.0

    asyncio.run(_with_server(facade, fn))


# --------------------------------------------------------------------------- #
# Performance / query count / no writer lock.
# --------------------------------------------------------------------------- #


def test_500x100_matrix_bounded_queries_and_no_writer_lock(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    rows = [f"dev_{i:03d}" for i in range(500)]
    readers = [f"r{j:03d}" for j in range(100)]
    # Sparse: every 4th (row, reader) pair has a cell -> 12 500 cells.
    cells = [
        _make_cell(row, reader, power_w=1.0 + i, at=_ORIGIN)
        for i, (row, reader) in enumerate(
            (row, reader)
            for ri, row in enumerate(rows)
            for rj, reader in enumerate(readers)
            if (ri + rj) % 4 == 0
        )
    ]
    _seed(db, cells)
    service = MatrixService(SqliteCellSource(db), mapping_version_id="map-1")

    # Trace every statement on a dedicated connection; count only matrix reads.
    statements: list[str] = []
    original = sqlite3.connect

    def tracing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    sqlite3.connect = tracing_connect  # type: ignore[assignment]
    try:
        samples_ms: list[float] = []
        payload = None
        for _ in range(12):
            started = time.perf_counter()
            payload = service.payload(
                MatrixFilters(page=1, page_size=1000),
                fingerprint=service.fingerprint(),
            )
            samples_ms.append((time.perf_counter() - started) * 1000.0)
    finally:
        sqlite3.connect = original  # type: ignore[assignment]

    reads = [s for s in statements if "matrix_cell" in s.lower()]
    # One fingerprint read + one page read per call: no per-cell N+1.
    assert len(reads) <= 24, f"unbounded query count: {len(reads)} ({reads[:5]})"
    assert "BEGIN IMMEDIATE" not in " ".join(statements)
    assert payload is not None
    assert payload.mapping_version_id == "map-1"
    assert len(payload.cells) == 1000  # page_size bound; no full 50k fan-out
    p95 = sorted(samples_ms)[int(0.95 * (len(samples_ms) - 1))]
    assert p95 < 300.0, f"p95 {p95:.1f} ms exceeds the 300 ms budget"


def test_matrix_reads_never_take_writer_lock(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    _seed(db, [_make_cell("dev_a", "reader_1", power_w=1.0, at=_ORIGIN)])

    statements: list[str] = []
    original = sqlite3.connect

    def tracing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    sqlite3.connect = tracing_connect  # type: ignore[assignment]
    try:
        service = MatrixService(SqliteCellSource(db), mapping_version_id="map-1")
        service.fingerprint()
        service.payload(MatrixFilters())
        service.cell_detail("dev_a", "reader_1")
        service.reader_detail("reader_1")
        service.row_detail("dev_a")
    finally:
        sqlite3.connect = original  # type: ignore[assignment]

    # Only SELECT/PRAGMA reads were issued: no DML statement and no write
    # transaction was started by any matrix read.
    dml = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")
    for statement in statements:
        upper = statement.strip().upper()
        assert not any(upper.startswith(word) for word in dml), f"write statement: {statement!r}"


# --------------------------------------------------------------------------- #
# Pure decode + paging helpers.
# --------------------------------------------------------------------------- #


def test_decode_cell_preserves_none_and_confidence(db_path: Path) -> None:
    db = Database(db_path)
    _set_active_map(db)
    cell = _make_cell("dev_a", "reader_1", power_w=None, at=_ORIGIN, status=EstimateStatus.UNKNOWN)
    _seed(db, [cell])
    rows = db.repos.mapping.bulk_cells()
    assert len(rows) == 1
    decoded = decode_cell(rows[0])
    assert decoded.power.estimated_power_w is None
    assert decoded.confidence.measurement_quality == pytest.approx(0.9)


def test_paging_bounds_page_size() -> None:
    filters = MatrixFilters.from_query({"page": "2", "page_size": "99999"})
    assert filters.page == 2
    assert filters.page_size == MatrixFilters.MAX_PAGE_SIZE


# --------------------------------------------------------------------------- #
# M3b: real L0–L4 columns / non-empty rows from the canonical repositories.
# --------------------------------------------------------------------------- #


def test_hb17_columns_have_depth_five_and_one_leaf_per_endpoint(db_path: Path) -> None:
    """A DB seeded with the HB17 topology returns real L0..L4 column trees."""
    db = Database(db_path)
    _set_active_map(db)
    _seed_hb17_topology(db)
    _seed(db, [_make_cell("dev_a", "Main", power_w=100.0, at=_ORIGIN)])
    facade = MatrixFacade(db_path)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/matrix", headers=HEADERS) as resp:
            assert resp.status == 200
            payload = MatrixPayload.model_validate(await resp.json())

        assert len(payload.columns) == 1
        root = payload.columns[0]
        assert root.level == "L0" and root.kind == "domain"
        assert _max_depth(root) == 5, "expected L0..L4 levels"
        levels = set()

        def walk(node: Any) -> None:
            levels.add(node.level)
            for child in node.children:
                walk(child)

        walk(root)
        assert levels == {"L0", "L1", "L2", "L3", "L4"}

        leaves = _leaf_ids(payload.columns)
        assert len(leaves) == len(_HB17_READERS)
        assert set(leaves) == set(_HB17_READERS)
        # One leaf per endpoint — no duplicates from secondary relations.
        assert len(leaves) == len(set(leaves))

        # Rows are real too: one device row plus reader-residual and summary rows.
        assert payload.rows
        row_ids = {row.row_id for row in _all_rows(payload.rows)}
        assert "summary" in row_ids
        kinds = {str(row.kind) for row in _all_rows(payload.rows)}
        assert "reader_residual" in kinds

    asyncio.run(_with_server(facade, fn))


def test_hb17_rows_include_devices_components_profiles_groups(db_path: Path) -> None:
    """Rows come from devices/components/groups/profiles/readers that exist."""
    db = Database(db_path)
    _set_active_map(db)
    _seed_hb17_topology(db)

    # Physical devices + a load component via the S13 repository.
    db.repos.entities.upsert_device(
        dict(
            device_id="dev_tv",
            name="Television",
            origin="user",
            created_at=_ORIGIN,
        )
    )
    db.repos.entities.upsert_component(
        dict(component_id="lc_screen", device_id="dev_tv", name="Screen")
    )
    # Unknown-load profile via the S19 repository.
    from wattmosaic_engine.contracts import UnknownLoadProfileV3

    db.repos.residual.put_profile(
        UnknownLoadProfileV3(
            profile_id="prof_fridge",
            scope_endpoint_id="Main",
            lifecycle="characterised",
            signature={},
            event_count=1,
            independent_event_count=1,
            first_seen=_ORIGIN,
            last_seen=_ORIGIN,
        )
    )
    # A current group id via the S17 group-estimate repository.
    with db._lock:
        db._conn.execute(
            "INSERT OR REPLACE INTO device_group_estimates("
            "group_id, at, member_ids_json, source_reader_ids_json, evidence_refs_json, "
            "topology_version_id, mapping_version_id, model_set_id, estimate_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "grp_lights",
                _ORIGIN.isoformat(),
                json.dumps(["dev_tv"]),
                "[]",
                "[]",
                "topo-1",
                "map-1",
                "mset-x",
                "{}",
            ),
        )

    facade = MatrixFacade(db_path)
    payload = facade.matrix_payload()
    kinds = {str(row.kind) for row in _all_rows(payload.rows)}
    assert "device" in kinds
    assert "component" in kinds
    assert "unknown_profile" in kinds
    assert "group" in kinds
    assert "reader_residual" in kinds
    assert "summary" in kinds


def test_etag_changes_when_mapping_version_changes(db_path: Path) -> None:
    """The ETag tracks the mapping version even when the cells do not change."""
    db = Database(db_path)
    _set_active_map(db, "map-1", number=1)
    _seed_hb17_topology(db)
    _seed(db, [_make_cell("dev_a", "Main", power_w=100.0, at=_ORIGIN)])
    facade = MatrixFacade(db_path)

    first_etag = facade.matrix_etag()
    assert first_etag.startswith("map-1-")

    # Bump the active mapping version; cells and topology stay identical.
    _set_active_map(db, "map-2", number=2)
    second_etag = facade.matrix_etag()
    assert second_etag.startswith("map-2-")
    assert second_etag != first_etag

    # The second request still serves real trees after the cache key changed.
    payload = facade.matrix_payload()
    assert payload.mapping_version_id == "map-2"
    assert _max_depth(payload.columns[0]) == 5
    assert len(_leaf_ids(payload.columns)) == len(_HB17_READERS)

