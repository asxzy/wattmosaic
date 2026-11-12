#!/usr/bin/env python3
"""WattMosaic Engine — ingress / auth gateway.

Why this exists
---------------
Home Assistant's Supervisor watchdog and ingress proxy talk to the app's
``ingress_port``.  The engine's own API is authenticated with ``X-API-Token``,
which the Supervisor cannot attach to a watchdog probe, and Home Assistant
users viewing the ingress panel would otherwise have to paste the token into a
URL.

The gateway is a *very* small reverse proxy that:

* listens on the ingress port (default 8787) inside the app container,
* exposes an unauthenticated liveness endpoint ``/healthz`` (no data, no
  secrets) for the Supervisor watchdog and Docker HEALTHCHECK,
* forwards everything else to the engine on loopback (default 127.0.0.1:8788),
* injects ``X-API-Token`` for requests that arrive through the Supervisor
  ingress proxy (already authenticated as a Home Assistant user), and
* requires a valid ``X-API-Token`` from every other peer.

It never listens on a public interface beyond the app's own Docker network, and
it never logs headers or tokens.

Configuration (environment):
    WM_GATEWAY_PORT                 default 8787
    WM_GATEWAY_BIND                 default 0.0.0.0
    WM_GATEWAY_TRUSTED_INGRESS_IPS  default 172.30.32.2 (Supervisor)
    WM_API_HOST / WM_API_PORT       upstream engine, default 127.0.0.1:8788
    WM_API_TOKEN                    shared token, may be empty
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web

APP_DIR = Path(__file__).resolve().parent
#: The panel is shipped beside gateway.py.  A staged App has it at
#: ``<stage>/ui``; the container copies it to ``/app/ui``.
UI_DIR = APP_DIR / "ui"
ENGINE_HOST = os.environ.get("WM_API_HOST", "127.0.0.1")
ENGINE_PORT = int(os.environ.get("WM_API_PORT", "8788"))
GATEWAY_PORT = int(os.environ.get("WM_GATEWAY_PORT", "8787"))
GATEWAY_BIND = os.environ.get("WM_GATEWAY_BIND", "0.0.0.0")
API_TOKEN = os.environ.get("WM_API_TOKEN", "")
TRUSTED_IPS = {
    ip.strip()
    for ip in os.environ.get("WM_GATEWAY_TRUSTED_INGRESS_IPS", "172.30.32.2").split(",")
    if ip.strip()
}

# When no API token is configured the gateway cannot authenticate anybody, so it
# only serves peers that can only be reached from inside the App's own Docker
# network: loopback (the container's own HEALTHCHECK) and the Supervisor's
# internal network (Home Assistant Core, the integration, the ingress proxy).
# A host port published by the user is *not* in these ranges, so enabling one
# without setting api_token still fails closed.
UNAUTHENTICATED_NETWORKS = tuple(
    ipaddress.ip_network(part.strip(), strict=False)
    for part in os.environ.get(
        "WM_GATEWAY_UNAUTHENTICATED_NETWORKS",
        "127.0.0.0/8,::1/128,172.30.32.0/23",
    ).split(",")
    if part.strip()
)


TOKEN_HEADER = "X-API-Token"
CONTRACT_HEADER = "X-WattMosaic-Contract-Version"
REMOTE_USER_HEADER = "X-Remote-User-Id"

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1), plus the ingress
# identity headers, which we never trust from a non-Supervisor peer.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "expect",
    }
)
DROP_REQUEST_HEADERS = HOP_BY_HOP | {
    "x-api-token",
    "x-remote-user-id",
    "x-remote-user-name",
    "x-remote-user-display-name",
    "x-remote-user-groups",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
}
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding", "content-length"}

MAX_BODY = 8 * 1024 * 1024

#: The engine's own budget for the placement-discovery history query, mirrored
#: from ``engine/wattmosaic_engine/main.py`` (``HISTORY_TIMEOUT_BASE_S`` and
#: ``HISTORY_TIMEOUT_PER_HOUR_S``): ``max(60, 2.5 * lookback_hours)``.  The two
#: constants are duplicated rather than imported because the gateway is deployed
#: as a single file beside the engine, not as part of its package;
#: ``test_analysis_budget_covers_every_allowed_lookback`` imports the engine's
#: real constants and fails if they ever exceed what the proxy allows.
ENGINE_HISTORY_BASE_S = 60.0
ENGINE_HISTORY_PER_HOUR_S = 2.5
#: The API refuses a window longer than 90 days (``main.py`` rejects
#: ``lookback_hours > 2160``), so 2160 h is the largest history query a caller
#: can legitimately ask for.
MAX_LOOKBACK_HOURS = 2160.0
#: Silent time the engine needs *after* the history query: correlation, fitting
#: and placement selection all run before the response is written.  At 24 h the
#: whole request was measured at 9.8 s against a 60 s history floor, so the
#: non-history work was a few seconds; scaling that linearly to 2160 h (90x)
#: gives ~900 s, and 1200 s leaves headroom for the larger fit.
ANALYSIS_FIT_OVERHEAD_S = 1200.0

#: Socket-read budget for the placement-discovery request, derived from the
#: numbers above.
#:
#: ``sock_read`` is a *per-read* timeout, and the engine deliberately writes
#: nothing while it works: ``POST /api/v1/discover/placements`` runs a
#: whole-house recorder analysis and answers only when it is done.  Measured on
#: production ``0.2.1-dev.18`` against real recorder history: 24 h answered
#: HTTP 200 in 9.8 s, while 48 h, 72 h and 168 h were all killed by the previous
#: ``sock_read=30`` after 30.02 s and surfaced as a false "engine is not
#: reachable" 502.
#:
#: The proxy budget therefore covers the engine's *own* budget for the longest
#: window the API accepts -- 2160 h * 2.5 s/h = 5400 s of history -- plus the
#: fitting that follows it: 6600 s.  That is 15.7x the 420 s the engine grants a
#: 168 h query and 220x the 30 s that used to fail, and it is still a real
#: bound: a hung engine cannot pin a panel request forever.
ANALYSIS_TIMEOUT_S = MAX_LOOKBACK_HOURS * ENGINE_HISTORY_PER_HOUR_S + ANALYSIS_FIT_OVERHEAD_S  # 6600 s

ANALYSIS_TIMEOUT = ClientTimeout(
    # ``total`` bounds the whole exchange (connect + upload + every read), so it
    # must stay above ``sock_read`` or it would silently reintroduce the same
    # cap: 10 minutes of slack covers the upload and the response write.
    total=ANALYSIS_TIMEOUT_S + 600,
    # Dialling a loopback container that never accepts is a dead engine, and
    # finding that out in 5 s instead of in the analysis budget is the point.
    connect=5,
    sock_read=ANALYSIS_TIMEOUT_S,
)

#: Budget for every other proxied request.  Unchanged from the old single
#: timeout on purpose: it was only wrong for the one endpoint that stays silent
#: for minutes.  Keeping it means a hung engine still frees the panel in under a
#: minute, exactly as before this fix.
DEFAULT_TIMEOUT = ClientTimeout(total=60, connect=5, sock_read=30)

#: Liveness gets its own, short budget.  ``/healthz`` backs the Supervisor
#: watchdog (``addon/config.yaml``) and the container HEALTHCHECK
#: (``--timeout=8s``, and ``addon/healthcheck.py`` gives up after 5 s), so it has
#: to answer in a couple of seconds -- with the analysis budget a dead engine
#: would not be restarted for over an hour.  The same budget covers the cheap
#: ``/api/v1/health`` the panel polls: it was measured at 1.1-2.3 s, so 4 s
#: keeps a real bound without failing a slow disk.
HEALTH_TIMEOUT = ClientTimeout(total=4, connect=2, sock_read=4)

logging.basicConfig(
    level=getattr(logging, os.environ.get("WM_LOG_LEVEL", "INFO").upper(), logging.INFO),
    stream=sys.stdout,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("wattmosaic.gateway")

#: The panel is static files, not an inline string.  Inline HTML cannot be
#: cached separately from the response, and a single 300-line blob is impossible
#: to test or lint.  These files are served by :func:`static` below.
UI_INDEX = UI_DIR / "index.html"
UI_ASSETS = {
    "ui/app.css": UI_DIR / "app.css",
    "ui/app.js": UI_DIR / "app.js",
    "ui/mark.svg": UI_DIR / "mark.svg",
}
#: (content type, charset).  aiohttp appends the charset itself, so it must not
#: be baked into ``content_type``.
ASSET_TYPES = {
    ".css": ("text/css", "utf-8"),
    ".js": ("text/javascript", "utf-8"),
    ".svg": ("image/svg+xml", None),
    ".html": ("text/html", "utf-8"),
}

#: Content-Security-Policy for the panel.  No external origins are allowed at
#: all: the App has no internet access at runtime, so a CDN reference would
#: fail silently in the field.  ``style-src`` allows the inline ``style``
#: attribute the panel uses for meter widths and dial values; everything else
#: is a same-origin file.
PANEL_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'self'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(status: int, error: str, detail: str | None = None) -> web.Response:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return web.json_response(body, status=status)


def _peer_trusted(request: web.Request) -> bool:
    peer = request.remote or ""
    return peer in TRUSTED_IPS and bool(request.headers.get(REMOTE_USER_HEADER))


def _peer_internal(request: web.Request) -> bool:
    """True when the peer can only be inside the App's own Docker network."""
    peer = request.remote or ""
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in UNAUTHENTICATED_NETWORKS)


