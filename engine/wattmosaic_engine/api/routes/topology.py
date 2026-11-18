"""Topology, panel-tree, circuit, relation and isolation routes."""

from __future__ import annotations

import asyncio
import math

from aiohttp import web

from ..facade import FacadeUnavailable, InvalidRequest
from ..server import _facade, _ok, _read_json, _validate_topology_payload, maybe_await

#: Truthy spellings accepted by ``?include_nesting=``.  Anything else (including
#: an empty value) is false: nesting is opt-in because it is the expensive path.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


async def topology_get(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).topology()))


async def topology_put(request: web.Request) -> web.Response:
    topology = _validate_topology_payload(await _read_json(request))
    return _ok(await maybe_await(_facade(request).set_topology(topology)))


async def topology_confirm(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).confirm_topology()))


async def topology_nesting(request: web.Request) -> web.Response:
    """Candidate parent links and duplicate feeds from stored history.

    Read-only: nothing here writes a parent link.  The computation is CPU- and
    SQLite-bound, so it runs in a worker thread to avoid starving ``/health``.
    """
    raw = request.query.get("hours")
    hours = 12.0
    if raw is not None:
        try:
            hours = float(raw)
        except ValueError as exc:
            raise InvalidRequest("hours must be a number") from exc
        if not math.isfinite(hours) or hours <= 0 or hours > 24 * 90:
            raise InvalidRequest("hours must be greater than 0 and at most 2160")
    facade = _facade(request)
    method = getattr(facade, "nesting_candidates", None)
    if method is None:
        raise FacadeUnavailable("nesting candidates are not supported by this backend")
    payload = await asyncio.to_thread(method, hours=hours)
    return _ok(await maybe_await(payload))


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


async def relations(request: web.Request) -> web.Response:
    """Typed edges, grouped by kind (``?kind=`` filters to one kind)."""
    kind = request.query.get("kind")
    method = getattr(_facade(request), "relations", None)
    if method is None:
        raise FacadeUnavailable("relations are not supported by this backend")
    return _ok(await maybe_await(await asyncio.to_thread(method, kind)))


async def isolation_status(request: web.Request) -> web.Response:
    """Isolation verdicts from the last analysis pass."""
    method = getattr(_facade(request), "isolation_status", None)
    if method is None:
        raise FacadeUnavailable("isolation status is not supported by this backend")
    return _ok(await maybe_await(method()))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/topology", topology_get)
    router.add_put(f"{base}/topology", topology_put)
    router.add_post(f"{base}/topology/confirm", topology_confirm)
    router.add_get(f"{base}/topology/nesting", topology_nesting)
    router.add_get(f"{base}/topology/tree", topology_tree)
    router.add_get(f"{base}/circuits", circuits)
    router.add_get(f"{base}/relations", relations)
    router.add_get(f"{base}/isolation", isolation_status)


__all__ = [
    "circuits",
    "isolation_status",
    "relations",
    "register",
    "topology_confirm",
    "topology_get",
    "topology_nesting",
    "topology_put",
    "topology_tree",
]
