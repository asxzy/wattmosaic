"""Migration v014 — typed topology edges, versions and configuration events (S14).

Today's topology is a set of pointer columns on ``ct_channels`` (``parent_ct_id``,
``paired_ct_id``, ``alias_of_ct_id``) plus four ``topology_relations`` kinds.  Two
things are lost on every reload:

* the **lifecycle** of an inferred relation — the ``topology_relations`` table has
  no ``lifecycle`` column (analysis C, trap T2), so a CONFIRMED edge comes back as
  a candidate;
* the distinction between a parent-child containment and a mere measurement
  overlap — ``CT_MEASUREMENT_OVERLAP`` is used for any two CTs sharing a device.

This migration creates the v3 store (HB §4, DEC-10/DEC-11):

* ``topology_edges`` — every :class:`TopologyEdgeV3` field plus ``evidence_json``;
* ``topology_versions_v3`` — an append-only version of an edge set, with the
  legacy snapshot number carried as metadata;
* ``configuration_events`` — declared/inferred change events (HB §14), empty on
  backfill because the legacy schema has no equivalent table.

Backfill rules (task S14 §2):

* ``parent_ct_id`` → ``CONTAINS`` (``declared_by=migration``), lifecycle
  ``CONFIRMED`` when the *child* channel's ``topology_status`` was user-confirmed,
  otherwise ``PROVISIONAL`` (a declared parent is a prior, not a single-event
  confirmation — HB §4.3 applies to inferred evidence);
* ``paired_ct_id`` → one ``PAIRED_LEG`` per unordered pair;
* ``alias_of_ct_id`` → ``SAME_CONDUCTOR``;
* ``topology_relations`` of kind ``ELECTRICAL_PARENT_CHILD`` → ``CONTAINS`` with
  its recorded status mapped; ``CT_MEASUREMENT_OVERLAP`` → ``CORRELATED`` (never
  containment — HB §4.2); the mapping kinds (``DEVICE_CT_ASSOCIATION``,
  ``CIRCUIT_MEMBERSHIP``) are left for S15.

Both the DDL and the ``apply`` hook are idempotent: ``CREATE TABLE IF NOT EXISTS``
and ``INSERT OR IGNORE`` (keyed by a deterministic edge id) make a re-run a true
no-op, which the atomic migration wrapper relies on when it is interrupted
between the DDL and the ``user_version`` bump.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import Migration, table_columns

#: Storage tier of every table created here (plan §5, interfaces §10).
TIERS: dict[str, str] = {
    "topology_edges": "L4",
    "topology_versions_v3": "L4",
    "configuration_events": "L4",
}

#: Stable instant used when a source row has no usable timestamp.  A constant
#: (rather than ``utcnow()``) keeps backfill deterministic across re-runs.
_FALLBACK_TIME = "2026-01-01T00:00:00+00:00"

# Relation / lifecycle / declared-by values are the string values of
# ``RelationType``, ``EdgeLifecycleV3`` and the v3 ``declared_by`` literal.  They
# are written as plain strings so a migration module never imports the contract
# package (discovery loads these files before the application facade exists).
_CONTAINS = "contains"
_PAIRED_LEG = "paired_leg"
_SAME_CONDUCTOR = "same_conductor"
_CORRELATED = "correlated"

_CONFIRMED = "confirmed"
_PROVISIONAL = "provisional"
_CANDIDATE = "candidate"
_REJECTED = "rejected"

_RELATION_ELECTRICAL_PARENT_CHILD = "electrical_parent_child"
_RELATION_CT_MEASUREMENT_OVERLAP = "ct_measurement_overlap"
# Mapping relations belong to S15, not to topology.
_MAPPING_RELATIONS = frozenset({"device_ct_association", "circuit_membership"})

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS topology_edges (
        edge_id TEXT PRIMARY KEY,
        relation TEXT NOT NULL,
        source_endpoint_id TEXT NOT NULL,
        target_endpoint_id TEXT NOT NULL,
        lifecycle TEXT NOT NULL,
        declared_by TEXT NOT NULL,
        beta REAL,
        beta_basis TEXT,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        valid_from TEXT NOT NULL,
        valid_until TEXT,
        pinned INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_topology_edges_relation "
    "ON topology_edges(relation, lifecycle)",
    "CREATE INDEX IF NOT EXISTS idx_topology_edges_source "
    "ON topology_edges(source_endpoint_id)",
    "CREATE INDEX IF NOT EXISTS idx_topology_edges_target "
    "ON topology_edges(target_endpoint_id)",
    """
    CREATE TABLE IF NOT EXISTS topology_versions_v3 (
        version_id TEXT PRIMARY KEY,
        number INTEGER NOT NULL UNIQUE,
        parent_version_id TEXT,
        edge_ids_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        change_event_id TEXT,
        created_at TEXT NOT NULL,
        digest TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_topology_versions_number "
    "ON topology_versions_v3(number)",
    "CREATE INDEX IF NOT EXISTS idx_topology_versions_status "
    "ON topology_versions_v3(status)",
    """
    CREATE TABLE IF NOT EXISTS configuration_events (
        event_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        detection TEXT NOT NULL,
        status TEXT NOT NULL,
        affected_endpoint_ids_json TEXT NOT NULL DEFAULT '[]',
        affected_edge_ids_json TEXT NOT NULL DEFAULT '[]',
        affected_mapping_edge_ids_json TEXT NOT NULL DEFAULT '[]',
        candidate_topology_version_id TEXT,
        candidate_mapping_version_id TEXT,
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_configuration_events_status "
    "ON configuration_events(status, created_at)",
)


# --------------------------------------------------------------------------- #
# Tolerant helpers
# --------------------------------------------------------------------------- #


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timestamp(value: Any) -> str:
    text = _text(value)
    if text is None:
        return _FALLBACK_TIME
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _FALLBACK_TIME
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if parsed is not None else default
        except (ValueError, TypeError):
            return default
    return default


def _edge_id(relation: str, source: str, target: str) -> str:
    """Deterministic id of an edge, so backfill and re-runs collapse to one row."""
    raw = f"{relation}|{source}|{target}"
    return "te-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _lifecycle_from_status(status: Any) -> str:
    """Map the legacy ``topology_relations.status`` to an ``EdgeLifecycleV3``."""
    return {
        _CONFIRMED: _CONFIRMED,
        _CANDIDATE: _CANDIDATE,
        _REJECTED: _REJECTED,
    }.get(_text(status) or _CANDIDATE, _CANDIDATE)


def _declared_by(value: Any) -> str:
    """Coerce the legacy ``declared_by`` to the v3 literal set."""
    text = _text(value)
    if text in {"user", "migration"}:
        return text
    # 'inference', 'discovery', 'default' and anything else are engine assertions.
    return "inference"


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    relation: str,
    source: str,
    target: str,
    lifecycle: str,
    declared_by: str,
    valid_from: str,
    evidence: dict[str, Any],
    valid_until: str | None = None,
    beta: float | None = None,
    beta_basis: str | None = None,
) -> str:
    """Insert one edge (ignoring a duplicate id) and return its deterministic id."""
    edge_id = _edge_id(relation, source, target)
    conn.execute(
        """INSERT OR IGNORE INTO topology_edges(
            edge_id, relation, source_endpoint_id, target_endpoint_id, lifecycle,
            declared_by, beta, beta_basis, evidence_json, valid_from, valid_until, pinned)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            edge_id,
            relation,
            source,
            target,
            lifecycle,
            declared_by,
            beta,
            beta_basis,
            json.dumps(evidence, sort_keys=True, default=str),
            valid_from,
            valid_until,
            0,
        ),
    )
    return edge_id