def _authorised(request: web.Request) -> tuple[bool, bool]:
    """Return (allowed, via_ingress)."""
    if _peer_trusted(request):
        return True, True
    if not API_TOKEN:
        # No token configured: the API is unauthenticated, but only for peers
        # that cannot be reached from outside the Supervisor network.
        return _peer_internal(request), False
    supplied = request.headers.get(TOKEN_HEADER, "")
    return (
        bool(supplied) and hmac.compare_digest(supplied.encode(), API_TOKEN.encode()),
        False,
    )


def _upstream_base() -> str:
    return f"http://{ENGINE_HOST}:{ENGINE_PORT}"


def _analysis_request(request: web.Request, body: bytes) -> bool:
    """True when the request runs the slow placement analysis.

    Two callers reach the same work:

    * ``POST /api/v1/discover/placements`` (the panel); and
    * ``POST /api/v1/commands`` with ``{"command": "discover_placements", ...}``
      (the Home Assistant integration's service).

    Both answer only after reading the whole requested recorder window and
    fitting it, so both need :data:`ANALYSIS_TIMEOUT`.  Missing the command path
    would just move the same false failure to the integration.
    """
    path = request.rel_url.path.rstrip("/")
    if request.method != "POST":
        return False
    if path == "/api/v1/discover/placements":
        return True
    if path != "/api/v1/commands" or not body:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("command") == "discover_placements"


