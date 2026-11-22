"""Event-sourced topology v3 routes (T4).

Per-edge declaration, pin/reject, version history, endpoint config edits and the
panel hierarchy (``/topology/tree``, built from T1's primary display tree).
These are the only topology-write routes since the 3.0 cutover; the legacy
declared-topology handlers were removed.  A backend that does not implement
them answers 503 through :class:`FacadeUnavailable`, matching the other
optional areas.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable, InvalidRequest
from ..server import _facade, _ok, _read_json, maybe_await


def _require(facade: Any, name: str) -> Any:
    method = getattr(facade, name, None)
    if method is None:
        raise FacadeUnavailable(f"{name} is not supported by this backend")
    return method


async def topology_v3_get(request: web.Request) -> web.Response:
    """GET /topology/v3 — active version, edges (all lifecycles) and primary tree."""
    facade = _facade(request)
    method = _require(facade, "topology_v3")
    raw = request.query.get("lifecycles")
    lifecycles = [part.strip() for part in raw.split(",") if part.strip()] if raw else None
    return _ok(await maybe_await(method(lifecycles=lifecycles)))


async def topology_edges_post(request: web.Request) -> web.Response:
    """POST /topology/edges — declare a user edge."""
    payload = await _read_json(request)
    if not isinstance(payload, dict):
        raise InvalidRequest("edge declaration must be a JSON object")
    facade = _facade(request)
    method = _require(facade, "declare_topology_edge")
    result = await maybe_await(
        method(
            relation=str(payload.get("relation", "")),
            source=str(payload.get("source", "")),
            target=str(payload.get("target", "")),
            declared_by=str(payload.get("declared_by", "user")),
        )
    )
    return _ok(result, status=201)


async def topology_edge_pin(request: web.Request) -> web.Response:
    """POST /topology/edges/{id}/pin — take user authority over an edge."""
    method = _require(_facade(request), "pin_topology_edge")
    return _ok(await maybe_await(method(request.match_info["edge_id"])))


async def topology_edge_reject(request: web.Request) -> web.Response:
    """POST /topology/edges/{id}/reject — reject an edge (with valid_until)."""
    method = _require(_facade(request), "reject_topology_edge")
    return _ok(await maybe_await(method(request.match_info["edge_id"])))


async def topology_circuit_parent(request: web.Request) -> web.Response:
    """POST /topology/circuits/{reader_id}/parent — move a circuit.

    The body must be a JSON object with a ``parent`` key whose value is a
    non-empty string or ``null`` (top level).  The move is a user declaration:
    it closes/rejects other parent edges and promotes one version.
    """
    payload = await _read_json(request)
    if not isinstance(payload, dict) or "parent" not in payload:
        raise InvalidRequest("body must be a JSON object with a 'parent' key")
    parent = payload["parent"]
    if parent is not None and (
        not isinstance(parent, str) or not parent.strip()
    ):
        raise InvalidRequest("'parent' must be a non-empty string or null")
    method = _require(_facade(request), "move_circuit")
    result = await maybe_await(method(request.match_info["reader_id"], parent))
    return _ok(result)


async def topology_versions(request: web.Request) -> web.Response:
    """GET /topology/versions — the version history."""
    method = _require(_facade(request), "topology_versions")
    return _ok(await maybe_await(method()))


async def topology_version(request: web.Request) -> web.Response:
    """GET /topology/versions/{id} — one version."""
    method = _require(_facade(request), "topology_versions")
    return _ok(await maybe_await(method(request.match_info["version_id"])))


async def topology_endpoint_config(request: web.Request) -> web.Response:
    """PUT /topology/endpoints/{id}/config — append an EndpointConfigRevision."""
    changes = await _read_json(request)
    if not isinstance(changes, dict):
        raise InvalidRequest("config edits must be a JSON object")
    method = _require(_facade(request), "edit_endpoint_config")
    return _ok(await maybe_await(method(request.match_info["endpoint_id"], changes)))


#: Truthy spellings accepted by ``?include_nesting=``.  Anything else (including
#: an empty value) is false: nesting is opt-in because it is the expensive path.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


async def topology_tree(request: web.Request) -> web.Response:
    """GET /topology/tree — panel hierarchy from the active v3 topology.

    The blocking read runs in a worker thread; nesting suggestions are opt-in.
    """
    raw = request.query.get("include_nesting")
    include_nesting = raw is not None and raw.strip().lower() in _TRUTHY
    method = _require(_facade(request), "panel_tree")
    return _ok(
        await maybe_await(await asyncio.to_thread(method, include_nesting=include_nesting))
    )


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    # The v3 topology-write routes, always mounted unconditionally.
    router.add_get(f"{base}/topology/v3", topology_v3_get)
    router.add_get(f"{base}/topology/tree", topology_tree)
    router.add_post(f"{base}/topology/edges", topology_edges_post)
    router.add_post(f"{base}/topology/edges/{{edge_id}}/pin", topology_edge_pin)
    router.add_post(f"{base}/topology/edges/{{edge_id}}/reject", topology_edge_reject)
    router.add_post(
        f"{base}/topology/circuits/{{reader_id}}/parent", topology_circuit_parent
    )
    router.add_get(f"{base}/topology/versions", topology_versions)
    router.add_get(f"{base}/topology/versions/{{version_id}}", topology_version)
    router.add_put(f"{base}/topology/endpoints/{{endpoint_id}}/config", topology_endpoint_config)


__all__ = [
    "register",
    "topology_edge_pin",
    "topology_edge_reject",
    "topology_edges_post",
    "topology_circuit_parent",
    "topology_endpoint_config",
    "topology_tree",
    "topology_v3_get",
    "topology_version",
    "topology_versions",
]
