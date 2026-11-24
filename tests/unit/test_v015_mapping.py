"""Migration v015: mapping edges/versions and the sparse matrix cell store (S15).

The fixture is a genuine pre-migration database built with raw ``sqlite3``:
migrations 1-11 plus legacy rows in ``devices``, ``device_configurations`` and
``topology_relations``, with ``user_version`` left at 11.  ``Database.__init__``
migrates immediately, so it cannot produce a pre-migration fixture.

Idempotency is tested by calling the migration's ``apply`` callable twice
directly; a failure injected mid-apply is wrapped in the same
``BEGIN IMMEDIATE``/``PRAGMA user_version`` transaction ``apply_migrations``
uses, so it must leave ``user_version`` unchanged and no new tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.contracts import (
    CellEvents,
    CellMapping,
    CellPower,
    CellProvenance,
    CellTimestamps,
    CellVisual,
    ConfidenceVectorV3,
    EstimateStatus,
    IdentifiabilityStatus,
    MappingCell,
    MappingStatus,
    Observability,
    RowKind,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations import v015_mapping as v015

PRE_MIGRATION_VERSION = 11
NOW = "2026-11-01T00:00:00+00:00"
_V015 = v015.MIGRATION


# --------------------------------------------------------------------------- #
# Fixture helpers: a genuine schema-11 database with legacy mapping sources
# --------------------------------------------------------------------------- #


def _build_schema11(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > PRE_MIGRATION_VERSION:
            break
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")
    conn.commit()
    return conn


def _device(conn: sqlite3.Connection, device_id: str, branch: list[str]) -> None:
    conn.execute(
        """INSERT INTO devices(device_id, name, dedicated_ct_ids_json, branch_ct_ids_json,
           phase, double_pole, parent_device_id, topology_status, enabled, metadata_json, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            device_id, device_id, "[]", json.dumps(branch), "l1", 0, None,
            "candidate", 1, "{}", NOW,
        ),
    )


def _config(
    conn: sqlite3.Connection, device_id: str, ct_ids: list[str], configured_by: str = "user"
) -> None:
    payload = {"monitored_by_ct_ids": ct_ids, "configured_by": configured_by}
    conn.execute(
        "INSERT INTO device_configurations(device_id, payload_json, updated_at) VALUES(?,?,?)",
        (device_id, json.dumps(payload), NOW),
    )


def _relation(
    conn: sqlite3.Connection,
    relation_id: str,
    subject: str,
    ct_id: str,
    verdict: str,
    *,
    valid_to: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO topology_relations(relation_id, kind, subject_id, object_id,
           status, confidence, evidence_json, requires_confirmation, declared_by,
           algorithm_version, valid_from, valid_to, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            relation_id, "DEVICE_CT_ASSOCIATION", subject, ct_id, "confirmed", 0.9,
            json.dumps({"verdict": verdict}), 1, "inference", "v1", NOW, valid_to, NOW,
        ),
    )


def _seed(conn: sqlite3.Connection) -> None:
    # Source 1: user-declared branch CTs.
    _device(conn, "dev-a", ["ct-1", "ct-2"])
    _device(conn, "dev-b", ["ct-2"])  # ct-2 shared -> dedup by (subject, endpoint)
    # Source 2: monitored_by_ct_ids (one user, one auto placement).
    _config(conn, "dev-a", ["ct-1", "ct-3"])  # ct-1 duplicate with branch
    _config(conn, "dev-c", ["ct-4"], configured_by="auto")
    # Source 3: associations, one superseded (valid_to).
    _relation(conn, "rel-1", "dev-a", "ct-2", "supported")
    _relation(conn, "rel-2", "dev-d", "ct-5", "single_event")
    _relation(conn, "rel-3", "dev-e", "ct-6", "ambiguous", valid_to=NOW)
    conn.commit()


