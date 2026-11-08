"""Minimal read-only Home Assistant REST/WebSocket client for auditing.

Read-only by construction: every WebSocket command is checked against
:data:`READ_ONLY_WS_COMMANDS` before it is sent, and the transport only issues
``GET`` over REST.  Calling a service is impossible from this module — there is
no code path that can name one.

``history/history_during_period`` is included because the recorder's own
history is the only source of *quantitative* evidence for associating a
CT with an HA entity.  It is a read query: it starts no devices and mutates no
state.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_PORT = 8123

#: WebSocket commands this client is permitted to send.  Anything else raises
#: before a frame is written, so a future edit cannot quietly turn the audit
#: tooling into an actuator.
READ_ONLY_WS_COMMANDS = frozenset(
    {
        "get_states",
        "get_config",
        "get_services",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/floor_registry/list",
        "config/label_registry/list",
        "history/history_during_period",
    }
)


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Load a simple KEY=VALUE .env file without printing secret values."""
    env: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def normalize_base_url(host: str) -> str:
    """Turn a bare host/IP or host:port into a usable base URL.

    The user's ``.env`` supplies a bare ``HA_HOST`` value without a scheme or
    port.  We probe the conventional Home Assistant port rather than guessing
    silently, and allow an explicit ``HA_URL`` to override.
    """
    host = host.strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    if ":" in host:
        return f"http://{host}"
    return f"http://{host}:{DEFAULT_PORT}"


def connection_settings(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = env if env is not None else {**load_env(), **os.environ}
    host = env.get("HA_URL") or env.get("HA_HOST") or ""
    token = env.get("HA_TOKEN") or env.get("HA_LONG_LIVED_TOKEN") or ""
    if not host or not token:
        raise RuntimeError("HA_HOST/HA_URL and HA_TOKEN must be configured in .env")
    return normalize_base_url(host), token


class HAClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_env(cls) -> HAClient:
        return cls(*connection_settings())

    async def __aenter__(self) -> HAClient:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session:
            raise RuntimeError("client not started")
        return self._session

    async def get(self, path: str) -> Any:
        async with self.session.get(f"{self.base_url}/api/{path.lstrip('/')}") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def states(self) -> list[dict[str, Any]]:
        return await self.get("states")

    async def config(self) -> dict[str, Any]:
        return await self.get("config")

    async def ws_command(self, msg_type: str, **params: Any) -> Any:
        """Issue a single read-only WebSocket command.

        ``params`` are merged into the request frame, which is what recorder
        queries such as ``history/history_during_period`` require.  The command
        must be in :data:`READ_ONLY_WS_COMMANDS`; an unknown command raises
        before anything is sent.
        """
        if msg_type not in READ_ONLY_WS_COMMANDS:
            raise PermissionError(
                f"refusing to send non-read-only websocket command {msg_type!r}"
            )
        import websockets

        ws_url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        ssl_ctx = ssl.create_default_context()
        kwargs = {"ssl": ssl_ctx} if ws_url.startswith("wss://") else {}
        async with websockets.connect(f"{ws_url}/api/websocket", max_size=256 * 1024 * 1024, **kwargs) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError("unexpected websocket handshake")
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError("websocket authentication failed")
            await ws.send(json.dumps({"id": 1, "type": msg_type, **params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    if not msg.get("success"):
                        raise RuntimeError(f"ws command {msg_type} failed: {msg.get('error')}")
                    return msg.get("result")

    async def registries(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, msg_type in (
            ("entities", "config/entity_registry/list"),
            ("devices", "config/device_registry/list"),
            ("areas", "config/area_registry/list"),
            ("floors", "config/floor_registry/list"),
        ):
            try:
                out[name] = await self.ws_command(msg_type)
            except Exception as exc:  # pragma: no cover - environment dependent
                out[name] = {"error": str(exc)}
        return out


async def _main() -> None:
    async with HAClient.from_env() as client:
        cfg = await client.config()
        print(json.dumps({"version": cfg.get("version"), "time_zone": cfg.get("time_zone"), "state": cfg.get("state")}))


if __name__ == "__main__":
    asyncio.run(_main())


async def fetch_history(
    client: HAClient,
    entity_ids: list[str],
    *,
    days: float = 7.0,
    start: str | None = None,
    end: str | None = None,
    minimal: bool = True,
    significant: bool = False,
) -> dict[str, Any]:
    """Read recorder history for ``entity_ids`` (read-only).

    ``minimal=True`` shrinks the payload but **drops ``last_changed``**, so a
    row means "``last_updated`` moved", which includes attribute-only updates.
    For an entity whose attributes change often (a hot-tub temperature sensor,
    a climate entity with ``current_temperature``) that makes one physical
    switch look like dozens of state transitions.  Callers that compare state
    transitions must therefore re-derive the state sequence from the values, not
    from row counts.  See ``derive_state_entities``.

    The window is clamped to ``days`` before ``now`` when explicit bounds are
    not given.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    stop = end or now.isoformat()
    begin = start or (now - timedelta(days=days)).isoformat()
    return await client.ws_command(
        "history/history_during_period",
        start_time=begin,
        end_time=stop,
        entity_ids=list(entity_ids),
        minimal_response=minimal,
        no_attributes=True,
        significant_changes_only=significant,
    )
