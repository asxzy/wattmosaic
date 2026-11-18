"""The derived mapping matrix and reader-residual routes."""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable, derive_mapping_matrix
from ..server import _bounded_float, _facade, _jsonable, _ok, maybe_await


def _mapping_coverage_counts(matrix: dict[str, Any]) -> dict[str, Any]:
    """Add honest per-status/per-cardinality counts to the coverage block.

    The counts are derived from the cells actually published, so they are always
    consistent with the cells and are all zero for an empty matrix.  Unknown-load
    cells are counted separately (REQ-MAPPING-004/007) and never named as devices.
    """
    cells = matrix.get("cells", [])
    statuses: dict[str, int] = {}
    cardinalities: dict[str, int] = {}
    unknown_load_count = 0
    for cell in cells:
        status = str(cell.get("status", "UNKNOWN"))
        statuses[status] = statuses.get(status, 0) + 1
        cardinality = cell.get("relation_type") or "unstated"
        cardinalities[str(cardinality)] = cardinalities.get(str(cardinality), 0) + 1
        if cell.get("is_unknown_load"):
            unknown_load_count += 1
    return {
        "cell_count": len(cells),
        "by_status": statuses,
        "by_cardinality": cardinalities,
        "unknown_load_count": unknown_load_count,
    }


async def mapping_matrix(request: web.Request) -> web.Response:
    """The derived Device↔Reader Mapping Matrix with a coverage block.

    Read-only and additive.  A backend may publish a matrix itself through its
    facade's ``mapping_matrix()``; otherwise the matrix is projected on request
    from the facade's current topology, relations and models.
    """
    facade = _facade(request)
    method = getattr(facade, "mapping_matrix", None)
    payload = await maybe_await(method()) if method is not None else None
    if payload is None:
        # Facades that predate the optional method (or return None from its
        # default) still get an honest, derived matrix from current state.
        payload = await derive_mapping_matrix(facade)
    payload = _jsonable(payload)
    matrix: dict[str, Any] = payload if isinstance(payload, dict) else {}
    coverage = matrix.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    coverage = dict(coverage)
    coverage["counts"] = _mapping_coverage_counts(matrix)
    matrix = dict(matrix)
    matrix["coverage"] = coverage
    return _ok(matrix)


async def residual_components(request: web.Request) -> web.Response:
    """Recent residual observations: signed, never clamped, never zeroed."""
    hours = _bounded_float(request, "hours", default=24.0, minimum=0.0, maximum=2160.0)
    limit_raw = _bounded_float(request, "limit", default=500.0, minimum=1.0, maximum=5000.0)
    method = getattr(_facade(request), "residual_components", None)
    if method is None:
        raise FacadeUnavailable("residual components are not supported by this backend")
    payload = await asyncio.to_thread(method, hours=hours, limit=int(limit_raw))
    return _ok(await maybe_await(payload))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/mapping", mapping_matrix)
    router.add_get(f"{base}/residuals", residual_components)


__all__ = ["_mapping_coverage_counts", "mapping_matrix", "register", "residual_components"]
