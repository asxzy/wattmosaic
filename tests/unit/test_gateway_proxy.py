"""Why the ingress proxy must let a slow discovery finish, and fail honestly.

The defect, measured on production ``0.2.1-dev.18`` against real recorder
history through the Home Assistant ingress panel::

    lookback   result
    24 h       HTTP 200 in 9.8 s
    48 h       HTTP 502 after 30.02 s
    72 h       HTTP 502 after 30.02 s
    168 h      HTTP 502 after 30.05 s

The engine was healthy in every one of those cases.  ``addon/gateway.py`` used
``ClientTimeout(total=60, connect=5, sock_read=30)``, and ``sock_read`` is a
*per-read* timeout while a discovery sends nothing at all until the analysis is
finished.  So the proxy killed a valid request and reported "engine is not
reachable", which was false.

The engine grants 168 h a 420 s history budget (``max(60, 2.5 * hours)``,
``engine/wattmosaic_engine/main.py``) and the API accepts up to 2160 h (90 days),
so the proxy's bound is derived from that budget rather than picked as a round
number.  These tests pin:

* the configured budget covers every allowed window, including 2160 h, plus the
  fitting that runs after the history query;
* a request that outlives the old 30 s budget is not cut off;
* a refused connection still says "engine is not reachable";
* a timeout says something else entirely, because the engine *was* reachable;
* ``/healthz`` and the proxied ``/api/v1/health`` keep a short budget, so a dead
  engine is still restarted promptly.

How the slow-request test proves the behaviour
----------------------------------------------
Sleeping for 31 s in a unit test is not acceptable, so the *production* numbers
are scaled by 1/1000: the old ``sock_read=30`` becomes 0.03 s and the new
``6600 s`` becomes 6.6 s.  A real upstream handler stalls 1.2 s and the real
``proxy()`` runs over a real socket.  Scaling both budgets by the same factor
preserves the ratio and the mechanics of ``sock_read`` (aiohttp does not care
what the number means), so a request that would die at 30 s dies at 0.03 s here.
A paired positive control with the scaled *old* budget proves the harness can
still detect the regression.  The absolute production values are asserted
separately against the engine's own constants, so neither half of the evidence
depends on the other.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PATH = REPO_ROOT / "addon" / "gateway.py"

#: The old per-read budget, in seconds.  Every 502 above landed at 30.02 s.
OLD_SOCK_READ_S = 30.0
#: Scale for the slow-request tests: 30 s -> 0.03 s, 6600 s -> 6.6 s.
SCALE = 0.001
#: How long the fake upstream stalls; > scaled old budget, << scaled new one.
SLOW_UPSTREAM_S = 1.2


def _load_gateway(monkeypatch: pytest.MonkeyPatch):
    """Import addon/gateway.py with a controlled environment.

    The module reads configuration at import time, so each test gets a fresh
    copy.
    """
    monkeypatch.delenv("WM_API_TOKEN", raising=False)
    monkeypatch.delenv("WM_GATEWAY_TRUSTED_INGRESS_IPS", raising=False)
    monkeypatch.delenv("WM_GATEWAY_UNAUTHENTICATED_NETWORKS", raising=False)
    spec = importlib.util.spec_from_file_location("wm_gateway_proxy", GATEWAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Upstream:
    """A stand-in engine that answers only after ``delay`` seconds.

    It writes nothing while it works, which is exactly what makes ``sock_read``
    the operative gateway timeout for a discovery.
    """

    def __init__(self, delay: float, *, body: dict[str, Any] | None = None) -> None:
        self.delay = delay
        self.body = body if body is not None else {"ok": True}
        self.requests: list[str] = []

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append(f"{request.method} {request.path}")
        await asyncio.sleep(self.delay)
        return web.json_response(self.body)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app


def _closed_port() -> int:
    """A port nothing is listening on, for the connection-refused case."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RaisingSession:
    """Session stand-in that raises one chosen aiohttp error on ``request``.

    The gateway branches on the exception *type*, and reproducing a real
    ``sock_read`` expiry for every class would only be slower, not more faithful.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.timeouts: list[ClientTimeout] = []

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self.timeouts.append(kwargs["timeout"])
        return _RaisingContext(self._exc)


class _RaisingContext:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


async def _gateway_client(gateway, *, host: str, port: int, session: Any = None) -> TestClient:
    """Start the real gateway app pointed at ``host:port``.

    ``_upstream_base()`` reads ``ENGINE_HOST``/``ENGINE_PORT`` on every request,
    so overriding them routes the proxy without faking routes.
    """
    gateway.ENGINE_HOST = host
    gateway.ENGINE_PORT = port
    app = gateway.create_app()
    if session is not None:
        # ``on_startup`` would replace the deliberate failing session.
        app["session"] = session
        app.on_startup.clear()
        app.on_cleanup.clear()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _proxied(gateway, *, host: str, port: int, path: str = "/api/v1/discover/placements",
                   body: dict[str, Any] | None = None, session: Any = None) -> dict[str, Any]:
    """POST ``path`` through the gateway and return status/headers/json."""
    client = await _gateway_client(gateway, host=host, port=port, session=session)
    try:
        started = time.monotonic()
        if body is None:
            response = await client.get(path)
        else:
            response = await client.post(path, json=body)
        elapsed = time.monotonic() - started
        return {
            "status": response.status,
            "headers": dict(response.headers),
            "payload": await response.json(),
            "elapsed": elapsed,
        }
    finally:
        await client.close()


async def _with_upstream(gateway, upstream: _Upstream, fn) -> Any:
    server = TestClient(TestServer(upstream.app()))
    await server.start_server()
    try:
        return await fn(server.server.host, server.port)
    finally:
        await server.close()


# --------------------------------------------------------------------------- #
# The budget itself
# --------------------------------------------------------------------------- #


def test_analysis_budget_covers_every_allowed_lookback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy bound must be derived from the engine's own budget and exceed it.

    ``HISTORY_TIMEOUT_PER_HOUR_S = 2.5`` gives 168 h a 420 s history budget; the
    gateway must not cut that off at 30 s, and must hold for the largest window
    the API accepts (2160 h = 90 days) plus the fitting that follows the query.
    """
    from wattmosaic_engine.main import HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S

    gateway = _load_gateway(monkeypatch)

    # The numbers the gateway mirrors must still match the engine's, or the
    # derivation below is describing the wrong budget.
    assert gateway.ENGINE_HISTORY_BASE_S == HISTORY_TIMEOUT_BASE_S
    assert gateway.ENGINE_HISTORY_PER_HOUR_S == HISTORY_TIMEOUT_PER_HOUR_S
    assert gateway.MAX_LOOKBACK_HOURS == 2160.0

    budget_168h = max(HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S * 168)
    assert budget_168h == 420.0
    # 15.7x the engine's 168 h budget, and 220x the 30 s that failed live.
    assert gateway.ANALYSIS_TIMEOUT.sock_read == 6600.0
    assert gateway.ANALYSIS_TIMEOUT.sock_read > budget_168h * 15
    assert gateway.ANALYSIS_TIMEOUT.sock_read > OLD_SOCK_READ_S

    for hours in (24, 48, 72, 168, 720, 2160):
        engine_budget = max(HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S * hours)
        assert gateway.ANALYSIS_TIMEOUT.sock_read > engine_budget, hours
        # The history query is not the whole request: fitting follows it, so the
        # proxy must leave room beyond the history budget alone.
        assert gateway.ANALYSIS_TIMEOUT.sock_read - engine_budget >= 1200.0, hours

    # ``total`` bounds connect + upload + every read; if it fell below
    # ``sock_read`` it would silently reintroduce the same cap.
    assert gateway.ANALYSIS_TIMEOUT.total is not None
    assert gateway.ANALYSIS_TIMEOUT.total > gateway.ANALYSIS_TIMEOUT.sock_read
    # A tight connect keeps a dead engine from costing minutes to notice.
    assert gateway.ANALYSIS_TIMEOUT.connect == 5


