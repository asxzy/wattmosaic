"""D4: the v3 snapshot / devices / groups / provenance reads (§9).

At the 3.0 cutover ``GET /snapshot`` and ``GET /devices`` serve the v3 shapes
unconditionally (their handlers live in :mod:`.snapshot` and :mod:`.devices`).
This module owns the additional paths::

    GET /groups
    GET /groups/{group_id}
    GET /provenance/{kind}/{id}

The ``snapshot_v3`` / ``devices_v3`` / ``device_v3`` / ``snapshot_dispatch``
names are re-exported for callers that imported them here before Z1c; there is
no longer a ``?v=3`` dispatch — the v3 payload is the only payload.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await
from .devices import device as device_v3
from .devices import devices as devices_v3
from .snapshot import snapshot as snapshot_v3

#: Backwards-compatible alias: the dispatcher is now just the v3 handler.
snapshot_dispatch = snapshot_v3

__all__ = [
    "register",
    "snapshot_v3",
    "devices_v3",
    "device_v3",
    "groups_v3",
    "group_v3",
    "provenance",
    "snapshot_dispatch",
]


def _method(request: web.Request, name: str) -> Any:
    method = getattr(_facade(request), name, None)
    if method is None:
        raise FacadeUnavailable(f"{name} is not supported by this backend")
    return method


async def groups_v3(request: web.Request) -> web.Response:
    """Serve the v3 group estimates (``GET /groups``)."""
    return _ok(await maybe_await(_method(request, "groups_v3")()))


async def group_v3(request: web.Request) -> web.Response:
    """Serve one v3 group estimate (``GET /groups/{id}``)."""
    return _ok(await maybe_await(_method(request, "group_v3")(request.match_info["group_id"])))


async def provenance(request: web.Request) -> web.Response:
    """Serve a typed provenance answer (``GET /provenance/{kind}/{id}``)."""
    kind = request.match_info["kind"]
    identifier = request.match_info["id"]
    return _ok(await maybe_await(_method(request, "provenance")(kind, identifier)))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Register the v3-only groups and provenance paths."""
    router.add_get(f"{base}/groups", groups_v3)
    router.add_get(f"{base}/groups/{{group_id}}", group_v3)
    router.add_get(f"{base}/provenance/{{kind}}/{{id}}", provenance)
