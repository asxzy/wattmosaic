"""Stand-in for Home Assistant ingress, for local development only.

Forwards every request to the WattMosaic gateway and adds the
``X-Remote-User-Id`` header HA ingress adds.  The gateway serves the panel and
injects the API token only for a trusted ingress peer, so it must list this
proxy's address in ``WM_GATEWAY_TRUSTED_INGRESS_IPS`` (docker-compose.dev.yml
does that with a fixed address on its own network).

Anyone who can reach this proxy gets the panel and an authenticated API.  Only
run it on a trusted network.

Environment:
    WM_PROXY_UPSTREAM  gateway URL           (default http://127.0.0.1:8787)
    WM_PROXY_HOST      listen address        (default 0.0.0.0)
    WM_PROXY_PORT      listen port           (default 8790)
"""

from __future__ import annotations

import os

import aiohttp
from aiohttp import web

UPSTREAM = os.environ.get("WM_PROXY_UPSTREAM", "http://127.0.0.1:8787").rstrip("/")
HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}


async def forward(request: web.Request) -> web.StreamResponse:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["X-Remote-User-Id"] = "local-dev"
    headers["X-Remote-User-Name"] = "local-dev"
    body = await request.read()
    session: aiohttp.ClientSession = request.app["session"]
    async with session.request(
        request.method,
        UPSTREAM + request.rel_url.raw_path_qs,
        headers=headers,
        data=body or None,
        allow_redirects=False,
    ) as upstream:
        payload = await upstream.read()
        out = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP | {"content-encoding"}}
        return web.Response(status=upstream.status, body=payload, headers=out)


async def _session(app: web.Application):
    app["session"] = aiohttp.ClientSession(auto_decompress=True)
    yield
    await app["session"].close()


def main() -> None:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.cleanup_ctx.append(_session)
    app.router.add_route("*", "/{tail:.*}", forward)
    host = os.environ.get("WM_PROXY_HOST", "0.0.0.0")  # noqa: S104 - dev proxy, see docstring
    port = int(os.environ.get("WM_PROXY_PORT", "8790"))
    print(f"dev ingress proxy on {host}:{port} -> {UPSTREAM}", flush=True)
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
