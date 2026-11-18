"""Migration v014: typed topology edges, versions and configuration events (S14).

The fixture is a genuine pre-migration database built with raw ``sqlite3``:
migrations 1-13 are applied, pointer columns and ``topology_relations`` /
``topology_versions`` rows are seeded, and ``user_version`` is left at 13.
``Database.__init__`` migrates immediately, so it cannot produce a pre-migration
fixture; the repository is reached through ``db.repos.topology_v3``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    ChangeDetection,
    ChangeStatus,
    ConfigurationChangeKind,
    ConfigurationEvent,
    EdgeEvidence,
    EdgeLifecycleV3,
    RelationType,
    TopologyEdgeV3,
    TopologyVersionV3,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations.v014_topology import (
    MIGRATION as V014,
)
from wattmosaic_engine.storage.migrations.v014_topology import (
    TIERS,
)

PRE_MIGRATION_VERSION = 13
NOW = datetime(2026, 11, 1, tzinfo=UTC)
TS = "2026-11-01T00:00:00+00:00"


def _build_pre_migration_db(path: Path) -> sqlite3.Connection:
    """Apply migrations 1-13, seed pointer + relation fixtures, stamp version 13."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for migration in MIGRATIONS:
            if migration.version > PRE_MIGRATION_VERSION:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")

        # Channels.  c-nest is a confirmed child of c-parent; c-provisional is
        # an unconfirmed child; c-paired/c-partner form a leg pair; c-alias is an
        # alias of c-parent.
        channels = [
            ("c-parent", None, None, None, "confirmed"),
            ("c-nest", "c-parent", None, None, "confirmed"),
            ("c-provisional", "c-parent", None, None, "candidate"),
            ("c-paired", None, "c-partner", None, "confirmed"),
            ("c-partner", None, "c-paired", None, "confirmed"),
            ("c-alias", None, None, "c-parent", "candidate"),
        ]
        for ct_id, parent, paired, alias, status in channels:
            conn.execute(
                """INSERT INTO ct_channels(
                    ct_id, entity_id, ct_type, parent_ct_id, paired_ct_id, alias_of_ct_id,
                    topology_status, updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (ct_id, f"sensor.{ct_id}", "circuit", parent, paired, alias, status, TS),
            )

        # Inferred relations: a confirmed parent-child (child = subject),
        # a candidate parent-child, a measurement overlap, and a mapping relation
        # which S14 must ignore (S15 takes it).
        relations = [
            ("r-confirm", "electrical_parent_child", "c-child-a", "c-parent-a", "confirmed", "inference"),
            ("r-candidate", "electrical_parent_child", "c-child-b", "c-parent-a", "candidate", "inference"),
            ("r-overlap", "ct_measurement_overlap", "c-x", "c-y", "confirmed", "inference"),
            ("r-mapping", "device_ct_association", "dev-1", "c-x", "confirmed", "user"),
        ]
        for rid, kind, subject, obj, status, declared in relations:
            conn.execute(
                """INSERT INTO topology_relations(
                    relation_id, kind, subject_id, object_id, status, declared_by,
                    evidence_json, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (rid, kind, subject, obj, status, declared,
                 json.dumps({"support": 3}), TS),
            )

        # Two legacy snapshots -> new version number is 3.
        conn.execute(
            "INSERT INTO topology_versions(version, created_at, status, payload_json) "
            "VALUES(2, ?, 'active', ?)",
            (TS, "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    return conn


def _open(path: Path) -> Database:
    db = Database(path)
    assert db.schema_version == LATEST_VERSION
    return db


# --------------------------------------------------------------------------- #
# Migration contract, backfill, idempotency and atomicity
# --------------------------------------------------------------------------- #


def test_module_declares_the_reserved_version_and_tiers() -> None:
    assert V014.version == 14
    assert TIERS == {
        "topology_edges": "L4",
        "topology_versions_v3": "L4",
        "configuration_events": "L4",
    }


def test_pointer_fixtures_produce_the_expected_edges(tmp_path: Path) -> None:
    path = tmp_path / "pointers.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        rows = db._conn.execute(
            "SELECT relation, source_endpoint_id, target_endpoint_id, lifecycle, declared_by "
            "FROM topology_edges WHERE declared_by = 'migration' ORDER BY relation, edge_id"
        ).fetchall()
        by_key = {(r["source_endpoint_id"], r["target_endpoint_id"], r["relation"]): r for r in rows}

        # parent_ct_id -> CONTAINS; confirmed child -> CONFIRMED, unconfirmed -> PROVISIONAL
        nested = by_key[("c-parent", "c-nest", "contains")]
        assert nested["lifecycle"] == "confirmed"
        provisional = by_key[("c-parent", "c-provisional", "contains")]
        assert provisional["lifecycle"] == "provisional"

        # paired_ct_id -> one PAIRED_LEG per unordered pair
        paired = [r for r in rows if r["relation"] == "paired_leg"]
        assert len(paired) == 1
        assert {paired[0]["source_endpoint_id"], paired[0]["target_endpoint_id"]} == {
            "c-paired", "c-partner"
        }

        # alias_of_ct_id -> SAME_CONDUCTOR
        assert ("c-parent", "c-alias", "same_conductor") in by_key
    finally:
        db.close()


def test_relations_backfill_preserves_lifecycle_and_kind_mapping(tmp_path: Path) -> None:
    """Lifecycle survives the round-trip through SQLite (the current bug)."""
    path = tmp_path / "relations.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        rows = db._conn.execute(
            "SELECT edge_id, relation, source_endpoint_id, target_endpoint_id, lifecycle, "
            "evidence_json FROM topology_edges WHERE declared_by = 'inference' ORDER BY edge_id"
        ).fetchall()
        by_source = {r["source_endpoint_id"]: r for r in rows}

        # ELECTRICAL_PARENT_CHILD -> CONTAINS with parent(source) -> child(target)
        confirmed = by_source["c-parent-a"]
        assert confirmed["relation"] == "contains"
        assert confirmed["target_endpoint_id"] == "c-child-a"
        assert confirmed["lifecycle"] == "confirmed"
        both = [r for r in rows if r["source_endpoint_id"] == "c-parent-a"]
        assert {r["lifecycle"] for r in both} == {"confirmed", "candidate"}
        assert {r["target_endpoint_id"] for r in both} == {"c-child-a", "c-child-b"}

        # CT_MEASUREMENT_OVERLAP -> CORRELATED; never containment.
        overlap = next(r for r in rows if r["relation"] == "correlated")
        assert {overlap["source_endpoint_id"], overlap["target_endpoint_id"]} == {"c-x", "c-y"}
        assert "support" in json.loads(overlap["evidence_json"])

        # DEVICE_CT_ASSOCIATION is not topology: S15 takes it.
        assert all(r["relation"] != "device_ct_association" for r in rows)
    finally:
        db.close()


def test_lifecycle_survives_a_reopen(tmp_path: Path) -> None:
    """Reopening the database must not downgrade a CONFIRMED edge."""
    path = tmp_path / "reopen.db"
    _build_pre_migration_db(path)
    db = _open(path)
    db.close()

    second = _open(path)
    try:
        row = second._conn.execute(
            "SELECT lifecycle FROM topology_edges WHERE relation = 'contains' "
            "AND target_endpoint_id = 'c-child-a'"
        ).fetchone()
        assert row["lifecycle"] == "confirmed"
    finally:
        second.close()


def test_version_is_topo_n_with_legacy_snapshot_metadata(tmp_path: Path) -> None:
    path = tmp_path / "version.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        row = db._conn.execute(
            "SELECT version_id, number, parent_version_id, status, metadata_json, edge_ids_json "
            "FROM topology_versions_v3"
        ).fetchone()
        assert row["version_id"] == "topo-3"  # max legacy (2) + 1
        assert row["number"] == 3
        assert row["parent_version_id"] is None
        assert row["status"] == "active"
        assert json.loads(row["metadata_json"])["legacy_snapshot_version"] == 2
        edge_ids = json.loads(row["edge_ids_json"])
        count = db._conn.execute("SELECT COUNT(*) FROM topology_edges").fetchone()[0]
        assert len(edge_ids) == count
    finally:
        db.close()