def _open(path: Path) -> Database:
    db = Database(path)
    # Other wave-2 migrations are discovered in the same tree; the constructor
    # runs through 15 on its way to LATEST, so 15 is a floor, not an exact value.
    assert db.schema_version >= _V015.version
    return db


def _cell(
    row_id: str,
    reader_id: str,
    *,
    updated: datetime,
    power_w: float | None,
    mapping_status: MappingStatus = MappingStatus.CONFIRMED,
    observability: Observability = Observability.INFERRED,
) -> MappingCell:
    return MappingCell(
        row_kind=RowKind.DEVICE,
        row_id=row_id,
        reader_id=reader_id,
        mapping=CellMapping(
            observability=observability,
            relation_type=None,
            mapping_status=mapping_status,
            valid_from=updated,
        ),
        power=CellPower(measurement_status=EstimateStatus.ESTIMATED, estimated_power_w=power_w),
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        event_association=CellEvents(),
        confidence=ConfidenceVectorV3(),
        provenance=CellProvenance(topology_version_id="topo-1", mapping_version_id="map-1"),
        timestamps=CellTimestamps(
            first_observed_at=updated, last_observed_at=updated, updated_at=updated
        ),
        visual=CellVisual.CONFIRMED_INFERRED,
    )


# --------------------------------------------------------------------------- #
# Migration contract, DDL, idempotency and atomicity
# --------------------------------------------------------------------------- #


def test_module_declares_the_reserved_version_and_tiers() -> None:
    assert _V015.version == 15
    assert v015.TIERS == {
        "mapping_edges": "L4",
        "mapping_versions": "L4",
        "matrix_cells": "L4",
        "matrix_cell_versions": "L4",
    }


def test_migration_creates_all_four_tables(tmp_path: Path) -> None:
    path = tmp_path / "tables.db"
    _build_schema11(path)
    db = _open(path)
    try:
        tables = {
            row[0]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"mapping_edges", "mapping_versions", "matrix_cells", "matrix_cell_versions"} <= tables
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(matrix_cells)")}
        assert {
            "row_kind", "row_id", "reader_id", "mapping_json", "power_json",
            "mapping_status", "observability", "identifiability", "updated_at", "visual",
        } <= columns
        history = {row[1] for row in db._conn.execute("PRAGMA table_info(matrix_cell_versions)")}
        assert {"mapping_version_id", "valid_from", "valid_until", "cell_version_id"} <= history
    finally:
        db.close()


def test_apply_callable_is_idempotent(tmp_path: Path) -> None:
    """Invoking the apply hook twice directly leaves the schema and rows unchanged."""
    path = tmp_path / "idempotent.db"
    conn = _build_schema11(path)
    try:
        for statement in _V015.statements:
            conn.execute(statement)
        assert _V015.apply is not None
        _V015.apply(conn)
        first_edges = conn.execute("SELECT COUNT(*) FROM mapping_edges").fetchone()[0]
        first_versions = conn.execute("SELECT COUNT(*) FROM mapping_versions").fetchone()[0]
        _V015.apply(conn)
        assert conn.execute("SELECT COUNT(*) FROM mapping_edges").fetchone()[0] == first_edges
        assert conn.execute("SELECT COUNT(*) FROM mapping_versions").fetchone()[0] == first_versions
        assert first_versions == 1
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(tmp_path: Path) -> None:
    """An injected failure inside the hooks rolls the whole migration back."""
    path = tmp_path / "atomicity.db"
    _build_schema11(path)

    conn = sqlite3.connect(path)
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _V015.statements:
                conn.execute(statement)
            raise RuntimeError("boom mid-apply")
        except RuntimeError:
            conn.execute("ROLLBACK")

        assert current_version(conn) == PRE_MIGRATION_VERSION
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "mapping_edges" not in tables
        assert "matrix_cells" not in tables
    finally:
        conn.close()


def test_apply_migrations_reaches_v015(tmp_path: Path) -> None:
    path = tmp_path / "reach.db"
    _build_schema11(path)
    conn = sqlite3.connect(path)
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Backfill from the three sources, deduplicated by (subject, endpoint)
# --------------------------------------------------------------------------- #


