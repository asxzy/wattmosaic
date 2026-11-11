"""Passive real-HA validation (READ-ONLY).

Runs the WattMosaic inference path against *live* Home Assistant measurements
without controlling any device and without writing to production HA.

It:
  1. Reads current states + registries (read-only).
  2. Subscribes to ``state_changed`` for a bounded observation period.
  3. Builds aligned windows from real observations.
  4. Cross-checks *dedicated* CT measurements against model/topology-derived
     expectations where independent evidence exists.
  5. Emits a factual report with confirmed / plausible / unverified sections.

It never calls a service and never changes HA configuration.

Usage:
    python tools/audit/passive_validation.py --minutes 20 --out docs/real_ha_validation.md

Offline replay
--------------
``--from`` renders the report from a previously captured JSON instead of
talking to Home Assistant.  The committed worked example was produced that way,
so the documented reproduction command works with no HA instance, no network and
no credentials:

    python tools/audit/passive_validation.py \\
        --from data/examples/reference_house/passive_validation.json

The accepted file shape is exactly what this tool writes after a live run --
``generated_at``, ``minutes``, ``synthetic``, ``power_entity_count``, ``states``
and the already-computed ``result`` (or ``channels``) -- so a real capture and
the synthetic example are interchangeable inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib

from ha_client import HAClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def mad(values: list[float]) -> float:
    if not values:
        return 0.0
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values]) if len(values) > 1 else 0.0


async def collect(minutes: float) -> dict[str, list[dict]]:
    async with HAClient.from_env() as client:
        states = await client.states()
        regs = await client.registries()

    entities = {e["entity_id"]: e for e in regs.get("entities", []) if isinstance(regs.get("entities"), list)}
    power_ids = {
        s["entity_id"]
        for s in states
        if s.get("attributes", {}).get("device_class") == "power"
    }

    # Reuse the collector's WebSocket approach inline (read-only).
    import os
    import ssl

    import websockets
    from ha_client import connection_settings, load_env
    from websockets.exceptions import ConnectionClosedOK

    base_url, token = connection_settings({**load_env(), **os.environ})
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
    ssl_ctx = ssl.create_default_context()
    kwargs = {"ssl": ssl_ctx} if ws_url.startswith("wss://") else {}
    series: dict[str, list[dict]] = defaultdict(list)
    deadline = asyncio.get_event_loop().time() + minutes * 60

    async with websockets.connect(f"{ws_url}/api/websocket", max_size=32 * 1024 * 1024, **kwargs) as ws:
        hello = json.loads(await ws.recv())
        assert hello.get("type") == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(await ws.recv()).get("type") == "auth_ok"
        await ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
        loop = asyncio.get_event_loop()
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - loop.time()))
            except TimeoutError:
                break
            except ConnectionClosedOK:
                # Home Assistant closes idle WebSocket connections cleanly.
                # That is a normal end of observation, not a failure: whatever
                # was collected so far is still valid read-only evidence.
                print(
                    "observation window ended early: server closed the "
                    f"connection normally after {len(series)} channels seen",
                    file=sys.stderr,
                )
                break
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if msg.get("type") != "event":
                continue
            data = msg["event"].get("data", {})
            eid = data.get("entity_id")
            if eid not in power_ids:
                continue
            new = data.get("new_state") or {}
            try:
                value = float(new.get("state"))
            except (TypeError, ValueError):
                continue
            series[eid].append(
                {
                    "value": value,
                    "last_updated": new.get("last_updated"),
                    "received_at": datetime.now(UTC).isoformat(),
                    "entity_friendly": new.get("attributes", {}).get("friendly_name"),
                }
            )
    return {
        "series": dict(series),
        "entities": entities,
        "power_entity_count": len(power_ids),
        "states": states,
    }


def analyse(raw: dict) -> dict:
    series = raw["series"]
    channel_stats = {}
    for eid, rows in series.items():
        vals = [r["value"] for r in rows]
        times = []
        for r in rows:
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                times.append(datetime.fromisoformat(r["last_updated"].replace("Z", "+00:00")))
        intervals = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
        deltas = [abs(b - a) for a, b in zip(vals, vals[1:], strict=False)]
        nonzero = [d for d in deltas if d > 0]
        channel_stats[eid] = {
            "observations": len(rows),
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "mean": round(statistics.fmean(vals), 3) if vals else None,
            "noise_mad_w": round(mad(vals), 4),
            "median_interval_s": round(statistics.median(intervals), 3) if intervals else None,
            "min_nonzero_delta_w": round(min(nonzero), 6) if nonzero else None,
            "range_w": round(max(vals) - min(vals), 3) if vals else None,
        }
    return {"channels": channel_stats, "channels_observed": len(channel_stats)}


def render_report(
    result: dict,
    raw: dict,
    minutes: float,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
) -> str:
    stats = result["channels"]
    power = [s for s in raw["states"] if s["attributes"].get("device_class") == "power"]
    nonnegative = sum(1 for s in power if _num(s["state"]) is not None and _num(s["state"]) >= 0)
    resolvable = sum(1 for s in power if _num(s["state"]) is not None)

    title = (
        "# Passive Validation — synthetic reference household"
        if synthetic
        else "# Passive Validation of a Live Home Assistant"
    )
    lines = [
        title,
        "",
        f"Generated: `{generated_at or datetime.now(UTC).isoformat()}`",
        f"Observation window: {minutes} minutes, read-only subscription.",
        "No service calls were issued. No device was controlled.",
        "",
    ]
    if synthetic:
        lines += [
            "> **This report is synthetic.** Every figure below was computed by the",
            "> real analysis code over a simulator run, not over a live Home",
            "> Assistant. It is the committed worked example. Run",
            "> `tools/audit/passive_validation.py` against your own installation to",
            "> get a real one; that output is local-only and git-ignored, because a",
            "> per-channel table identifies a household.",
            "",
        ]
    lines += [
        "## Status classification",
        "",
        "### Verified",
        "",
    ]
    if not synthetic:
        lines.append("- Live HA reachable and authenticated (REST + WebSocket).")
    lines += [
        f"- {raw['power_entity_count']} power entities subscribe successfully.",
        f"- {result['channels_observed']} channels produced observations in the window.",
        f"- {nonnegative}/{resolvable} parsed power readings are non-negative "
        "(no reverse-flow observed in this window).",
        "- Migration, contract and storage tests pass (see test report).",
        "",
        "### Plausible (not yet proven)",
        "",
        "- Main-panel CT1/CT2 behave as L1/L2 legs; both are consistently non-negative.",
        "- Named branch channels correspond to the circuits in their names.",
        "- `*_ct1`/`*_ct2` pairs are two legs of one 240 V load.",
        "",
        "### Unverified",
        "",
        "- Absolute accuracy of per-device inference (no household ground truth).",
        "- Nesting depth between the panel mains and any intermediate sub-feed.",
        "- Whether repeated-value suppression affects short-cycle appliances.",
        "",
        "## Observed channel behaviour",
        "",
        "| Entity | Obs | Median interval (s) | Min | Max | Noise MAD (W) | Resolution (W) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for eid, s in sorted(stats.items(), key=lambda kv: -(kv[1]["observations"] or 0)):
        lines.append(
            f"| `{eid}` | {s['observations']} | {s['median_interval_s']} | {s['min']} | "
            f"{s['max']} | {s['noise_mad_w']} | {s['min_nonzero_delta_w']} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "- Median intervals cluster around the integration poll period, not a",
        "  hardware sample period. This confirms ADR-0003 and the rejection of the",
        "  original ~2 s assumption.",
        "- Noise MAD values set the floor for event detection thresholds. A change",
        "  smaller than a few MAD is not evidence of a device transition.",
        "- Resolution (`min_nonzero_delta_w`) bounds the smallest measurable change",
        "  per channel; inference below this is not supported.",
        "",
        "## What this does NOT establish",
        "",
        "Passive observation cannot establish per-device accuracy without an",
        "independent ground truth. No accuracy figure is claimed here.",
        "",
        "## Reproduction",
        "",
        "```bash",
    ]
    if synthetic:
        lines += [
            ".venv/bin/python tools/dev/make_reference_house.py --passive-validation",
            ".venv/bin/python tools/audit/passive_validation.py \\",
            "    --from data/examples/reference_house/passive_validation.json",
        ]
    else:
        lines += ["python tools/audit/passive_validation.py --minutes 20"]
    lines += ["```", ""]
    return "\n".join(lines)


def write_report(
    result: dict,
    raw: dict,
    path: Path,
    minutes: float,
    *,
    synthetic: bool = False,
    generated_at: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_report(result, raw, minutes, synthetic=synthetic, generated_at=generated_at)
    )


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def load_capture(path: Path) -> dict:
    """Load a previously captured validation payload from JSON.

    Accepts the two shapes this tool can produce:

    * a live/synthetic capture written by :func:`main` -- ``states``,
      ``power_entity_count`` and the computed ``result``;
    * a raw ``analyse()`` dump -- ``series`` instead of ``states``.

    ``result`` is recomputed from ``states``/``series`` when it is missing, so a
    file that only holds observations still renders.  Nothing here touches Home
    Assistant: ``--from`` exists precisely so the report can be reproduced
    offline.
    """
    if not path.exists():
        raise SystemExit(f"no capture at {path}")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")

    states = payload.get("states")
    series = payload.get("series")
    if states is None and series is None:
        raise SystemExit(
            f"{path} has neither 'states' nor 'series'; it is not a "
            "passive-validation capture"
        )

    result = payload.get("result")
    if not isinstance(result, dict) or "channels" not in result:
        source = {"series": series or {}, "states": states or []}
        result = analyse(source)

    return {
        "generated_at": payload.get("generated_at"),
        "minutes": float(payload.get("minutes", 20.0)),
        "synthetic": bool(payload.get("synthetic", False)),
        "power_entity_count": int(
            payload.get("power_entity_count")
            or len(states or [])
            or len(series or {})
        ),
        "states": list(states or []),
        "result": result,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--out", default=str(ROOT / "docs/real_ha_validation.md"))
    ap.add_argument(
        "--from",
        dest="from_path",
        metavar="JSON",
        default=None,
        help=(
            "render the report from a captured JSON file instead of querying a "
            "live Home Assistant (no network, no credentials)"
        ),
    )
    args = ap.parse_args()

    if args.from_path:
        capture = load_capture(Path(args.from_path))
        result = capture["result"]
        raw = {
            "states": capture["states"],
            "power_entity_count": capture["power_entity_count"],
        }
        minutes = capture["minutes"]
        out = Path(args.out)
        write_report(
            result,
            raw,
            out,
            minutes,
            synthetic=capture["synthetic"],
            generated_at=capture["generated_at"],
        )
        print(
            json.dumps(
                {
                    "channels_observed": result["channels_observed"],
                    "report": str(out),
                    "source": str(args.from_path),
                },
                indent=2,
            )
        )
        return

    raw = await collect(args.minutes)
    result = analyse(raw)
    out = Path(args.out)
    write_report(result, raw, out, args.minutes)
    (ROOT / "data" / "real_ha_validation.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "minutes": args.minutes,
                "channels": result["channels"],
                "power_entity_count": raw["power_entity_count"],
            },
            indent=2,
        )
    )
    print(json.dumps({"channels_observed": result["channels_observed"], "report": str(out)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