def test_counts_and_tiers(tmp_path: Path) -> None:
    path = tmp_path / "counts.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        # 3 pointer contains? No: two CONTAINS (nest, provisional) + paired + alias
        # = 4 pointer edges; + 3 topology relations (2 parent-child, 1 overlap).
        count = db._conn.execute("SELECT COUNT(*) FROM topology_edges").fetchone()[0]
        assert count == 7
        assert db._conn.execute("SELECT COUNT(*) FROM topology_versions_v3").fetchone()[0] == 1
        assert db._conn.execute("SELECT COUNT(*) FROM configuration_events").fetchone()[0] == 0
    finally:
        db.close()


def test_apply_callable_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    _build_pre_migration_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        # Apply the DDL, then run the backfill callback twice directly.
        for statement in V014.statements:
            conn.execute(statement)
        assert V014.apply is not None
        V014.apply(conn)
        conn.commit()
        first = conn.execute(
            "SELECT edge_id, lifecycle, relation FROM topology_edges ORDER BY edge_id"
        ).fetchall()
        versions_first = conn.execute(
            "SELECT version_id, edge_ids_json, metadata_json FROM topology_versions_v3"
        ).fetchall()
        V014.apply(conn)
        conn.commit()
        second = conn.execute(
            "SELECT edge_id, lifecycle, relation FROM topology_edges ORDER BY edge_id"
        ).fetchall()
        versions_second = conn.execute(
            "SELECT version_id, edge_ids_json, metadata_json FROM topology_versions_v3"
        ).fetchall()
        assert [dict(r) for r in first] == [dict(r) for r in second]
        assert [dict(r) for r in versions_first] == [dict(r) for r in versions_second]
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failure.db"
    _build_pre_migration_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected failure mid-apply")

        # The registered migration's apply closure belongs to the discovery-loaded
        # module object; patch its globals so the failure happens inside the real
        # apply_migrations transaction.
        registered = next(m for m in MIGRATIONS if m.version == 14)
        assert registered.apply is not None
        monkeypatch.setitem(registered.apply.__globals__, "_backfill_version", boom)
        with pytest.raises(RuntimeError, match="injected failure"):
            apply_migrations(conn)

        assert current_version(conn) == PRE_MIGRATION_VERSION
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "topology_edges" not in tables
        assert "topology_versions_v3" not in tables
        assert "configuration_events" not in tables
        # Restore the real helper before the clean reopen below.
        monkeypatch.undo()
    finally:
        conn.close()

    db = _open(path)
    try:
        assert db.schema_version == LATEST_VERSION
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