def test_backfill_from_three_sources_is_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "backfill.db"
    conn = _build_schema11(path)
    _seed(conn)
    db = _open(path)
    try:
        rows = db._conn.execute(
            "SELECT subject_id, endpoint_id, lifecycle, declared_by, observability, valid_until, "
            "evidence_json FROM mapping_edges"
        ).fetchall()
        pairs = {(r["subject_id"], r["endpoint_id"]) for r in rows}
        # dev-a/ct-1 from branch + config, dev-a/ct-2 from branch + association,
        # dev-a/ct-3 from config only, dev-b/ct-2 from branch, dev-c/ct-4 auto,
        # dev-d/ct-5 assoc, dev-e/ct-6 superseded.
        assert pairs == {
            ("dev-a", "ct-1"),
            ("dev-a", "ct-2"),
            ("dev-a", "ct-3"),
            ("dev-b", "ct-2"),
            ("dev-c", "ct-4"),
            ("dev-d", "ct-5"),
            ("dev-e", "ct-6"),
        }
        assert len(rows) == len(pairs)  # one row per (subject, endpoint)
        assert all(r["declared_by"] == "migration" for r in rows)
        assert all(r["observability"] == "inferred" for r in rows)

        by_pair = {(r["subject_id"], r["endpoint_id"]): r for r in rows}
        # User declaration -> CONFIRMED, auto -> PROVISIONAL.
        assert by_pair[("dev-a", "ct-1")]["lifecycle"] == "confirmed"
        assert by_pair[("dev-c", "ct-4")]["lifecycle"] == "provisional"
        # v015 records its provenance in an ``evidence.source`` key that is not
        # an ``EdgeEvidence`` field; v031 strips it from every stored row.  On a
        # database at LATEST the evidence is therefore empty -- the lifecycle
        # above, not the legacy key, is what the backfill means.  The provenance
        # itself is pinned by ``test_backfill_writes_a_legacy_source_key_v031_removes``.
        assert json.loads(by_pair[("dev-c", "ct-4")]["evidence_json"]) == {}
        # Association verdict-mapped lifecycles; single_event is only PROVISIONAL.
        assert by_pair[("dev-a", "ct-2")]["lifecycle"] == "confirmed"  # branch wins over assoc
        assert by_pair[("dev-d", "ct-5")]["lifecycle"] == "provisional"  # single_event
        # A superseded association keeps its valid_to and is RETIRED.
        retired = by_pair[("dev-e", "ct-6")]
        assert retired["lifecycle"] == "retired"
        assert retired["valid_until"] == NOW
    finally:
        db.close()


def test_backfill_writes_a_legacy_source_key_v031_removes(tmp_path: Path) -> None:
    """v015's provenance is real, but it is not an ``EdgeEvidence`` field.

    Pinned in both directions: v015 writes ``{"source": ...}`` (which is what
    made the mapping edge unloadable), and v031 removes it so the row can be
    rebuilt as a contract.  Splitting the assertions keeps v015's intent visible
    instead of silently relaxing the backfill test above.
    """
    from wattmosaic_engine.storage.migrations.v031_evidence_key_repair import MIGRATION

    path = tmp_path / "v015-source.db"
    conn = _build_schema11(path)
    _seed(conn)
    # ``_build_schema11`` stopped at v11; apply only the released migrations
    # through v015, so v031 has not run yet.
    from wattmosaic_engine.storage.migrations import MIGRATIONS

    for migration in MIGRATIONS:
        if migration.version > _V015.version:
            break
        if migration.version <= PRE_MIGRATION_VERSION:
            continue
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.commit()

    row = conn.execute(
        "SELECT evidence_json FROM mapping_edges WHERE subject_id='dev-c' AND endpoint_id='ct-4'"
    ).fetchone()
    assert json.loads(row[0]) == {"source": "auto_placement"}

    MIGRATION.apply(conn)  # noqa: SLF001
    row = conn.execute(
        "SELECT evidence_json FROM mapping_edges WHERE subject_id='dev-c' AND endpoint_id='ct-4'"
    ).fetchone()
    assert json.loads(row[0]) == {}
    conn.close()


