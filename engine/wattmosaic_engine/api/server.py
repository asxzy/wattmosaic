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
from .timeouts import DISCOVERY_REQUEST_TIMEOUT_S

log = logging.getLogger("wattmosaic.api")

API_PREFIX = "/api/v1"
CONTRACT_HEADER = "X-WattMosaic-Contract-Version"
TOKEN_HEADER = "X-API-Token"
DEFAULT_REQUEST_TIMEOUT_S = 30.0
DEFAULT_MAX_CONCURRENCY = 32

#: Route-level timeout overrides, keyed by the exact request path.
#:
#: The 30 s default is deliberate: it frees a panel request from a hung engine
#: promptly, and every endpoint except placement discovery answers in
#: milliseconds.  Discovery is the one endpoint defined to be slow -- it runs a
#: whole-house recorder analysis and writes nothing until the analysis is done
#: -- so it gets a budget derived from the history window it may be asked for.
#: Keep this table small and explicit; a pattern language here would make it
#: impossible to say which budget an endpoint actually has.
DISCOVERY_PLACEMENTS_PATH = f"{API_PREFIX}/discover/placements"
COMMANDS_PATH = f"{API_PREFIX}/commands"

#: Commands that run the same whole-house analysis as the discovery route.
#:
#: The path-keyed timeout table cannot express this: both callers post the
#: *same* work, but only one of them has the work in its path.  The Home
#: Assistant integration's ``discover_placements`` service reaches discovery
#: through ``POST /commands``, and keying the budget on the path alone left that
#: caller on the 30 s default while the gateway had already granted the analysis
#: 6600 s.  A 72 h run answers in tens of seconds, so the engine returned 504
#: ``timeout`` from this middleware -- for work it was still perfectly able to
#: finish.
SLOW_COMMANDS = frozenset({"discover_placements"})

#: Paths that are allowed to outlive ``DEFAULT_REQUEST_TIMEOUT_S``.  The
#: discovery budget comes from ``api/timeouts.py``, which is also where the
#: engine's history-query budget lives, so the two cannot drift apart.
DEFAULT_ROUTE_TIMEOUTS: dict[str, float] = {
    DISCOVERY_PLACEMENTS_PATH: DISCOVERY_REQUEST_TIMEOUT_S,
}

# Typed application keys (avoids aiohttp NotAppKeyWarning).
FACADE_KEY: web.AppKey[Any] = web.AppKey("wm_facade", Any)
CONFIG_KEY: web.AppKey[dict[str, Any]] = web.AppKey("wm_config", dict)
TIMEOUTS_KEY: web.AppKey[dict[str, float]] = web.AppKey("wm_route_timeouts", dict)

#: Per-request cache for the raw body.  Reading it in the middleware (to pick a
#: budget) must not consume it for the handler, and ``web.Request`` exposes
#: ``__setitem__``/``get`` for exactly this kind of handler-local state.
BODY_KEY: web.RequestKey[bytes] = web.RequestKey("wm_raw_body", bytes)
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

    async def __aenter__(self) -> APIServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


async def _request_body(request: web.Request) -> bytes:
    """The raw request body, cached so reading it twice is free.

    ``_is_slow_command`` has to inspect the body *before* the handler runs to
    decide the budget, and aiohttp does not rewind the stream.  Caching it here
    keeps ``_read_json`` working instead of making the handler see an empty body.
    """
    cached = request.get(BODY_KEY)
    if cached is not None:
        return cached
    raw = await request.read()
    request[BODY_KEY] = raw
    return raw


async def _is_slow_command(request: web.Request) -> bool:
    """True when ``POST /commands`` carries one of the slow analysis commands.

    A body that cannot be parsed is *not* an error at this point -- the handler
    reports that -- so it simply keeps the default budget.
    """
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
        # ``/commands`` is the integration's route to the same analysis, so it
        # gets the same budget rather than the 30 s default.
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
    # Same whole-table latest-result read as ``devices`` and ``topology_tree``.
    return _ok(await maybe_await(await asyncio.to_thread(_facade(request).snapshot)))


async def devices(request: web.Request) -> web.Response:
    # Same reason as ``topology_tree``: the per-device latest-result lookup is a
    # whole-table scan, so it must not run on the event loop.
    return _ok(await maybe_await(await asyncio.to_thread(_facade(request).devices)))


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
    # Even without nesting the tree merges the latest result per device, and
    # that query is a covering-index scan of the whole ``inference_results``
    # table.  Measured at ~24 ms warm but ~590 ms on a cold page cache against
    # the production 343k rows, which would stall the loop and starve /health
    # exactly as the nesting scan used to.  Same remedy: the blocking part runs
    # in a thread.
    return _ok(
        await maybe_await(
            await asyncio.to_thread(
                _facade(request).panel_tree, include_nesting=include_nesting
            )
        )
    )


