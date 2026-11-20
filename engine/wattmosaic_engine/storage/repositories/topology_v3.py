"""Repository for the v3 topology tables created by migration 14.

Reached as ``db.repos.topology_v3``.  Methods accept the contract models from
:mod:`wattmosaic_engine.contracts` (``TopologyEdgeV3``,
``TopologyVersionV3`` and ``ConfigurationEvent``; the v3 names carry a ``V3``
suffix where they collide with the legacy surface) or plain dicts, and always
return plain dicts keyed by column name — callers instantiate the contract
models themselves.

Bulk reads use ``_reader()`` so a large edge scan does not block ``/health``;
writes take ``self._lock`` and run in ``BEGIN IMMEDIATE``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..repositories import Repository, as_utc, enum_value, iso

_EDGE_COLUMNS = (
    "edge_id", "relation", "source_endpoint_id", "target_endpoint_id", "lifecycle",
    "declared_by", "beta", "beta_basis", "evidence_json", "valid_from", "valid_until", "pinned",
)
_VERSION_COLUMNS = (
    "version_id", "number", "parent_version_id", "edge_ids_json", "status",
    "change_event_id", "created_at", "digest", "metadata_json",
)
_EVENT_COLUMNS = (
    "event_id", "kind", "detection", "status", "affected_endpoint_ids_json",
    "affected_edge_ids_json", "affected_mapping_edge_ids_json",
    "candidate_topology_version_id", "candidate_mapping_version_id",
    "evidence_refs_json", "created_at", "resolved_at",
)


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _model_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
    return dict(vars(value))


def _time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return iso(as_utc(value))
    return iso(value)


def _json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or [], separators=(",", ":"), default=str)


def _decode_edges(row: dict[str, Any]) -> dict[str, Any]:
    row["edge_ids"] = json.loads(row.pop("edge_ids_json") or "[]")
    return row


def _decode_metadata(row: dict[str, Any]) -> dict[str, Any]:
    row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
    return row


def _decode_event(row: dict[str, Any]) -> dict[str, Any]:
    row["affected_endpoint_ids"] = json.loads(row.pop("affected_endpoint_ids_json") or "[]")
    row["affected_edge_ids"] = json.loads(row.pop("affected_edge_ids_json") or "[]")
    row["affected_mapping_edge_ids"] = json.loads(row.pop("affected_mapping_edge_ids_json") or "[]")
    row["evidence_refs"] = json.loads(row.pop("evidence_refs_json") or "[]")
    return row


class TopologyV3Repository(Repository):
    """CRUD over the migration-14 topology edge, version and event tables."""

    # -- edges -------------------------------------------------------------- #

    def edges(
        self, version_id: str | None = None, lifecycles: Any = None
    ) -> list[dict[str, Any]]:
        """Edges of a version, optionally filtered by lifecycle.

        When ``version_id`` is given, only the ids in that version's edge set are
        returned; otherwise every stored edge is returned.  A ``pinned`` edge is
        included regardless of lifecycle filter (a user pin wins over inference).
        """
        query = f"SELECT {', '.join(_EDGE_COLUMNS)} FROM topology_edges"
        clauses: list[str] = []
        params: list[Any] = []
        if version_id is not None:
            row = self._conn.execute(
                "SELECT edge_ids_json FROM topology_versions_v3 WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            ids = json.loads(row[0]) if row is not None else []
            if not ids:
                return []
            clauses.append(f"edge_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if lifecycles is not None:
            values = [enum_value(item) for item in lifecycles]
            if not values:
                return []
            clauses.append(
                f"(lifecycle IN ({','.join('?' for _ in values)}) OR pinned = 1)"
            )
            params.extend(values)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY edge_id"
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query, tuple(params)).fetchall()]

    def edge_history(self, edge_id: str) -> list[dict[str, Any]]:
        """Every historical row for one edge id (the row is the history here)."""
        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_EDGE_COLUMNS)} FROM topology_edges "
                "WHERE edge_id = ? ORDER BY valid_from",
                (edge_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def upsert_edge(self, edge: Any) -> None:
        """Insert or update one edge from a contract model or dict."""
        data = _model_dict(edge)
        evidence = data.get("evidence", data.get("evidence_json", {}))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO topology_edges(
                        edge_id, relation, source_endpoint_id, target_endpoint_id, lifecycle,
                        declared_by, beta, beta_basis, evidence_json, valid_from, valid_until, pinned)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["edge_id"], enum_value(data["relation"]),
                        data["source_endpoint_id"], data["target_endpoint_id"],
                        enum_value(data["lifecycle"]), enum_value(data["declared_by"]),
                        data.get("beta"), data.get("beta_basis"),
                        _json(evidence), _time(data["valid_from"]),
                        _time(data.get("valid_until")), int(bool(data.get("pinned", False))),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- versions ----------------------------------------------------------- #

    def insert_version(self, version: Any) -> None:
        """Append one immutable topology version (refuse a duplicate id)."""
        data = _model_dict(version)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._insert_version_row(data)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def insert_next_version(self, build: Callable[[int, str | None], Any]) -> dict[str, Any]:
        """Allocate the next version number and append the version, atomically.

        ``build(number, active_version_id)`` returns the version to insert.  The
        number (``MAX(number) + 1``) and the active parent are read inside the
        same ``BEGIN IMMEDIATE`` transaction as the insert, under the writer
        lock, so two concurrent proposers can never be handed the same number
        (read-max-then-insert outside one transaction is a lost-update race).
        Returns the inserted version as a dict.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                number = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(number), 0) + 1 FROM topology_versions_v3"
                    ).fetchone()[0]
                )
                active = self._conn.execute(
                    "SELECT version_id FROM topology_versions_v3 "
                    "WHERE status = 'active' ORDER BY number DESC LIMIT 1"
                ).fetchone()
                data = _model_dict(build(number, str(active[0]) if active is not None else None))
                self._insert_version_row(data)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return data

    def _insert_version_row(self, data: dict[str, Any]) -> None:
        """INSERT one version row; the caller holds the lock and a transaction."""
        edge_ids = data.get("edge_ids", data.get("edge_ids_json", []))
        metadata = data.get("metadata", data.get("metadata_json", {}))
        self._conn.execute(
            """INSERT INTO topology_versions_v3(
                version_id, number, parent_version_id, edge_ids_json, status,
                change_event_id, created_at, digest, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                data["version_id"], int(data["number"]), data.get("parent_version_id"),
                _json(edge_ids), enum_value(data.get("status") or "candidate"),
                data.get("change_event_id"), _time(data["created_at"]),
                data["digest"], _json(metadata),
            ),
        )

    def versions(self) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_VERSION_COLUMNS)} FROM topology_versions_v3 "
                "ORDER BY number"
            ).fetchall()
        return [_decode_metadata(_row(row)) for row in rows]

    def active_version(self) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_VERSION_COLUMNS)} FROM topology_versions_v3 "
                "WHERE status = 'active' ORDER BY number DESC LIMIT 1"
            ).fetchone()
        return _decode_metadata(_row(row)) if row is not None else None

    def set_active(self, version_id: str) -> None:
        """Promote a version to active; supersede every other version, append-only.

        Only the ``status`` column changes (prior versions, their edge sets and
        their digests are preserved verbatim).  The new version's parent link is
        pointed at the previously active version when it has none yet.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                previous = self._conn.execute(
                    "SELECT version_id FROM topology_versions_v3 "
                    "WHERE status = 'active' ORDER BY number DESC LIMIT 1"
                ).fetchone()
                self._conn.execute(
                    "UPDATE topology_versions_v3 SET status = 'superseded' WHERE status = 'active'"
                )
                if previous is not None and previous[0] != version_id:
                    self._conn.execute(
                        "UPDATE topology_versions_v3 SET parent_version_id = ? "
                        "WHERE version_id = ? AND parent_version_id IS NULL",
                        (previous[0], version_id),
                    )
                self._conn.execute(
                    "UPDATE topology_versions_v3 SET status = 'active' WHERE version_id = ?",
                    (version_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- configuration events ---------------------------------------------- #

    def append_configuration_event(self, event: Any) -> None:
        """Append one immutable configuration-change event."""
        data = _model_dict(event)
        refs = data.get("evidence_refs", [])
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO configuration_events(
                        event_id, kind, detection, status, affected_endpoint_ids_json,
                        affected_edge_ids_json, affected_mapping_edge_ids_json,
                        candidate_topology_version_id, candidate_mapping_version_id,
                        evidence_refs_json, created_at, resolved_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["event_id"], enum_value(data["kind"]),
                        enum_value(data["detection"]), enum_value(data["status"]),
                        _json(data.get("affected_endpoint_ids", [])),
                        _json(data.get("affected_edge_ids", [])),
                        _json(data.get("affected_mapping_edge_ids", [])),
                        data.get("candidate_topology_version_id"),
                        data.get("candidate_mapping_version_id"),
                        _json(refs), _time(data["created_at"]),
                        _time(data.get("resolved_at")),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def has_configuration_event(self, event_id: str) -> bool:
        """Whether a configuration event with this id is already stored."""
        with self._reader() as conn:
            row = conn.execute(
                "SELECT 1 FROM configuration_events WHERE event_id = ? LIMIT 1", (str(event_id),)
            ).fetchone()
        return row is not None

    def events(self, status: str | None = None) -> list[dict[str, Any]]:
        """Configuration events, optionally filtered by ``ChangeStatus`` value."""
        query = f"SELECT {', '.join(_EVENT_COLUMNS)} FROM configuration_events"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (enum_value(status),)
        query += " ORDER BY created_at, event_id"
        with self._reader() as conn:
            return [_decode_event(_row(row)) for row in conn.execute(query, params).fetchall()]


__all__ = ["TopologyV3Repository"]