def _liveness_request(request: web.Request) -> bool:
    """True for the proxied health path, which must never inherit a long budget.

    ``/healthz`` never reaches here: it has its own handler and its own
    :data:`HEALTH_TIMEOUT` in :func:`_fetch_health`.  This covers the
    ``/api/v1/health`` the panel polls on every overview refresh, which is cheap
    (measured at 1.1-2.3 s) and must fail fast like the probe.
    """
    return request.method == "GET" and request.rel_url.path.rstrip("/") == "/api/v1/health"


def _requested_lookback(body: bytes) -> float | None:
    """Read the requested window from a discovery body, for an honest message.

    Accepts both shapes: ``{"lookback_hours": N}`` on the route and
    ``{"command": ..., "parameters": {"lookback_hours": N}}`` on the command
    path.  The body has already been validated by the engine, so this only makes
    the failure explain itself; anything unexpected returns ``None`` rather than
    raising on an error path.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    params = payload.get("parameters")
    if isinstance(params, dict) and "lookback_hours" in params:
        payload = params
    window = payload.get("lookback_hours")
    if isinstance(window, bool) or not isinstance(window, (int, float)):
        return None
    return float(window)


def _upstream_timeout_response(request: web.Request, body: bytes) -> web.Response:
    """504 for an upstream that accepted the request but did not finish in budget.

    This is emphatically *not* "engine is not reachable": the engine was
    reachable and was still working.  Saying otherwise is what made the live
    48 h, 72 h and 168 h discoveries look like a dead container when the engine
    had simply been cut off at 30 s.

    504 rather than 502 because the gateway, not the engine, ran out of time --
    and because 504 is already the engine's own code for "exceeded a timeout"
    (``api/server.py`` maps ``TimeoutError`` to ``504 timeout``), so a client
    that handled a slow engine keeps working.  The integration already treats
    504 as retryable (``api_client._RETRYABLE_STATUS``), so nothing that only
    checked 502 breaks on a timeout -- the old 502 was the wrong answer anyway.
    """
    budget = _timeout_for(request, body)
    detail = (
        "the engine was reachable but sent no response within the gateway's "
        f"{int(budget.sock_read or 0)} s proxy budget; this is a gateway timeout, "
        "not an engine failure"
    )
    window = _requested_lookback(body)
    if window is not None:
        detail += f" (lookback_hours={window:g})"
    return _json(504, "upstream_timeout", detail)


def _timeout_for(request: web.Request, body: bytes = b"") -> ClientTimeout:
    """Pick the proxy budget: long for analysis, short for liveness, default else."""
    if _analysis_request(request, body):
        return ANALYSIS_TIMEOUT
    if _liveness_request(request):
        return HEALTH_TIMEOUT
    return DEFAULT_TIMEOUT


async def _fetch_health(session: ClientSession) -> tuple[int, dict[str, Any] | None]:
    """Probe the engine's cheap ``/api/v1/health`` under the *probe* budget.

    It deliberately does not use :data:`ANALYSIS_TIMEOUT`: ``/healthz`` feeds the
    Supervisor watchdog and the container HEALTHCHECK, whose own timeout is 8 s,
    so a multi-minute budget here would keep reporting "healthy" long after the
    engine had died and delay every restart.
    """
    headers = {TOKEN_HEADER: API_TOKEN} if API_TOKEN else {}
    try:
        async with session.get(
            f"{_upstream_base()}/api/v1/health", headers=headers, timeout=HEALTH_TIMEOUT
        ) as resp:
            if resp.content_type != "application/json":
                return resp.status, None
            payload = await resp.json()
            return resp.status, payload if isinstance(payload, dict) else None
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError):
        return 0, None


async def healthz(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe. Returns no data beyond a status word."""
    status, payload = await _fetch_health(request.app["session"])
    healthy = status == 200 and payload is not None and payload.get("status") in ("ok", "degraded")
    return web.Response(
        text="ok" if healthy else "unavailable",
        status=200 if healthy else 503,
        content_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


def _panel_headers() -> dict[str, str]:
    """Headers for the HTML panel and its assets.

    ``Cache-Control: no-store`` is deliberate: the panel is served from an App
    whose ingress URL contains a per-install token, and an upgraded image must
    never leave a browser running the previous release's script.
    """
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": PANEL_CSP,
        **SECURITY_HEADERS,
    }


