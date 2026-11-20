"""The ``EngineFacade`` protocol and its fallbacks.

The protocol is intentionally narrow: it mirrors the HTTP surface one-to-one in
snake_case.  Every method may return either a value or a coroutine; the server
awaits the result, so synchronous backends work too.

The return types are the authoritative contract models wherever one exists
(``HealthResponse``, ``InferenceSnapshot``, ...).  Methods without a
contract model return plain JSON-safe dicts/lists.

``NullFacade`` answers every call with :class:`FacadeUnavailable`, which the
server turns into HTTP 503 + ``ErrorResponse``.  API tests use it to prove the
transport works standalone, and the real engine replaces it at startup.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable

from .. import __version__ as ENGINE_VERSION
from ..contracts import CONTRACT_VERSION, ServiceCommand, ServiceResult
from .protocols import (
    DiagnosticsProvider,
    DiscoveryProvider,
    EndpointsProvider,
    MappingProvider,
    ModelsProvider,
    SnapshotProvider,
    TopologyProvider,
)


class FacadeUnavailable(RuntimeError):
    """Raised when a facade method cannot be served (→ HTTP 503)."""


class NotFound(LookupError):
    """Raised when a requested resource does not exist (→ HTTP 404)."""


class InvalidRequest(ValueError):
    """Raised for a semantically invalid request (→ HTTP 422)."""


async def maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, otherwise return it unchanged."""
    if inspect.isawaitable(value):
        return await value
    return value


@runtime_checkable
class EngineFacade(
    SnapshotProvider,
    TopologyProvider,
    MappingProvider,
    ModelsProvider,
    DiscoveryProvider,
    EndpointsProvider,
    DiagnosticsProvider,
    Protocol,
):
    """Everything the HTTP layer needs from the engine.

    This is the composition of the seven per-area protocols in
    :mod:`api.protocols`.  It declares no methods of its own, so existing
    facades and test doubles that satisfy the old monolithic protocol keep
    working unchanged.
    """


class NullFacade:
    """A facade that is not wired to an engine.

    Used before startup finishes and in transport tests.  Every method raises
    :class:`FacadeUnavailable`; ``health`` reports a degraded engine rather than
    failing so health checks can distinguish "not ready" from "broken".
    """

    def __init__(self, reason: str = "inference backend not installed") -> None:
        self.reason = reason

    def health(self) -> dict[str, Any]:
        return {
            "status": "degraded",
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "database": "unavailable",
            "ha_connected": False,
            "ha_last_event_at": None,
            "uptime_seconds": 0.0,
            "queue_depth": 0,
            "inference_last_run_at": None,
            "learning_active": False,
            "details": {"facade": "null", "reason": self.reason},
        }

    def _unavailable(self, *_: Any, **__: Any) -> Any:
        raise FacadeUnavailable(self.reason)

    def status(self) -> Any:
        return self._unavailable()

    def panel_tree(self, *, include_nesting: bool = False) -> Any:
        return self._unavailable()

    def quality(self) -> Any:
        return self._unavailable()

    def diagnostics(self) -> Any:
        return self._unavailable()

    def inference_records(self, device_id: str, *, limit: int = 200) -> Any:
        return self._unavailable()

    def residual_components(self, *, hours: float = 24.0, limit: int = 500) -> Any:
        return self._unavailable()

    def models(self, device_id: str) -> Any:
        return self._unavailable()

    def command(self, command: ServiceCommand) -> Any:
        return self._unavailable()

    def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> Any:
        """No engine, so nothing was analysed and nothing may be applied.

        Returning a shaped answer rather than raising keeps the transport usable
        before startup finishes: a caller can render "not available" without
        special-casing one endpoint.
        """
        return {
            "ok": False,
            "detail": "not available",
            "accepted": [],
            "refused": {},
            "groups": [],
        }

    def export(self, device_id: str) -> Any:
        return self._unavailable()

    def metrics(self) -> Any:
        return self._unavailable()

    def reset_database(self) -> Any:
        raise FacadeUnavailable(self.reason)


class StubFacade(NullFacade):
    """A deterministic in-memory facade for tests and demos.

    ``StubFacade`` implements every read endpoint with small, fixed data so the
    HTTP surface can be exercised end to end without an inference engine.  It is
    never used in production: ``main`` wires the real ``EngineCore``.
    """

    def __init__(self) -> None:
        super().__init__("stub facade")
        #: The v3 topology read: no active version and no edges yet.
        self._topology_v3: dict[str, Any] = {
            "active_version": None,
            "edges": [],
            "primary_display_tree": [],
            "discovery": {
                "counts": {"candidate": 0, "provisional": 0, "confirmed": 0, "conflicted": 0},
                "top_pairs": [],
                "thresholds": {
                    "MIN_SUPPORT_PROVISIONAL": 3,
                    "MIN_INDEPENDENT_CAUSES": 2,
                    "MIN_SUPPORT_CONFIRMED": 8,
                },
            },
        }
        self.commands: list[ServiceCommand] = []

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "database": "ok",
            "ha_connected": True,
            "ha_last_event_at": None,
            "uptime_seconds": 0.0,
            "queue_depth": 0,
            "inference_last_run_at": None,
            "learning_active": False,
            "details": {"facade": "stub"},
        }

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "facade": "stub",
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "uptime_seconds": 0.0,
            "learning_active": False,
        }

    def topology_v3(self, *, lifecycles: Any = None) -> dict[str, Any]:
        return dict(self._topology_v3)

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        """An empty v3 panel tree (the stub has no endpoints and no edges)."""
        return {
            "version": 2,
            "status": "confirmed",
            "generated_at": "2026-01-01T00:00:00Z",
            "topology_version": None,
            "stats": {
                "panels": 0,
                "circuits": 0,
                "devices": 0,
                "unplaced_devices": 0,
                "unplaced_circuits": 0,
                "max_depth": 0,
                "duplicate_feeds": 0,
                "channels": 0,
                "feed_circuits": 0,
            },
            "roots": [],
            "flat": {"panels": [], "circuits": [], "devices": []},
            "notes": [],
        }

    def quality(self) -> dict[str, Any]:
        return {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "overall_score": 1.0,
            "channels": {},
            "issues": [],
        }

    def diagnostics(self) -> dict[str, Any]:
        return {"generated_at": "2026-01-01T00:00:00+00:00", "facade": "stub", "ok": True}

    def inference_records(self, device_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return []

    def residual_components(self, *, hours: float = 24.0, limit: int = 500) -> dict[str, Any]:
        return {"window_hours": float(hours), "count": 0, "components": []}

    def models(self, device_id: str) -> dict[str, Any]:
        return {"device_id": device_id, "active": None, "versions": []}

    def command(self, command: ServiceCommand) -> ServiceResult:
        self.commands.append(command)
        return ServiceResult(ok=True, command=command.command, detail="stub")

    def export(self, device_id: str) -> dict[str, Any]:
        return {"device_id": device_id, "exported_at": "2026-01-01T00:00:00+00:00", "data": {}}

    def metrics(self) -> str:
        return "# HELP wattmosaic_up Engine process is up\n# TYPE wattmosaic_up gauge\nwattmosaic_up 1\n"


__all__ = [
    "ENGINE_VERSION",
    "EngineFacade",
    "FacadeUnavailable",
    "InvalidRequest",
    "NotFound",
    "NullFacade",
    "StubFacade",
    "maybe_await",
]