def _edge(edge_id: str, relation: RelationType, lifecycle: EdgeLifecycleV3) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=relation,
        source_endpoint_id="c-parent",
        target_endpoint_id="c-child-" + edge_id,
        lifecycle=lifecycle,
        declared_by="inference",
        evidence=EdgeEvidence(support=2),
        valid_from=NOW,
        pinned=False,
    )


def test_repository_edges_lifecycles_and_history(tmp_path: Path) -> None:
    path = tmp_path / "repo_edges.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        repo = db.repos.topology_v3
        repo.upsert_edge(_edge("e-confirm", RelationType.CONTAINS, EdgeLifecycleV3.CONFIRMED))
        repo.upsert_edge(_edge("e-candidate", RelationType.CONTAINS, EdgeLifecycleV3.CANDIDATE))

        all_edges = repo.edges()
        ids = {e["edge_id"] for e in all_edges}
        assert {"e-confirm", "e-candidate"} <= ids

        confirmed = repo.edges(lifecycles=[EdgeLifecycleV3.CONFIRMED])
        assert {e["edge_id"] for e in confirmed} >= {"e-confirm"}

        history = repo.edge_history("e-confirm")
        assert len(history) == 1
        assert history[0]["lifecycle"] == "confirmed"
        assert history[0]["evidence_json"]
    finally:
        db.close()


def test_repository_version_and_set_active(tmp_path: Path) -> None:
    path = tmp_path / "repo_version.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        repo = db.repos.topology_v3
        version = TopologyVersionV3(
            version_id="topo-4",
            number=4,
            parent_version_id=None,
            edge_ids=[],
            status="candidate",
            change_event_id=None,
            created_at=NOW,
            digest="deadbeef",
        )
        repo.insert_version(version)
        assert repo.active_version()["version_id"] == "topo-3"

        repo.set_active("topo-4")
        active = repo.active_version()
        assert active["version_id"] == "topo-4"
        assert active["parent_version_id"] == "topo-3"
        versions = {v["version_id"]: v for v in repo.versions()}
        assert versions["topo-3"]["status"] == "superseded"
        assert versions["topo-4"]["status"] == "active"
        # Prior version payload preserved (append-only).
        assert versions["topo-3"]["digest"] != "deadbeef"
    finally:
        db.close()


def test_repository_configuration_events(tmp_path: Path) -> None:
    path = tmp_path / "repo_events.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        repo = db.repos.topology_v3
        event = ConfigurationEvent(
            event_id="evt-1",
            kind=ConfigurationChangeKind.SMART_PLUG_RELOCATION,
            detection=ChangeDetection.DECLARED,
            status=ChangeStatus.SUSPECTED,
            affected_endpoint_ids=["c-parent"],
            evidence_refs=[],
            created_at=NOW,
        )
        repo.append_configuration_event(event)
        events = repo.events(status=ChangeStatus.SUSPECTED)
        assert len(events) == 1
        assert events[0]["event_id"] == "evt-1"
        assert events[0]["affected_endpoint_ids"] == ["c-parent"]
        assert repo.events(status=ChangeStatus.CONFIRMED) == []
    finally:
        db.close()


def test_migration_is_part_of_the_discovered_registry() -> None:
    assert any(m.version == V014.version and m.name == V014.name for m in MIGRATIONS)