def test_verdict_mapping_without_a_competing_source(tmp_path: Path) -> None:
    """split -> conflicted, ambiguous -> candidate, supported -> confirmed."""
    path = tmp_path / "verdicts.db"
    conn = _build_schema11(path)
    _relation(conn, "rel-x", "dev-x", "ctx", "supported")
    _relation(conn, "rel-y", "dev-y", "cty", "split")
    _relation(conn, "rel-z", "dev-z", "ctz", "ambiguous")
    _relation(conn, "rel-w", "dev-w", "ctw", "unknown-verdict")
    conn.commit()
    db = _open(path)
    try:
        rows = {
            r["subject_id"]: r["lifecycle"]
            for r in db._conn.execute("SELECT subject_id, lifecycle FROM mapping_edges")
        }
        assert rows == {
            "dev-x": "confirmed",
            "dev-y": "conflicted",
            "dev-z": "candidate",
            "dev-w": "candidate",
        }
    finally:
        db.close()


def test_initial_mapping_version_is_map_1(tmp_path: Path) -> None:
    path = tmp_path / "version.db"
    conn = _build_schema11(path)
    _seed(conn)
    db = _open(path)
    try:
        versions = db._conn.execute(
            "SELECT version_id, number, status, mapping_edge_ids_json FROM mapping_versions"
        ).fetchall()
        assert len(versions) == 1
        row = versions[0]
        assert row["version_id"] == "map-1"
        assert row["number"] == 1
        assert row["status"] == "active"
        edge_ids = json.loads(row["mapping_edge_ids_json"])
        count = db._conn.execute("SELECT COUNT(*) FROM mapping_edges").fetchone()[0]
        assert len(edge_ids) == count
        assert set(edge_ids) == {
            r[0] for r in db._conn.execute("SELECT mapping_edge_id FROM mapping_edges")
        }
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# No cells backfilled; upsert appends one history row per changed cell
# --------------------------------------------------------------------------- #


def test_no_cells_are_backfilled(tmp_path: Path) -> None:
    path = tmp_path / "empty_cells.db"
    _build_schema11(path)
    db = _open(path)
    try:
        assert db._conn.execute("SELECT COUNT(*) FROM matrix_cells").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM matrix_cell_versions").fetchone()[0] == 0
    finally:
        db.close()


def test_upsert_cells_appends_exactly_one_history_row_per_cell(tmp_path: Path) -> None:
    path = tmp_path / "upsert.db"
    _build_schema11(path)
    db = _open(path)
    try:
        repo = db.repos.mapping
        t0 = datetime(2026, 11, 1, tzinfo=UTC)
        t1 = t0 + timedelta(minutes=5)
        assert repo.upsert_cells([_cell("dev-a", "ct-1", updated=t0, power_w=42.0)], at=t0) == 1
        assert repo.upsert_cells(
            [
                _cell("dev-a", "ct-1", updated=t1, power_w=55.0),
                _cell("dev-b", "ct-2", updated=t1, power_w=13.0),
            ],
            at=t1,
        ) == 2

        assert db._conn.execute("SELECT COUNT(*) FROM matrix_cells").fetchone()[0] == 2
        history = db._conn.execute("SELECT COUNT(*) FROM matrix_cell_versions").fetchone()[0]
        assert history == 3  # one initial + two appended

        # The current view holds the latest value per cell.
        current = {(r["row_id"], r["reader_id"]): r for r in repo.bulk_cells()}
        assert current[("dev-a", "ct-1")]["power_json"]["estimated_power_w"] == 55.0
        assert current[("dev-b", "ct-2")]["power_json"]["estimated_power_w"] == 13.0
        # The history keeps the old value and closes it at the next version time.
        cell_history = repo.cell_history(("device", "dev-a"), "ct-1")
        assert [h["power_json"]["estimated_power_w"] for h in cell_history] == [42.0, 55.0]
        assert cell_history[0]["valid_until"] is not None
        assert cell_history[1]["valid_until"] is None
        assert all(h["mapping_version_id"] == "map-1" for h in cell_history)
    finally:
        db.close()


