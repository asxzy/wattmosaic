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
UPSTREAM_TIMEOUT = ClientTimeout(total=60, connect=5, sock_read=30)

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


async def _fetch_health(session: ClientSession) -> tuple[int, dict[str, Any] | None]:
    headers = {TOKEN_HEADER: API_TOKEN} if API_TOKEN else {}
    try:
        async with session.get(
            f"{_upstream_base()}/api/v1/health", headers=headers, timeout=UPSTREAM_TIMEOUT
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
            timeout=UPSTREAM_TIMEOUT,
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
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
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
