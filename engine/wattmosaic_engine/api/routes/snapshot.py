"""Snapshot read — v3 shape only (contract 3.0).

``GET /snapshot`` serves the ``InferenceSnapshot``.  The v2 shape was removed at
the cutover; there is no legacy fallback and no ``?v=3`` dispatch.  This handler
is the canonical implementation; :mod:`.snapshot_v3` re-exports the name for
callers that imported it there before Z1c.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await


def _method(request: web.Request, name: str) -> Any:
    method = getattr(_facade(request), name, None)
    if method is None:
        raise FacadeUnavailable(f"{name} is not supported by this backend")
    return method


async def snapshot(request: web.Request) -> web.Response:
    """``GET /snapshot`` -> v3 ``InferenceSnapshot``."""
    return _ok(await maybe_await(_method(request, "snapshot_v3")()))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/snapshot", snapshot)


__all__ = ["register", "snapshot"]
