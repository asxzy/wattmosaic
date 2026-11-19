"""The v2 matrix read API and its cell/reader/row drill-downs.

M3 · interfaces.md §9 · HB §19.4 · ``analysis/E-…`` WP-E3 / trap T6.

The handlers are transport-only: they parse query parameters, build the ETag,
and delegate all meaning to the facade's ``matrix_*`` methods through the
:class:`~wattmosaic_engine.api.protocols.MappingProvider` Protocol.  Every read
goes through the facade's reader connection; no writer lock is taken, and there
is no N+1 (one bulk query per endpoint).
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable, maybe_await
from ..server import _facade, _ok

_ETAG_HEADER = "ETag"
_IF_NONE_MATCH = "If-None-Match"


def _method(request: web.Request, name: str) -> Any:
    method = getattr(_facade(request), name, None)
    if method is None:
        raise FacadeUnavailable(f"matrix route '{name}' is not supported by this backend")
    return method


async def _call(method: Any, *args: Any, **kwargs: Any) -> Any:
    return await maybe_await(method(*args, **kwargs))


def _etag_from_header(value: str | None) -> set[str]:
    """Parse ``If-None-Match`` into a set of (weak) entity tags."""
    if not value:
        return set()
    return {part.strip().lstrip("W/") for part in value.split(",") if part.strip()}


async def matrix_view(request: web.Request) -> web.Response:
    """``GET /matrix`` — typed ``MatrixPayload`` with version-based ETag.

    Query parameters (interfaces §9): ``at``, ``panel``, ``reader``, ``room``,
    ``row_kind``, ``mapping_status``, ``identifiability``, ``visual``,
    ``min_confidence``, ``active_state``, ``page`` and ``page_size``.  The ETag
    is ``<mapping_version_id>-<cells_hash>``; an unchanged ``If-None-Match``
    yields ``304 Not Modified`` with no body.
    """
    facade = _facade(request)

    # ETag short-circuit first: a cheap fingerprint read, no payload build.
    payload_method = getattr(facade, "matrix_payload", None)
    if payload_method is None:
        raise FacadeUnavailable("matrix route is not supported by this backend")

    version = "map-1"
    fingerprint = ""
    etag_method = getattr(facade, "matrix_etag", None)
    if etag_method is not None:
        etag = await _call(etag_method)
        supplied = _etag_from_header(request.headers.get(_IF_NONE_MATCH))
        if etag in supplied or f"W/{etag}" in supplied or "*" in supplied:
            response = web.Response(status=304)
            response.headers[_ETAG_HEADER] = etag
            return response
        version, _, fingerprint = str(etag).rpartition("-")
        if not version or not fingerprint:
            version, fingerprint = "map-1", ""

    payload = await _call(payload_method, request.query)
    response = _ok(payload)
    if fingerprint:
        # Prefer the payload's own version so the ETag always matches the bytes
        # actually served.
        version = getattr(payload, "mapping_version_id", None) or version
        response.headers[_ETAG_HEADER] = f"{version}-{fingerprint}"
    return response


async def cell_detail(request: web.Request) -> web.Response:
    """``GET /matrix/cells/{row_id}/{reader_id}`` — cell plus its evidence."""
    row_id = request.match_info["row_id"]
    reader_id = request.match_info["reader_id"]
    row_kind = request.query.get("row_kind")
    method = _method(request, "matrix_cell_detail")
    payload = await _call(method, row_id, reader_id, row_kind=row_kind)
    return _ok(payload)


async def reader_detail(request: web.Request) -> web.Response:
    """``GET /matrix/readers/{id}`` — scope, totals and residual (never summed)."""
    reader_id = request.match_info["id"]
    method = _method(request, "matrix_reader_detail")
    return _ok(await _call(method, reader_id))


async def row_detail(request: web.Request) -> web.Response:
    """``GET /matrix/rows/{id}`` — operating state, profile and mapping history."""
    row_id = request.match_info["id"]
    method = _method(request, "matrix_row_detail")
    return _ok(await _call(method, row_id))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/matrix", matrix_view)
    router.add_get(f"{base}/matrix/cells/{{row_id}}/{{reader_id}}", cell_detail)
    router.add_get(f"{base}/matrix/readers/{{id}}", reader_detail)
    router.add_get(f"{base}/matrix/rows/{{id}}", row_detail)


__all__ = [
    "cell_detail",
    "matrix_view",
    "reader_detail",
    "register",
    "row_detail",
]
