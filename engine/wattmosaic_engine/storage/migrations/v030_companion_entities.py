"""Migration 30: companion entities of a measurement endpoint (ADR-0044).

An endpoint config revision may declare the HA entity ids of the monitor's
voltage, current and power-factor quantities.  The integration resolves those
entity ids on the same HA device and sends them with the endpoint declaration;
the engine never reads HA's registries (ADR-0028 is unchanged).

Three nullable columns are added to ``endpoint_config_revisions``.  The DDL is
metadata-only in SQLite and each column is added idempotently, so re-running the
migration is a no-op and existing rows read back ``None`` (absent, never a
zero -- HB §18).  No other table is touched.
"""

from __future__ import annotations

import sqlite3

from . import Migration, add_column_if_missing

_COMPANION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("voltage_entity_id", "TEXT"),
    ("current_entity_id", "TEXT"),
    ("power_factor_entity_id", "TEXT"),
)


def _apply(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "endpoint_config_revisions" not in tables:
        return
    for name, declaration in _COMPANION_COLUMNS:
        add_column_if_missing(conn, "endpoint_config_revisions", name, declaration)


MIGRATION = Migration(
    version=30,
    name="v030_companion_entities",
    statements=(),
    apply=_apply,
)

#: Altering an existing table is the whole migration; no new tier.
TIERS: dict[str, str] = {}

__all__ = ["MIGRATION", "TIERS", "COMPANION_COLUMNS"]

#: Public so a repository/consumer can enumerate the declared columns.
COMPANION_COLUMNS = tuple(name for name, _ in _COMPANION_COLUMNS)
