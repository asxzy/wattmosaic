#!/usr/bin/env python3
"""WattMosaic Engine — app-install discovery publisher.

Why this exists
---------------
Without it a user who installs the App has to work out what to type into the
integration's connection step: a container hostname that the Supervisor derives
(and prefixes with a hash for locally installed apps), plus a port.  Guessing is
where the integration setup stalls.

Home Assistant has a purpose-built mechanism for exactly this.  An App lists the
integration domains it can feed under ``discovery:`` in ``config.yaml``, then
POSTs ``{"service": <domain>, "config": {...}}`` to the Supervisor.  The
Supervisor validates that the domain is in that list, stores the message, and
Home Assistant starts a config flow with ``async_step_hassio(discovery_info)``.
The user confirms one screen instead of typing an address.

What is announced
-----------------
Only what the integration needs to reach the API: ``host`` and ``port``.  No
token, no secrets, no household data.  The message is stored by the Supervisor
and is visible to Home Assistant, so anything placed here should be treated as
public within the installation.

The host is resolved in this order, because the App's DNS name is not fixed:

1. ``WM_DISCOVERY_HOST``, if set (an operator override);
2. the Supervisor's own record of this App (``GET /addons/self/info``), which
   already contains any local-install hash prefix;
3. ``socket.gethostname()``, which the Supervisor sets to the same value;
4. the app slug's dashed form, which is the Supervisor's DNS naming rule.

Failure is never fatal: outside the Supervisor (plain Docker, CI, a developer
laptop) there is no discovery to publish, and a Supervisor that is still booting
may refuse the first attempts.  The publisher retries for a bounded time, logs a
single warning if it gives up, and exits 0.  The App must start either way.

Configuration (environment):
    SUPERVISOR_TOKEN        required; supplied by the Supervisor
    WM_DISCOVERY_ENABLED    default true ("false" disables publishing)
    WM_DISCOVERY_HOST       optional override for the announced host
    WM_DISCOVERY_PORT       default 8787 (the gateway's ingress port)
    WM_DISCOVERY_SERVICE    default "wattmosaic" (the integration domain)
    WM_DISCOVERY_WAIT_S     default 120; how long to wait for the gateway

Usage:
    python3 discovery.py            publish and exit
    python3 discovery.py --dry-run  print the resolved message and exit
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any

SUPERVISOR_URL = os.environ.get("WM_SUPERVISOR_URL", "http://supervisor").rstrip("/")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

#: Default gateway ingress port (``ingress_port`` in config.yaml).
DEFAULT_PORT = "8787"
#: The integration domain; the Supervisor routes the message by service name.
DEFAULT_SERVICE = "wattmosaic"

#: One DNS label: no leading/trailing hyphen, at most 63 characters.
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
#: A bare host name (optionally fully qualified) — no scheme, path, userinfo,
#: port or whitespace.  Mirrors ``flow_logic.discovered_base_url``; a test keeps
#: the two in step, because shipping a value the integration rejects would only
#: produce a discovery card that cannot connect.
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")
#: A bracketed IPv6 literal, the only host form that legitimately contains ``:``.
_IPV6_RE = re.compile(r"^\[[0-9A-Fa-f:.]+\]$")
#: Names that point Home Assistant at itself rather than at the App.
_LOOPBACK_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "0.0.0.0", "::1", "[::1]"})


def is_announceable_host(value: str) -> bool:
    """True if ``value`` is a host name the integration can turn into a URL.

    Rejects URL syntax (a path, userinfo, scheme or embedded port) and anything
    loopback.  Both matter: ``user@evil.example`` would otherwise be announced as
    ``http://evil.example``, and announcing ``localhost`` would make Home
    Assistant connect to itself.
    """
    if not value or any(ch.isspace() for ch in value):
        return False
    if value.lower() in _LOOPBACK_NAMES or value.lower() in _LOOPBACK_ADDRESSES:
        return False
    if _IPV6_RE.match(value):
        return True
    return bool(_HOSTNAME_RE.match(value))


def _float_or_zero(value: str, fallback: float = 120.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return fallback


def _enabled() -> bool:
    return os.environ.get("WM_DISCOVERY_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


def _host_override() -> str:
    return os.environ.get("WM_DISCOVERY_HOST", "").strip()


def _port() -> str:
    return os.environ.get("WM_DISCOVERY_PORT", DEFAULT_PORT).strip() or DEFAULT_PORT


def _service() -> str:
    return os.environ.get("WM_DISCOVERY_SERVICE", DEFAULT_SERVICE).strip() or DEFAULT_SERVICE


def _wait_seconds() -> float:
    return _float_or_zero(os.environ.get("WM_DISCOVERY_WAIT_S", "120"))


def _health_path() -> str:
    return os.environ.get("WM_DISCOVERY_HEALTH_PATH", "/healthz")


def log(message: str, level: str = "info") -> None:
    """Structured log line, matching run.sh and the gateway's shape."""
    print(
        json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "level": level,
                "component": "discovery",
                "msg": message,
            }
        ),
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Resolving the host to announce
# --------------------------------------------------------------------------- #