def test_timeout_is_chosen_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only discovery may wait for minutes; liveness and ordinary calls may not."""
    gateway = _load_gateway(monkeypatch)

    class _Request:
        def __init__(self, method: str, path: str) -> None:
            self.method = method
            self.rel_url = URL(path)

    def pick(method: str, path: str, body: bytes = b"") -> ClientTimeout:
        return gateway._timeout_for(_Request(method, path), body)

    analysis = b'{"lookback_hours": 168}'
    assert pick("POST", "/api/v1/discover/placements", analysis) is gateway.ANALYSIS_TIMEOUT
    # A trailing slash is the same endpoint, not a silent bypass of the budget.
    assert pick("POST", "/api/v1/discover/placements/", b"{}") is gateway.ANALYSIS_TIMEOUT
    # GET is not discovery, even on the same path.
    assert pick("GET", "/api/v1/discover/placements") is gateway.DEFAULT_TIMEOUT
    assert pick("GET", "/api/v1/health") is gateway.HEALTH_TIMEOUT
    assert pick("GET", "/api/v1/health/") is gateway.HEALTH_TIMEOUT
    assert pick("POST", "/api/v1/commands", b'{"command": "run_diagnostics"}') is gateway.DEFAULT_TIMEOUT
    assert pick("GET", "/api/v1/snapshot") is gateway.DEFAULT_TIMEOUT


def test_the_integration_command_path_gets_the_analysis_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HA integration runs the same analysis through ``POST /commands``.

    ``service_logic``/``api_client.command("discover_placements", ...)`` reaches
    the same facade method as the panel's route, so it must not keep the 30 s
    budget while only the route is fixed -- that would just move the false 502 to
    the integration.
    """
    gateway = _load_gateway(monkeypatch)

    class _Request:
        def __init__(self, method: str, path: str) -> None:
            self.method = method
            self.rel_url = URL(path)

    request = _Request("POST", "/api/v1/commands")
    discovery = b'{"command": "discover_placements", "parameters": {"lookback_hours": 720}}'
    assert gateway._timeout_for(request, discovery) is gateway.ANALYSIS_TIMEOUT
    # And the requested window is named in the timeout message for that shape too.
    assert gateway._requested_lookback(discovery) == 720.0


