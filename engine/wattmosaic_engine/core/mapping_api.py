"""Mapping-matrix facade: the v2 matrix read path and its drill-downs.

Owned by M3 after X03 (matrix API).  The legacy ``mapping_matrix()`` bulk
projection is kept for ``/mapping`` (which stays until Z1); the new
``matrix_*`` methods serve ``/matrix`` and the HB §19.4 drill-downs from the
materialised sparse view on a reader connection (``mapping/service.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from ..api.facade import maybe_await
from ..mapping.service import (
    MatrixFilters,
    MatrixService,
    SqliteCellSource,
)


class MappingApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    # -- legacy /mapping path (stays until Z1) ------------------------------ #

    async def mapping_matrix(self) -> dict[str, Any]:
        """The Device↔Reader mapping matrix with a single bulk model read.

        Kept for the legacy ``/mapping`` route until Z1 removes it.  Reuses the
        read-time projection in ``derive_mapping_matrix`` so the response is
        byte-identical to the fallback path — only the model source changes: one
        bulk read feeds an in-memory adapter instead of a per-device ``models()``
        N+1.
        """
        from ..api.facade import derive_mapping_matrix
        from ..models.serde import version_from_row

        active_by_device: dict[str, Any] = {}
        for device_id, row in self._bulk_active_models().items():
            version = version_from_row(row)
            if version is not None:
                active_by_device[device_id] = version.model_dump(mode="json")

        def models_reader(device_id: str) -> dict[str, Any]:
            return {
                "device_id": device_id,
                "active": active_by_device.get(device_id),
                "versions": [],
            }

        projection_facade = SimpleNamespace(
            topology=self.topology,
            relations=self.relations,
            models=models_reader,
            ha_device_registry=self.ha_device_registry,
        )
        return await derive_mapping_matrix(projection_facade)

    def _bulk_active_models(self) -> dict[str, dict[str, Any]]:
        """All devices' active model rows in ONE query, keyed by device id."""
        with self.db._reader() as conn:
            rows = conn.execute(
                """SELECT v.* FROM device_model_versions v JOIN device_models m
                ON v.device_id=m.device_id AND v.model_type=m.model_type AND v.version=m.active_version
                ORDER BY v.created_at DESC"""
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            data = dict(row)
            device_id = str(data.get("device_id") or "")
            if device_id and device_id not in out:
                out[device_id] = data
        return out

    async def ha_device_registry(self) -> dict[str, Any]:
        """HA device registry (cached), for the mapping matrix device axis."""
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

        ``columns``/``rows`` come from the (already-built) M1 trees when the
        caller has them; otherwise the service serves an empty tree, which is
        honest for an engine that has not materialised one yet.  The cell
        source reads from ``self.db`` through its reader connection only.
        """
        return MatrixService(
            SqliteCellSource(self.db),
            columns=columns or (),
            rows=rows or (),
            topology_view=getattr(self, "topology_view", None),
            mapping_version_id=self._matrix_version_id(),
            versions=self._matrix_versions(),
            clock=self._clock,
        )

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
        """The full typed matrix payload, filtered and paged from query params."""
        filters = MatrixFilters.from_query(dict(query or {}))
        return self._matrix_service().payload(filters)

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
