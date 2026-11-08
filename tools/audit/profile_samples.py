"""Build a per-CT update profile from collected passive samples.

Distinguishes:
  * ``last_updated`` deltas (what HA reports as state update time)
  * ``received_at`` deltas (what a subscriber actually observes)
  * repeated identical values (NOT new physical measurements)
  * missing/absent intervals

Run: python tools/audit/profile_samples.py [--input data/raw/ct_events.jsonl]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DOCS = ROOT / "docs"


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def _relative_source(path: Path) -> str:
    """Path for the report, tolerant of the file living outside the repo."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def profile(path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    snapshot: list[dict[str, Any]] = []
    if not path.exists():
        raise SystemExit(f"no sample file at {path}")
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "state":
            rows.append(rec)
        elif rec.get("type") == "snapshot":
            snapshot = rec.get("states", [])

    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_entity[r["entity_id"]].append(r)

    out: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": _relative_source(path),
        "sample_count": len(rows),
        "entities_seen": len(by_entity),
        "snapshot_entities": len(snapshot),
        "channels": {},
        "method": (
            "state_changed subscription; intervals measured from both HA last_updated "
            "and local receive time; repeated values flagged separately"
        ),
    }

    for entity_id, rs in sorted(by_entity.items()):
        rs.sort(key=lambda r: r.get("received_at") or "")
        lu = [parse(r["last_updated"]) for r in rs if r.get("last_updated")]
        rc = [parse(r["received_at"]) for r in rs if r.get("received_at")]
        lu_d = [(b - a).total_seconds() for a, b in zip(lu, lu[1:], strict=False)]
        rc_d = [(b - a).total_seconds() for a, b in zip(rc, rc[1:], strict=False)]
        values = [r.get("state") for r in rs]
        numeric = []
        for v in values:
            with contextlib.suppress(TypeError, ValueError):
                numeric.append(float(v))
        repeats = sum(1 for a, b in zip(values, values[1:], strict=False) if a == b)
        deltas = [abs(b - a) for a, b in zip(numeric, numeric[1:], strict=False)]
        nonzero = [d for d in deltas if d > 0]
        burst = sum(1 for d in rc_d if d < 0.5)
        out["channels"][entity_id] = {
            "observations": len(rs),
            "repeated_values": repeats,
            "repeat_ratio": round(repeats / max(1, len(values) - 1), 4),
            "last_updated": {
                "median_s": round(statistics.median(lu_d), 3) if lu_d else None,
                "mean_s": round(statistics.fmean(lu_d), 3) if lu_d else None,
                "p95_s": round(pct(lu_d, 0.95), 3) if lu_d else None,
                "max_s": round(max(lu_d), 3) if lu_d else None,
                "min_s": round(min(lu_d), 3) if lu_d else None,
            },
            "received": {
                "median_s": round(statistics.median(rc_d), 3) if rc_d else None,
                "p95_s": round(pct(rc_d, 0.95), 3) if rc_d else None,
                "bursts_below_0_5s": burst,
            },
            "resolution": {
                "min_nonzero_delta": round(min(nonzero), 6) if nonzero else None,
                "median_nonzero_delta": round(statistics.median(nonzero), 6) if nonzero else None,
            },
            "value_range": [round(min(numeric), 4), round(max(numeric), 4)] if numeric else None,
        }

    medians = [c["last_updated"]["median_s"] for c in out["channels"].values() if c["last_updated"]["median_s"]]
    out["summary"] = {
        "median_of_channel_medians_s": round(statistics.median(medians), 3) if medians else None,
        "channels_updating_faster_than_1s": sum(1 for m in medians if m < 1.0),
        "channels_updating_slower_than_30s": sum(1 for m in medians if m > 30),
        "two_second_assumption_supported": bool(medians) and 1.0 <= statistics.median(medians) <= 5.0,
    }
    out["batching"] = _batching(rows)
    return out