def test_an_ordinary_command_keeps_the_default_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The long budget is for the slow analysis, not for every command."""
    gateway = _load_gateway(monkeypatch)

    class _Request:
        def __init__(self, method: str, path: str) -> None:
            self.method = method
            self.rel_url = URL(path)

    request = _Request("POST", "/api/v1/commands")
    for body in (
        b'{"command": "rebuild_model"}',
        b'{"command": "pause_learning"}',
        b"",
        b"not json",
    ):
        assert gateway._timeout_for(request, body) is gateway.DEFAULT_TIMEOUT, body


def test_liveness_budget_is_far_below_the_analysis_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/healthz`` must fail fast: the probe itself gives up after 5 s.

    The Supervisor watchdog and the container HEALTHCHECK both target
    ``/healthz`` (``addon/healthcheck.py`` uses 5 s, the Docker ``HEALTHCHECK``
    8 s).  A minutes-long budget there would keep reporting a dead engine alive
    and delay every restart.
    """
    gateway = _load_gateway(monkeypatch)
    assert gateway.HEALTH_TIMEOUT.sock_read is not None
    assert gateway.HEALTH_TIMEOUT.sock_read <= 5
    assert gateway.HEALTH_TIMEOUT.total is not None
    assert gateway.HEALTH_TIMEOUT.total <= 5
    assert gateway.HEALTH_TIMEOUT.sock_read < gateway.ANALYSIS_TIMEOUT.sock_read / 100


