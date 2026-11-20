"""Measurement Endpoint facade (D2) for :class:`EngineCore`.

``api/routes/endpoints.py`` calls ``endpoints()``, ``endpoint(id)``,
``set_endpoint_scope`` and ``update_endpoint`` on the facade.  The D2
``DeviceService`` owns that logic (its ``EndpointFacade`` adapter); this mixin
binds it to the engine's database and injected clock and maps a missing
endpoint or a bad action onto the transport's error types.
"""

from __future__ import annotations

from typing import Any

from ..api.facade import InvalidRequest, NotFound


class EndpointsApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def _endpoint_facade(self) -> Any:
        from ..devices.service import DeviceService

        clock = getattr(self, "_clock", None)
        return DeviceService(self.db, clock=clock).as_facade()

    def endpoints(self) -> dict[str, Any]:
        """Every valid S13 endpoint, contract-shaped (``{"endpoints": [...]}``)."""
        return self._endpoint_facade().endpoints()

    def endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """One contract-shaped endpoint; :class:`NotFound` when it is unknown."""
        found = self._endpoint_facade().endpoint(endpoint_id)
        if found is None:
            raise NotFound(f"no endpoint {endpoint_id!r}")
        return found

    def set_endpoint_scope(
        self, endpoint_id: str, action: str, subject_id: str | None = None
    ) -> dict[str, Any]:
        try:
            return self._endpoint_facade().set_endpoint_scope(endpoint_id, action, subject_id)
        except KeyError as exc:
            raise NotFound(f"no endpoint {endpoint_id!r}") from exc
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc

    def update_endpoint(
        self,
        endpoint_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        try:
            return self._endpoint_facade().update_endpoint(
                endpoint_id, display_name=display_name, enabled=enabled
            )
        except KeyError as exc:
            raise NotFound(f"no endpoint {endpoint_id!r}") from exc


__all__ = ["EndpointsApiMixin"]
