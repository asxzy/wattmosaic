"""Health and status routes."""

from __future__ import annotations

from aiohttp import web

from ..server import _facade, _ok, maybe_await


async def health(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).health()))


async def status(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).status()))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/health", health)
    router.add_get(f"{base}/status", status)


__all__ = ["health", "register", "status"]
