#!/usr/bin/env python3
"""Container HEALTHCHECK for the WattMosaic Engine.

Probes the gateway's unauthenticated liveness endpoint ``/healthz`` on the
ingress port. That endpoint is *not* a shallow port check: the gateway itself
calls the engine's ``/api/v1/health`` with its own ``WM_API_TOKEN`` and returns
200 only when the engine reports ``ok`` or ``degraded``. One request therefore
covers the gateway, the engine, the SQLite database and the HA ingestion task.

Why the probe does not call ``/api/v1/health`` directly
-------------------------------------------------------
A Docker HEALTHCHECK is a fresh ``exec`` whose environment comes from the
container configuration, not from PID 1. ``run.sh`` sets ``WM_API_TOKEN`` from
the App's ``api_token`` option *inside* the entrypoint, so the probe process
never sees it. Calling the engine directly therefore sent no
``X-API-Token`` and was rejected with 401, which made every App install report
unhealthy and restart on the Supervisor watchdog. Routing through ``/healthz``
keeps the token in exactly one process.

Exit codes:
    0   healthy
    1   unhealthy (gateway unreachable, or the engine is not ok/degraded)
The check is deliberately quiet: it never prints token values.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

GATEWAY_URL = os.environ.get("WM_HEALTH_GATEWAY_URL", "http://127.0.0.1:8787/healthz")
TIMEOUT_S = float(os.environ.get("WM_HEALTH_TIMEOUT_S", "5"))


def _get(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - local URL
            return response.status, response.read(4096)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, type(exc).__name__.encode()


def main() -> int:
    status, body = _get(GATEWAY_URL)
    if status == 200:
        print(f"healthy: {body[:40].decode('utf-8', 'replace')}")
        return 0
    if status == 0:
        print(f"unhealthy: gateway /healthz unreachable ({body[:40].decode('utf-8', 'replace')})")
        return 1
    print(f"unhealthy: gateway /healthz returned {status} {body[:80]!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
