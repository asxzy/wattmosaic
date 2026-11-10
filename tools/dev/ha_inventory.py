"""Read-only inventory of WattMosaic installs on the live Home Assistant.

Uses the WebSocket API because the entity/device registries have no REST list
endpoint.  Reads only; performs no service calls and no registry writes.

Usage:
    python tools/dev/ha_inventory.py            # summary
    python tools/dev/ha_inventory.py --entities # per-entry entity ids
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> tuple[str, str]:
    """Load HA_HOST / HA_TOKEN from .env without echoing either."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    return os.environ["HA_HOST"], os.environ["HA_TOKEN"]


async def _ws_call(host: str, token: str, requests: list[dict]) -> dict[int, object]:
    """Run each request and return the response whose ``id`` matches.

    ``config_entries/subscribe`` keeps emitting events after its first reply,
    so responses cannot be assumed to arrive in order or to belong to the
    request just sent.
    """
    import aiohttp

    responses: dict[int, dict] = {}
    results: dict[int, object] = {}
    # The entity registry on a real installation is several megabytes and
    # exceeds aiohttp's default 4 MiB frame limit.
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(f"http://{host}:8123/api/websocket", max_msg_size=0) as ws,
    ):
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise SystemExit(f"auth failed: {auth.get('message')}")

        pending = set(range(1, len(requests) + 1))
        for index, request in enumerate(requests, start=1):
            await ws.send_json({"id": index, **request})

        while pending:
            message = await ws.receive_json()
            message_id = message.get("id")
            if message_id in pending and "success" in message:
                responses[message_id] = message
                results[message_id] = message.get("result")
                pending.discard(message_id)
                if not message.get("success"):
                    raise SystemExit(
                        f"request {message_id} failed: {message.get('error')}"
                    )
    return results


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities", action="store_true", help="list entity ids per entry")
    args = parser.parse_args(argv)

    host, token = _load_env()
    results = await _ws_call(
        host,
        token,
        [
            {"type": "config_entries/get"},
            {"type": "config/entity_registry/list"},
            {"type": "config/device_registry/list"},
        ],
    )

    entries = results[1] or []
    entities = results[2] or []
    devices = results[3] or []

    wm = [e for e in entries if e.get("domain") == "wattmosaic"]
    by_entry = defaultdict(list)
    for entity in entities:
        by_entry[entity.get("config_entry_id")].append(entity)

    devices_by_entry = defaultdict(list)
    for device in devices:
        for entry_id in device.get("config_entries") or []:
            devices_by_entry[entry_id].append(device)

    print("=== WattMosaic config entries ===")
    for entry in sorted(wm, key=lambda e: e["entry_id"]):
        entry_id = entry["entry_id"]
        title = entry.get("title") or ""
        url = (entry.get("data") or {}).get("url") or ""
        print(f"\nentry {entry_id}")
        print(f"  state    : {entry.get('state')}")
        print(f"  title    : {title!r}")
        print(f"  url      : {url!r}")
        print(f"  source   : {entry.get('source')}")
        print(f"  version  : {entry.get('version')}")
        print(f"  entities : {len(by_entry[entry_id])}")
        print(f"  devices  : {len(devices_by_entry[entry_id])}")

        sample = Counter(
            e["entity_id"].split(".")[0] for e in by_entry[entry_id]
        )
        print(f"  domains  : {dict(sample)}")
        update = [e for e in by_entry[entry_id] if e["entity_id"].startswith("update.")]
        for entity in update:
            print(f"  update entity: {entity['entity_id']}")

        if args.entities:
            for entity in sorted(by_entry[entry_id], key=lambda e: e["entity_id"]):
                print(f"      {entity['entity_id']}")

    print()
    print(f"total wattmosaic entries: {len(wm)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
