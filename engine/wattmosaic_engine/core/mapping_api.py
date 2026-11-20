"""Mapping-matrix facade: the v2 matrix read path and its drill-downs.

Owned by M3 after X03 (matrix API).  The legacy ``mapping_matrix()`` bulk
projection is kept for ``/mapping`` (which stays until Z1); the new
``matrix_*`` methods serve ``/matrix`` and the HB §19.4 drill-downs from the
materialised sparse view on a reader connection (``mapping/service.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..api.facade import maybe_await
from ..mapping.service import (
    MatrixFilters,
    MatrixService,
    SqliteCellSource,
)


class MappingApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    async def ha_device_registry(self) -> dict[str, Any]:
        """HA device registry (cached), for the matrix device axis."""
        try:
            registries = await maybe_await(self.hasource.registries())
        except Exception:  # noqa: BLE001 - registry is optional
            return {"devices": []}
        if not isinstance(registries, Mapping):
            return {"devices": []}
        devices = registries.get("devices", [])
        return {"devices": [d for d in devices if isinstance(d, Mapping)]}

    # -- /matrix v2 path (M3) ------------------------------------------------ #

    def _matrix_service(self, *, columns: Any = None, rows: Any = None) -> MatrixService:
        """Build the read-only matrix service over the materialised view.

        The L0–L4 column and DEC-27 row trees are rebuilt lazily from the
        canonical repositories on every distinct ``(topology, mapping)`` version
        pair and cached for the life of the facade.  Callers may still pass
        already-built trees (used by pure tests/fakes); when they do, those win.
        The cell source reads from ``self.db`` through its reader connection
        only, so no matrix read takes the writer lock.
        """
        source = SqliteCellSource(self.db)
        versions = self._matrix_versions()
        if columns is None or rows is None:
            built_columns, built_rows = self._matrix_trees(versions)
        else:
            built_columns, built_rows = (), ()
        return MatrixService(
            source,
            columns=columns if columns is not None else built_columns,
            rows=rows if rows is not None else built_rows,
            topology_view=getattr(self, "topology_view", None),
            mapping_version_id=self._matrix_version_id(),
            versions=versions,
            clock=self._clock,
        )

    # -- tree building (M3b) ------------------------------------------------- #

    def _matrix_trees(self, versions: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
        """Build (columns, rows) for the current version pair, cached per pair.

        Columns come from T1's ``primary_display_tree()`` built over the active
        topology version's edges (S14) and the measurement endpoints (S13), fed
        through M1's pure ``build_columns``.  Rows come from M1's ``build_rows``
        over physical devices, load components, current group ids, unknown-load
        profiles and readers — every source read through a repository that
        exists, and a missing source contributes nothing (never an invented row).
        Building is pure; the injected clock supplies the graph's validity time.
        """
        # Rows: only sources that actually exist; missing ones contribute nothing.
        devices = self._matrix_devices()
        components = self._matrix_components()
        groups = self._matrix_groups()
        profiles = self._matrix_profiles()
        # The version pair alone is not enough: a device created, a group
        # formed or an unknown profile learned does not bump either version,
        # and a pair-only key served the first-built rows until restart.  The
        # key therefore also digests the row and column sources themselves.
        key = (
            str(versions.get("topology_version_id") or ""),
            str(versions.get("mapping_version_id") or ""),
            self._matrix_source_digest(devices, components, groups, profiles),
        )
        cache = getattr(self, "_matrix_tree_cache", None)
        if cache is None:
            cache = {}
            self._matrix_tree_cache = cache
        cached = cache.get(key)
        if cached is not None:
            return cached

        from ..mapping.columns import build_columns
        from ..mapping.rows import build_rows

        endpoints = self._matrix_endpoints()

        # Columns: active topology version's edges + endpoints -> T1 graph.
        topology_view = self._matrix_topology_graph(endpoints, key[0])
        columns = build_columns(topology_view, endpoints)

        readers = self._matrix_readers(endpoints)
        rows = build_rows(
            devices=devices,
            components=components,
            groups=groups,
            profiles=profiles,
            readers=readers,
        )

        trees = (columns, rows)
        # Only the latest key is useful; older source states never come back.
        cache.clear()
        cache[key] = trees
        return trees

    def _matrix_source_digest(self, *sources: Any) -> str:
        """A stable digest of the row sources plus the raw endpoint rows."""
        import hashlib
        import json

        try:
            endpoints = [dict(row) for row in self.db.repos.entities.endpoints()]
        except Exception:  # noqa: BLE001 - no S13 source -> no endpoints
            endpoints = []
        blob = json.dumps([*sources, endpoints], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _matrix_endpoints(self) -> list[Any]:
        """Canonical S13 measurement endpoints as contract models."""
        from ..contracts import MeasurementEndpoint

        try:
            rows = self.db.repos.entities.endpoints()
        except Exception:  # noqa: BLE001 - no S13 source -> no endpoints
            return []
        out: list[Any] = []
        for row in rows:
            data = dict(row)
            # The repository exposes the storage-only ``metadata_json`` column;
            # the contract forbids extra keys, so drop it before validation.
            data.pop("metadata_json", None)
            # A confirmed single-subject CT validates only with its user-declared
            # revision, so the revisions are loaded rather than defaulted away.
            try:
                data["revisions"] = [
                    {k: v for k, v in dict(r).items() if k != "id"}
                    for r in self.db.repos.entities.revisions(data["endpoint_id"])
                ]
            except Exception:  # noqa: BLE001 - no revision source -> none
                data["revisions"] = []
            try:
                out.append(MeasurementEndpoint(**data))
            except Exception:  # noqa: BLE001 - one bad row must not break the tree
                continue
        return out

    def _matrix_topology_graph(self, endpoints: Sequence[Any], version_id: str) -> Any:
        """T1 graph over the active topology version's edges and the endpoints."""
        from ..contracts import TopologyEdgeV3
        from ..electrical.topology_graph import TopologyGraph

        repo = getattr(self.db.repos, "topology_v3", None)
        if repo is None:
            return TopologyGraph.from_edges((), endpoints, at=None)
        try:
            raw = repo.edges(version_id or None)
        except Exception:  # noqa: BLE001 - no S14 source -> an empty graph
            raw = []
        edges: list[Any] = []
        for row in raw:
            try:
                edges.append(TopologyEdgeV3(**self._matrix_edge_row(row)))
            except Exception:  # noqa: BLE001 - skip malformed edges, never invent
                continue
        at = self._clock() if callable(getattr(self, "_clock", None)) else None
        return TopologyGraph.from_edges(edges, endpoints, at=at)

    @staticmethod
    def _matrix_edge_row(row: Mapping[str, Any]) -> dict[str, Any]:
        """Normalise a raw S14 edge row for :class:`TopologyEdgeV3`."""
        import contextlib
        import json
        from datetime import datetime

        data = dict(row)
        evidence = data.get("evidence", data.get("evidence_json", {}))
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence or "{}")
            except (TypeError, ValueError):
                evidence = {}
        data["evidence"] = evidence
        data.pop("evidence_json", None)
        for field in ("valid_from", "valid_until"):
            value = data.get(field)
            if isinstance(value, str):
                with contextlib.suppress(ValueError):
                    data[field] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return data

    def _matrix_devices(self) -> list[Any]:
        try:
            return [dict(row) for row in self.db.repos.entities.devices()]
        except Exception:  # noqa: BLE001
            return []

    def _matrix_components(self) -> list[Any]:
        repo = getattr(self.db.repos, "entities", None)
        if repo is None or not callable(getattr(repo, "components", None)):
            return []
        try:
            return [dict(row) for row in repo.components()]
        except Exception:  # noqa: BLE001
            return []

    def _matrix_groups(self) -> list[Any]:
        """Current device group ids, from the group estimates repository.

        No source means no group rows — a group id is never invented.
        """
        repo = getattr(self.db.repos, "inference", None)
        if repo is None:
            return []
        try:
            with self.db._reader() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT group_id FROM device_group_estimates "
                    "WHERE group_id IS NOT NULL ORDER BY group_id"
                ).fetchall()
            return [{"group_id": str(row[0])} for row in rows if row[0]]
        except Exception:  # noqa: BLE001 - table/repo absent -> no group rows
            return []

    def _matrix_profiles(self) -> list[Any]:
        repo = getattr(self.db.repos, "residual", None)
        if repo is None or not callable(getattr(repo, "profiles", None)):
            return []
        try:
            return [dict(row) for row in repo.profiles()]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _matrix_readers(endpoints: Sequence[Any]) -> list[str]:
        """Reader ids for residual rows, derived from the real endpoints."""
        out: list[str] = []
        for endpoint in endpoints:
            reader_id = getattr(endpoint, "endpoint_id", None)
            if isinstance(reader_id, str) and reader_id:
                out.append(reader_id)
        return out

    def _matrix_version_id(self) -> str:
        try:
            return str(self.db.repos.mapping.active_mapping_version_id())
        except Exception:  # noqa: BLE001 - no mapping version recorded yet
            return "map-1"

    def _matrix_versions(self) -> dict[str, Any]:
        mapping = self._matrix_version_id()
        topo = "topo-1"
        try:
            active = self.db.repos.topology_v3.active_version()
            if isinstance(active, Mapping):
                candidate = str(active.get("version_id") or "").strip()
                if candidate:
                    topo = candidate
        except Exception:  # noqa: BLE001
            pass
        return {
            "topology_version_id": topo,
            "mapping_version_id": mapping,
            "model_set_id": "mset-unknown",
        }

    def matrix_fingerprint(self) -> str:
        """A cheap digest of the current view, for ETag / 304 polling."""
        return self._matrix_service().fingerprint()

    def matrix_etag(self) -> str:
        """The full ETag ``<mapping_version_id>-<cells_hash>`` for ``/matrix``."""
        from ..mapping.service import etag_for

        return etag_for(self._matrix_version_id(), self.matrix_fingerprint())

    def matrix_payload(self, query: Mapping[str, str] | None = None) -> Any:
        """The full typed matrix payload, filtered and paged from query params.

        The current view folds in the published snapshot, so each reader column
        carries its measured power and residual and the physical total its
        estimates, groups and root residual (HB §19.4).  A historical ``at=``
        view has no snapshot for that instant and keeps the cell-only totals.
        """
        filters = MatrixFilters.from_query(dict(query or {}))
        if filters.at is not None:
            return self._matrix_service().payload(filters)
        try:
            snapshot = getattr(self, "_v3_snapshot", lambda: None)()
        except Exception:  # noqa: BLE001 - no snapshot yet: cell-only totals
            snapshot = None
        roots: tuple[str, ...] = ()
        root_readers = getattr(self, "_root_readers", None)
        if snapshot is not None and callable(root_readers):
            try:
                roots = tuple(root_readers(getattr(snapshot, "versions", None)))
            except Exception:  # noqa: BLE001 - unknown topology: no root residual
                roots = ()
        payload = self._matrix_service().payload(
            filters, snapshot=snapshot, root_reader_ids=roots
        )
        # The current view carries the published snapshot's stale readers, so a
        # null column total always comes with its reason (HB §18, AT-17).
        stale = list(getattr(snapshot, "stale_sources", None) or [])
        return payload.model_copy(update={"stale_sources": stale}) if stale else payload

    def matrix_cells_as_of(self, at: Any) -> Any:
        """Historical cells (HB §9.9), used by the ``at=`` query."""
        if hasattr(at, "year"):
            stamp = at
        else:
            from datetime import datetime

            stamp = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        return self._matrix_service().source.cells_as_of(stamp)

    def matrix_cell_detail(self, row_id: str, reader_id: str, *, row_kind: str | None = None) -> Any:
        return self._matrix_service().cell_detail(row_id, reader_id, row_kind=row_kind)

    def matrix_reader_detail(self, reader_id: str) -> Any:
        return self._matrix_service().reader_detail(reader_id)

    def matrix_row_detail(self, row_id: str) -> Any:
        return self._matrix_service().row_detail(row_id)
