"""Read-side matrix service (M3, interfaces §9 / HB §19.4).

This module assembles the typed v3 matrix payload and the three drill-downs
from the *materialised* sparse view written by M2.  It is the read-only half of
the matrix: every query goes through a short-lived ``Database._reader()``
connection, so a large matrix read never takes the writer lock and never
blocks ``/healthz`` (``analysis/E-…`` T6).

Design constraints kept here:

* **One bulk query per endpoint, no N+1.**  :meth:`SqliteCellSource.page` reads
  a single paged ``SELECT`` and :meth:`SqliteCellSource.fingerprint` reads one
  compact ``SELECT`` for the ETag.  Filters are pushed into SQL over indexed
  columns; unknown filter keys are ignored so a caller cannot inject SQL.
* **Absent is ``None``, never ``0.0``** (HB §18).  Cells are decoded through the
  K05 contracts, whose null policy is enforced by their validators.
* **No wall clock.**  ``generated_at`` comes from an injected clock; the
  default is a fixed origin, never ``datetime.now()`` in production code.
* **Production depends only on Protocols.**  Nothing here imports
  ``runtime.fakes``; tests inject deterministic doubles.

The :class:`MatrixService` itself is transport-agnostic: the HTTP layer calls
it through the :class:`~wattmosaic_engine.api.protocols.MappingProvider`
Protocol, so no concrete engine type is required.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from wattmosaic_engine.contracts import (
    CellDetail,
    CellEvents,
    CellMapping,
    CellPower,
    CellProvenance,
    CellTimestamps,
    ColumnNode,
    ConfidenceVectorV3,
    MappingCell,
    MatrixPayload,
    ReaderColumnSummary,
    ReaderDetail,
    RowDetail,
    RowKind,
    RowNode,
    VersionTriple,
)

from .cells import stable_json
from .projection import physical_total_from_cells, reader_totals_from_cells

__all__: list[str] = [
    "CellSource",
    "SqliteCellSource",
    "StoreCellSource",
    "MatrixFilters",
    "MatrixService",
    "cells_hash",
    "etag_for",
    "decode_cell",
]

_FIXED_ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)

#: Columns that may be used as SQL equality filters on ``matrix_cells``.  These
#: mirror the S15 table's indexed scalar columns; a key outside this set is
#: dropped before it reaches SQL.
_FILTER_COLUMNS: frozenset[str] = frozenset(
    {
        "reader_id",
        "row_id",
        "row_kind",
        "load_component_id",
        "mapping_status",
        "observability",
        "identifiability",
        "visual",
        "group_id",
        "derived",
    }
)


# --------------------------------------------------------------------------- #
# Cell source: the only place that touches SQLite.
# --------------------------------------------------------------------------- #


@runtime_checkable
class CellSource(Protocol):
    """The read seam the service needs from the materialised view."""

    def fingerprint(self) -> str: ...
    def page(
        self,
        filters: MappingABC[str, Any],
        *,
        page: int,
        page_size: int,
    ) -> list[MappingCell]: ...
    def history(self, row_id: str, reader_id: str, row_kind: str | None = None) -> list[MappingCell]: ...


def decode_cell(row: Any) -> MappingCell:
    """Decode one S15 ``matrix_cells`` / ``matrix_cell_versions`` row.

    The repository stores each nested block as ``model_dump(mode="json")`` text
    and exposes it as a dict after ``json.loads``.  We rebuild the K05 contract,
    dropping keys that belong to a subclass (e.g. ``CellEventsV3``'s
    ``independent_event_count``) so the strict base model accepts them.  Absent
    nested blocks become empty dicts, never zero-valued measurements.
    """

    def get(name: str, default: Any = None) -> Any:
        if isinstance(row, MappingABC):
            return row.get(name, default)
        # ``sqlite3.Row`` (and generic row objects) expose keys but no ``get``.
        keys = getattr(row, "keys", None)
        if callable(keys) and name in keys():
            return row[name]
        return getattr(row, name, default)

    def block(name: str) -> dict[str, Any]:
        value = get(name)
        if value is None:
            return {}
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except (TypeError, ValueError):
                return {}
        return dict(value) if isinstance(value, MappingABC) else {}

    def known(model: type[Any], data: dict[str, Any]) -> dict[str, Any]:
        return {key: data[key] for key in model.model_fields if key in data}

    mapping = block("mapping_json") or block("mapping")
    power = block("power_json") or block("power")
    events = block("event_association_json") or block("event_association")
    confidence = block("confidence_json") or block("confidence")
    provenance = block("provenance_json") or block("provenance")
    timestamps = block("timestamps_json") or block("timestamps")

    def scalar(name: str, default: Any = None) -> Any:
        return get(name, default)

    derived = scalar("derived", False)
    return MappingCell(
        row_kind=str(scalar("row_kind", RowKind.DEVICE.value)),
        row_id=str(scalar("row_id", "")),
        reader_id=str(scalar("reader_id", "")),
        load_component_id=scalar("load_component_id"),
        mapping=CellMapping(**known(CellMapping, mapping)),
        power=CellPower(**known(CellPower, power)),
        identifiability=scalar("identifiability") or "insufficient_evidence",
        group_id=scalar("group_id"),
        event_association=CellEvents(**known(CellEvents, events)),
        confidence=ConfidenceVectorV3(**known(ConfidenceVectorV3, confidence)),
        provenance=CellProvenance(**known(CellProvenance, provenance)),
        timestamps=CellTimestamps(**known(CellTimestamps, timestamps)),
        visual=str(scalar("visual", "unknown")),
        derived=bool(derived),
    )


class SqliteCellSource:
    """Bulk cell reader over a ``Database`` using only its reader connection.

    Every query runs inside ``db._reader()`` (WAL, ``query_only``) so no writer
    lock is taken.  A fresh short-lived connection is opened per call rather
    than shared, which is what keeps concurrent matrix polls off the hot path.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _clauses(filters: MappingABC[str, Any]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in filters.items():
            if key not in _FILTER_COLUMNS or value is None:
                continue
            if key == "derived":
                clauses.append("derived = ?")
                params.append(int(bool(value)))
            else:
                clauses.append(f"{key} = ?")
                params.append(str(value))
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def fingerprint(self) -> str:
        """One compact read: a stable digest of every current cell's identity.

        Reading only short indexed columns keeps this cheap even for a 500×100
        matrix, and the digest changes whenever a cell is upserted (its
        ``updated_at`` / mapping status change), so a contribution update that
        does not bump the mapping version still invalidates the client's ETag.
        Note ``matrix_cells`` has no ``mapping_version_id`` column — that lives
        only on the append-only history table.
        """
        with self.db._reader() as conn:
            rows = conn.execute(
                "SELECT row_kind, row_id, reader_id, mapping_status, visual, updated_at "
                "FROM matrix_cells ORDER BY row_kind, row_id, reader_id"
            ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                "|".join(
                    str(row[name] or "")
                    for name in (
                        "row_kind",
                        "row_id",
                        "reader_id",
                        "mapping_status",
                        "visual",
                        "updated_at",
                    )
                ).encode("utf-8")
            )
            digest.update(b"\n")
        return digest.hexdigest()[:16]

    def page(
        self,
        filters: MappingABC[str, Any],
        *,
        page: int,
        page_size: int,
    ) -> list[MappingCell]:
        where, params = self._clauses(filters)
        offset = (max(1, int(page)) - 1) * max(1, int(page_size))
        query = (
            "SELECT * FROM matrix_cells"
            + where
            + " ORDER BY row_kind, row_id, reader_id LIMIT ? OFFSET ?"
        )
        with self.db._reader() as conn:
            rows = conn.execute(query, (*params, int(page_size), offset)).fetchall()
        return [decode_cell(row) for row in rows]

    def cells_as_of(self, at: datetime) -> list[MappingCell]:
        """Every cell live at ``at`` (HB §9.9) from the append-only history."""
        stamp = at.isoformat()
        with self.db._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM matrix_cell_versions AS v "
                "WHERE v.valid_from <= ? AND (v.valid_until IS NULL OR v.valid_until > ?) "
                "AND v.cell_version_id = ("
                "  SELECT MAX(w.cell_version_id) FROM matrix_cell_versions AS w "
                "  WHERE w.row_kind=v.row_kind AND w.row_id=v.row_id AND w.reader_id=v.reader_id "
                "    AND w.valid_from <= ? AND (w.valid_until IS NULL OR w.valid_until > ?)"
                ") ORDER BY v.row_kind, v.row_id, v.reader_id",
                (stamp, stamp, stamp, stamp),
            ).fetchall()
        return [decode_cell(row) for row in rows]

    def history(
        self, row_id: str, reader_id: str, row_kind: str | None = None
    ) -> list[MappingCell]:
        query = "SELECT * FROM matrix_cell_versions WHERE row_id = ? AND reader_id = ?"
        params: list[Any] = [str(row_id), str(reader_id)]
        if row_kind is not None:
            query += " AND row_kind = ?"
            params.append(str(row_kind))
        query += " ORDER BY valid_from ASC, cell_version_id ASC"
        with self.db._reader() as conn:
            rows = conn.execute(query, params).fetchall()
        return [decode_cell(row) for row in rows]


