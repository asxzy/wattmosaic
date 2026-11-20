"""Migration 29: dynamic topology (contract 4.0.0, D1).

Topology edges no longer have a terminal CONFIRMED state: ``confirmed`` is
renamed ``structural``, meaning only that the observation operator H uses the
edge right now.  Mapping edges are untouched (their confirmation is a separate
concept).

This data-only migration:

* rewrites ``topology_edges.lifecycle`` from ``confirmed`` to ``structural``;
* seeds ``EdgeEvidence.support_w`` / ``conflict_w`` in every edge's
  ``evidence_json`` from the lifetime counts, decaying by verdict count when
  the decisive count exceeds the evidence window (20) -- the same weight the
  D2 fold maintains.  Rows already carrying ``support_w`` are left alone, which
  keeps the migration idempotent;
* deletes the persisted sticky structure-decision cache (``t5_structure_decisions``
  in ``engine_state``), which D2 stops using as a lifecycle source.

No table is created; ``mapping_edges`` is untouched; no auto-committing
statement is used.  Re-running is a no-op.
"""

from __future__ import annotations

import json
import sqlite3

from . import Migration

#: The steady-state ceiling of the D2 decay (EVIDENCE_WINDOW = 1/(1-0.95)).
EVIDENCE_WINDOW = 20
_STRUCTURE_KEY = "t5_structure_decisions"


def _apply(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "topology_edges" in tables:
        # 1. confirmed -> structural on topology rows only.
        conn.execute(
            "UPDATE topology_edges SET lifecycle = 'structural' WHERE lifecycle = 'confirmed'"
        )
        # 2. Seed the decaying evidence weights.  Rows already carrying
        #    support_w are left untouched (idempotent re-runs and newer writes).
        rows = conn.execute(
            "SELECT edge_id, evidence_json FROM topology_edges"
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for edge_id, raw in rows:
            try:
                evidence = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence, dict) or "support_w" in evidence:
                continue
            try:
                support = float(evidence.get("support", 0) or 0)
                conflict = float(evidence.get("conflict", 0) or 0)
            except (TypeError, ValueError):
                continue
            n = support + conflict
            if n > EVIDENCE_WINDOW:
                scale = EVIDENCE_WINDOW / n
                support_w = support * scale
                conflict_w = conflict * scale
            else:
                support_w, conflict_w = support, conflict
            evidence["support_w"] = support_w
            evidence["conflict_w"] = conflict_w
            updates.append(
                (json.dumps(evidence, separators=(",", ":")), str(edge_id))
            )
        conn.executemany(
            "UPDATE topology_edges SET evidence_json = ? WHERE edge_id = ?", updates
        )
    # 3. Drop the persisted sticky structure-decision cache.  The runtime may
    #    re-create it as a pure cache in D2, but no lifecycle will read it.
    if "engine_state" in tables:
        conn.execute("DELETE FROM engine_state WHERE key = ?", (_STRUCTURE_KEY,))


MIGRATION = Migration(
    version=29,
    name="v029_dynamic_topology",
    statements=(),
    apply=_apply,
)

#: Data-only migration: no table is created.
TIERS: dict[str, str] = {}

__all__ = ["MIGRATION", "TIERS", "EVIDENCE_WINDOW"]
