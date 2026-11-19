"""Event and versioned-record read routes (I6, interfaces.md §9).

* ``GET /events?status=&since=`` — event groups, newest first.
* ``GET /events/{group_id}`` — one group plus its record revisions.
* ``GET /devices/{id}/records`` — an append-only ``EventInferenceRecord`` chain.

The read surface is a thin adapter over the injected I6
:class:`~wattmosaic_engine.inference.records.EventRecordStore` (or the facade's
v3 hooks when a real backend is wired in at P6/Z1).  It never reads the wall
clock and never mutates a stored record.  The legacy per-device records handler
is registered by ``devices.py`` and is removed in Z1; this module exposes the
v3 handler for the same canonical path but does not register a duplicate (see
``models_v3.py`` for the established pattern).
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from ...contracts import EventGroupStatus
from ...inference.records import EventRecordStore
from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await

#: App key under which a caller injects the I6 record store for these routes.
EVENT_RECORD_STORE_KEY = web.AppKey("event_record_store", EventRecordStore)


def get_event_store(request: web.Request) -> EventRecordStore | None:
    """The store injected on the app, or ``None`` when no v3 backend is wired."""
    return request.app.get(EVENT_RECORD_STORE_KEY)


def _parse_status(raw: str | None) -> EventGroupStatus | None:
    if raw is None or not raw.strip():
        return None
    try:
        return EventGroupStatus(raw.strip().lower())
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"unknown event status: {raw}") from exc


def _parse_since(raw: str | None) -> Any:
    if raw is None or not raw.strip():
        return None
    from datetime import datetime

    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"invalid since timestamp: {raw}") from exc
    if value.tzinfo is None:
        from datetime import UTC

        value = value.replace(tzinfo=UTC)
    return value


async def events(request: web.Request) -> web.Response:
    """List event groups, optionally filtered by ``status`` and ``since``."""
    status = _parse_status(request.query.get("status"))
    since = _parse_since(request.query.get("since"))
    method = getattr(_facade(request), "v3_events", None)
    if method is not None:
        payload = await maybe_await(await asyncio.to_thread(method, status, since))
        return _ok(payload if payload is not None else [])
    # No backend: an empty list is the honest answer (never fabricated rows).
    return _ok([])


async def event(request: web.Request) -> web.Response:
    """One event group plus the full chain of record revisions."""
    group_id = request.match_info["group_id"]
    method = getattr(_facade(request), "v3_event", None)
    if method is not None:
        payload = await maybe_await(await asyncio.to_thread(method, group_id))
        if payload is None:
            raise web.HTTPNotFound(reason=f"unknown event group: {group_id}")
        return _ok(payload)
    store = get_event_store(request)
    if store is not None:
        latest = store.latest(group_id)
        if latest is None:
            raise web.HTTPNotFound(reason=f"unknown event group: {group_id}")
        return _ok(
            {
                "group_id": group_id,
                "status": latest.status.value if hasattr(latest.status, "value") else latest.status,
                "records": [r.model_dump(mode="json") for r in store.history(group_id)],
            }
        )
    raise FacadeUnavailable("event groups are not supported by this backend")


async def device_records(request: web.Request) -> web.Response:
    """The append-only record chain for one device, oldest revision first."""
    device_id = request.match_info["device_id"]
    limit_raw = request.query.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 200
    except ValueError as exc:
        raise web.HTTPBadRequest(reason="limit must be an integer") from exc
    limit = max(1, min(limit, 5000))

    method = getattr(_facade(request), "v3_device_records", None)
    if method is not None:
        payload = await maybe_await(await asyncio.to_thread(method, device_id, limit))
        return _ok(payload if payload is not None else [])

    store = get_event_store(request)
    if store is not None:
        records = [
            record
            for record in store.records.values()
            if any(est.subject_id == device_id for est in record.result.device_estimates)
        ]
        records.sort(key=lambda r: (r.created_at, r.interpretation_revision))
        return _ok([r.model_dump(mode="json") for r in records[:limit]])
    raise FacadeUnavailable("device records are not supported by this backend")


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Register the two new v3 event paths.

    ``/devices/{device_id}/records`` is already registered by ``devices.py``;
    aiohttp forbids a second registration, so the v3 handler above is exposed
    for direct use and will serve the canonical path once the legacy handler is
    removed in Z1.
    """
    router.add_get(f"{base}/events", events)
    router.add_get(f"{base}/events/{{group_id}}", event)


__all__ = [
    "EVENT_RECORD_STORE_KEY",
    "device_records",
    "event",
    "events",
    "get_event_store",
    "register",
]