# --------------------------------------------------------------------------- #
# A slow discovery must finish
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sock_read", "expected"),
    [
        (OLD_SOCK_READ_S * SCALE, 504),  # the old 30 s budget, scaled
        (6600.0 * SCALE, 200),  # the new 6600 s budget, scaled
    ],
    ids=["old-budget-cuts-it-off", "new-budget-allows-it"],
)
def test_the_scaled_budget_pair_still_detects_the_regression(
    monkeypatch: pytest.MonkeyPatch, sock_read: float, expected: int
) -> None:
    """Positive control: the harness must still see the *old* budget fail.

    Without this, the passing slow-request test below could just mean "aiohttp
    ignored the timeout".  The same 1.2 s upstream fails at the scaled 30 s
    (0.03 s) and passes at the scaled 6600 s (6.6 s).
    """
    gateway = _load_gateway(monkeypatch)
    upstream = _Upstream(delay=SLOW_UPSTREAM_S)

    async def run() -> dict[str, Any]:
        async def go(host: str, port: int) -> dict[str, Any]:
            gateway.ANALYSIS_TIMEOUT = ClientTimeout(total=sock_read * 2, connect=5, sock_read=sock_read)
            return await _proxied(gateway, host=host, port=port, body={"lookback_hours": 168})

        return await _with_upstream(gateway, upstream, go)

    result = asyncio.run(run())
    assert result["status"] == expected
    if expected == 504:
        assert result["payload"]["error"] == "upstream_timeout"


def test_a_discovery_longer_than_the_old_budget_now_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression itself: slower than 30 s must not be a 502.

    Production measured 48 h, 72 h and 168 h dying at 30.02 s.  Here the upstream
    stalls 40x longer than the scaled old budget and 5.5x shorter than the scaled
    new one; the gateway must return the engine's own answer, untouched.
    """
    gateway = _load_gateway(monkeypatch)
    upstream = _Upstream(delay=SLOW_UPSTREAM_S, body={"ok": True, "lookback_hours": 168})

    async def run() -> dict[str, Any]:
        async def go(host: str, port: int) -> dict[str, Any]:
            gateway.ANALYSIS_TIMEOUT = ClientTimeout(
                total=6600.0 * SCALE * 2,
                connect=5,
                sock_read=6600.0 * SCALE,
            )
            return await _proxied(gateway, host=host, port=port, body={"lookback_hours": 168})

        return await _with_upstream(gateway, upstream, go)

    result = asyncio.run(run())
    assert result["status"] == 200, "a discovery slower than 30 s must not be a 502"
    assert result["headers"]["X-WattMosaic-Gateway"] == "1"
    assert result["payload"]["lookback_hours"] == 168
    assert upstream.requests == ["POST /api/v1/discover/placements"]


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #


def test_connection_refused_says_the_engine_is_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case where the old message was true, so it stays a 502."""
    gateway = _load_gateway(monkeypatch)
    port = _closed_port()

    async def run() -> dict[str, Any]:
        return await _proxied(gateway, host="127.0.0.1", port=port, body={"lookback_hours": 48})

    result = asyncio.run(run())
    assert result["status"] == 502
    assert result["payload"]["error"] == "bad_gateway"
    assert result["payload"]["detail"] == "engine is not reachable"


def test_an_upstream_timeout_is_not_reported_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine answered the socket; it just did not finish.  Say so.

    48 h, 72 h and 168 h were blamed on an unreachable engine while it was alive
    and working.  ``ServerTimeoutError`` must produce a distinct code and name
    the proxy budget and the requested window instead.
    """
    gateway = _load_gateway(monkeypatch)
    session = _RaisingSession(aiohttp.ServerTimeoutError("Timeout on reading data from socket"))
    gateway.ANALYSIS_TIMEOUT = ClientTimeout(total=6600, connect=5, sock_read=6600)

    async def run() -> dict[str, Any]:
        return await _proxied(
            gateway,
            host="127.0.0.1",
            port=8788,
            body={"lookback_hours": 168},
            session=session,
        )

    result = asyncio.run(run())
    assert result["status"] == 504
    assert result["payload"]["error"] == "upstream_timeout"
    assert "engine is not reachable" not in result["payload"]["detail"]
    assert "reachable" in result["payload"]["detail"]
    assert "6600 s" in result["payload"]["detail"]
    assert "lookback_hours=168" in result["payload"]["detail"]


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ServerTimeoutError("Timeout on reading data from socket"),
        aiohttp.SocketTimeoutError("Timeout on reading data from socket"),
        TimeoutError(),
    ],
    ids=["server_timeout", "socket_timeout", "builtin_timeout"],
)
def test_every_timeout_spelling_maps_to_upstream_timeout(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """All the timeout classes aiohttp can surface mean the same thing.

    ``SocketTimeoutError`` is a subclass of ``ServerTimeoutError``, and both are
    ``TimeoutError``/``OSError`` subclasses themselves; catching the generic
    errors first is what used to collapse a timeout into "not reachable".
    """
    gateway = _load_gateway(monkeypatch)
    session = _RaisingSession(exc)

    async def run() -> dict[str, Any]:
        return await _proxied(
            gateway, host="127.0.0.1", port=8788, body={}, session=session
        )

    result = asyncio.run(run())
    assert result["status"] == 504
    assert result["payload"]["error"] == "upstream_timeout"


def test_a_connect_timeout_still_blames_reachability(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ConnectionTimeoutError`` means the socket was never accepted.

    It is a ``ServerTimeoutError`` subclass, so it must be caught first; unlike a
    ``sock_read`` expiry it really is a reachability problem and keeps the 502.
    """
    gateway = _load_gateway(monkeypatch)
    session = _RaisingSession(aiohttp.ConnectionTimeoutError("Connection timeout"))

    async def run() -> dict[str, Any]:
        return await _proxied(
            gateway,
            host="127.0.0.1",
            port=8788,
            path="/api/v1/commands",
            body={"command": "discover_placements"},
            session=session,
        )

    result = asyncio.run(run())
    assert result["status"] == 502
    assert result["payload"]["error"] == "bad_gateway"
    assert result["payload"]["detail"].startswith("engine is not reachable")


