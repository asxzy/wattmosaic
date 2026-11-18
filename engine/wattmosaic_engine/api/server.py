"""aiohttp HTTP server implementing the versioned engine API.

Every response carries ``X-WattMosaic-Contract-Version``.  Errors are always
``ErrorResponse`` JSON with an appropriate 4xx/5xx status.  The server is
deliberately thin: it validates transport concerns (auth, body parsing, timeouts,
bounded concurrency) and delegates all meaning to the injected facade.  Route
handlers live in :mod:`api.routes` and are discovered automatically by
``routes.register_all(router, base, deps)``.

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
import uuid
from collections import OrderedDict
from datetime import date, datetime
from enum import Enum
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from ..contracts import CONTRACT_VERSION, ErrorResponse, Topology
from . import routes
from .facade import (
    ENGINE_VERSION,
    EngineFacade,
    FacadeUnavailable,
    InvalidRequest,
    NotFound,
    NullFacade,
    maybe_await,
)
from .timeouts import DISCOVERY_REQUEST_TIMEOUT_S

log = logging.getLogger("wattmosaic.api")

API_PREFIX = "/api/v1"
CONTRACT_HEADER = "X-WattMosaic-Contract-Version"
TOKEN_HEADER = "X-API-Token"
DEFAULT_REQUEST_TIMEOUT_S = 30.0
DEFAULT_MAX_CONCURRENCY = 32

#: Route-level timeout overrides, keyed by the exact request path.
DISCOVERY_PLACEMENTS_PATH = f"{API_PREFIX}/discover/placements"
COMMANDS_PATH = f"{API_PREFIX}/commands"

#: Commands that run the same whole-house analysis as the discovery route.
SLOW_COMMANDS = frozenset({"discover_placements"})

#: Paths that are allowed to outlive ``DEFAULT_REQUEST_TIMEOUT_S``.
DEFAULT_ROUTE_TIMEOUTS: dict[str, float] = {
    DISCOVERY_PLACEMENTS_PATH: DISCOVERY_REQUEST_TIMEOUT_S,
}

#: How many *finished* discovery jobs are kept for status polling.
DISCOVERY_JOB_HISTORY_LIMIT = 32


class DiscoveryJobManager:
    """Runs placement discovery as a bounded background job.

    Placement discovery reads the whole recorder window and fits it; on a real
    install that is minutes of work.  Holding an HTTP request open that long
    starves ``/healthz`` and surfaces as a gateway 504.  This manager owns the
    work instead: :meth:`start` creates a task and returns a handle immediately,
    and :meth:`status` reports progress and the final result/error.

    Ownership rules, chosen deliberately:

    * **One at a time, idempotent.**  A second ``start`` while a job is running
      returns the *existing* handle rather than a fresh task or a 409.
    * **CPU stays off the loop.**  The task awaits the facade's
      ``discover_placements`` coroutine, whose CPU-bound half already runs in
      ``asyncio.to_thread`` (``main.EngineCore.discover_placements``).
    * **Bounded history.**  At most ``DISCOVERY_JOB_HISTORY_LIMIT`` finished
      jobs are retained; a caller that polls late gets a ``not_found``.
    * **Clean lifecycle.**  :meth:`stop` cancels the running task and awaits it,
      so shutdown leaves no dangling task.

    Both the direct route and ``POST /commands`` use this one manager, so there
    is a single concurrency bound across the two entry points.
    """

    def __init__(self, facade: Any, *, history_limit: int = DISCOVERY_JOB_HISTORY_LIMIT) -> None:
        self._facade = facade
        self._history_limit = max(1, int(history_limit))
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._current_id: str | None = None

    @staticmethod
    def handle(job_id: str, status: str) -> dict[str, Any]:
        """The immediate job handle returned by a start request."""
        return {"job_id": job_id, "status": status}

    def _public(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        """Build the status response from a job's private record."""
        status = str(job["status"])
        result: dict[str, Any] = {"job_id": job_id, "status": status}
        if status in ("complete", "failed"):
            if "error" in job:
                result["error"] = job["error"]
            else:
                result["result"] = job.get("result")
        return result

    def _current(self) -> dict[str, Any] | None:
        """The single in-flight job record, if one is still running or pending."""
        if self._current_id is None:
            return None
        job = self._jobs.get(self._current_id)
        if job is None or job["status"] not in ("pending", "running"):
            return None
        return job

    async def start(
        self,
        *,
        lookback_hours: float,
        apply: bool | None,
        include_groups: bool,
        max_candidates: int,
    ) -> dict[str, Any]:
        """Start discovery in the background and return a handle immediately."""
        existing = self._current()
        if existing is not None and self._current_id is not None:
            return self.handle(self._current_id, str(existing["status"]))

        job_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "status": "pending",
            "request": {
                "lookback_hours": lookback_hours,
                "apply": apply,
                "include_groups": include_groups,
                "max_candidates": max_candidates,
            },
        }
        self._jobs[job_id] = record
        self._current_id = job_id
        self._task = asyncio.create_task(
            self._run(job_id, record), name=f"wm-discovery-{job_id[:8]}"
        )
        return self.handle(job_id, "running")

    async def _run(self, job_id: str, record: dict[str, Any]) -> None:
        """Execute the discovery coroutine and store its result or error."""
        record["status"] = "running"
        try:
            method = getattr(self._facade, "discover_placements", None)
            if method is None:
                raise FacadeUnavailable("placement discovery is not supported by this backend")
            result = await maybe_await(method(**record["request"]))
        except asyncio.CancelledError:
            record["status"] = "failed"
            record["error"] = "discovery cancelled during shutdown"
            if self._current_id == job_id:
                self._current_id = None
            raise
        except Exception as exc:  # noqa: BLE001 - a failed job must not crash the server
            record["status"] = "failed"
            record["error"] = str(exc) or type(exc).__name__
        else:
            record["status"] = "complete"
            record["result"] = result
        finally:
            if self._current_id == job_id:
                self._current_id = None
            self._evict()

    def _evict(self) -> None:
        """Keep the finished-job history bounded, never evict a running job."""
        finished = [key for key, value in self._jobs.items() if value["status"] != "running"]
        over = len(finished) - self._history_limit
        if over <= 0:
            return
        for key in finished[:over]:
            self._jobs.pop(key, None)

    def status(self, job_id: str) -> dict[str, Any] | None:
        """Return the public job record, or ``None`` when the id is unknown."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return self._public(job_id, job)

    async def stop(self) -> None:
        """Cancel the in-flight job (if any) and await it.  Idempotent."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._current_id = None


