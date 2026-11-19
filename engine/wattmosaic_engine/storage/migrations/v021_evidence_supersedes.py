"""Migration 21 (D5): persist the ``supersedes`` link between evidence revisions.

P4 keeps the supersession link only in the in-memory ``EvidenceBatch`` wrapper,
because the K02 ``EventEvidence`` contract has no metadata field (and forbids
extra fields).  A late sample after a window was FINAL creates a *new* immutable
evidence record whose ``metadata[SUPERSEDES_KEY]`` names the old id; the old
record is never edited.  Until now that link was lost on write, so retention
could not tell that an expired-but-referenced row was still needed for replay.

This migration adds one nullable column to ``event_evidence``:

``supersedes_evidence_id``
    the evidence id this immutable revision supersedes, or ``NULL``.  A plain
    optional string, not a foreign key: the superseded row may legitimately be
    older than the retention horizon, and the link is provenance rather than a
    cascading constraint (DEC-30, HB §6.5).  The repository writes the value
    from the batch metadata and never edits an existing row.

The DDL uses :func:`add_column_if_missing`, so applying twice is a true no-op --
which is what the atomic migration wrapper relies on if it is interrupted after
the ``ALTER`` but before ``user_version`` advances.  No table is created here,
so this migration declares no tiers of its own; ``event_evidence`` is already
classified L2 by migration 16.
"""

from __future__ import annotations

import sqlite3

from . import Migration, add_column_if_missing

#: Column added to ``event_evidence``.  Nullable; existing rows get ``NULL``.
SUPERSEDES_COLUMN = "supersedes_evidence_id"


def _apply(conn: sqlite3.Connection) -> None:
    add_column_if_missing(
        conn, "event_evidence", SUPERSEDES_COLUMN, "TEXT"
    )


MIGRATION = Migration(
    version=21,
    name="v021_evidence_supersedes",
    statements=(),
    apply=_apply,
)

#: No new tables -- migration 16 already classified ``event_evidence`` as L2.
TIERS: dict[str, str] = {}


__all__ = ["MIGRATION", "SUPERSEDES_COLUMN", "TIERS"]
