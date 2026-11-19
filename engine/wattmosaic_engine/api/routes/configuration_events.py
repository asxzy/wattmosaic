"""Configuration-event read route (T7, HB §14, interfaces.md §9).

* ``GET /configuration-events`` — the append-only ledger of declared and
  inferred configuration changes, optionally filtered by ``status``.

The route is mounted unconditionally (like every other v3 route) and is a thin
adapter over the facade's ``v3_configuration_events`` hook.  When no backend
provides the hook, the honest answer is an empty list — the ledger is empty,
never a fabricated event.  This module reads no wall clock and never mutates a
stored event.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ...contracts import ChangeStatus
from ..server import _facade, _ok, maybe_await


def _parse_status(raw: str | None) -> Any | None:
    if raw is None or not raw.strip():
        return None
    try:
        return ChangeStatus(raw.strip().lower())
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"unknown change status: {raw}") from exc


async def configuration_events(request: web.Request) -> web.Response:
    """List configuration events, newest first, optionally filtered by status."""
    status = _parse_status(request.query.get("status"))
    method = getattr(_facade(request), "v3_configuration_events", None)
    if method is not None:
        payload = await maybe_await(method(status))
        return _ok(payload if payload is not None else [])
    # A backend without the additive hook has no configuration events to show.
    # The ledger is empty — never a fabricated row, never a 503 for a read.
    return _ok([])


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Mount ``GET /configuration-events`` unconditionally."""
    router.add_get(f"{base}/configuration-events", configuration_events)


__all__ = ["configuration_events", "register"]
