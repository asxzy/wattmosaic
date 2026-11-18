"""Device and per-device inference-record routes."""

from __future__ import annotations

import asyncio

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _bounded_float, _facade, _ok, maybe_await


async def devices(request: web.Request) -> web.Response:
    # The per-device latest-result lookup is a whole-table scan, so it must not
    # run on the event loop.
    return _ok(await maybe_await(await asyncio.to_thread(_facade(request).devices)))


async def device(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).device(request.match_info["device_id"])))


async def inference_records(request: web.Request) -> web.Response:
    """The append-only revision chain for one device, oldest revision first."""
    device_id = request.match_info["device_id"]
    limit_raw = _bounded_float(request, "limit", default=200.0, minimum=1.0, maximum=5000.0)
    method = getattr(_facade(request), "inference_records", None)
    if method is None:
        raise FacadeUnavailable("inference records are not supported by this backend")
    payload = await asyncio.to_thread(method, device_id, limit=int(limit_raw))
    return _ok(await maybe_await(payload))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/devices", devices)
    router.add_get(f"{base}/devices/{{device_id}}", device)
    router.add_get(f"{base}/devices/{{device_id}}/records", inference_records)


__all__ = ["device", "devices", "inference_records", "register"]
