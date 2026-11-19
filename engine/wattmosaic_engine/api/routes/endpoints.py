"""Measurement Endpoint routes (D2).

``GET /endpoints`` lists discovered endpoints; ``PUT /endpoints/{id}`` updates
display name / enabled; ``POST /endpoints/{id}/scope`` confirms or rejects a
candidate single-subject scope.  Every change is append-only at the service
layer (a config revision plus a configuration event).
"""

from __future__ import annotations

from aiohttp import web

from ..facade import InvalidRequest
from ..server import _facade, _ok, _read_json, maybe_await


async def endpoints(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).endpoints()))


async def endpoint(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).endpoint(request.match_info["endpoint_id"])))


async def endpoint_scope(request: web.Request) -> web.Response:
    """Confirm or reject an endpoint's declared scope."""
    body = await _read_json(request)
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    action = body.get("action")
    if action not in ("confirm", "reject"):
        raise InvalidRequest("action must be 'confirm' or 'reject'")
    subject_id = body.get("subject_id")
    if subject_id is not None and not isinstance(subject_id, str):
        raise InvalidRequest("subject_id must be a string")
    return _ok(
        await maybe_await(
            _facade(request).set_endpoint_scope(
                request.match_info["endpoint_id"], action, subject_id
            )
        )
    )


async def endpoint_update(request: web.Request) -> web.Response:
    """Update an endpoint's display name and/or enabled flag."""
    body = await _read_json(request)
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    display_name = body.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise InvalidRequest("display_name must be a string")
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise InvalidRequest("enabled must be a boolean")
    return _ok(
        await maybe_await(
            _facade(request).update_endpoint(
                request.match_info["endpoint_id"],
                display_name=display_name,
                enabled=enabled,
            )
        )
    )


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/endpoints", endpoints)
    router.add_get(f"{base}/endpoints/{{endpoint_id}}", endpoint)
    router.add_put(f"{base}/endpoints/{{endpoint_id}}", endpoint_update)
    router.add_post(f"{base}/endpoints/{{endpoint_id}}/scope", endpoint_scope)


__all__ = ["endpoint", "endpoint_scope", "endpoint_update", "endpoints", "register"]
