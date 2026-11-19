"""Topology, panel-tree, circuit and isolation routes.

The legacy writes (``PUT /topology``, ``POST /topology/confirm``,
``GET /topology/nesting``) and ``GET /relations`` were removed at the 3.0
cutover.  Topology changes now go through the per-edge v3 routes in
:mod:`.topology_v3`.  The remaining reads below are still part of the v3 HTTP
surface and are mounted unconditionally.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await

#: Truthy spellings accepted by ``?include_nesting=``.  Anything else (including
#: an empty value) is false: nesting is opt-in because it is the expensive path.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


async def topology_get(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).topology()))


async def topology_tree(request: web.Request) -> web.Response:
    """Panel → circuits → devices hierarchy (contract v1 §1).

    The default answer is the cheap one: ``include_nesting`` is false unless the
    caller explicitly asks.  The blocking read runs in a worker thread.
    """
    raw = request.query.get("include_nesting")
    include_nesting = raw is not None and raw.strip().lower() in _TRUTHY
    return _ok(
        await maybe_await(
            await asyncio.to_thread(_facade(request).panel_tree, include_nesting=include_nesting)
        )
    )


async def circuits(request: web.Request) -> web.Response:
    """Stored circuit scopes from the last analysis pass (read-only)."""
    method = getattr(_facade(request), "circuits", None)
    if method is None:
        raise FacadeUnavailable("circuits are not supported by this backend")
    return _ok(await maybe_await(await asyncio.to_thread(method)))


async def isolation_status(request: web.Request) -> web.Response:
    """Isolation verdicts from the last analysis pass."""
    method = getattr(_facade(request), "isolation_status", None)
    if method is None:
        raise FacadeUnavailable("isolation status is not supported by this backend")
    return _ok(await maybe_await(method()))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/topology", topology_get)
    router.add_get(f"{base}/topology/tree", topology_tree)
    router.add_get(f"{base}/circuits", circuits)
    router.add_get(f"{base}/isolation", isolation_status)


__all__ = [
    "circuits",
    "isolation_status",
    "register",
    "topology_get",
    "topology_tree",
]
