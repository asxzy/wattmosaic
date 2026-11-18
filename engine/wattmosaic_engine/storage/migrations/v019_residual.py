"""Migration 19: per-reader residual series and unknown-load profiles (S19).

DEF-05 (``analysis/C-…`` C5): the existing ``residual_power`` table has no scope
key, so one install-wide window supplies the "baseline" for every circuit.  This
migration adds a per-reader, per-topology-version series keyed by ``(reader_id,
at)`` (HB §9.4 E) and a table for unknown-load profiles (HB §9.4 D).

The legacy scope-less history is **not reinterpreted**.  ``residual_power`` gains
a ``legacy_scope`` column whose existing rows are flagged ``scope_unknown``;
they stay readable forever as un-attributed install-wide observations (§16.6).
A new column with a constant default is a metadata-only ``ALTER TABLE`` in
SQLite, so this is cheap even on a large history.

Both statements and the ``apply`` hook are idempotent: ``CREATE TABLE
IF NOT EXISTS`` and :func:`add_column_if_missing` make a re-run a no-op, which is
what the atomic migration wrapper relies on when it is interrupted between the
DDL and the ``user_version`` bump.
"""

from __future__ import annotations

import sqlite3

from . import Migration, add_column_if_missing

#: Storage tier of every table this migration creates (plan §5).  The
#: tier-completeness test folds these in without editing ``storage/tiers.py``.
TIERS = {
    "reader_residual_series": "L3",
    "unknown_load_profiles": "L4",
}

#: Sentinel written on rows that predate the per-reader series.  Legacy rows are
#: flagged, never re-keyed: a scope-less observation cannot honestly claim a
#: reader or a topology version it was not computed under.
LEGACY_SCOPE = "scope_unknown"


def _add_legacy_scope(conn: sqlite3.Connection) -> None:
    """Flag the old ``residual_power`` rows as ``scope_unknown``.

    ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS`` in SQLite, so the
    shared helper guards it.  Existing rows take the constant default; this is
    metadata-only and rewrites no historical evidence (§11.1).
    """
    add_column_if_missing(
        conn,
        "residual_power",
        "legacy_scope",
        f"TEXT NOT NULL DEFAULT '{LEGACY_SCOPE}'",
    )


MIGRATION = Migration(
    version=19,
    name="residual_and_unknown_profiles",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS reader_residual_series (
            reader_id TEXT NOT NULL,
            at TEXT NOT NULL,
            residual_w REAL,
            baseline_w REAL,
            dynamic_w REAL,
            noise_sigma_w REAL,
            topology_version_id TEXT NOT NULL,
            basis TEXT NOT NULL,
            PRIMARY KEY (reader_id, at)
        )
        """,
        # Time-range reads are the hot path (``series(reader_id, since,
        # until)``); the composite index starts with the equality column so the
        # range on ``at`` can use the rest of the key.
        """
        CREATE INDEX IF NOT EXISTS idx_reader_residual_at
        ON reader_residual_series(reader_id, at)
        """,
        """
        CREATE TABLE IF NOT EXISTS unknown_load_profiles (
            profile_id TEXT PRIMARY KEY,
            scope_endpoint_id TEXT NOT NULL,
            lifecycle TEXT NOT NULL,
            signature_json TEXT NOT NULL DEFAULT '{}',
            event_count INTEGER NOT NULL DEFAULT 0,
            independent_event_count INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_unknown_profile_scope
        ON unknown_load_profiles(scope_endpoint_id, lifecycle)
        """,
    ),
    apply=_add_legacy_scope,
)


__all__ = ["LEGACY_SCOPE", "MIGRATION", "TIERS"]
