"""Focused tests for migration 31 (strip non-contract evidence keys).

Migration 14 wrote ``{"source": "ct_channels.parent_ct_id"}`` (and the paired,
alias and ``topology_relations`` variants) into ``topology_edges.evidence_json``;
migration 15 wrote the mapping-edge equivalents; migration 29 copied the dict
forward with the decay weights added.  The contract forbids extra keys, so such
a row crashed ``topology_view`` at startup (production outage).

v031 removes the keys that are not ``EdgeEvidence`` fields.  It must preserve
every valid key verbatim, leave an already-clean row byte-identical, leave an
unparseable row alone, and be a no-op on a second run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from wattmosaic_engine.contracts import EdgeEvidence
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations.v031_evidence_key_repair import (
    MIGRATION,
    TIERS,
    evidence_fields,
    sanitise_evidence,
)

_NOW = "2026-01-01T00:00:00+00:00"

#: Version v031 repairs *from*: everything through the dynamic-topology rewrite.
_PRE_V031 = 30


def _db_at_v30(path: Path) -> sqlite3.Connection:
    """A real database at schema v30, before v031's data repair."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > _PRE_V031:
            break
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute(f"PRAGMA user_version = {_PRE_V031}")
    conn.commit()
    return conn


def _add_topology_edge(conn: sqlite3.Connection, edge_id: str, evidence_json: str) -> None:
    conn.execute(
        "INSERT INTO topology_edges(edge_id, relation, source_endpoint_id, "
        "target_endpoint_id, lifecycle, declared_by, beta, beta_basis, evidence_json, "
        "valid_from, pinned) VALUES(?, 'contains', 'parent', ?, 'structural', "
        "'migration', 1.0, 'unity', ?, ?, 0)",
        (edge_id, edge_id, evidence_json, _NOW),
    )


def _add_mapping_edge(conn: sqlite3.Connection, edge_id: str, evidence_json: str) -> None:
    conn.execute(
        "INSERT INTO mapping_edges(mapping_edge_id, subject_kind, subject_id, "
        "endpoint_id, observability, lifecycle, declared_by, evidence_json, valid_from) "
        "VALUES(?, 'device', 'dev', 'reader', 'direct', 'confirmed', 'migration', ?, ?)",
        (edge_id, evidence_json, _NOW),
    )


def _evidence(conn: sqlite3.Connection, table: str, key: str, row_id: str) -> str:
    return conn.execute(
        f"SELECT evidence_json FROM {table} WHERE {key} = ?", (row_id,)  # noqa: S608 - literals
    ).fetchone()[0]


def test_v031_declares_version_and_tiers() -> None:
    assert MIGRATION.version == 31
    assert MIGRATION.name == "v031_evidence_key_repair"
    assert TIERS == {}
    assert LATEST_VERSION >= 31


def test_allow_list_comes_from_the_contract_model() -> None:
    """The allow-list is the model's own field set, so it cannot rot."""
    assert evidence_fields() == frozenset(EdgeEvidence.model_fields)
    clean = {"support": 1, "conflict_w": 2.5}
    assert sanitise_evidence(json.dumps(clean), evidence_fields()) == (None, [])


def test_fallback_field_set_matches_the_contract_model() -> None:
    """The offline fallback (used only if the contracts cannot import) is in sync."""
    from wattmosaic_engine.storage.migrations.v031_evidence_key_repair import (
        _FALLBACK_FIELDS,
    )

    assert frozenset(EdgeEvidence.model_fields) == _FALLBACK_FIELDS


def test_sanitiser_reports_dropped_keys_and_keeps_values() -> None:
    raw = json.dumps({"source": "ct_channels.paired_ct_id", "support": 4, "support_w": 1.5})
    cleaned, dropped = sanitise_evidence(raw, evidence_fields())
    assert dropped == ["source"]
    assert json.loads(cleaned) == {"support": 4, "support_w": 1.5}


def test_sanitiser_leaves_unknown_shapes_alone() -> None:
    allowed = evidence_fields()
    assert sanitise_evidence("{not json", allowed) == (None, [])
    assert sanitise_evidence(None, allowed) == (None, [])
    assert sanitise_evidence('["a"]', allowed) == (None, [])


