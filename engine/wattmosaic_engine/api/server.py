"""aiohttp HTTP server implementing the versioned engine API.

Every response carries ``X-WattMosaic-Contract-Version``.  Errors are always
``ErrorResponse`` JSON with an appropriate 4xx/5xx status.  The server is
deliberately thin: it validates transport concerns (auth, body parsing, timeouts,
bounded concurrency) and delegates all meaning to the injected facade.

Contract notes
--------------
* A facade method returning ``None`` is treated centrally as "resource not
  found" → HTTP 404 + ``ErrorResponse``.  Handlers do not duplicate that check.
* Request-validation failures return HTTP **400** with
  ``{"error": "bad_request", "detail": ...}``.  HTTP 422 is never exposed.
* ``APIServer(facade, host=..., port=0, api_token=...)`` binds an ephemeral port;
  ``server.port`` reflects the real bound port after ``start()``.  ``stop()`` is
  idempotent.

Usage::

    app = create_app(facade=my_engine, api_token="secret")
    server = APIServer(facade, host="127.0.0.1", port=0, api_token="secret")
    await server.start()
    ...
    await server.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import time
from datetime import date, datetime
from enum import Enum
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from ..contracts import CONTRACT_VERSION, ErrorResponse, ServiceCommand, Topology
from .facade import (
    ENGINE_VERSION,
    EngineFacade,
    FacadeUnavailable,
    InvalidRequest,
    NotFound,
    NullFacade,
    maybe_await,
)

log = logging.getLogger("wattmosaic.api")

API_PREFIX = "/api/v1"
CONTRACT_HEADER = "X-WattMosaic-Contract-Version"
TOKEN_HEADER = "X-API-Token"
DEFAULT_REQUEST_TIMEOUT_S = 30.0
DEFAULT_MAX_CONCURRENCY = 32

# Typed application keys (avoids aiohttp NotAppKeyWarning).
FACADE_KEY: web.AppKey[Any] = web.AppKey("wm_facade", Any)
CONFIG_KEY: web.AppKey[dict[str, Any]] = web.AppKey("wm_config", dict)
SEMAPHORE_KEY: web.AppKey[asyncio.Semaphore] = web.AppKey("wm_semaphore", asyncio.Semaphore)
STARTED_AT_KEY: web.AppKey[float] = web.AppKey("wm_started_at", float)


def _jsonable(value: Any) -> Any:
    """Convert a facade return value into something stdlib json can encode.

    Facades return Pydantic models, plain dataclasses, dicts/lists of either,
    and occasionally raw ``datetime``/``date``/``Enum`` values (for example
    ``EngineCore.status()["started_at"]``). Recursing here keeps every endpoint
    safe rather than leaking a ``TypeError`` out of ``json.dumps`` as a 500.
    """
    if isinstance(value, web.Response):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _error(status: int, error: str, detail: str | None = None, *, retryable: bool = False) -> web.Response:
    body = ErrorResponse(error=error, detail=detail, retryable=retryable)
    return web.json_response(body.model_dump(mode="json"), status=status)


def _ok(payload: Any, *, status: int = 200) -> web.Response:
    if isinstance(payload, web.Response):
        return payload
    # Central contract rule: a missing resource is a 404, never a 200 + null.
    if payload is None:
        raise NotFound("resource not found")
    return web.json_response(_jsonable(payload), status=status, dumps=json.dumps)


class APIServer:
    """Owns the aiohttp application/runner lifecycle."""

    def __init__(
        self,
        facade: EngineFacade | None = None,
        *,
        api_token: str = "",
        host: str = "0.0.0.0",
        port: int = 8787,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self.facade = facade or NullFacade()
        self.api_token = api_token or ""
        self.host = host
        self.port = int(port)
        self.request_timeout_s = float(request_timeout_s)
        self.max_concurrency = max(1, int(max_concurrency))
        self.started_at = time.monotonic()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._running_port: int | None = None
        self.app = create_app(
            facade=self.facade,
            api_token=self.api_token,
            request_timeout_s=self.request_timeout_s,
            max_concurrency=self.max_concurrency,
        )

    @property
    def started(self) -> bool:
        return self._runner is not None

    @property
    def bound_port(self) -> int:
        """The actual listening port (resolves ephemeral bindings)."""
        return self._running_port if self._running_port is not None else self.port

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{host}:{self.bound_port}{API_PREFIX}"

    async def start(self) -> int:
        """Start listening.  Returns the bound port (useful when ``port=0``)."""
        if self._runner is not None:
            return self.bound_port
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        addresses = self._runner.addresses
        if addresses:
            self.port = int(addresses[0][1])
        self._running_port = self.port
        log.info("WattMosaic API listening on %s:%s", self.host, self.port)
        return self.port

    async def stop(self) -> None:
        """Stop accepting connections and shut down cleanly.  Idempotent."""
        runner, self._runner = self._runner, None
        site, self._site = self._site, None
        self._running_port = None
        if site is not None:
            with contextlib.suppress(Exception):
                await site.stop()
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()

    async def __aenter__(self) -> APIServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


@web.middleware
async def _contract_and_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    cfg = request.app[CONFIG_KEY]
    token: str = cfg["api_token"]

    if token:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            response = _error(401, "unauthorized", "missing or invalid X-API-Token")
            response.headers[CONTRACT_HEADER] = CONTRACT_VERSION
            return response

    try:
        async with asyncio.timeout(cfg["request_timeout_s"]):
            response = await handler(request)
    except Exception as exc:  # noqa: BLE001 - mapped below
        response = _map_exception(request, exc)

    response.headers.setdefault(CONTRACT_HEADER, CONTRACT_VERSION)
    return response


@web.middleware
async def _bounded(request: web.Request, handler: Any) -> web.StreamResponse:
    semaphore: asyncio.Semaphore = request.app[SEMAPHORE_KEY]
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
    except TimeoutError:
        response = _error(503, "server_busy", "concurrency limit reached", retryable=True)
        response.headers[CONTRACT_HEADER] = CONTRACT_VERSION
        return response
    try:
        return await handler(request)
    finally:
        semaphore.release()


def _map_exception(request: web.Request, exc: Exception) -> web.Response:
    if isinstance(exc, web.HTTPException):
        if exc.status < 400:
            return exc  # type: ignore[return-value]
        return _error(exc.status, _code_for_status(exc.status), exc.reason)
    if isinstance(exc, FacadeUnavailable):
        return _error(503, "engine_unavailable", str(exc), retryable=True)
    if isinstance(exc, NotFound):
        return _error(404, "not_found", str(exc))
    if isinstance(exc, (InvalidRequest, ValidationError)):
        return _error(400, "bad_request", str(exc))
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return _error(504, "timeout", "request exceeded the engine timeout", retryable=True)
    log.exception("unhandled API error on %s %s", request.method, request.path)
    return _error(500, "internal_error", "unexpected engine error", retryable=True)


def _code_for_status(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        408: "timeout",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "bad_request",  # never expose 422 as a distinct code
        429: "rate_limited",
        500: "internal_error",
        503: "engine_unavailable",
    }.get(status, "http_error")


async def _read_json(request: web.Request) -> Any:
    if not request.can_read_body:
        raise InvalidRequest("request body is required")
    try:
        return await request.json()
    except json.JSONDecodeError as exc:
        raise InvalidRequest(f"request body is not valid JSON: {exc.msg}") from exc
    except Exception as exc:  # noqa: BLE001 - aiohttp raises several types
        raise InvalidRequest("request body could not be parsed") from exc


def _validate(model: type, payload: Any) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        # One consistent public shape: 400 + {"error": "bad_request", "detail": ...}
        raise InvalidRequest(exc.json(include_url=False)) from exc


def _validate_topology(topology: Topology) -> Topology:
    """Reject structurally valid but semantically empty topologies.

    ``Topology`` gives every field a default, so pydantic accepts ``{}``.  An
    empty topology cannot describe an installation, so the API requires:

    * at least one channel;
    * at least one channel with ``ct_type == "main"`` (a branch-only topology has
      no reference for reconciling total power);
    * at least one device (a topology with nothing to disaggregate is useless).

    These are request-shape rules; the contract model itself is untouched.
    """
    problems: list[str] = []
    if not topology.channels:
        problems.append("topology must contain at least one channel")
    elif not any(_enum_value(ch.ct_type) == "main" for ch in topology.channels):
        problems.append("topology must contain at least one channel with ct_type='main'")
    if not topology.devices:
        problems.append("topology must contain at least one device")
    if problems:
        raise InvalidRequest("; ".join(problems))
    return topology


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _validate_topology_payload(payload: Any) -> Topology:
    return _validate_topology(_validate(Topology, payload))


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _facade(request: web.Request) -> EngineFacade:
    return request.app[FACADE_KEY]


async def health(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).health()))


async def status(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).status()))


async def snapshot(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).snapshot()))


async def devices(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).devices()))


async def device(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).device(request.match_info["device_id"])))


async def confidence_tiers(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).confidence_tiers()))


async def topology_get(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).topology()))


async def topology_put(request: web.Request) -> web.Response:
    topology = _validate_topology_payload(await _read_json(request))
    return _ok(await maybe_await(_facade(request).set_topology(topology)))


async def topology_confirm(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).confirm_topology()))


async def topology_nesting(request: web.Request) -> web.Response:
    """Candidate parent links and duplicate feeds from stored history.

    Read-only: nothing here writes a parent link.  The integration wizard turns a
    candidate into a parent only when the user confirms it.

    The computation is CPU- and SQLite-bound (tens of milliseconds of Python and
    a few hundred milliseconds of row materialisation for a 12 h window), so it
    runs in a worker thread.  Doing it inline blocked the event loop for the whole
    response time, which starved ``/health``, ingestion and every other request
    for seconds.  ``EngineCore.nesting_candidates`` is a pure read, and the
    database connection is shared and lock-protected (``check_same_thread=False``
    plus ``Database._lock``), so a thread is safe here.
    """
    raw = request.query.get("hours")
    hours = 12.0
    if raw is not None:
        try:
            hours = float(raw)
        except ValueError as exc:
            raise InvalidRequest("hours must be a number") from exc
        if not math.isfinite(hours) or hours <= 0 or hours > 24 * 90:
            raise InvalidRequest("hours must be greater than 0 and at most 2160")
    facade = _facade(request)
    method = getattr(facade, "nesting_candidates", None)
    if method is None:
        raise FacadeUnavailable("nesting candidates are not supported by this backend")
    # ``to_thread`` copies the current context, so logging/correlation behaves
    # exactly as it did inline; only the blocking part moves off the loop.
    payload = await asyncio.to_thread(method, hours=hours)
    return _ok(await maybe_await(payload))


#: Truthy spellings accepted by ``?include_nesting=``.  Anything else (including
#: an empty value) is false: nesting is opt-in because it is the expensive path.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


async def topology_tree(request: web.Request) -> web.Response:
    """Panel → circuits → devices hierarchy (contract v1 §1).

    Computed from declared data only; ``parent_ct_id`` is never written.  The
    default answer is the cheap one: ``include_nesting`` is false unless the
    caller explicitly asks, because nesting runs a multi-second sample scan and
    can only add advisories on top of a tree that is already complete.
    """
    raw = request.query.get("include_nesting")
    include_nesting = raw is not None and raw.strip().lower() in _TRUTHY
    return _ok(
        await maybe_await(_facade(request).panel_tree(include_nesting=include_nesting))
    )


async def quality(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).quality()))


async def diagnostics(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).diagnostics()))


async def models(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).models(request.match_info["device_id"])))


async def commands(request: web.Request) -> web.Response:
    payload = _validate(ServiceCommand, await _read_json(request))
    result = await maybe_await(_facade(request).command(payload))
    return _ok(result)


async def export(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).export(request.match_info["device_id"])))


def _fallback_metrics(facade: EngineFacade, uptime_s: float) -> str:
    """Prometheus exposition used when a facade has no native implementation."""
    lines: list[str] = []
    health_data: dict[str, Any] = {}
    try:
        raw = facade.health()
        if hasattr(raw, "model_dump"):
            health_data = raw.model_dump(mode="json")
        elif isinstance(raw, dict):
            health_data = raw
    except Exception:  # noqa: BLE001 - metrics must always render
        health_data = {}
    status_name = str(health_data.get("status", "unknown"))
    up = 1 if status_name in ("ok", "degraded") else 0
    lines.append("# HELP wattmosaic_up Engine process is up (1) or unavailable (0)")
    lines.append("# TYPE wattmosaic_up gauge")
    lines.append(f"wattmosaic_up {up}")
    lines.append("# HELP wattmosaic_engine_status Engine status as a label")
    lines.append("# TYPE wattmosaic_engine_status gauge")
    lines.append(f'wattmosaic_engine_status{{status="{status_name}"}} 1')
    lines.append("# HELP wattmosaic_ha_connected Home Assistant WebSocket connected")
    lines.append("# TYPE wattmosaic_ha_connected gauge")
    lines.append(f"wattmosaic_ha_connected {1 if health_data.get('ha_connected') else 0}")
    lines.append("# HELP wattmosaic_queue_depth Ingestion queue depth")
    lines.append("# TYPE wattmosaic_queue_depth gauge")
    lines.append(f"wattmosaic_queue_depth {float(health_data.get('queue_depth') or 0)}")
    lines.append("# HELP wattmosaic_uptime_seconds Engine uptime")
    lines.append("# TYPE wattmosaic_uptime_seconds counter")
    lines.append(f"wattmosaic_uptime_seconds {uptime_s:.3f}")
    lines.append("# HELP wattmosaic_contract_info Contract version")
    lines.append("# TYPE wattmosaic_contract_info gauge")
    lines.append(f'wattmosaic_contract_info{{version="{CONTRACT_VERSION}",engine="{ENGINE_VERSION}"}} 1')
    return "\n".join(lines) + "\n"


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


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app(
    facade: EngineFacade | None = None,
    *,
    api_token: str = "",
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> web.Application:
    """Build the aiohttp app.  Import-safe and side-effect free."""
    app = web.Application(middlewares=[_bounded, _contract_and_auth])
    app[FACADE_KEY] = facade or NullFacade()
    app[CONFIG_KEY] = {
        "api_token": api_token or "",
        "request_timeout_s": float(request_timeout_s),
    }
    app[SEMAPHORE_KEY] = asyncio.Semaphore(max(1, int(max_concurrency)))
    app[STARTED_AT_KEY] = time.monotonic()

    router = app.router
    base = API_PREFIX
    router.add_get(f"{base}/health", health)
    router.add_get(f"{base}/status", status)
    router.add_get(f"{base}/snapshot", snapshot)
    router.add_get(f"{base}/devices", devices)
    router.add_get(f"{base}/devices/{{device_id}}", device)
    router.add_get(f"{base}/confidence_tiers", confidence_tiers)
    router.add_get(f"{base}/topology", topology_get)
    router.add_put(f"{base}/topology", topology_put)
    router.add_post(f"{base}/topology/confirm", topology_confirm)
    router.add_get(f"{base}/topology/nesting", topology_nesting)
    router.add_get(f"{base}/topology/tree", topology_tree)
    router.add_get(f"{base}/quality", quality)
    router.add_get(f"{base}/diagnostics", diagnostics)
    router.add_get(f"{base}/models/{{device_id}}", models)
    router.add_post(f"{base}/commands", commands)
    router.add_get(f"{base}/export/{{device_id}}", export)
    router.add_get(f"{base}/metrics", metrics)
    return app


def create_runner(
    app: web.Application, *, host: str = "0.0.0.0", port: int = 8787
) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    return runner


__all__ = [
    "API_PREFIX",
    "APIServer",
    "CONTRACT_HEADER",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "TOKEN_HEADER",
    "create_app",
    "create_runner",
]
