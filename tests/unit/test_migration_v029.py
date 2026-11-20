"""Focused tests for migration 29 (dynamic topology, D1).

The migration rewrites topology ``confirmed`` lifecycles to ``structural``,
seeds decaying evidence weights from the lifetime counters, and deletes the old
sticky structure-decision cache.  It must be idempotent and must leave mapping
edges untouched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from wattmosaic_engine.storage.migrations import LATEST_VERSION, apply_migrations
from wattmosaic_engine.storage.migrations.v029_dynamic_topology import (
    EVIDENCE_WINDOW,
    MIGRATION,
)

_NOW = "2026-01-01T00:00:00+00:00"


def _db(path: Path) -> sqlite3.Connection:
    """A real database at LATEST so all tables exist, then rewind to v28."""
    from wattmosaic_engine.storage import Database

    db = Database(path)
    conn = db._conn  # noqa: SLF001
    conn.execute("PRAGMA user_version = 28")
    conn.commit()
    return conn


def _add_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    lifecycle: str,
    evidence: dict[str, object],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO topology_edges("
        "edge_id, relation, source_endpoint_id, target_endpoint_id, lifecycle, "
        "declared_by, evidence_json, valid_from, pinned) "
        "VALUES(?, 'contains', 'parent', 'child', ?, 'inference', ?, ?, 0)",
        (edge_id, lifecycle, json.dumps(evidence), _NOW),
    )


def test_v029_declares_version_and_tiers() -> None:
    from wattmosaic_engine.storage.migrations.v029_dynamic_topology import TIERS

    assert MIGRATION.version == 29
    assert MIGRATION.name == "v029_dynamic_topology"
    assert TIERS == {}
    assert LATEST_VERSION >= 29


def test_confirmed_becomes_structural_and_evidence_is_weighted(tmp_path: Path) -> None:
    conn = _db(tmp_path / "v029.db")
    try:
        # Decisive count at or below the window: weights equal the counts.
        _add_edge(
            conn,
            "e-small",
            "confirmed",
            {"support": 8, "conflict": 1, "parent_only": 5, "independent_causes": 3},
        )
        # Decisive count above the window: both scaled by window / n.
        _add_edge(
            conn,
            "e-large",
            "confirmed",
            {"support": 300, "conflict": 100, "parent_only": 2, "independent_causes": 3},
        )
        conn.execute(
            "INSERT INTO engine_state(key, value, updated_at) VALUES(?, ?, ?)",
            ("t5_structure_decisions", '{"child": "parent"}', _NOW),
        )
        conn.commit()

        assert apply_migrations(conn) == LATEST_VERSION

        rows = {
            row[0]: row[1] for row in conn.execute("SELECT edge_id, lifecycle FROM topology_edges")
        }
        assert rows["e-small"] == "structural"
        assert rows["e-large"] == "structural"

        small = json.loads(
            conn.execute(
                "SELECT evidence_json FROM topology_edges WHERE edge_id = 'e-small'"
            ).fetchone()[0]
        )
        assert small["support_w"] == pytest.approx(8.0)
        assert small["conflict_w"] == pytest.approx(1.0)
        assert small["support"] == 8  # lifetime counters untouched

        large = json.loads(
            conn.execute(
                "SELECT evidence_json FROM topology_edges WHERE edge_id = 'e-large'"
            ).fetchone()[0]
        )
        n = 400
        assert large["support_w"] == pytest.approx(300 * EVIDENCE_WINDOW / n)
        assert large["conflict_w"] == pytest.approx(100 * EVIDENCE_WINDOW / n)

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM engine_state WHERE key = 't5_structure_decisions'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_v029_is_idempotent_and_leaves_weighted_rows_alone(tmp_path: Path) -> None:
    conn = _db(tmp_path / "v029-idem.db")
    try:
        existing = {
            "support": 5,
            "conflict": 2,
            "support_w": 4.25,
            "conflict_w": 1.5,
            "parent_only": 1,
        }
        _add_edge(conn, "e-weighted", "structural", existing)
        conn.commit()
        MIGRATION.apply(conn)  # noqa: SLF001
        MIGRATION.apply(conn)  # noqa: SLF001

        row = conn.execute(
            "SELECT lifecycle, evidence_json FROM topology_edges WHERE edge_id = 'e-weighted'"
        ).fetchone()
        assert row[0] == "structural"
        evidence = json.loads(row[1])
        assert evidence["support_w"] == 4.25
        assert evidence["conflict_w"] == 1.5
    finally:
        conn.close()


def test_v029_leaves_mapping_edges_untouched(tmp_path: Path) -> None:
    conn = _db(tmp_path / "v029-map.db")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO mapping_edges("
            "mapping_edge_id, subject_kind, subject_id, endpoint_id, observability, "
            "lifecycle, declared_by, evidence_json, valid_from) "
            "VALUES('m1', 'device', 'dev', 'reader', 'direct', 'confirmed', "
            "'user', '{}', ?)",
            (_NOW,),
        )
        conn.commit()
        MIGRATION.apply(conn)  # noqa: SLF001
        # mapping_edges lifecycle is still the mapping-level 'confirmed'.
        row = conn.execute(
            "SELECT lifecycle FROM mapping_edges WHERE mapping_edge_id = 'm1'"
        ).fetchone()
        assert row[0] == "confirmed"
    finally:
        conn.close()
