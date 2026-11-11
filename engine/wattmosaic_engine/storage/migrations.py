"""Versioned SQLite schema migrations.

Each migration is a list of SQL statements applied in order.  ``schema_version``
lives in ``PRAGMA user_version`` so a partially applied migration rolls back.

Never edit an already-released migration; add a new one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...] = ()
    #: Optional step run after ``statements`` inside the same transaction.
    #: Needed only where the SQL is not idempotent on its own -- SQLite has no
    #: ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, and a migration must still
    #: no-op on a database that already carries the column (a database created
    #: by a build whose ``CREATE TABLE`` included it, or a migration that was
    #: interrupted after the ``ALTER`` but before ``user_version`` advanced).
    apply: Callable[[sqlite3.Connection], None] | None = None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``; empty when the table does not exist."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    """``ALTER TABLE ... ADD COLUMN`` only when the column is absent.

    ``ALTER TABLE ADD COLUMN`` in SQLite is metadata-only: it does not rewrite
    the table, so this is cheap on a production database with ~12k rows in
    ``reconciliation_results``.  Existing rows get NULL.
    """
    if column in table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _add_residual_split_columns(conn: sqlite3.Connection) -> None:
    """Persist the ``Reconciliation`` fields storage silently dropped.

    Contract 1.4.0 added ``unmonitored_w`` / ``meter_disagreement_w`` and the
    table was never extended, so ``insert_reconciliation`` dropped them and
    ``latest_reconciliation`` read nothing back: the engine computed the split
    correctly in memory (its own ``explanation`` names the disagreement) while
    every API read path served the model's ``0.0`` defaults.

    Three older fields have the **same** defect and are added here too, because
    a partial fix would leave the next audit to rediscover it:

    * ``configured_prior_w`` — contract 1.1.0.  It is part of the accounting
      (``accounted = direct + inferred + model + prior``), so a row read back
      without it under-states accounted power.
    * ``measurement_uncertainty_w`` — contract 1.2.0, the fit uncertainty.
    * ``topology_confirmed`` — contract 1.3.0.  The HA integration publishes it
      to say whether the balance came from a *confirmed* topology or a
      ``candidate`` hypothesis.  Dropped on write, it read as the ``False``
      default forever.

    Contract 1.5.0 added ``meter_spread_w`` / ``meter_spread_exceeds_residual``
    and they are added **here, inside version 7**, rather than as a version 8.
    Schema 7 has never been deployed: the reference database is still at
    ``user_version = 5``, and both new columns belong to the same logical change
    as the two split columns above (the 1.4.0 read path served a split that did
    not add up; 1.5.0 fixes the arithmetic and needs the honest spread beside
    it).  A version 8 would ship a schema 7 that no reader ever wants and force
    a second migration pass on every install for one release cycle.  The helper
    is idempotent, so a database that was already migrated to the 1.4.0 shape
    picks up just the two new columns on reopen.

    All are nullable and **without** a default: a row written before schema 7
    genuinely never had an answer, and NULL records that.  Readers resolve NULL
    to the model default, so old rows stay valid.
    """
    add_column_if_missing(conn, "reconciliation_results", "unmonitored_w", "REAL")
    add_column_if_missing(conn, "reconciliation_results", "meter_disagreement_w", "REAL")
    add_column_if_missing(conn, "reconciliation_results", "meter_spread_w", "REAL")
    add_column_if_missing(
        conn, "reconciliation_results", "meter_spread_exceeds_residual", "INTEGER"
    )
    add_column_if_missing(conn, "reconciliation_results", "configured_prior_w", "REAL")
    add_column_if_missing(conn, "reconciliation_results", "measurement_uncertainty_w", "REAL")
    add_column_if_missing(conn, "reconciliation_results", "topology_confirmed", "INTEGER")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS engine_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ct_channels (
                ct_id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL UNIQUE,
                name TEXT,
                ct_type TEXT NOT NULL DEFAULT 'unknown',
                phase TEXT NOT NULL DEFAULT 'unknown',
                parent_ct_id TEXT REFERENCES ct_channels(ct_id),
                polarity TEXT NOT NULL DEFAULT 'normal',
                device_id TEXT,
                paired_ct_id TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                resolution_w REAL,
                expected_interval_s REAL,
                reliability REAL NOT NULL DEFAULT 1.0,
                topology_status TEXT NOT NULL DEFAULT 'candidate',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT,
                dedicated_ct_ids_json TEXT NOT NULL DEFAULT '[]',
                branch_ct_ids_json TEXT NOT NULL DEFAULT '[]',
                phase TEXT NOT NULL DEFAULT 'unknown',
                double_pole INTEGER NOT NULL DEFAULT 0,
                parent_device_id TEXT,
                topology_status TEXT NOT NULL DEFAULT 'candidate',
                enabled INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS topology_versions (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                note TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ct_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ct_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                value_w REAL NOT NULL,
                raw_value REAL NOT NULL,
                unit TEXT NOT NULL,
                state_timestamp TEXT NOT NULL,
                received_at TEXT NOT NULL,
                physical_timestamp TEXT,
                quality TEXT NOT NULL,
                repeated INTEGER NOT NULL DEFAULT 0,
                sequence INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ct_samples_ct_time ON ct_samples(ct_id, received_at)",
            "CREATE INDEX IF NOT EXISTS idx_ct_samples_time ON ct_samples(received_at)",
            """
            CREATE TABLE IF NOT EXISTS device_events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                previous_state TEXT,
                new_state TEXT,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_events_device_time ON device_events(device_id, occurred_at)",
            "CREATE INDEX IF NOT EXISTS idx_device_events_time ON device_events(occurred_at)",
            """
            CREATE TABLE IF NOT EXISTS device_state_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                state TEXT NOT NULL,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                last_changed TEXT,
                last_updated TEXT,
                received_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_state_device_time ON device_state_samples(device_id, received_at)",
            """
            CREATE TABLE IF NOT EXISTS inference_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                power_w REAL NOT NULL,
                energy_kwh REAL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL,
                model_id TEXT,
                model_version INTEGER,
                operating_stage TEXT,
                stage_status TEXT,
                sample_count INTEGER NOT NULL DEFAULT 0,
                quality_status TEXT NOT NULL,
                measurement_age_seconds REAL NOT NULL DEFAULT 0,
                supporting_ct_channels_json TEXT NOT NULL DEFAULT '[]',
                explanation_json TEXT NOT NULL DEFAULT '[]',
                provenance_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_inference_device_time ON inference_results(device_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_inference_time ON inference_results(timestamp)",
            """
            CREATE TABLE IF NOT EXISTS learning_samples (
                sample_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                duration_s REAL NOT NULL DEFAULT 0,
                baseline_power_w REAL NOT NULL DEFAULT 0,
                event_power_w REAL NOT NULL DEFAULT 0,
                delta_power_w REAL NOT NULL DEFAULT 0,
                control_value REAL,
                control_variable TEXT,
                supporting_ct_ids_json TEXT NOT NULL DEFAULT '[]',
                quality_score REAL NOT NULL DEFAULT 0,
                accepted INTEGER NOT NULL DEFAULT 1,
                rejection_reason TEXT,
                notes_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_learning_device_time ON learning_samples(device_id, captured_at)",
            """
            CREATE TABLE IF NOT EXISTS device_models (
                model_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                model_type TEXT NOT NULL,
                active_version INTEGER,
                frozen INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (model_id, device_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS device_model_versions (
                device_id TEXT NOT NULL,
                model_type TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                trained_from TEXT,
                trained_to TEXT,
                sample_count INTEGER NOT NULL DEFAULT 0,
                training_quality REAL NOT NULL DEFAULT 0,
                validation_metrics_json TEXT NOT NULL DEFAULT '{}',
                parameters_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL NOT NULL DEFAULT 0,
                notes_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (device_id, model_type, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS device_runs (
                run_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                energy_kwh REAL NOT NULL DEFAULT 0,
                peak_power_w REAL NOT NULL DEFAULT 0,
                mean_power_w REAL NOT NULL DEFAULT 0,
                stages_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'inferred'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_device_runs_device_time ON device_runs(device_id, started_at)",
            """
            CREATE TABLE IF NOT EXISTS operating_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                run_id TEXT,
                stage_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                power_mean_w REAL,
                energy_kwh REAL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_stages_device_time ON operating_stages(device_id, started_at)",
            """
            CREATE TABLE IF NOT EXISTS power_aggregates (
                device_id TEXT NOT NULL,
                bucket_start TEXT NOT NULL,
                bucket_s INTEGER NOT NULL,
                mean_power_w REAL NOT NULL,
                max_power_w REAL NOT NULL,
                min_power_w REAL NOT NULL,
                sample_count INTEGER NOT NULL,
                PRIMARY KEY (device_id, bucket_start, bucket_s)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS energy_aggregates (
                device_id TEXT NOT NULL,
                bucket_start TEXT NOT NULL,
                bucket_s INTEGER NOT NULL,
                energy_kwh REAL NOT NULL,
                PRIMARY KEY (device_id, bucket_start, bucket_s)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reconciliation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                topology_version INTEGER NOT NULL,
                main_power_w REAL NOT NULL,
                l1_power_w REAL,
                l2_power_w REAL,
                direct_measured_w REAL NOT NULL,
                inferred_w REAL NOT NULL,
                model_estimated_w REAL NOT NULL,
                unknown_residual_w REAL NOT NULL,
                reconciliation_error_w REAL NOT NULL,
                reconciliation_error_ratio REAL NOT NULL,
                accounted_fraction REAL NOT NULL,
                quality_status TEXT NOT NULL,
                explanation_json TEXT NOT NULL DEFAULT '[]'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_reconciliation_time ON reconciliation_results(timestamp)",
            """
            CREATE TABLE IF NOT EXISTS data_quality_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                channel_id TEXT,
                severity TEXT NOT NULL,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_dq_time ON data_quality_events(timestamp)",
            """
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_system_events_time ON system_events(timestamp)",
        ),
    ),
    Migration(
        version=2,
        name="aggregation_indexes_and_metadata",
        statements=(
            "CREATE INDEX IF NOT EXISTS idx_energy_agg_time ON energy_aggregates(bucket_start)",
            "CREATE INDEX IF NOT EXISTS idx_power_agg_time ON power_aggregates(bucket_start)",
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            "INSERT OR IGNORE INTO schema_metadata(key, value) VALUES ('contract_version', '1.0.0')",
        ),
    ),
    Migration(
        version=3,
        name="device_flags",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS device_flags (
                device_id TEXT PRIMARY KEY,
                frozen INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=4,
        name="device_configurations",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS device_configurations (
                device_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=5,
        name="inference_confidence_tier",
        statements=(
            "ALTER TABLE inference_results ADD COLUMN confidence_tier TEXT NOT NULL DEFAULT 'tier_0_unknown'",
            "ALTER TABLE inference_results ADD COLUMN monitoring_level TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE inference_results ADD COLUMN power_behavior TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE inference_results ADD COLUMN tier_reasons_json TEXT NOT NULL DEFAULT '[]'",
            "CREATE INDEX IF NOT EXISTS idx_inference_tier ON inference_results(confidence_tier)",
        ),
    ),
    Migration(
        version=6,
        name="ct_channel_alias_and_multiplier",
        statements=(
            # ``CTChannel`` has carried ``alias_of_ct_id`` and
            # ``measurement_multiplier`` since contract 1.2.0, but
            # ``ct_channels`` had no columns for them.  ``save_topology`` writes
            # an explicit column list, so both fields were silently dropped on
            # every write and ``load_topology`` rebuilt channels without them:
            # a user's alias declaration never survived the round trip, and the
            # circuit measured twice stayed in the whole-home total.
            "ALTER TABLE ct_channels ADD COLUMN alias_of_ct_id TEXT",
            "ALTER TABLE ct_channels ADD COLUMN measurement_multiplier REAL NOT NULL DEFAULT 1.0",
        ),
    ),
    Migration(
        version=7,
        name="reconciliation_persisted_fields",
        apply=_add_residual_split_columns,
    ),
)

LATEST_VERSION = max(m.version for m in MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations atomically.  Returns the resulting version."""
    start = current_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= start:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in migration.statements:
                conn.execute(stmt)
            if migration.apply is not None:
                migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {migration.version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return current_version(conn)
