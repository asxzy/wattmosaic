"""Mapping-matrix facade: one bulk model read behind the read-time projection.

Owned by M3 after X03 (matrix API)."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from ..api.facade import maybe_await


class MappingApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    async def mapping_matrix(self) -> dict[str, Any]:
        """The Device↔Reader mapping matrix with a single bulk model read.

        Reuses the read-time projection in ``derive_mapping_matrix`` so the
        response (cells, coverage, devices/readers axes) is byte-identical to
        the fallback path — only the model source changes: one bulk read feeds
        an in-memory adapter instead of a per-device ``models()`` N+1.  The
        ``/mapping`` route's ``getattr(facade, "mapping_matrix")`` now resolves
        to this method on the production EngineCore.
        """
        from ..api.facade import derive_mapping_matrix
        from ..models.serde import version_from_row

        active_by_device: dict[str, Any] = {}
        for device_id, row in self._bulk_active_models().items():
            version = version_from_row(row)
            if version is not None:
                # Production ``models()`` publishes the active model via the
                # registry as a JSON dict (``_model_dump``).  Match that exact
                # ``active`` payload so resolve_model builds identical views.
                active_by_device[device_id] = version.model_dump(mode="json")

        def models_reader(device_id: str) -> dict[str, Any]:
            # ``derive_mapping_matrix`` skips devices whose ``active`` is None,
            # exactly as it did when ``models()`` raised NotFound for them.
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
        """All devices' active model rows in ONE query, keyed by device id.

        ``derive_mapping_matrix`` on a plain facade calls ``models(device_id)``
        once per device — an N+1 that fires on every 8s panel poll before the
        poll was trimmed.  Production serves the mapping matrix from here with a
        single bulk read instead.  ``Database._reader()`` keeps the read off the
        writer lock; the ``created_at DESC`` order plus the first-row-wins rule
        matches ``Database.active_model``'s per-device choice.
        """
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
        """HA device registry (cached), for the mapping matrix device axis.

        Additive read hook: the facade's mapping projection uses this to surface
        real HA appliances as device rows.  ``devices`` entries carry an ``id``
        and a ``name_by_user``/``name``; no device is fabricated when HA has no
        registry data.
        """
        try:
            registries = await maybe_await(self.hasource.registries())
        except Exception:  # noqa: BLE001 - registry is optional
            return {"devices": []}
        if not isinstance(registries, Mapping):
            return {"devices": []}
        devices = registries.get("devices", [])
        return {"devices": [d for d in devices if isinstance(d, Mapping)]}