# Typed application keys (avoids aiohttp NotAppKeyWarning).
FACADE_KEY: web.AppKey[Any] = web.AppKey("wm_facade", Any)
CONFIG_KEY: web.AppKey[dict[str, Any]] = web.AppKey("wm_config", dict)
TIMEOUTS_KEY: web.AppKey[dict[str, float]] = web.AppKey("wm_route_timeouts", dict)

#: Per-request cache for the raw body (read in middleware before the handler).
BODY_KEY: web.RequestKey[bytes] = web.RequestKey("wm_raw_body", bytes)
SEMAPHORE_KEY: web.AppKey[asyncio.Semaphore] = web.AppKey("wm_semaphore", asyncio.Semaphore)
STARTED_AT_KEY: web.AppKey[float] = web.AppKey("wm_started_at", float)
DISCOVERY_JOBS_KEY: web.AppKey[DiscoveryJobManager] = web.AppKey(
    "wm_discovery_jobs", DiscoveryJobManager
)

#: The literal the caller must send to authorise a destructive DB reset.
RESET_CONFIRM_TOKEN = "RESET"
RESET_DB_ROUTE = "/commands/reset-db"
RESET_DB_PATH = f"{API_PREFIX}{RESET_DB_ROUTE}"


# --------------------------------------------------------------------------- #
# Shared transport helpers (imported by the per-area route modules)
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    """Convert a facade return value into something stdlib json can encode."""
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


def _error(
    status: int, error: str, detail: str | None = None, *, retryable: bool = False
) -> web.Response:
    body = ErrorResponse(error=error, detail=detail, retryable=retryable)
    return web.json_response(body.model_dump(mode="json"), status=status)


def _ok(payload: Any, *, status: int = 200) -> web.Response:
    if isinstance(payload, web.Response):
        return payload
    # Central contract rule: a missing resource is a 404, never a 200 + null.
    if payload is None:
        raise NotFound("resource not found")
    return web.json_response(_jsonable(payload), status=status, dumps=json.dumps)


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


async def _request_body(request: web.Request) -> bytes:
    """The raw request body, cached so reading it twice is free."""
    cached = request.get(BODY_KEY)
    if cached is not None:
        return cached
    raw = await request.read()
    request[BODY_KEY] = raw
    return raw


async def _read_json(request: web.Request) -> Any:
    raw = await _request_body(request)
    if not raw:
        raise InvalidRequest("request body is required")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequest(f"request body is not valid JSON: {exc.msg}") from exc
    except Exception as exc:  # noqa: BLE001 - aiohttp raises several types
        raise InvalidRequest("request body could not be parsed") from exc


def _validate(model: type, payload: Any) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise InvalidRequest(exc.json(include_url=False)) from exc


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _validate_topology(topology: Topology) -> Topology:
    """Reject structurally valid but semantically empty topologies."""
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


def _validate_topology_payload(payload: Any) -> Topology:
    return _validate_topology(_validate(Topology, payload))


