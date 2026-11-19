"""Migration 20: the versioned derived namespace (L7).

Historical reconciliation replays immutable evidence under an explicit
``(model set, topology version, mapping version)`` triple and writes its outputs
into a *separate* namespace: ``derived_runs`` and ``derived_estimates``.  It
never writes measurements, evidence or Home Assistant long-term statistics
(DEC-35).  A replay is additive: a new run supersedes the previous run for the
same subject/range but the old rows are kept, so both results stay retrievable.

The derived tables are intentionally independent of the live inference tables.
No foreign key points at a raw sample: raw samples may be pruned (HB §15) while
the retained immutable evidence still drives a reproducible replay.

Both the DDL and the ``apply`` hook are idempotent, which is what the atomic
migration wrapper needs when it is interrupted between the DDL and the
``user_version`` bump.
"""

from __future__ import annotations

import sqlite3

from . import Migration, add_column_if_missing

#: Storage tier of every table this migration creates (plan §5).  Derived
#: reconciliation outputs are versioned artefacts (L2), like inference records.
TIERS = {
    "derived_runs": "L2",
    "derived_estimates": "L2",
}


def _add_version_tags(conn: sqlite3.Connection) -> None:
    """Tag live aggregate/run rows with the model set / topology they used.

    This is metadata-only (constant NULL default), so existing rows keep their
    values and stay readable; a tagged row records the explicit version under
    which it was (re)computed (F7: "energy buckets and device runs get
    ``model_set_id``/``topology_version`` columns").
    """
    for table in ("device_runs", "energy_aggregates", "power_aggregates"):
        add_column_if_missing(conn, table, "model_set_id", "TEXT")
        add_column_if_missing(conn, table, "topology_version_id", "TEXT")


MIGRATION = Migration(
    version=20,
    name="derived_runs",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS derived_runs (
            run_id TEXT PRIMARY KEY,
            triple_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            superseded_by TEXT,
            chunks INTEGER NOT NULL DEFAULT 1,
            fingerprint TEXT NOT NULL
        )
        """,
        # "what changed" reads always filter by reason and time window; the
        # explicit index keeps supersession lookups bounded on a large history.
        """
        CREATE INDEX IF NOT EXISTS idx_derived_runs_reason
        ON derived_runs(reason, started_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS derived_estimates (
            run_id TEXT NOT NULL,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            point_w REAL,
            lower_w REAL,
            upper_w REAL,
            status TEXT NOT NULL,
            identifiability TEXT NOT NULL,
            group_id TEXT,
            model_version_id TEXT,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            versions_json TEXT NOT NULL,
            PRIMARY KEY (run_id, subject_kind, subject_id),
            FOREIGN KEY (run_id) REFERENCES derived_runs(run_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_derived_estimates_run
        ON derived_estimates(run_id)
        """,
    ),
    apply=_add_version_tags,
)


__all__ = ["MIGRATION", "TIERS"]
