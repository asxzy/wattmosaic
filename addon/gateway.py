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
from typing import Any

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web

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

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WattMosaic Engine</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0; padding: 2rem; line-height: 1.5; }
  main { max-width: 44rem; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
  .sub { opacity: .7; margin-top: 0; }
  table { border-collapse: collapse; margin-top: 1.5rem; width: 100%; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #8884; }
  th { width: 40%; font-weight: 600; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .ok { color: #1a7f37; } .warn { color: #9a6700; } .bad { color: #cf222e; }
  footer { margin-top: 2rem; font-size: .85rem; opacity: .7; }
</style>
</head>
<body>
<main>
  <h1>WattMosaic Engine</h1>
  <p class="sub">Multi-CT load disaggregation engine &mdash; API <code>/api/v1</code></p>
  <table>
    <tr><th>Engine status</th><td id="status">&mdash;</td></tr>
    <tr><th>Home Assistant</th><td id="ha">&mdash;</td></tr>
    <tr><th>Database</th><td id="db">&mdash;</td></tr>
    <tr><th>Contract version</th><td id="contract">&mdash;</td></tr>
    <tr><th>Uptime</th><td id="uptime">&mdash;</td></tr>
  </table>
  <p>Raw endpoints: <code>/api/v1/health</code>, <code>/api/v1/status</code>,
     <code>/api/v1/snapshot</code>, <code>/api/v1/diagnostics</code>.</p>
  <footer>This panel is served by the app gateway. It shows liveness only;
  configure topology in the WattMosaic Home Assistant integration.</footer>
</main>
<script>
async function refresh() {
  const set = (id, text, cls) => {
    const el = document.getElementById(id);
    el.textContent = text;
    el.className = cls || "";
  };
  try {
    const res = await fetch("api/v1/health", {cache: "no-store"});
    if (!res.ok) { set("status", "HTTP " + res.status, "bad"); return; }
    const d = await res.json();
    set("status", d.status || "unknown",
        d.status === "ok" ? "ok" : d.status === "degraded" ? "warn" : "bad");
    set("ha", d.ha_connected ? "connected" : "disconnected",
        d.ha_connected ? "ok" : "warn");
    set("db", d.database || "unknown", d.database === "ok" ? "ok" : "warn");
    set("contract", d.contract_version || "unknown");
    const s = Math.round(d.uptime_seconds || 0);
    set("uptime", Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m " + (s % 60) + "s");
  } catch (err) {
    set("status", "engine unreachable", "bad");
  }
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


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


async def index(request: web.Request) -> web.Response:
    # The panel is only served to Supervisor-ingress peers; direct API clients
    # get the JSON health document instead of an HTML page.
    if not _peer_trusted(request):
        return web.json_response(
            {"service": "wattmosaic-engine", "api": "/api/v1", "health": "/healthz"},
            headers={"Cache-Control": "no-store"},
        )
    return web.Response(
        text=INDEX_HTML,
        content_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
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
