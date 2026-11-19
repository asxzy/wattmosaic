"""Migration 25 (Z1/ZC): archive the legacy tables read-only.

DEC-02 keeps legacy tables for one release rather than dropping them.  The
released S13-S19 migrations copied their contents into the canonical v3 tables
(ids preserved), so no runtime code path reads or writes the originals any more;
they are kept only as a rollback record.

``MIGRATION.apply`` renames each still-present legacy table to ``<name>_legacy``
(``ALTER TABLE ... RENAME TO ...``) and installs ``BEFORE INSERT/UPDATE/DELETE``
triggers that ``RAISE(ABORT)``, so the archived rows are immutable.  Nothing is
ever dropped.  Migrations 1-24 (and their backfills, which read these tables
under their original names) run *before* this one, so an upgrade from any
released schema still sees the tables it expects.  Applying the archive twice
is a no-op: a table that is already renamed is skipped.
"""

from __future__ import annotations

import sqlite3

from . import Migration

#: Legacy tables archived by the cutover (``interfaces.md`` §10 notes).
LEGACY_TABLES: tuple[str, ...] = (
    "ct_channels",
    "devices",
    "topology_versions",
    "topology_relations",
    "device_model_versions",
    "device_models",
    "learning_samples",
    "residual_power",
    "device_events",
    "ha_entity_bindings",
)

#: The storage tier each archived table keeps (it is the same data, renamed).
LEGACY_TABLE_TIERS: dict[str, str] = {
    "ct_channels": "L4",
    "devices": "L4",
    "topology_versions": "L4",
    "topology_relations": "L4",
    "device_model_versions": "L4",
    "device_models": "L4",
    "learning_samples": "L4",
    "residual_power": "L4",
    "device_events": "L2",
    "ha_entity_bindings": "L4",
}

#: Suffix applied to each archived table.
LEGACY_SUFFIX = "_legacy"


def archived_name(table: str) -> str:
    """The ``*_legacy`` name a table is renamed to."""
    return f"{table}{LEGACY_SUFFIX}"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def archive_legacy_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Rename every still-present legacy table and add read-only triggers.

    Returns the names of the tables that were actually archived (so a caller can
    report what changed).  A table that is already ``*_legacy``, or whose target
    name already exists, is skipped -- the operation is idempotent.  No data is
    deleted and no table is dropped.
    """
    archived: list[str] = []
    for table in LEGACY_TABLES:
        target = archived_name(table)
        if not _table_exists(conn, table) or _table_exists(conn, target):
            continue
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{target}"')
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"wm_legacy_readonly_{target}_{operation.lower()}"
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS \"{trigger}\" "
                f"BEFORE {operation} ON \"{target}\" "
                f"BEGIN SELECT RAISE(ABORT, 'legacy table is read-only'); END"
            )
        archived.append(table)
    return tuple(archived)


def _apply(conn: sqlite3.Connection) -> None:
    archive_legacy_tables(conn)


MIGRATION = Migration(
    version=25,
    name="v025_legacy_readonly",
    statements=(),
    apply=_apply,
)

#: The archived tables, classified in the tier their data always had.
TIERS: dict[str, str] = {
    archived_name(table): tier for table, tier in LEGACY_TABLE_TIERS.items()
}


__all__ = [
    "LEGACY_SUFFIX",
    "LEGACY_TABLE_TIERS",
    "LEGACY_TABLES",
    "MIGRATION",
    "TIERS",
    "archive_legacy_tables",
    "archived_name",
]
