"""Per-device export route."""

from __future__ import annotations

from aiohttp import web

from ..server import _facade, _ok, maybe_await


async def export(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).export(request.match_info["device_id"])))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/export/{{device_id}}", export)


__all__ = ["export", "register"]
