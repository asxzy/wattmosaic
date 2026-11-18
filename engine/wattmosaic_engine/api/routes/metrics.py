"""Prometheus metrics scrape route."""

from __future__ import annotations

import logging
import time

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import STARTED_AT_KEY, _facade, _fallback_metrics, maybe_await

log = logging.getLogger("wattmosaic.api")


async def metrics(request: web.Request) -> web.Response:
    facade = _facade(request)
    uptime = time.monotonic() - request.app[STARTED_AT_KEY]
    try:
        payload = await maybe_await(facade.metrics())
    except FacadeUnavailable:
        payload = _fallback_metrics(facade, uptime)
    except Exception:  # noqa: BLE001 - never fail a scrape
        log.exception("metrics generation failed")
        payload = _fallback_metrics(facade, uptime)
    if not isinstance(payload, str):
        payload = _fallback_metrics(facade, uptime)
    return web.Response(text=payload, content_type="text/plain", charset="utf-8", status=200)


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/metrics", metrics)


__all__ = ["metrics", "register"]
