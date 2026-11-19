"""P6: runtime loop metrics (loop lag, per-stage latency, queue depth, drops).

Mounted unconditionally as ``GET /runtime-metrics``.  A backend without the
``runtime_metrics`` hook reports an honest empty object rather than a 503, so
the route is always discoverable (transport-only deployments included).
"""

from __future__ import annotations

from aiohttp import web

from ..server import _facade, _ok, maybe_await

__all__ = ["register", "runtime_metrics"]


async def runtime_metrics(request: web.Request) -> web.Response:
    method = getattr(_facade(request), "runtime_metrics", None)
    if method is None:
        return _ok({})
    payload = await maybe_await(method())
    return _ok(payload if payload is not None else {})


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Mount ``GET /runtime-metrics`` unconditionally."""
    router.add_get(f"{base}/runtime-metrics", runtime_metrics)
