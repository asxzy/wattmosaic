"""Migration 16 (S16): reader samples v2, immutable event evidence, event groups
and versioned event inference records.

The four event tables are the storage half of HB §6/§12.  Evidence is immutable
(an ``INSERT`` that collides on ``evidence_id`` is an error, not an update),
groups are immutable *per revision* (``PRIMARY KEY (group_id, revision)``), and
inference records form a revision chain.  All five tables are L2: retention is
handled later by D5.

``ct_samples`` keeps its legacy ``value_w NOT NULL`` column -- SQLite cannot
relax that with ``ALTER TABLE`` and a rebuild is out of scope here (Z1 may
rebuild).  The remaining HB §2.1 fields arrive as nullable columns, added
through :func:`add_column_if_missing` so the migration is safe to re-run on a
database whose ``CREATE TABLE`` already carried them.
"""

from __future__ import annotations

import sqlite3

from . import Migration, add_column_if_missing

#: Every HB §2.1 field that is not already a column on ``ct_samples``.
#:
#: ``voltage_reference_v`` is included even though the task's column list omits
#: it: the K02 :class:`ReaderMeasurement` carries it and step 3 of S16 requires
#: ``insert_readings`` to persist *every* field.  It is nullable and additive.
SAMPLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("active_power_v2_w", "REAL"),
    ("current_rms_a", "REAL"),
    ("voltage_rms_v", "REAL"),
    ("reactive_power_var", "REAL"),
    ("power_factor", "REAL"),
    ("energy_wh", "REAL"),
    ("phase", "TEXT"),
    ("leg", "TEXT"),
    ("measurement_type", "TEXT"),
    ("measurement_window_ms", "INTEGER"),
    ("calibration_version", "TEXT"),
    ("quality_flags_json", "TEXT"),
    ("measurement_basis", "TEXT"),
    ("observed_at", "TEXT"),
    ("voltage_reference_v", "REAL"),
)


def _apply(conn: sqlite3.Connection) -> None:
    """Add the nullable sample columns.  Idempotent by construction."""
    for column, declaration in SAMPLE_COLUMNS:
        add_column_if_missing(conn, "ct_samples", column, declaration)


MIGRATION = Migration(
    version=16,
    name="v016_events",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS event_evidence (
            evidence_id TEXT PRIMARY KEY,
            reader_id TEXT NOT NULL,
            measurement_event_id TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            samples_json TEXT NOT NULL DEFAULT '[]',
            baseline_w REAL,
            sigma_w REAL,
            delta_w REAL NOT NULL,
            quality_flags_json TEXT NOT NULL DEFAULT '[]',
            topology_version_id TEXT NOT NULL,
            finalized INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_event_evidence_reader_time "
        "ON event_evidence(reader_id, window_start)",
        """
        CREATE TABLE IF NOT EXISTS event_groups (
            group_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            sub_events_json TEXT NOT NULL DEFAULT '[]',
            reader_deltas_json TEXT NOT NULL DEFAULT '[]',
            grouping_reasons_json TEXT NOT NULL DEFAULT '[]',
            versions_json TEXT NOT NULL,
            PRIMARY KEY (group_id, revision)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_event_groups_opened ON event_groups(opened_at)",
        "CREATE INDEX IF NOT EXISTS idx_event_groups_status ON event_groups(status, opened_at)",
        """
        CREATE TABLE IF NOT EXISTS event_group_members (
            group_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            delta_w REAL,
            sigma_w REAL,
            reader_id TEXT,
            PRIMARY KEY (group_id, revision, subject_kind, subject_id),
            FOREIGN KEY (group_id, revision) REFERENCES event_groups(group_id, revision)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_event_group_members_subject "
        "ON event_group_members(subject_id, group_id)",
        """
        CREATE TABLE IF NOT EXISTS event_inference_records (
            record_id TEXT PRIMARY KEY,
            event_group_id TEXT NOT NULL,
            interpretation_revision INTEGER NOT NULL,
            previous_record_id TEXT,
            result_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (event_group_id, interpretation_revision)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_event_records_group "
        "ON event_inference_records(event_group_id, interpretation_revision)",
        "CREATE INDEX IF NOT EXISTS idx_event_records_created "
        "ON event_inference_records(created_at)",
        """
        CREATE TABLE IF NOT EXISTS ha_events_v3 (
            event_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            attributes_delta_json TEXT NOT NULL DEFAULT '{}',
            context_id TEXT,
            context_parent_id TEXT,
            context_user_id TEXT,
            linked INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ha_events_v3_entity_time "
        "ON ha_events_v3(entity_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS idx_ha_events_v3_context ON ha_events_v3(context_id)",
    ),
    apply=_apply,
)

#: All tables created here are L2 (immutable evidence / versioned interpretations).
TIERS: dict[str, str] = {
    "event_evidence": "L2",
    "event_groups": "L2",
    "event_group_members": "L2",
    "event_inference_records": "L2",
    "ha_events_v3": "L2",
}


__all__ = ["MIGRATION", "TIERS"]
