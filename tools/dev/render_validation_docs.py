"""Render the published validation documents from a simulator run.

The repository must never publish a real installation's channel table: a list of
entity ids with observed intervals, ranges and noise floors identifies a specific
house.  Instead the committed documents are produced by running the *real*
analysis code over the deterministic synthetic household, so every number in
them is genuinely measured while describing no real building.

This tool is the only sanctioned way to (re)generate:

* ``docs/ct_update_profile.md``
* ``docs/real_ha_validation.md``
* ``data/examples/reference_house/passive_validation.json``
* ``data/examples/reference_house/ct_update_profile.json``

Run::

    python tools/dev/render_validation_docs.py
    python tools/dev/render_validation_docs.py --check   # fail if out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "audit"))

import passive_validation  # noqa: E402
import profile_samples  # noqa: E402

from tools.simulator import HouseholdScenario, SimulatorConfig  # noqa: E402
from tools.simulator.sensors import QualityLevel  # noqa: E402

EXAMPLES = ROOT / "data" / "examples" / "reference_house"
DOCS = ROOT / "docs"

#: Observation window for the synthetic passive run.  Matches the live tool's
#: default so the two reports are directly comparable.
MINUTES = 20
SEED = 7


def _simulated_states(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the REST-shaped state list a live audit would read."""
    latest = records[-1]["measurements"]
    states: list[dict[str, Any]] = []
    for ct_id, m in latest.items():
        states.append(
            {
                "entity_id": m["entity_id"],
                "state": str(m["value_w"]),
                "attributes": {
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "friendly_name": f"{ct_id} power",
                },
            }
        )
    return states


def _simulated_series(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per-entity observation series, in the shape ``analyse()`` expects."""
    series: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for m in record["measurements"].values():
            if not m.get("delivered", True):
                continue
            series.setdefault(m["entity_id"], []).append(
                {
                    "value": float(m["value_w"]),
                    "last_updated": m["state_timestamp"],
                    "received_at": m["received_at"],
                }
            )
    return series


def _ct_profile(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate cadence statistics measured from the simulated series.

    Deliberately summary-only: the per-channel rows stay out of the published
    document, but the aggregates are computed from real observations.
    """
    series = _simulated_series(records)
    intervals: list[float] = []
    per_channel: dict[str, float] = {}
    for entity_id, rows in series.items():
        stamps = []
        for row in rows:
            stamp = row.get("last_updated")
            if stamp:
                from datetime import datetime

                stamps.append(datetime.fromisoformat(str(stamp)))
        deltas = [
            (b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)
        ]
        deltas = [d for d in deltas if d > 0]
        if deltas:
            per_channel[entity_id] = sorted(deltas)[len(deltas) // 2]
            intervals.extend(deltas)
    intervals.sort()
    medians = sorted(per_channel.values())

    def at(q: float) -> float | None:
        if not intervals:
            return None
        idx = min(len(intervals) - 1, max(0, int(round(q * (len(intervals) - 1)))))
        return round(intervals[idx], 1)

    return {
        "generated_at": records[0]["timestamp"],
        "source": "synthetic reference household",
        "sample_window_minutes": MINUTES,
        "channels_profiled": len(per_channel),
        "median_interval_s": round(medians[len(medians) // 2], 1) if medians else None,
        "p10_interval_s": at(0.10),
        "p90_interval_s": at(0.90),
        "mean_interval_s": round(sum(intervals) / len(intervals), 1) if intervals else None,
        "min_interval_s": round(intervals[0], 1) if intervals else None,
        "max_interval_s": round(intervals[-1], 1) if intervals else None,
        "interpretation": (
            "Bursts smaller than the channel cadence are integration poll cycles, "
            "not higher-rate measurements. Inference windows must be sized from "
            "per-channel cadence and aligned by timestamp."
        ),
    }


def build() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    records = list(
        HouseholdScenario(
            SimulatorConfig(
                seed=SEED,
                minutes=MINUTES,
                interval=15,
                quality=QualityLevel.GOOD,
            )
        ).records()
    )
    series = _simulated_series(records)
    states = _simulated_states(records)

    analysis_raw = {
        "series": series,
        "entities": [],
        "power_entity_count": len(series),
        "states": states,
    }
    analysis = passive_validation.analyse(analysis_raw)

    committed_raw = {
        "generated_at": records[0]["timestamp"],
        "minutes": float(MINUTES),
        "synthetic": True,
        "power_entity_count": len(series),
        "states": states,
        "result": analysis,
    }
    return committed_raw, analysis, analysis_raw, _ct_profile(records)


def render(
    committed_raw: dict[str, Any],
    analysis: dict[str, Any],
    analysis_raw: dict[str, Any],
    ct_profile: dict[str, Any],
) -> dict[Path, str]:
    return {
        EXAMPLES / "passive_validation.json": json.dumps(committed_raw, indent=2) + "\n",
        EXAMPLES / "ct_update_profile.json": json.dumps(ct_profile, indent=2) + "\n",
        DOCS / "ct_update_profile.md": profile_samples.render_doc(ct_profile),
        DOCS / "real_ha_validation.md": passive_validation.render_report(
            analysis,
            analysis_raw,
            float(MINUTES),
            synthetic=True,
            generated_at=str(committed_raw["generated_at"]),
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify the committed documents match a fresh run; do not write",
    )
    args = ap.parse_args()

    committed_raw, analysis, analysis_raw, ct_profile = build()
    outputs = render(committed_raw, analysis, analysis_raw, ct_profile)

    stale: list[str] = []
    for path, content in outputs.items():
        if args.check:
            current = path.read_text() if path.exists() else ""
            if current.strip() != content.strip():
                stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    if args.check:
        if stale:
            print("stale validation docs: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(json.dumps({"status": "up_to_date", "files": len(outputs)}))
        return 0

    print(
        json.dumps(
            {
                "written": [str(p.relative_to(ROOT)) for p in outputs],
                "channels": analysis["channels_observed"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
