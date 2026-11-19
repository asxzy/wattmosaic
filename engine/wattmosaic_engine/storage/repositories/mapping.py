"""Subject↔reader mapping edges and the sparse matrix cell store (S15).

Migration 15 creates four tables: ``mapping_edges`` and ``mapping_versions``
(rule 20 — mapping independent of topology) plus the sparse current view
``matrix_cells`` and its append-only history ``matrix_cell_versions``
(HB §9.8–9.9).  This repository is the only writer of the cell tables: M2 (the
event-driven updater) funnels every change through :meth:`upsert_cells`, which
updates the current row and appends exactly one history row in one transaction.

Reads are split by intent (``analysis/E-…`` T6): :meth:`bulk_cells` streams from a
short-lived ``_reader()`` connection and deliberately never takes the writer
lock, so a large matrix read cannot stall the writer or the health probe.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ...contracts import MappingCell, utcnow
from ..repositories import Repository, enum_value, iso

#: Cell columns shared by ``matrix_cells`` and ``matrix_cell_versions``.
#: Each ``*_json`` block is the model_dump of the corresponding MappingCell
#: block; the scalar columns are indexed filter keys.
_CELL_COLUMNS = (
    "row_kind",
    "row_id",
    "reader_id",
    "load_component_id",
    "mapping_json",
    "power_json",
    "event_association_json",
    "confidence_json",
    "provenance_json",
    "timestamps_json",
    "identifiability",
    "group_id",
    "visual",
    "mapping_status",
    "observability",
    "updated_at",
    "derived",
)

_JSON_BLOCKS = (
    "mapping_json",
    "power_json",
    "event_association_json",
    "confidence_json",
    "provenance_json",
    "timestamps_json",
)

def _dump(value: Any) -> str:
    """JSON-encode a block, tolerating contract models and already-encoded text."""
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value if value is not None else {}, default=str)


def _cell_fields(cell: Any, at: str, mapping_version_id: str) -> dict[str, Any]:
    """Normalise a cell (contract model or dict) to the shared column dict.

    Accepts the model_dump(mode="json") shape of :class:`MappingCell` (nested
    ``mapping``/``power``/... blocks) as well as a flat dict already keyed by the
    column names, so M2 can pass either a freshly built model or a row it read
    back.  Missing optional blocks become ``{}`` — never a zero watt value
    (HB §18).
    """
    if isinstance(cell, MappingCell):
        raw = cell.model_dump(mode="json")
    elif isinstance(cell, dict):
        raw = cell
    else:
        raise TypeError(f"unsupported cell type: {type(cell).__name__}")

    # Flat column dict (already normalised, e.g. read back from the store).
    if "mapping_json" in raw or "power_json" in raw:
        fields = {column: raw.get(column) for column in _CELL_COLUMNS}
    else:
        mapping = raw.get("mapping") or {}
        timestamps = raw.get("timestamps") or {}
        provenance = raw.get("provenance") or {}
        fields = {
            "row_kind": enum_value(raw.get("row_kind")),
            "row_id": str(raw.get("row_id") or ""),
            "reader_id": str(raw.get("reader_id") or ""),
            "load_component_id": raw.get("load_component_id"),
            "mapping_json": _dump(mapping),
            "power_json": _dump(raw.get("power") or {}),
            "event_association_json": _dump(raw.get("event_association") or {}),
            "confidence_json": _dump(raw.get("confidence") or {}),
            "provenance_json": _dump(provenance),
            "timestamps_json": _dump(timestamps),
            "identifiability": enum_value(raw.get("identifiability")),
            "group_id": raw.get("group_id"),
            "visual": enum_value(raw.get("visual")),
            "mapping_status": enum_value(mapping.get("mapping_status")),
            "observability": enum_value(mapping.get("observability")),
            "updated_at": str(timestamps.get("updated_at") or raw.get("updated_at") or at),
            "derived": int(bool(raw.get("derived", False))),
        }

    fields["mapping_version_id"] = str(raw.get("mapping_version_id") or mapping_version_id)
    fields["valid_from"] = str(raw.get("valid_from") or at)
    fields["valid_until"] = raw.get("valid_until")
    # Never trust a foreign column set into the INSERT (SQLite would reject it).
    return fields


def _decode_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in _JSON_BLOCKS:
        if key in out:
            out[key] = json.loads(out[key] or "{}")
    if "derived" in out:
        out["derived"] = bool(out["derived"])
    return out


class MappingRepository(Repository):
    """DAO for mapping edges and the sparse matrix cell store (v015)."""

    # -- mapping edges ----------------------------------------------------- #
    def mapping_edges(
        self,
        version_id: str,
        subject_id: str | None = None,
        endpoint_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """The edges of one mapping version, optionally filtered by endpoint.

        A version stores its edge-id list as JSON (the v3 ``MappingVersion``
        shape).  Filtering by subject/endpoint is done in SQL over the referenced
        edges so callers asking "which readers observe device X?" do not scan the
        whole table.  The result is key-version agnostic: it works for "map-1"
        and for any later version D1 promotes.
        """
        with self._lock:
            version = self._conn.execute(
                "SELECT mapping_edge_ids_json FROM mapping_versions WHERE version_id=?",
                (str(version_id),),
            ).fetchone()
            if version is None:
                return []
            edge_ids = json.loads(version["mapping_edge_ids_json"] or "[]")
            if not edge_ids:
                return []
            placeholders = ",".join("?" for _ in edge_ids)
            query = (
                f"SELECT * FROM mapping_edges WHERE mapping_edge_id IN ({placeholders})"  # noqa: S608 - ids are bound, not interpolated
            )
            params: list[Any] = list(edge_ids)
            if subject_id is not None:
                query += " AND subject_id = ?"
                params.append(str(subject_id))
            if endpoint_id is not None:
                query += " AND endpoint_id = ?"
                params.append(str(endpoint_id))
            query += " ORDER BY mapping_edge_id"
            rows = self._conn.execute(query, params).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
        return out

    def active_mapping_version_id(self) -> str:
        """The active mapping version id, falling back to the backfilled "map-1"."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version_id FROM mapping_versions WHERE status='active' "
                "ORDER BY number DESC LIMIT 1"
            ).fetchone()
        return str(row[0]) if row is not None else "map-1"

    # -- bulk matrix reads (no writer lock) -------------------------------- #
    def bulk_cells(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Read the sparse current view from a read-only connection.

        This is the deliberately lock-free read of ``analysis/E-…`` T6.  Every
        filter key is an equality on a real column; unknown keys are ignored
        rather than injected into SQL, so a caller cannot turn a bulk read into
        an arbitrary query.
        """
        allowed = {
            "row_kind",
            "row_id",
            "reader_id",
            "load_component_id",
            "mapping_status",
            "observability",
            "identifiability",
            "visual",
            "group_id",
            "derived",
        }
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in (filters or {}).items():
            if key not in allowed or value is None:
                continue
            clauses.append(f"{key} = ?")
            params.append(int(value) if key == "derived" else str(value))
        query = "SELECT * FROM matrix_cells"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY row_kind, row_id, reader_id"
        with self._reader() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_decode_row(row) for row in rows]

    # -- transactional cell writes ---------------------------------------- #
    _cell_fields = staticmethod(_cell_fields)

    def upsert_cells(
        self,
        delta: Any,
        *,
        mapping_version_id: str | None = None,
        at: datetime | None = None,
    ) -> int:
        """Upsert current cells and append one history row each, atomically.

        ``delta`` is an iterable of :class:`MappingCell` models (or dicts in the
        same shape).  All writes happen inside one ``BEGIN IMMEDIATE``: the old
        open history row is closed, the current cell is upserted, and a fresh
        history row is appended.  A failure part-way rolls the whole batch back,
        so a half-updated matrix can never be observed.  Returns the number of
        cells written (one history row per cell).
        """
        version_id = str(mapping_version_id or self.active_mapping_version_id())
        stamp = iso(at or utcnow())
        cells = [self._cell_fields(cell, stamp, version_id) for cell in delta]
        if not cells:
            return 0

        columns_sql = ", ".join(_CELL_COLUMNS)
        placeholders = ", ".join("?" for _ in _CELL_COLUMNS)
        updates = ", ".join(f"{name}=excluded.{name}" for name in _CELL_COLUMNS if name not in ("row_kind", "row_id", "reader_id"))
        current_sql = (
            f"INSERT INTO matrix_cells({columns_sql}) VALUES({placeholders}) "
            f"ON CONFLICT(row_kind, row_id, reader_id) DO UPDATE SET {updates}"
        )
        history_columns = (*_CELL_COLUMNS, "mapping_version_id", "valid_from", "valid_until")
        history_sql = (
            f"INSERT INTO matrix_cell_versions({', '.join(history_columns)}) "
            f"VALUES({', '.join('?' for _ in history_columns)})"
        )

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for fields in cells:
                    key = (fields["row_kind"], fields["row_id"], fields["reader_id"])
                    # Close the previous open version so as-of queries see a
                    # single continuous interval per cell.
                    self._conn.execute(
                        "UPDATE matrix_cell_versions SET valid_until=? "
                        "WHERE row_kind=? AND row_id=? AND reader_id=? AND valid_until IS NULL",
                        (stamp, *key),
                    )
                    values = tuple(fields[name] for name in _CELL_COLUMNS)
                    self._conn.execute(current_sql, values)
                    history_values = tuple(
                        fields[name] for name in _CELL_COLUMNS
                    ) + (fields["mapping_version_id"], fields["valid_from"], fields["valid_until"])
                    self._conn.execute(history_sql, history_values)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(cells)

    # -- current-view removal (HB §17 step 10) ----------------------------- #
    def remove_cells(self, keys: Any) -> int:
        """Delete current-view rows for retired cells, keeping their history.

        A promotion retires a mapping edge, so the subject x reader cell it
        justified must no longer appear in the ``/matrix`` payload.  The
        append-only ``matrix_cell_versions`` rows are deliberately untouched, so
        provenance and historical ``at=`` queries still resolve the old values.
        Returns the number of current rows removed.
        """
        removed = 0
        rows = [(str(item[0]), str(item[1]), str(item[2])) for item in keys]
        if not rows:
            return 0
        with self._lock:
            for key in rows:
                cursor = self._conn.execute(
                    "DELETE FROM matrix_cells WHERE row_kind=? AND row_id=? AND reader_id=?",
                    key,
                )
                removed += cursor.rowcount
        return removed

    # -- history / as-of reads -------------------------------------------- #
    def cell_history(
        self,
        row: str | tuple[str, str],
        reader: str,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Append-only history for one cell, oldest first.

        ``row`` is either a ``row_id`` (all row kinds with that id) or an
        explicit ``(row_kind, row_id)`` pair.  ``since`` filters on ``valid_from``.
        """
        clauses: list[str] = ["reader_id = ?"]
        params: list[Any] = [str(reader)]
        if isinstance(row, tuple):
            clauses.append("row_kind = ? AND row_id = ?")
            params.extend((str(row[0]), str(row[1])))
        else:
            clauses.append("row_id = ?")
            params.append(str(row))
        if since is not None:
            clauses.append("valid_from >= ?")
            params.append(iso(since))
        query = (
            "SELECT * FROM matrix_cell_versions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY valid_from ASC, cell_version_id ASC"
        )
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_decode_row(row) for row in rows]

    def cells_as_of(self, t: datetime) -> list[dict[str, Any]]:
        """The value of every cell at time ``t`` (HB §9.9 historical query).

        A version is live at ``t`` when it starts at or before ``t`` and either
        has no end or ends strictly after ``t``.  Where two versions overlap
        (should not happen through :meth:`upsert_cells`, but can be planted), the
        latest ``valid_from`` wins.
        """
        stamp = iso(t)
        query = """
            SELECT * FROM matrix_cell_versions AS v
            WHERE v.valid_from <= ?
              AND (v.valid_until IS NULL OR v.valid_until > ?)
              AND v.cell_version_id = (
                  SELECT MAX(w.cell_version_id) FROM matrix_cell_versions AS w
                  WHERE w.row_kind = v.row_kind AND w.row_id = v.row_id
                    AND w.reader_id = v.reader_id AND w.valid_from <= ?
                    AND (w.valid_until IS NULL OR w.valid_until > ?)
              )
            ORDER BY v.row_kind, v.row_id, v.reader_id
        """
        with self._lock:
            rows = self._conn.execute(query, (stamp, stamp, stamp, stamp)).fetchall()
        return [_decode_row(row) for row in rows]


__all__ = ["MappingRepository"]
