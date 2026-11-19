"""Device and per-device inference-record routes — v3 shapes only.

``GET /devices`` and ``GET /devices/{id}`` serve v3 ``DeviceEstimate`` payloads.
The v2 shapes and the ``?v=3`` dispatch were removed at the 3.0 cutover; there is
no legacy fallback.  :mod:`.snapshot_v3` re-exports the handler names for
callers that imported them there before Z1c.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _bounded_float, _facade, _ok, maybe_await


def _method(request: web.Request, name: str) -> Any:
    method = getattr(_facade(request), name, None)
    if method is None:
        raise FacadeUnavailable(f"{name} is not supported by this backend")
    return method


async def devices(request: web.Request) -> web.Response:
    """``GET /devices`` -> v3 ``DeviceEstimate[]``."""
    return _ok(await maybe_await(_method(request, "devices_v3")()))


async def device(request: web.Request) -> web.Response:
    """``GET /devices/{device_id}`` -> one v3 ``DeviceEstimate``."""
    identifier = request.match_info.get("device_id") or request.match_info.get("id")
    return _ok(await maybe_await(_method(request, "device_v3")(identifier)))


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