def _batching(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect asynchronous update batches.

    Home Assistant delivers updates in bursts when one integration polls many
    entities at once.  A burst is not evidence of a faster physical sample rate,
    and channels from different integrations arrive at different times.  The
    engine must align by timestamp, not assume a shared tick.
    """
    if not rows:
        return {}
    stamps = sorted(parse(r["received_at"]) for r in rows if r.get("received_at"))
    if len(stamps) < 2:
        return {}
    groups: list[list[datetime]] = [[stamps[0]]]
    for ts in stamps[1:]:
        if (ts - groups[-1][-1]).total_seconds() > 1.0:
            groups.append([ts])
        else:
            groups[-1].append(ts)
    starts = [g[0] for g in groups]
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:], strict=False)]
    sizes = sorted(len(g) for g in groups)
    spans = [(g[-1] - g[0]).total_seconds() for g in groups]
    multi = sum(1 for s in sizes if s > 1)
    return {
        "bursts": len(groups),
        "multi_entity_bursts": multi,
        "single_entity_bursts": len(groups) - multi,
        "median_entities_per_burst": round(statistics.median(sizes), 1) if sizes else None,
        "max_entities_per_burst": max(sizes) if sizes else None,
        "burst_interval_median_s": round(statistics.median(gaps), 3) if gaps else None,
        "burst_interval_p95_s": round(pct(gaps, 0.95), 3) if gaps else None,
        "burst_span_median_s": round(statistics.median(spans), 3) if spans else None,
        "burst_span_max_s": round(max(spans), 3) if spans else None,
        "interpretation": (
            "Bursts smaller than the channel cadence are integration poll cycles, not "
            "higher-rate measurements. Inference windows must be sized from per-channel "
            "cadence and aligned by timestamp."
        ),
    }


def render_doc(result: dict[str, Any]) -> str:
    """Render the CT update profile as Markdown.

    Tolerates a *summary-only* payload (the committed synthetic example carries
    the aggregate cadence statistics but no per-channel rows).  A live run
    includes the per-channel table; the published example deliberately does not,
    because per-channel rows are a household fingerprint.
    """
    channels = result.get("channels") or {}
    per_channel = bool(channels) and all("last_updated" in c for c in channels.values())

    lines = [
        "# CT Update Profile",
        "",
        f"Generated: `{result['generated_at']}`",
    ]
    if result.get("source"):
        lines.append(f"Source: `{result['source']}`.")
    if "sample_count" in result:
        lines += [
            f"{result['sample_count']} state-change records,",
            f"{result['entities_seen']} entities observed by subscription.",
        ]
    lines += [""]
    if "synthetic" in str(result.get("source", "")).lower():
        lines += [
            "> **This profile is synthetic.** It is the committed example used by the",
            "> test suite and documentation, not the operator's installation. Run",
            "> `tools/audit/profile_samples.py` against your own recorded samples to",
            "> produce a real one; that output is local-only and git-ignored.",
            "",
        ]
    lines += [
        "## Method",
        "",
        result.get(
            "method",
            "state_changed subscription; intervals measured from both HA "
            "last_updated and local receive time; repeated values flagged separately",
        ),
        "",
        "- `last_updated` is a Home Assistant state update timestamp. It is **not** a",
        "  physical measurement timestamp.",
        "- Repeated identical values are counted and are not treated as independent",
        "  measurements by the engine.",
        "- Bursts below 0.5 s indicate batched/refreshed entity writes, not faster",
        "  physical sampling.",
        "",
        "## Summary",
        "",
    ]
    summary = result.get("summary")
    if summary:
        lines += [f"```json\n{json.dumps(summary, indent=2)}\n```", ""]
    else:
        # Summary-only payload: present the headline cadence statistics.
        stats = {
            k: v
            for k, v in result.items()
            if k.endswith("_interval_s") or k in {"channels_profiled", "sample_window_minutes"}
        }
        lines += [f"```json\n{json.dumps(stats, indent=2)}\n```", ""]
        if result.get("interpretation"):
            lines += [result["interpretation"], ""]
    if result.get("batching"):
        lines += [
            "## Update batching (measured)",
            "",
            f"```json\n{json.dumps(result['batching'], indent=2)}\n```",
            "",
            "This is the decisive observation: a single integration poll emits many entities",
            "within a fraction of a second. That is **one** measurement cycle, not dozens of",
            "independent samples. Any algorithm that counts those as separate evidence will",
            "badly overstate its own confidence.",
            "",
        ]
    if per_channel:
        lines += [
            "## Per channel",
            "",
            "| Entity | Obs | Repeat % | Median interval (s) | p95 (s) | Max (s) | Resolution |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for eid, c in channels.items():
            lu = c["last_updated"]
            res = c["resolution"]["median_nonzero_delta"]
            lines.append(
                f"| `{eid}` | {c['observations']} | {c['repeat_ratio'] * 100:.1f} | "
                f"{lu['median_s']} | {lu['p95_s']} | {lu['max_s']} | {res} |"
            )
        lines.append("")
    else:
        lines += [
            "## Per channel",
            "",
            "Per-channel rows are omitted from the published example on purpose: a",
            "channel list with its observed statistics identifies a specific",
            "installation. A local run of `tools/audit/profile_samples.py` prints them.",
            "",
        ]
    lines += [
        "## Limitations",
        "",
        "- A short observation window cannot characterise infrequent channels.",
        "- Home Assistant exposes no per-reading hardware timestamp; only the state",
        "  update time. The engine therefore keeps engine receipt time separately.",
        "- Update cadence observed here is the cadence *after* the integration poll, so",
        "  it is an upper bound on staleness, not the sensor's internal rate.",
        "",
    ]
    return "\n".join(lines)


def write_doc(result: dict[str, Any], path: Path | None = None) -> None:
    target = path or (DOCS / "ct_update_profile.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_doc(result))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(ROOT / "data/raw/ct_events.jsonl"))
    ap.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=None,
        help="Render the report from an existing profile JSON instead of "
        "profiling raw samples (used to publish the synthetic example).",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Markdown output path (default: docs/ct_update_profile.md).",
    )
    ap.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Profile JSON output path (default: data/ct_update_profile.json).",
    )
    args = ap.parse_args()

    if args.source is not None:
        payload = json.loads(args.source.read_text())
        write_doc(payload, args.out)
        print(json.dumps({"rendered_from": str(args.source)}))
        return

    result = profile(Path(args.input))
    (args.json_out or (DATA / "ct_update_profile.json")).write_text(
        json.dumps(result, indent=2)
    )
    write_doc(result, args.out)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