async def index(request: web.Request) -> web.Response:
    # The panel is only served to Supervisor-ingress peers; direct API clients
    # get a small JSON descriptor instead of an HTML page.
    if not _peer_trusted(request):
        return web.json_response(
            {"service": "wattmosaic-engine", "api": "/api/v1", "health": "/healthz"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        html = UI_INDEX.read_text(encoding="utf-8")
    except OSError:
        # A packaged image without the UI directory must fail loudly and
        # usefully: the API is still reachable, and the message says so.
        log.error("panel assets missing at %s", UI_INDEX)
        return _json(503, "panel_unavailable", "the UI files are missing from this image")
    return web.Response(text=html, content_type="text/html", headers=_panel_headers())


async def static(request: web.Request) -> web.Response:
    """Serve one immutable panel asset.

    Only the fixed allowlist in :data:`UI_ASSETS` is reachable, so a crafted
    path cannot walk outside the UI directory.  Assets are also gated on the
    ingress peer: an unauthenticated caller on a published host port must not
    be able to enumerate or fetch them.
    """
    if not _peer_trusted(request):
        return _json(401, "unauthorized", "the panel is only served through Home Assistant ingress")

    name = request.match_info["asset"]
    path = UI_ASSETS.get(f"ui/{name}")
    if path is None:
        return _json(404, "not_found", "unknown asset")
    try:
        payload = path.read_bytes()
    except OSError:
        log.error("panel asset missing: %s", path)
        return _json(503, "panel_unavailable", "the UI files are missing from this image")

    content_type, charset = ASSET_TYPES.get(path.suffix, ("application/octet-stream", None))
    return web.Response(
        body=payload,
        content_type=content_type,
        charset=charset,
        headers=_panel_headers(),
    )


async def proxy(request: web.Request) -> web.StreamResponse:
    allowed, via_ingress = _authorised(request)
    if not allowed:
        return _json(
            401,
            "unauthorized",
            "send X-API-Token, or open this app through Home Assistant ingress",
        )

    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in DROP_REQUEST_HEADERS
    }
    headers["Host"] = f"{ENGINE_HOST}:{ENGINE_PORT}"
    if API_TOKEN:
        headers[TOKEN_HEADER] = API_TOKEN
    if via_ingress:
        headers["X-WattMosaic-Ingress"] = "1"

    body = await request.read()
    if len(body) > MAX_BODY:
        return _json(413, "payload_too_large", "request body exceeds 8 MiB")

    session: ClientSession = request.app["session"]
    try:
        async with session.request(
            request.method,
            f"{_upstream_base()}{request.rel_url}",
            headers=headers,
            data=body or None,
            timeout=_timeout_for(request, body),
            allow_redirects=False,
        ) as upstream:
            payload = await upstream.read()
            response = web.Response(
                status=upstream.status,
                body=payload,
                headers={
                    name: value
                    for name, value in upstream.headers.items()
                    if name.lower() not in DROP_RESPONSE_HEADERS
                },
            )
            response.headers.setdefault(CONTRACT_HEADER, "unknown")
            response.headers["X-WattMosaic-Gateway"] = "1"
            return response
    except aiohttp.ConnectionTimeoutError:
        # ``ConnectionTimeoutError`` is also a ``ServerTimeoutError`` and an
        # ``OSError``; it means the TCP connection was never accepted within the
        # 5 s connect budget.  That is a reachability failure, not a slow engine,
        # so it keeps the 502 -- but it names the connect step so it is not
        # mistaken for a busy analysis.
        log.warning("upstream did not accept a connection (%s %s)", request.method, request.rel_url.path)
        return _json(
            502,
            "bad_gateway",
            "engine is not reachable: it did not accept a connection within 5 s",
        )
    except aiohttp.ServerTimeoutError:
        # ``ServerTimeoutError`` (and therefore ``SocketTimeoutError``) covers
        # ``sock_read`` and ``total``.  It is an ``OSError`` and a
        # ``TimeoutError`` subclass, so it must be caught *before* the generic
        # clauses below.
        log.warning(
            "upstream timed out after %s s (%s %s)",
            _timeout_for(request, body).sock_read,
            request.method,
            request.rel_url.path,
        )
        return _upstream_timeout_response(request, body)
    except TimeoutError:
        # ``asyncio.TimeoutError`` is ``TimeoutError`` on 3.11+, and a handler
        # timeout may arrive unwrapped.  Same meaning: reachable, too slow.
        log.warning("upstream timed out (%s %s)", request.method, request.rel_url.path)
        return _upstream_timeout_response(request, body)
    except (aiohttp.ClientConnectorError, aiohttp.ClientError, OSError) as exc:
        # A refused/missing connection or a reset socket: the engine really is
        # unreachable, and that message is accurate.
        log.warning("upstream request failed: %s", type(exc).__name__)
        return _json(502, "bad_gateway", "engine is not reachable")


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession(auto_decompress=True)
    log.info(
        "gateway listening on %s:%d -> engine %s:%d trusted_ingress=%s",
        GATEWAY_BIND,
        GATEWAY_PORT,
        ENGINE_HOST,
        ENGINE_PORT,
        ",".join(sorted(TRUSTED_IPS)) or "(none)",
    )


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    app.router.add_get("/ui/{asset}", static)
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> int:
    if not API_TOKEN:
        log.warning(
            "WM_API_TOKEN is empty; serving only loopback and the Supervisor "
            "network (set api_token to allow other clients)"
        )
    web.run_app(
        create_app(),
        host=GATEWAY_BIND,
        port=GATEWAY_PORT,
        access_log=None,
        print=None,
        shutdown_timeout=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