def test_a_reset_socket_still_blames_reachability(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport-level ``OSError`` is genuinely "not reachable"."""
    gateway = _load_gateway(monkeypatch)
    session = _RaisingSession(ConnectionResetError("peer reset"))

    async def run() -> dict[str, Any]:
        return await _proxied(
            gateway, host="127.0.0.1", port=8788, body={}, session=session
        )

    result = asyncio.run(run())
    assert result["status"] == 502
    assert result["payload"]["detail"] == "engine is not reachable"


# --------------------------------------------------------------------------- #
# Liveness keeps its own, short budget
# --------------------------------------------------------------------------- #


def test_healthz_fails_fast_and_does_not_inherit_the_analysis_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled engine must still be reported unhealthy within seconds.

    ``/healthz`` drives the Supervisor watchdog and the container HEALTHCHECK
    (5-8 s); with the analysis budget a dead engine would look alive for over an
    hour and never be restarted.
    """
    gateway = _load_gateway(monkeypatch)
    upstream = _Upstream(delay=1.5, body={"status": "ok"})

    async def run() -> dict[str, Any]:
        async def go(host: str, port: int) -> dict[str, Any]:
            # Scaled so the test is quick; the shipped value is 4 s.
            gateway.HEALTH_TIMEOUT = ClientTimeout(total=0.3, connect=0.2, sock_read=0.3)
            gateway.ANALYSIS_TIMEOUT = ClientTimeout(total=6600, connect=5, sock_read=6600)
            client = await _gateway_client(gateway, host=host, port=port)
            try:
                started = time.monotonic()
                response = await client.get("/healthz")
                return {
                    "status": response.status,
                    "payload": await response.text(),
                    "elapsed": time.monotonic() - started,
                }
            finally:
                await client.close()

        return await _with_upstream(gateway, upstream, go)

    result = asyncio.run(run())
    assert result["status"] == 503
    assert result["payload"] == "unavailable"
    assert result["elapsed"] < 1.5, "healthz must not wait for the analysis budget"


def test_proxied_health_uses_the_short_budget_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel polls ``/api/v1/health``; it must not hang for minutes either."""
    gateway = _load_gateway(monkeypatch)
    upstream = _Upstream(delay=1.5, body={"status": "ok"})

    async def run() -> dict[str, Any]:
        async def go(host: str, port: int) -> dict[str, Any]:
            gateway.HEALTH_TIMEOUT = ClientTimeout(total=0.3, connect=0.2, sock_read=0.3)
            gateway.ANALYSIS_TIMEOUT = ClientTimeout(total=6600, connect=5, sock_read=6600)
            return await _proxied(gateway, host=host, port=port, path="/api/v1/health")

        return await _with_upstream(gateway, upstream, go)

    result = asyncio.run(run())
    assert result["status"] == 504
    assert result["payload"]["error"] == "upstream_timeout"
