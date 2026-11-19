"""D4: the v3 snapshot / devices / groups / provenance reads (§9).

The legacy ``GET /snapshot`` route stays registered and unchanged until Z1.
This module owns only the *new* paths::

    GET /groups
    GET /groups/{group_id}
    GET /provenance/{kind}/{id}
    GET /snapshot?v=3   (dispatched through the existing /snapshot handler)

For ``/snapshot?v=3`` a thin dispatcher wraps the existing GET route (a second
registration of the same canonical path is impossible in aiohttp, and the X04
route-snapshot test pins one registration per path).  Without ``?v=3`` the
legacy payload is returned unchanged.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await

#: Legacy snapshot handlers captured per app, so the named dispatcher can fall
#: back to the original payload when ``?v=3`` is absent.
_LEGACY_SNAPSHOT_HANDLERS: dict[int, Any] = {}

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


async def snapshot_v3(request: web.Request) -> web.Response:
    """Serve the v3 ``InferenceSnapshot`` for ``GET /snapshot?v=3``."""
    return _ok(await maybe_await(_method(request, "snapshot_v3")()))


async def devices_v3(request: web.Request) -> web.Response:
    """Serve the v3 device estimates (``GET /devices?v=3``)."""
    return _ok(await maybe_await(_method(request, "devices_v3")()))


async def device_v3(request: web.Request) -> web.Response:
    """Serve one v3 device estimate (``GET /devices/{id}?v=3``)."""
    identifier = request.match_info.get("device_id") or request.match_info.get("id")
    return _ok(await maybe_await(_method(request, "device_v3")(identifier)))


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


def _wants_v3(request: web.Request) -> bool:
    return request.query.get("v", "") == "3"


async def snapshot_dispatch(request: web.Request) -> web.StreamResponse:
    """A standalone v3-or-legacy dispatcher for the ``/snapshot`` path.

    ``_legacy`` is attached to the request app by :func:`register`; when absent
    (a bare router test) the v3 handler is used directly.  This exists as a
    named module attribute so callers can reuse the dispatch rule.
    """
    if _wants_v3(request):
        return await snapshot_v3(request)
    legacy = _LEGACY_SNAPSHOT_HANDLERS.get(id(request.app))
    if legacy is None:
        return await snapshot_v3(request)
    return await maybe_await(legacy(request))


def _wrap_legacy_get(router: web.UrlDispatcher, base: str) -> None:
    """Wrap the existing GET handler so ``?v=3`` selects the v3 payload.

    ``snapshot`` sorts before ``snapshot_v3`` in auto-discovery, so the legacy
    resource is always present by the time this registers.  We replace only its
    GET handler; the route count and canonical path stay exactly as X04 pinned
    them.
    """
    path = f"{base}/snapshot"
    for resource in router.resources():
        if getattr(resource, "canonical", None) != path:
            continue
        for route in resource:
            if route.method != "GET" or getattr(route, "_handler", None) is None:
                continue
            legacy = route._handler
            app = getattr(router, "_app", None)
            if app is not None:
                _LEGACY_SNAPSHOT_HANDLERS[id(app)] = legacy

            async def dispatch(
                request: web.Request, _legacy: Any = legacy
            ) -> web.StreamResponse:
                if _wants_v3(request):
                    return await snapshot_v3(request)
                return await maybe_await(_legacy(request))

            route._handler = dispatch
            return
    # Defensive: no legacy resource (e.g. a bare router in a unit test).  In a
    # real app the X04 snapshot module registers it first.
    router.add_get(path, snapshot_v3)


def _wrap_devices_get(router: web.UrlDispatcher, base: str) -> None:
    """Wrap ``GET /devices``/``/devices/{id}`` so ``?v=3`` selects v3 payloads."""
    wrappers = {
        f"{base}/devices": (devices_v3, None),
        f"{base}/devices/{{device_id}}": (device_v3, "device_id"),
    }
    for path, (v3_handler, _key) in wrappers.items():
        for resource in router.resources():
            if getattr(resource, "canonical", None) != path:
                continue
            for route in resource:
                if route.method != "GET" or getattr(route, "_handler", None) is None:
                    continue
                legacy = route._handler

                def make_dispatch(handler: Any, fallback: Any) -> Any:
                    async def dispatch(request: web.Request) -> web.StreamResponse:
                        if _wants_v3(request):
                            return await handler(request)
                        return await maybe_await(fallback(request))

                    return dispatch

                route._handler = make_dispatch(v3_handler, legacy)
                break


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Register the new v3 paths and wrap the shared legacy GET handlers."""
    _wrap_legacy_get(router, base)
    _wrap_devices_get(router, base)
    router.add_get(f"{base}/groups", groups_v3)
    router.add_get(f"{base}/groups/{{group_id}}", group_v3)
    router.add_get(f"{base}/provenance/{{kind}}/{{id}}", provenance)