async def discover_placements(request: web.Request) -> web.Response:
    """Learn placements from recorded history; writes only when explicitly asked.

    Every field is optional, so an empty body is a valid "show me what you would
    place" request.  Validation here is transport-level only: the bounds match
    ``topology_nesting``'s ``hours`` because both name a lookback over stored
    history, and a non-finite or non-positive value cannot describe a window.

    The planning step reads the recorder and the configuration store, so it runs
    in a worker thread for the same reason ``topology_tree`` does -- it must not
    stall ``/health`` or ingestion.  The frozen idiom (``to_thread`` +
    ``maybe_await``) is required because a facade may be ``async`` and calling it
    in a thread returns a coroutine that still needs awaiting.
    """
    body: Any = {}
    if request.can_read_body:
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be a JSON object")

    lookback_raw = body.get("lookback_hours", 24.0)
    if isinstance(lookback_raw, bool) or not isinstance(lookback_raw, (int, float)):
        raise InvalidRequest("lookback_hours must be a number")
    lookback_hours = float(lookback_raw)
    if not math.isfinite(lookback_hours) or lookback_hours <= 0 or lookback_hours > 2160:
        raise InvalidRequest("lookback_hours must be greater than 0 and at most 2160")

    apply_raw = body.get("apply")
    if apply_raw is not None and not isinstance(apply_raw, bool):
        raise InvalidRequest("apply must be a boolean")
    include_groups = body.get("include_groups", True)
    if not isinstance(include_groups, bool):
        raise InvalidRequest("include_groups must be a boolean")
    max_candidates_raw = body.get("max_candidates", 200)
    if isinstance(max_candidates_raw, bool) or not isinstance(max_candidates_raw, int):
        raise InvalidRequest("max_candidates must be an integer")
    if max_candidates_raw < 1:
        raise InvalidRequest("max_candidates must be at least 1")

    facade = _facade(request)
    method = getattr(facade, "discover_placements", None)
    if method is None:
        raise FacadeUnavailable("placement discovery is not supported by this backend")
    return _ok(
        await maybe_await(
            await asyncio.to_thread(
                method,
                lookback_hours=lookback_hours,
                apply=apply_raw,
                include_groups=include_groups,
                max_candidates=max_candidates_raw,
            )
        )
    )


async def quality(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).quality()))


async def diagnostics(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).diagnostics()))


# -- audit artifacts (read-only, additive) ---------------------------------- #


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
        raise InvalidRequest(
            f"{name} must be at least {minimum:g} and at most {maximum:g}"
        )
    return value


async def circuits(request: web.Request) -> web.Response:
    """Stored circuit scopes from the last analysis pass.

    Read-only.  A circuit is written only after a pass had the evidence to name a
    class, and its status stays ``candidate`` until a user confirms it, so this
    never presents a guess as a fact.
    """
    method = getattr(_facade(request), "circuits", None)
    if method is None:
        raise FacadeUnavailable("circuits are not supported by this backend")
    return _ok(await maybe_await(await asyncio.to_thread(method)))


async def relations(request: web.Request) -> web.Response:
    """Typed edges, grouped by kind.

    ``?kind=`` filters to one kind.  The kinds are never merged: an association,
    a measurement overlap and a parent link are different claims about the
    installation and combining them would hide which one is true.
    """
    kind = request.query.get("kind")
    method = getattr(_facade(request), "relations", None)
    if method is None:
        raise FacadeUnavailable("relations are not supported by this backend")
    return _ok(await maybe_await(await asyncio.to_thread(method, kind)))


async def inference_records(request: web.Request) -> web.Response:
    """The append-only revision chain for one device, oldest revision first."""
    device_id = request.match_info["device_id"]
    limit_raw = _bounded_float(
        request, "limit", default=200.0, minimum=1.0, maximum=5000.0
    )
    method = getattr(_facade(request), "inference_records", None)
    if method is None:
        raise FacadeUnavailable("inference records are not supported by this backend")
    payload = await asyncio.to_thread(method, device_id, limit=int(limit_raw))
    return _ok(await maybe_await(payload))


async def residual_components(request: web.Request) -> web.Response:
    """Recent residual observations: signed, never clamped, never zeroed."""
    hours = _bounded_float(request, "hours", default=24.0, minimum=0.0, maximum=2160.0)
    limit_raw = _bounded_float(
        request, "limit", default=500.0, minimum=1.0, maximum=5000.0
    )
    method = getattr(_facade(request), "residual_components", None)
    if method is None:
        raise FacadeUnavailable("residual components are not supported by this backend")
    payload = await asyncio.to_thread(method, hours=hours, limit=int(limit_raw))
    return _ok(await maybe_await(payload))


async def isolation_status(request: web.Request) -> web.Response:
    """Isolation verdicts from the last analysis pass.

    ``source`` names where the answer came from, so "no pass yet" is never
    rendered as "all clear".
    """
    method = getattr(_facade(request), "isolation_status", None)
    if method is None:
        raise FacadeUnavailable("isolation status is not supported by this backend")
    return _ok(await maybe_await(method()))


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
    app[CONFIG_KEY] = {
        "api_token": api_token or "",
        "request_timeout_s": float(request_timeout_s),
    }
    app[TIMEOUTS_KEY] = {**DEFAULT_ROUTE_TIMEOUTS, **(route_timeouts or {})}
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
    # Audit artifacts (contract 1.6.0).  Additive and read-only: each one
    # reports what a pass already derived and never triggers analysis or a write.
    router.add_get(f"{base}/circuits", circuits)
    router.add_get(f"{base}/relations", relations)
    router.add_get(f"{base}/devices/{{device_id}}/records", inference_records)
    router.add_get(f"{base}/residuals", residual_components)
    router.add_get(f"{base}/isolation", isolation_status)
    router.add_get(f"{base}/models/{{device_id}}", models)
    router.add_post(f"{base}/commands", commands)
    router.add_post(f"{base}/discover/placements", discover_placements)
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
    "DEFAULT_ROUTE_TIMEOUTS",
    "DISCOVERY_PLACEMENTS_PATH",
    "DISCOVERY_REQUEST_TIMEOUT_S",
    "TIMEOUTS_KEY",
    "TOKEN_HEADER",
    "create_app",
    "create_runner",
]
