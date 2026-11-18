"""Quality and diagnostics routes."""

from __future__ import annotations

from aiohttp import web

from ..server import _facade, _ok, maybe_await


async def quality(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).quality()))


async def diagnostics(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).diagnostics()))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/quality", quality)
    router.add_get(f"{base}/diagnostics", diagnostics)


__all__ = ["diagnostics", "quality", "register"]