class StoreCellSource:
    """In-memory reader over M2's :class:`MatrixStore` (no database at all).

    Useful for pure tests and for a deployment that keeps the sparse view in
    memory.  It implements the same :class:`CellSource` seam as the SQL reader.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for cell in sorted(
            self.store.cells(),
            key=lambda c: (str(c.row_kind), c.row_id, c.reader_id),
        ):
            digest.update(stable_json(cell).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()[:16]

    def _current(self) -> list[MappingCell]:
        return list(self.store.cells())

    def page(
        self,
        filters: MappingABC[str, Any],
        *,
        page: int,
        page_size: int,
    ) -> list[MappingCell]:
        cells = self._current()
        for key, value in filters.items():
            if value is None:
                continue
            cells = [cell for cell in cells if _cell_matches(cell, key, value)]
        size = max(1, int(page_size))
        start = (max(1, int(page)) - 1) * size
        return cells[start : start + size]

    def cells_as_of(self, at: datetime) -> list[MappingCell]:
        return list(self.store.cells_as_of(at))

    def history(
        self, row_id: str, reader_id: str, row_kind: str | None = None
    ) -> list[MappingCell]:
        kind = row_kind or "device"
        return list(self.store.history((kind, str(row_id), str(reader_id))))


def _cell_matches(cell: MappingCell, key: str, value: Any) -> bool:
    """Filter predicate mirroring :class:`SqliteCellSource`'s SQL clauses."""
    if key == "reader_id":
        return cell.reader_id == str(value)
    if key == "row_id":
        return cell.row_id == str(value)
    if key == "row_kind":
        return str(cell.row_kind) == str(value)
    if key == "load_component_id":
        return str(cell.load_component_id) == str(value)
    if key == "mapping_status":
        return str(cell.mapping.mapping_status) == str(value)
    if key == "observability":
        return str(cell.mapping.observability) == str(value)
    if key == "identifiability":
        return str(cell.identifiability) == str(value)
    if key == "visual":
        return str(cell.visual) == str(value)
    if key == "group_id":
        return str(cell.group_id) == str(value)
    if key == "derived":
        return bool(cell.derived) == bool(value)
    return True