def _bounded_float(
    request: web.Request, name: str, *, default: float, minimum: float, maximum: float
) -> float:
    """Parse a bounded numeric query parameter with the transport's error shape."""
    raw = request.query.get(name)
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise InvalidRequest(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise InvalidRequest(f"{name} must be at least {minimum:g} and at most {maximum:g}")
    return value


def _facade(request: web.Request) -> Any:
    return request.app[FACADE_KEY]


def _discovery_jobs(request: web.Request) -> DiscoveryJobManager:
    """The process-wide discovery job manager attached to the app."""
    return request.app[DISCOVERY_JOBS_KEY]


def _fallback_metrics(facade: Any, uptime_s: float) -> str:
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


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


async def _is_slow_command(request: web.Request) -> bool:
    """True when ``POST /commands`` carries one of the slow analysis commands."""
    if request.method != "POST" or not request.can_read_body:
        return False
    try:
        raw = await _request_body(request)
        payload = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 - malformed bodies are the handler's problem
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("command") in SLOW_COMMANDS


async def _timeout_for_request(request: web.Request, cfg: dict[str, Any]) -> float:
    """Resolve the budget, letting a slow command borrow the analysis one."""
    overrides: dict[str, float] = request.app.get(TIMEOUTS_KEY, {})
    budget = overrides.get(request.path)
    if budget is not None:
        return budget
    if request.path == COMMANDS_PATH and await _is_slow_command(request):
        return overrides.get(DISCOVERY_PLACEMENTS_PATH, DISCOVERY_REQUEST_TIMEOUT_S)
    return cfg["request_timeout_s"]


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
        async with asyncio.timeout(await _timeout_for_request(request, cfg)):
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
        route_timeouts: dict[str, float] | None = None,
    ) -> None:
        self.facade = facade or NullFacade()
        self.api_token = api_token or ""
        self.host = host
        self.port = int(port)
        self.request_timeout_s = float(request_timeout_s)
        self.max_concurrency = max(1, int(max_concurrency))
        self.route_timeouts = dict(route_timeouts or {})
        self.started_at = time.monotonic()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._running_port: int | None = None
        self.app = create_app(
            facade=self.facade,
            api_token=self.api_token,
            request_timeout_s=self.request_timeout_s,
            max_concurrency=self.max_concurrency,
            route_timeouts=self.route_timeouts,
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
        try:
            await self.app[DISCOVERY_JOBS_KEY].stop()
        except Exception:  # noqa: BLE001 - shutdown must always complete
            log.exception("error stopping discovery jobs")

    async def __aenter__(self) -> APIServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app(
    facade: EngineFacade | None = None,
    *,
    api_token: str = "",
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    route_timeouts: dict[str, float] | None = None,
) -> web.Application:
    """Build the aiohttp app.  Import-safe and side-effect free.

    ``route_timeouts`` overrides the global ``request_timeout_s`` for specific
    request paths.  The defaults already cover placement discovery, because
    that endpoint is defined to outlive the 30 s default; a mapping passed here
    is merged over them, so callers can add budgets or scale them (tests scale
    both down) without losing the discovery entry by accident.
    """
    app = web.Application(middlewares=[_bounded, _contract_and_auth])
    app[FACADE_KEY] = facade or NullFacade()
    app[DISCOVERY_JOBS_KEY] = DiscoveryJobManager(app[FACADE_KEY])
    app[CONFIG_KEY] = {
        "api_token": api_token or "",
        "request_timeout_s": float(request_timeout_s),
    }
    app[TIMEOUTS_KEY] = {**DEFAULT_ROUTE_TIMEOUTS, **(route_timeouts or {})}
    app[SEMAPHORE_KEY] = asyncio.Semaphore(max(1, int(max_concurrency)))
    app[STARTED_AT_KEY] = time.monotonic()

    # Route registration is fully data-driven: every module in ``api.routes``
    # contributes its own handlers.  Adding a new area later needs no edit here.
    routes.register_all(app.router, API_PREFIX, deps=None)
    return app


def create_runner(
    app: web.Application, *, host: str = "0.0.0.0", port: int = 8787
) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    return runner


__all__ = [
    "API_PREFIX",
    "APIServer",
    "BODY_KEY",
    "COMMANDS_PATH",
    "CONFIG_KEY",
    "CONTRACT_HEADER",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "DEFAULT_ROUTE_TIMEOUTS",
    "DISCOVERY_JOB_HISTORY_LIMIT",
    "DISCOVERY_JOBS_KEY",
    "DISCOVERY_PLACEMENTS_PATH",
    "DISCOVERY_REQUEST_TIMEOUT_S",
    "FACADE_KEY",
    "RESET_CONFIRM_TOKEN",
    "RESET_DB_PATH",
    "RESET_DB_ROUTE",
    "SEMAPHORE_KEY",
    "SLOW_COMMANDS",
    "STARTED_AT_KEY",
    "TIMEOUTS_KEY",
    "TOKEN_HEADER",
    "DiscoveryJobManager",
    "create_app",
    "create_runner",
]