def test_v031_repairs_topology_and_mapping_edges(tmp_path: Path) -> None:
    conn = _db_at_v30(tmp_path / "v031.db")
    try:
        legacy_topology = {
            "e-parent": {"source": "ct_channels.parent_ct_id", "support_w": 0.0, "conflict_w": 0.0},
            "e-paired": {"source": "ct_channels.paired_ct_id", "support_w": 0.0, "conflict_w": 0.0},
            "e-alias": {
                "source": "ct_channels.alias_of_ct_id",
                "support_w": 0.0,
                "conflict_w": 0.0,
            },
            "e-rel": {
                "source": "topology_relations",
                "legacy_kind": "ct_measurement_overlap",
                "support_w": 0.0,
                "conflict_w": 0.0,
            },
        }
        for edge_id, evidence in legacy_topology.items():
            _add_topology_edge(conn, edge_id, json.dumps(evidence))
        clean_topology = '{"support":3,"conflict":1,"support_w":3.0,"conflict_w":1.0}'
        _add_topology_edge(conn, "e-clean", clean_topology)
        _add_topology_edge(conn, "e-unparseable", "{not json")
        _add_mapping_edge(conn, "m-legacy", json.dumps({"source": "branch_ct_ids"}))
        clean_mapping = '{"support":4,"conflict_w":2.0}'
        _add_mapping_edge(conn, "m-clean", clean_mapping)
        conn.commit()

        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == 31

        assert json.loads(_evidence(conn, "topology_edges", "edge_id", "e-parent")) == {
            "support_w": 0.0,
            "conflict_w": 0.0,
        }
        # The legacy kinds go too: neither is an EdgeEvidence field.
        assert json.loads(_evidence(conn, "topology_edges", "edge_id", "e-rel")) == {
            "support_w": 0.0,
            "conflict_w": 0.0,
        }
        assert json.loads(_evidence(conn, "mapping_edges", "mapping_edge_id", "m-legacy")) == {}

        # Clean rows are not rewritten: the stored text is byte-identical.
        assert _evidence(conn, "topology_edges", "edge_id", "e-clean") == clean_topology
        assert _evidence(conn, "mapping_edges", "mapping_edge_id", "m-clean") == clean_mapping
        # An unparseable value is left exactly as v029 leaves it.
        assert _evidence(conn, "topology_edges", "edge_id", "e-unparseable") == "{not json"
    finally:
        conn.close()


def test_v031_is_idempotent(tmp_path: Path) -> None:
    conn = _db_at_v30(tmp_path / "v031-idem.db")
    try:
        _add_topology_edge(
            conn, "e-paired", json.dumps({"source": "ct_channels.paired_ct_id", "support_w": 0.0})
        )
        _add_mapping_edge(conn, "m-legacy", json.dumps({"source": "association"}))
        conn.commit()

        MIGRATION.apply(conn)  # noqa: SLF001
        first = {
            (table, row_id): _evidence(conn, table, key, row_id)
            for table, key, row_id in (
                ("topology_edges", "edge_id", "e-paired"),
                ("mapping_edges", "mapping_edge_id", "m-legacy"),
            )
        }
        MIGRATION.apply(conn)  # noqa: SLF001
        second = {
            (table, row_id): _evidence(conn, table, key, row_id)
            for table, key, row_id in (
                ("topology_edges", "edge_id", "e-paired"),
                ("mapping_edges", "mapping_edge_id", "m-legacy"),
            )
        }
        assert first == second
        assert json.loads(first[("topology_edges", "e-paired")]) == {"support_w": 0.0}
        assert json.loads(first[("mapping_edges", "m-legacy")]) == {}
    finally:
        conn.close()


def test_v031_does_not_add_a_row_to_a_pre_existing_table(tmp_path: Path) -> None:
    """A repair must not change any pre-existing row count (rehearsal gate).

    The diagnostic summary goes to the log, not to ``engine_state``: adding a
    row there would break the "every pre-existing row count survives a startup
    migration" invariant that ``rehearse_migration.py`` enforces.
    """
    conn = _db_at_v30(tmp_path / "v031-rows.db")
    try:
        _add_topology_edge(
            conn, "e-parent", json.dumps({"source": "ct_channels.parent_ct_id"})
        )
        conn.commit()

        def counts() -> dict[str, int]:
            return {
                str(name): int(
                    conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # noqa: S608
                )
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

        before = counts()
        MIGRATION.apply(conn)  # noqa: SLF001
        after = counts()
        assert after == before
    finally:
        conn.close()


def test_database_at_v31_reads_a_repaired_row_through_the_contract(tmp_path: Path) -> None:
    """End to end: the real constructor path repairs, then the row loads."""
    from wattmosaic_engine.versioning.service import _edge_contract

    db = Database(tmp_path / "v031-e2e.db")
    try:
        db._conn.execute(
            "INSERT INTO topology_edges(edge_id, relation, source_endpoint_id, "
            "target_endpoint_id, lifecycle, declared_by, beta, beta_basis, evidence_json, "
            "valid_from, pinned) VALUES('e-paired', 'contains', 'parent', 'e-paired', "
            "'structural', 'migration', 1.0, 'unity', ?, ?, 0)",
            (json.dumps({"source": "ct_channels.paired_ct_id", "support_w": 0.0}), _NOW),
        )
        db._conn.commit()
        MIGRATION.apply(db._conn)  # noqa: SLF001
        row = dict(
            db._conn.execute("SELECT * FROM topology_edges WHERE edge_id='e-paired'").fetchone()
        )
        edge = _edge_contract(row)
        assert edge.evidence.support_w == 0.0
    finally:
        db.close()