# --------------------------------------------------------------------------- #
# Query parameters.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MatrixFilters:
    """Validated query parameters for ``GET /matrix`` (interfaces §9)."""

    at: datetime | None = None
    panel: str | None = None
    reader: str | None = None
    room: str | None = None
    row_kind: str | None = None
    mapping_status: str | None = None
    identifiability: str | None = None
    visual: str | None = None
    min_confidence: float | None = None
    active_state: str | None = None
    page: int = 1
    page_size: int = 200

    #: Maximum page size; protects the transport from an unbounded read.
    MAX_PAGE_SIZE = 1000

    @classmethod
    def from_query(cls, query: MappingABC[str, str]) -> MatrixFilters:
        def text(name: str) -> str | None:
            value = query.get(name)
            return value if value else None

        def number(name: str) -> float | None:
            value = query.get(name)
            if value is None or value == "":
                return None
            try:
                out = float(value)
            except ValueError:
                return None
            return out if math.isfinite(out) else None

        def integer(name: str, default: int) -> int:
            value = query.get(name)
            if value is None or value == "":
                return default
            try:
                return max(1, int(float(value)))
            except ValueError:
                return default

        return cls(
            at=_parse_at(text("at")),
            panel=text("panel"),
            reader=text("reader"),
            room=text("room"),
            row_kind=text("row_kind"),
            mapping_status=text("mapping_status"),
            identifiability=text("identifiability"),
            visual=text("visual"),
            min_confidence=number("min_confidence"),
            active_state=text("active_state"),
            page=integer("page", 1),
            page_size=min(integer("page_size", 200), cls.MAX_PAGE_SIZE),
        )

    def sql_filters(self) -> dict[str, Any]:
        """The subset of filters that map directly to ``matrix_cells`` columns."""
        out: dict[str, Any] = {
            "reader_id": self.reader,
            "row_kind": self.row_kind,
            "mapping_status": self.mapping_status,
            "identifiability": self.identifiability,
            "visual": self.visual,
        }
        return {key: value for key, value in out.items() if value is not None}


