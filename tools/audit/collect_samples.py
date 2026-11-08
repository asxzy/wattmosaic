"""Passive CT sampling collector (read-only).

Subscribes to Home Assistant ``state_changed`` events for power/energy sensors
and records every observed transition with three distinct timestamps:

* ``last_changed`` / ``last_updated`` reported by Home Assistant
* ``received_at`` — when this collector observed the event

The distinction matters: ``last_updated`` is not proof of a new physical
measurement (see master instructions §3.2 and §13).

Usage:
    python tools/audit/collect_samples.py --minutes 45 --out data/raw/ct_events.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets
from ha_client import connection_settings, load_env


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def collect(minutes: float, out_path: Path, entity_filter: list[str] | None) -> None:
    base_url, token = connection_settings({**load_env(), **__import__("os").environ})
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + minutes * 60

    ssl_ctx = ssl.create_default_context()
    kwargs = {"ssl": ssl_ctx} if ws_url.startswith("wss://") else {}
    async with websockets.connect(f"{ws_url}/api/websocket", max_size=64 * 1024 * 1024, **kwargs) as ws:
        hello = json.loads(await ws.recv())
        assert hello.get("type") == "auth_required", hello
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        assert auth.get("type") == "auth_ok", auth

        # Snapshot: discover candidate entities from live states.
        await ws.send(json.dumps({"id": 1, "type": "get_states"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                states = msg["result"]
                break
        candidates = []
        for st in states:
            attrs = st.get("attributes", {})
            dc = attrs.get("device_class")
            unit = attrs.get("unit_of_measurement")
            if dc in ("power", "energy") or unit in ("W", "kW", "kWh", "Wh"):
                if entity_filter and not any(f in st["entity_id"] for f in entity_filter):
                    continue
                candidates.append(st)
        ids = sorted(s["entity_id"] for s in candidates)
        with out_path.open("w") as fh:
            fh.write(json.dumps({"type": "snapshot", "received_at": now_iso(), "states": candidates}) + "\n")

        await ws.send(json.dumps({
            "id": 2,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }))
        subscribed = False
        with out_path.open("a") as fh:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic()))
                except TimeoutError:
                    break
                received = now_iso()
                msg = json.loads(raw)
                if msg.get("id") == 2 and msg.get("type") == "result":
                    subscribed = msg.get("success", False)
                    continue
                if msg.get("type") != "event":
                    continue
                ev = msg["event"]
                if ev.get("event_type") != "state_changed":
                    continue
                data = ev.get("data", {})
                entity_id = data.get("entity_id", "")
                if entity_id not in ids:
                    continue
                new = data.get("new_state") or {}
                old = data.get("old_state") or {}
                attrs = new.get("attributes", {})
                rec = {
                    "type": "state",
                    "entity_id": entity_id,
                    "state": new.get("state"),
                    "old_state": old.get("state"),
                    "last_changed": new.get("last_changed"),
                    "last_updated": new.get("last_updated"),
                    "received_at": received,
                    "event_fired_at": ev.get("time_fired"),
                    "unit": attrs.get("unit_of_measurement"),
                    "device_class": attrs.get("device_class"),
                }
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
            fh.write(json.dumps({"type": "complete", "received_at": now_iso(), "subscribed": subscribed}) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=45)
    ap.add_argument("--out", default="data/raw/ct_events.jsonl")
    ap.add_argument("--filter", nargs="*", default=None)
    args = ap.parse_args()
    asyncio.run(collect(args.minutes, Path(args.out), args.filter))


if __name__ == "__main__":
    main()