def test_cells_as_of_returns_the_old_value(tmp_path: Path) -> None:
    path = tmp_path / "asof.db"
    _build_schema11(path)
    db = _open(path)
    try:
        repo = db.repos.mapping
        t0 = datetime(2026, 11, 1, tzinfo=UTC)
        t1 = t0 + timedelta(minutes=5)
        between = t0 + timedelta(minutes=2)
        repo.upsert_cells([_cell("dev-a", "ct-1", updated=t0, power_w=42.0)], at=t0)
        repo.upsert_cells([_cell("dev-a", "ct-1", updated=t1, power_w=55.0)], at=t1)

        old = repo.cells_as_of(between)
        assert len(old) == 1
        assert old[0]["power_json"]["estimated_power_w"] == 42.0

        newer = repo.cells_as_of(t1 + timedelta(seconds=1))
        assert len(newer) == 1
        assert newer[0]["power_json"]["estimated_power_w"] == 55.0

        # Before the first version and after an explicitly closed interval: none.
        assert repo.cells_as_of(t0 - timedelta(seconds=1)) == []
    finally:
        db.close()


def test_bulk_cells_does_not_take_the_writer_lock(tmp_path: Path, monkeypatch) -> None:
    """``bulk_cells`` uses a reader connection, not the writer RLock."""
    path = tmp_path / "lock.db"
    _build_schema11(path)
    db = _open(path)
    try:
        repo = db.repos.mapping
        t0 = datetime(2026, 11, 1, tzinfo=UTC)
        repo.upsert_cells([_cell("dev-a", "ct-1", updated=t0, power_w=42.0)], at=t0)

        original_lock = db._lock

        class _NoLock:
            def __enter__(self) -> None:
                raise AssertionError("bulk_cells must not acquire the writer lock")

            def __exit__(self, *args: object) -> None:
                pass

        monkeypatch.setattr(db, "_lock", _NoLock())
        rows = repo.bulk_cells({"reader_id": "ct-1"})
        assert len(rows) == 1
        assert rows[0]["row_id"] == "dev-a"
        # Filter keys that are not real columns are ignored, never injected.
        assert len(repo.bulk_cells({"nonexistent_column": "x"})) == 1
        monkeypatch.setattr(db, "_lock", original_lock)
    finally:
        db.close()


def test_repository_is_reached_by_module_name(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    _build_schema11(path)
    db = _open(path)
    try:
        assert hasattr(db.repos.mapping, "mapping_edges")
        assert hasattr(db.repos.mapping, "upsert_cells")
        assert "mapping" in db.repos.loaded()
    finally:
        db.close()


def test_mapping_edges_query_filters_by_subject_and_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "query.db"
    conn = _build_schema11(path)
    _seed(conn)
    db = _open(path)
    try:
        repo = db.repos.mapping
        version = repo.active_mapping_version_id()
        assert version == "map-1"
        for_dev_a = repo.mapping_edges(version, subject_id="dev-a")
        assert {(r["subject_id"], r["endpoint_id"]) for r in for_dev_a} == {
            ("dev-a", "ct-1"), ("dev-a", "ct-2"), ("dev-a", "ct-3")
        }
        for_ct2 = repo.mapping_edges(version, endpoint_id="ct-2")
        assert {r["subject_id"] for r in for_ct2} == {"dev-a", "dev-b"}
        assert repo.mapping_edges("map-999") == []
    finally:
        db.close()
