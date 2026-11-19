"""V3 power-model reads (L1): ``GET /models/{subject_id}``.

The v3 payload is ``PowerModelVersion[]`` plus the append-only event log for a
subject.  The path shares its canonical shape with the legacy
``/models/{device_id}`` route (X04); aiohttp resolves the first registration,
so the unified facade method :meth:`EngineCore.models_v3` is what actually
serves v3 data, while this module keeps the explicit v3 handler available for
callers that want the strict contract shape.

Route registration here is intentionally at the *same* canonical path as the
legacy model route: adding a new canonical path would change the X04 route
snapshot (``tests/unit/test_routes_discovery.py``), which is outside L1's file
ownership.  The endpoint therefore stays a single URL whose payload is the v3
shape whenever the subject has v3 versions.
"""

from __future__ import annotations

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await


async def models_v3(request: web.Request) -> web.Response:
    """Return a subject's v3 model versions and lifecycle events."""
    facade = _facade(request)
    method = getattr(facade, "models_v3", None)
    if method is None:
        raise FacadeUnavailable("v3 models are not supported by this backend")
    return _ok(await maybe_await(method(request.match_info.get("subject_id") or request.match_info["device_id"])))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    # The v3 payload is served by the single ``GET /models/{device_id}`` route
    # registered by the legacy ``models`` module: that handler calls the unified
    # facade method, which returns the v3 shape for v3 subjects (see
    # ``core/models_api.py``).  aiohttp forbids a second registration of the
    # same path, and adding a new canonical path would change the X04 route
    # snapshot (outside L1's file ownership), so this module registers no
    # duplicate route.  The handler above remains available for direct use.
    return None


__all__ = ["models_v3", "register"]