# --------------------------------------------------------------------------- #
# Pointer backfill (ct_channels)
# --------------------------------------------------------------------------- #


def _backfill_pointers(conn: sqlite3.Connection) -> None:
    """Turn the three CT pointer columns into typed, lifecycle-bearing edges."""
    if not _table_exists(conn, "ct_channels"):
        return
    columns = table_columns(conn, "ct_channels")
    if "parent_ct_id" not in columns and "paired_ct_id" not in columns and "alias_of_ct_id" not in columns:
        return
    selects = ["ct_id", "topology_status"]
    optional = ("parent_ct_id", "paired_ct_id", "alias_of_ct_id", "updated_at")
    for column in optional:
        if column in columns:
            selects.append(column)
    rows = conn.execute(
        f"SELECT {', '.join(selects)} FROM ct_channels ORDER BY ct_id"
    ).fetchall()
    # Resolve names to positional indices for the optional columns.
    index = {name: position for position, name in enumerate(selects)}

    def value(row: Any, column: str) -> Any:
        position = index.get(column)
        return row[position] if position is not None else None

    paired_seen: set[tuple[str, str]] = set()
    for row in rows:
        ct_id = _text(row[0])
        if ct_id is None:
            continue
        # A user-confirmed *child* pointer is a declared fact; any other status
        # stays PROVISIONAL until measurement corroborates it.
        child_lifecycle = (
            _CONFIRMED if (_text(row[1]) or _CANDIDATE) == _CONFIRMED else _PROVISIONAL
        )
        valid_from = _timestamp(value(row, "updated_at"))

        parent = _text(value(row, "parent_ct_id"))
        if parent and parent != ct_id:
            _insert_edge(
                conn,
                relation=_CONTAINS,
                source=parent,
                target=ct_id,
                lifecycle=child_lifecycle,
                declared_by="migration",
                valid_from=valid_from,
                evidence={"source": "ct_channels.parent_ct_id"},
            )

        partner = _text(value(row, "paired_ct_id"))
        if partner and partner != ct_id:
            pair = tuple(sorted((ct_id, partner)))
            if pair not in paired_seen:
                paired_seen.add(pair)
                _insert_edge(
                    conn,
                    relation=_PAIRED_LEG,
                    source=pair[0],
                    target=pair[1],
                    lifecycle=child_lifecycle,
                    declared_by="migration",
                    valid_from=valid_from,
                    evidence={"source": "ct_channels.paired_ct_id"},
                )

        alias = _text(value(row, "alias_of_ct_id"))
        if alias and alias != ct_id:
            _insert_edge(
                conn,
                relation=_SAME_CONDUCTOR,
                source=alias,
                target=ct_id,
                lifecycle=child_lifecycle,
                declared_by="migration",
                valid_from=valid_from,
                evidence={"source": "ct_channels.alias_of_ct_id"},
            )