def _supervisor_self_info() -> dict[str, Any]:
    """Return this App's Supervisor record, or ``{}`` when unavailable."""
    if not TOKEN:
        return {}
    request = urllib.request.Request(
        f"{SUPERVISOR_URL}/addons/self/info",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def announced_host() -> str:
    """The DNS name Home Assistant should dial.

    The Supervisor registers every App on the internal network under its slug
    with ``_`` replaced by ``-``, and prefixes locally installed apps with a
    repository hash.  Its own record is therefore authoritative; the container
    hostname is set from the same value; the slug rule is the fallback.

    The result is validated the way the integration validates it: a value that
    is not a bare host name cannot become an engine URL, so announcing it would
    only produce a discovery card that fails to connect.
    """
    for candidate in _host_candidates():
        if is_announceable_host(candidate):
            return candidate
    return ""


def _host_candidates() -> list[str]:
    override = _host_override()
    info = _supervisor_self_info()
    recorded = info.get("hostname")
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""
    slug = os.environ.get("WM_APP_SLUG", os.environ.get("HASSIO_ADDON", ""))
    slug = slug.strip() or os.environ.get("WM_DISCOVERY_SLUG", "").strip()
    return [
        override,
        recorded.strip() if isinstance(recorded, str) else "",
        hostname,
        slug.replace("_", "-") if slug else "",
    ]


def discovery_message() -> dict[str, Any]:
    """The payload POSTed to ``/discovery``."""
    config: dict[str, Any] = {"port": _port()}
    host = announced_host()
    if host:
        config["host"] = host
    return {"service": _service(), "config": config}


# --------------------------------------------------------------------------- #
# Waiting for the engine, then publishing
# --------------------------------------------------------------------------- #


def _gateway_ready() -> bool:
    url = f"http://127.0.0.1:{_port()}{_health_path()}"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def wait_for_gateway(deadline: float) -> bool:
    while True:
        if _gateway_ready():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def publish(message: dict[str, Any]) -> bool:
    """POST one discovery message.  Returns whether the Supervisor accepted it."""
    body = json.dumps(message).encode("utf-8")
    request = urllib.request.Request(
        f"{SUPERVISOR_URL}/discovery",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            if 200 <= response.status < 300:
                return True
            log(f"supervisor returned HTTP {response.status}", "warning")
            return False
    except urllib.error.HTTPError as err:
        # The body can name the offending service; it never contains our token.
        detail = ""
        with contextlib.suppress(OSError):
            detail = err.read().decode("utf-8", "replace").strip()[:200]
        log(f"supervisor rejected discovery: HTTP {err.code} {detail}".strip(), "warning")
        return False
    except (urllib.error.URLError, OSError) as err:
        log(f"supervisor unreachable: {type(err).__name__}", "warning")
        return False


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    if not _enabled():
        log("discovery disabled by WM_DISCOVERY_ENABLED")
        return 0
    if not TOKEN and not dry_run:
        # Not running under the Supervisor (plain Docker, CI, a laptop).  That is
        # a supported deployment; there is simply nothing to announce.
        log("SUPERVISOR_TOKEN is unset; not running under the Supervisor, skipping")
        return 0

    message = discovery_message()
    if not message["config"].get("host"):
        log("could not resolve an app hostname; skipping discovery", "warning")
        return 0

    if dry_run:
        # Safe to print: the message holds no secrets by construction.
        print(json.dumps(message, indent=2, sort_keys=True))
        return 0

    wait_s = _wait_seconds()
    deadline = time.monotonic() + wait_s
    if wait_s and not wait_for_gateway(deadline):
        # Publish anyway: the engine may still come up, and a stale message is
        # harmless because the integration probes before creating an entry.
        log("engine did not answer before the wait elapsed; publishing anyway", "warning")

    if publish(message):
        log(
            f"announced service={message['service']} "
            f"host={message['config']['host']} port={message['config']['port']}"
        )
        return 0

    # Retry a few times: the Supervisor may still be starting or reloading.
    for attempt in range(1, 6):
        time.sleep(min(2 * attempt, 10))
        if publish(message):
            log(f"announced on retry {attempt}")
            return 0
    log("discovery could not be published; the integration can still be added manually", "warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
