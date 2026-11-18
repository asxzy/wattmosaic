"""Power-model reads."""

from __future__ import annotations

from aiohttp import web

from ..server import _facade, _ok, maybe_await


async def models(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).models(request.match_info["device_id"])))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/models/{{device_id}}", models)


__all__ = ["models", "register"]