def _parse_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# ETag helpers.
# --------------------------------------------------------------------------- #


def cells_hash(cells: Sequence[MappingCell]) -> str:
    """Canonical digest of a cell set (stable across processes)."""
    payload = [cell.model_dump(mode="json") for cell in cells]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def etag_for(mapping_version_id: str, fingerprint: str) -> str:
    """``<mapping_version_id>-<cells_hash>`` as required by interfaces §9."""
    return f"{mapping_version_id}-{fingerprint}"


# --------------------------------------------------------------------------- #
# Tree helpers.
# --------------------------------------------------------------------------- #


def _tree_leaves(nodes: Sequence[Any], kind: str) -> list[Any]:
    """Walk a contract tree, returning nodes of ``kind`` in stable order."""
    out: list[Any] = []
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        if str(getattr(node, "kind", "")) == kind:
            out.append(node)
        stack.extend(reversed(list(getattr(node, "children", ()) or ())))
    return out


def _matching_leaf_ids(nodes: Sequence[Any], match: str, kind: str) -> set[str]:
    """Ids under the first tree node whose label or node_id matches ``match``."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        label = str(getattr(node, "label", ""))
        node_id = str(getattr(node, "node_id", ""))
        if match in (label, node_id):
            return {
                str(getattr(leaf, "reader_id" if kind == "reader" else "row_id"))
                for leaf in _tree_leaves([node], kind)
                if getattr(leaf, "reader_id" if kind == "reader" else "row_id")
            }
        stack.extend(list(getattr(node, "children", ()) or ()))
    return set()


def _path_labels(nodes: Sequence[Any], target: str, *, reader: bool) -> list[str]:
    """Labels from the root down to the reader/row leaf named ``target``."""
    key = "reader_id" if reader else "row_id"

    def walk(node: Any, path: list[str]) -> list[str] | None:
        current = [*path, str(getattr(node, "label", ""))]
        if str(getattr(node, key, "")) == target and getattr(node, "kind", "") in (
            "reader",
            *(k.value for k in RowKind),
        ):
            return current
        for child in getattr(node, "children", ()) or ():
            found = walk(child, current)
            if found is not None:
                return found
        return None

    for node in nodes:
        found = walk(node, [])
        if found is not None:
            return found
    return []


def _confidence_value(cell: MappingCell) -> float | None:
    """The greatest assessed confidence component, or ``None``."""
    values = [
        value
        for value in cell.confidence.model_dump(mode="json").values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return max(values) if values else None


# --------------------------------------------------------------------------- #
# Service.
# --------------------------------------------------------------------------- #


class MatrixService:
    """Assemble the matrix payload and drill-downs from a :class:`CellSource`.

    Parameters
    ----------
    source:
        A bulk cell reader (SQL-backed or in-memory).  It is the only object
        that touches storage.
    columns, rows:
        Pre-built K05 trees.  The transport layer builds these once from the
        topology/device registry and injects them.
    topology_view:
        Optional duck-typed topology view used to annotate the reader
        drill-down with its relations.  Only the ``edges()`` method is used.
    mapping_version_id, versions:
        Explicit version metadata.  When omitted, values are derived from the
        cells' provenance (or honest placeholders when there are no cells).
    clock:
        Injected source of ``generated_at``.  Defaults to a fixed origin — no
        wall clock is read by production code.
    """

    def __init__(
        self,
        source: CellSource,
        *,
        columns: Sequence[ColumnNode] = (),
        rows: Sequence[RowNode] = (),
        topology_view: Any | None = None,
        mapping_version_id: str | None = None,
        versions: VersionTriple | MappingABC[str, Any] | None = None,
        clock: Any | None = None,
    ) -> None:
        self.source = source
        self.columns = list(columns)
        self.rows = list(rows)
        self.topology_view = topology_view
        self._mapping_version_id = mapping_version_id
        self._versions = versions
        self._clock = clock or (lambda: _FIXED_ORIGIN)

    # -- version metadata ---------------------------------------------------- #

    def _version_info(self, cells: Sequence[MappingCell]) -> tuple[str, VersionTriple]:
        topo = "topo-1"
        mapping = self._mapping_version_id or "map-1"
        model: str | None = "mset-unknown"
        if self._versions is not None:
            if isinstance(self._versions, MappingABC):
                topo = str(self._versions.get("topology_version_id") or topo)
                mapping = str(self._versions.get("mapping_version_id") or mapping)
                model = str(self._versions.get("model_set_id") or model)
            else:
                topo = str(getattr(self._versions, "topology_version_id", topo) or topo)
                mapping = str(getattr(self._versions, "mapping_version_id", mapping) or mapping)
                model = str(getattr(self._versions, "model_set_id", model) or model)
        elif cells:
            prov = cells[0].provenance
            topo = str(prov.topology_version_id or topo)
            mapping = str(prov.mapping_version_id or mapping)
            model = str(prov.model_version_id or model)
        return mapping, VersionTriple(
            topology_version_id=topo,
            mapping_version_id=mapping,
            model_set_id=model or "mset-unknown",
        )

    # -- payload ------------------------------------------------------------- #

    def fingerprint(self) -> str:
        return self.source.fingerprint()

    def etag(self) -> str:
        mapping, _ = self._version_info([])
        return etag_for(mapping, self.source.fingerprint())

    def payload(self, filters: MatrixFilters | None = None, *, fingerprint: str | None = None) -> MatrixPayload:
        """The full matrix view with filters and paging applied to its cells."""
        filters = filters or MatrixFilters()
        if filters.at is not None:
            all_cells = self.source.cells_as_of(filters.at)
        else:
            all_cells = self.source.page({}, page=1, page_size=MatrixFilters.MAX_PAGE_SIZE)

        mapping, versions = self._version_info(all_cells)
        if mapping != (self._mapping_version_id or "map-1"):
            self._mapping_version_id = mapping

        cells = self._filter_cells(all_cells, filters)
        reader_totals = reader_totals_from_cells(all_cells, None)
        total = physical_total_from_cells(all_cells)
        generated = self._clock()
        return MatrixPayload(
            mapping_version_id=mapping,
            versions=versions,
            generated_at=generated if isinstance(generated, datetime) else _FIXED_ORIGIN,
            columns=self.columns,
            rows=self.rows,
            cells=cells,
            reader_totals=reader_totals,
            physical_total=total,
        )

    def _filter_cells(self, cells: Sequence[MappingCell], filters: MatrixFilters) -> list[MappingCell]:
        out = list(cells)
        panel_readers = (
            _matching_leaf_ids(self.columns, filters.panel, "reader") if filters.panel else set()
        )
        room_rows = _matching_leaf_ids(self.rows, filters.room, "row") if filters.room else set()
        results: list[MappingCell] = []
        for cell in out:
            if filters.reader and cell.reader_id != filters.reader:
                continue
            if filters.row_kind and str(cell.row_kind) != filters.row_kind:
                continue
            if filters.mapping_status and str(cell.mapping.mapping_status) != filters.mapping_status:
                continue
            if filters.identifiability and str(cell.identifiability) != filters.identifiability:
                continue
            if filters.visual and str(cell.visual) != filters.visual:
                continue
            if filters.active_state and str(cell.power.operating_state) != filters.active_state:
                continue
            if filters.min_confidence is not None:
                confidence = _confidence_value(cell)
                if confidence is None or confidence < filters.min_confidence:
                    continue
            if filters.panel and cell.reader_id not in panel_readers:
                continue
            if filters.room and cell.row_id not in room_rows:
                continue
            results.append(cell)
        if filters.reader and not any(cell.reader_id == filters.reader for cell in results):
            # A historical reader that had no live cell at ``at`` is an honest
            # empty result — never a fabricated zero-valued cell.
            return []
        size = max(1, int(filters.page_size))
        start = (max(1, int(filters.page)) - 1) * size
        return results[start : start + size]

    # -- drill-downs (HB §19.4) ---------------------------------------------- #

    def cell_detail(self, row_id: str, reader_id: str, *, row_kind: str | None = None) -> CellDetail:
        kind = row_kind or "device"
        cells = self.source.history(str(row_id), str(reader_id), kind)
        if not cells:
            page = self.source.page(
                {"row_id": str(row_id), "reader_id": str(reader_id), "row_kind": kind},
                page=1,
                page_size=2,
            )
            cells = page
        cell = cells[-1] if cells else None
        if cell is None:
            # Honest empty detail: no fabricated cell, no zero-valued estimate.
            from wattmosaic_engine.contracts import (
                CellMapping,
                CellTimestamps,
                EstimateStatus,
                IdentifiabilityStatus,
                MappingStatus,
                Observability,
            )

            now = self._clock()
            stamp = now if isinstance(now, datetime) else _FIXED_ORIGIN
            cell = MappingCell(
                row_kind=kind,  # type: ignore[arg-type]
                row_id=str(row_id),
                reader_id=str(reader_id),
                mapping=CellMapping(
                    observability=Observability.NOT_OBSERVABLE,
                    relation_type=None,
                    mapping_status=MappingStatus.UNKNOWN,
                    valid_from=stamp,
                ),
                power=CellPower(measurement_status=EstimateStatus.UNKNOWN),
                identifiability=IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
                event_association=CellEvents(),
                confidence=ConfidenceVectorV3(),
                provenance=CellProvenance(
                    topology_version_id="", mapping_version_id=self._mapping_version_id or "map-1"
                ),
                timestamps=CellTimestamps(first_observed_at=stamp, last_observed_at=stamp, updated_at=stamp),
                visual="not_observable",
            )
        events_block = cell.event_association
        event_groups: list[dict[str, Any]] = []
        for event_id in list(getattr(events_block, "matched_event_ids", ()) or ()):
            event_groups.append({"event_group_id": event_id})
        if not event_groups and events_block.matched_event_count:
            event_groups.append(
                {
                    "event_group_id": None,
                    "matched": events_block.matched_event_count,
                    "confirmed": events_block.confirmed_match_count,
                    "provisional": events_block.provisional_match_count,
                }
            )
        uncertainty: list[str] = []
        if str(cell.mapping.mapping_status) in ("unknown", "learning", "provisional"):
            uncertainty.append(f"mapping status is {cell.mapping.mapping_status}")
        if str(cell.identifiability) in ("insufficient_evidence", "non_identifiable"):
            uncertainty.append(f"identifiability is {cell.identifiability}")
        if cell.power.estimated_power_w is None:
            uncertainty.append("no point estimate (absent, not zero)")
        if cell.power.bounds is None:
            uncertainty.append("no justified bounds")
        contradictions: list[dict[str, Any]] = []
        for previous in cells[:-1]:
            if str(previous.mapping.mapping_status) != str(cell.mapping.mapping_status):
                contradictions.append(
                    {
                        "at": previous.timestamps.updated_at.isoformat(),
                        "from": str(previous.mapping.mapping_status),
                        "to": str(cell.mapping.mapping_status),
                    }
                )
        return CellDetail(
            cell=cell,
            mapping_history=cells,
            event_groups=event_groups,
            evidence_refs=list(cell.provenance.evidence_refs),
            confidence_components=cell.confidence,
            uncertainty_reasons=uncertainty,
            contradictions=contradictions,
        )

    def reader_detail(self, reader_id: str) -> ReaderDetail:
        cells = self.source.page({"reader_id": str(reader_id)}, page=1, page_size=MatrixFilters.MAX_PAGE_SIZE)
        totals: ReaderColumnSummary = reader_totals_from_cells(cells, None).get(
            str(reader_id), ReaderColumnSummary()
        )
        relations: list[dict[str, Any]] = []
        getter = getattr(self.topology_view, "edges", None) if self.topology_view is not None else None
        if callable(getter):
            for edge in getter():
                source = str(getattr(edge, "source_endpoint_id", "") or "")
                target = str(getattr(edge, "target_endpoint_id", "") or "")
                if source != str(reader_id) and target != str(reader_id):
                    continue
                relations.append(
                    {
                        "edge_id": str(getattr(edge, "edge_id", "")),
                        "relation": str(getattr(getattr(edge, "relation", None), "value", getattr(edge, "relation", ""))),
                        "source_endpoint_id": source,
                        "target_endpoint_id": target,
                        "lifecycle": str(getattr(getattr(edge, "lifecycle", None), "value", getattr(edge, "lifecycle", ""))),
                    }
                )
        known = [
            {
                "row_kind": str(cell.row_kind),
                "row_id": cell.row_id,
                "estimated_power_w": cell.power.estimated_power_w,
                "measurement_status": str(cell.power.measurement_status),
            }
            for cell in cells
            if cell.power.estimated_power_w is not None
            and cell.row_kind not in ("reader_residual", "unknown_profile")
        ]
        # HB §19.4: residual = measured − Σ known − unknown.  An explicitly
        # published residual cell is authoritative; otherwise derive it from
        # the three measured components, but only when all are present — an
        # absent component stays None rather than becoming a fabricated 0.0.
        residual = totals.residual_w
        if (
            residual is None
            and totals.measured_w is not None
            and totals.known_contrib_w is not None
            and totals.unknown_w is not None
        ):
            residual = totals.measured_w - totals.known_contrib_w - totals.unknown_w
        scope = " / ".join(label for label in _path_labels(self.columns, str(reader_id), reader=True) if label)
        return ReaderDetail(
            reader_id=str(reader_id),
            scope=scope or None,
            topology_relations=relations,
            measured_w=totals.measured_w,
            known_contrib_w=totals.known_contrib_w,
            known_contributions=known,
            residual_w=residual,
            unknown_w=totals.unknown_w,
        )

    def row_detail(self, row_id: str) -> RowDetail:
        cells = self.source.page({"row_id": str(row_id)}, page=1, page_size=MatrixFilters.MAX_PAGE_SIZE)
        readers = sorted({cell.reader_id for cell in cells})
        operating_state = next(
            (cell.power.operating_state for cell in cells if cell.power.operating_state is not None),
            None,
        )
        profile_ref = next(
            (cell.power.expected_profile_ref for cell in cells if cell.power.expected_profile_ref is not None),
            None,
        )
        kind = str(cells[0].row_kind) if cells else RowKind.DEVICE.value
        history = [
            {
                "reader_id": cell.reader_id,
                "mapping_status": str(cell.mapping.mapping_status),
                "updated_at": cell.timestamps.updated_at.isoformat(),
                "estimated_power_w": cell.power.estimated_power_w,
            }
            for cell in cells
        ]
        label = next(
            (
                str(getattr(node, "label", ""))
                for node in _tree_leaves(self.rows, "device")
                if str(getattr(node, "row_id", "")) == str(row_id)
            ),
            str(row_id),
        )
        return RowDetail(
            row_id=str(row_id),
            kind=kind,  # type: ignore[arg-type]
            label=label,
            operating_state=operating_state,
            power_profile_ref=profile_ref,
            observed_readers=readers,
            mapping_history=history,
            cells=cells,
        )


__all__ = [  # noqa: F811 - final authoritative export list
    "CellSource",
    "SqliteCellSource",
    "StoreCellSource",
    "MatrixFilters",
    "MatrixService",
    "cells_hash",
    "etag_for",
    "decode_cell",
]