# --------------------------------------------------------------------------- #
# Relation backfill (topology_relations)
# --------------------------------------------------------------------------- #


def _backfill_relations(conn: sqlite3.Connection) -> None:
    """Map the two topology kinds of ``topology_relations`` into v3 edges."""
    if not _table_exists(conn, "topology_relations"):
        return
    # ``lifecycle`` is intentionally absent from this legacy table (trap T2);
    # the ``status`` column is the only recorded establishedness we can carry.
    rows = conn.execute(
        "SELECT relation_id, kind, subject_id, object_id, status, declared_by, "
        "evidence_json, valid_from, created_at FROM topology_relations "
        "ORDER BY relation_id"
    ).fetchall()
    for row in rows:
        kind = _text(row[1]) or ""
        if kind in _MAPPING_RELATIONS:
            continue  # S15 owns device/ circuit membership
        if kind == _RELATION_ELECTRICAL_PARENT_CHILD:
            relation = _CONTAINS
            # Legacy: subject = child, object = parent (audit.py synthesised it
            # that way).  v3 CONTAINS is parent -> child.
            source = _text(row[3])  # object_id (parent)
            target = _text(row[2])  # subject_id (child)
        elif kind == _RELATION_CT_MEASUREMENT_OVERLAP:
            relation = _CORRELATED
            source = _text(row[2])
            target = _text(row[3])
        else:
            continue
        if source is None or target is None or source == target:
            continue
        evidence = _json(row[6], {})
        if not isinstance(evidence, dict):
            evidence = {"legacy_evidence": evidence}
        evidence = {**evidence, "source": "topology_relations", "legacy_kind": kind}
        _insert_edge(
            conn,
            relation=relation,
            source=source,
            target=target,
            lifecycle=_lifecycle_from_status(row[4]),
            declared_by=_declared_by(row[5]),
            valid_from=_timestamp(row[7] if row[7] is not None else row[8]),
            evidence=evidence,
        )


# --------------------------------------------------------------------------- #
# Version backfill
# --------------------------------------------------------------------------- #


def _backfill_version(conn: sqlite3.Connection) -> None:
    """Create ``topo-<n>`` where n = max(legacy version) + 1."""
    latest: tuple[int, Any] | None = None
    if _table_exists(conn, "topology_versions"):
        row = conn.execute(
            "SELECT version, created_at FROM topology_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            latest = (int(row[0]), row[1])

    number = (latest[0] + 1) if latest is not None else 1
    version_id = f"topo-{number}"
    edge_ids = [
        str(row[0])
        for row in conn.execute("SELECT edge_id FROM topology_edges ORDER BY edge_id").fetchall()
    ]
    digest_payload = json.dumps(edge_ids, separators=(",", ":"))
    digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    metadata: dict[str, Any] = {
        "legacy_snapshot_version": latest[0] if latest is not None else None
    }
    created_at = _timestamp(latest[1]) if latest is not None else _FALLBACK_TIME
    conn.execute(
        """INSERT OR IGNORE INTO topology_versions_v3(
            version_id, number, parent_version_id, edge_ids_json, status, change_event_id,
            created_at, digest, metadata_json)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            version_id,
            number,
            None,
            json.dumps(edge_ids, separators=(",", ":")),
            "active",
            None,
            created_at,
            digest,
            json.dumps(metadata, sort_keys=True),
        ),
    )


def _apply(conn: sqlite3.Connection) -> None:
    """Backfill inside the migration transaction, edges before the version."""
    _backfill_pointers(conn)
    _backfill_relations(conn)
    _backfill_version(conn)


MIGRATION = Migration(
    version=14,
    name="topology",
    statements=_STATEMENTS,
    apply=_apply,
)


__all__ = ["MIGRATION", "TIERS"]
