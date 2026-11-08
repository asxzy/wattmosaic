"""Replay the stored topology through nesting inference (local DB only).

This is a **development tool, not a test.** It needs the local engine database,
which is not part of the repository, so it cannot run in CI. What it does is
show what the shipped inference *actually produces* on the reference
installation's real measurements, which is the only way to tell a guard that
works from a guard that only satisfies a synthetic fixture.

Usage::

    .venv/bin/python tools/dev/replay_nesting.py
    .venv/bin/python tools/dev/replay_nesting.py --hours 48
    .venv/bin/python tools/dev/replay_nesting.py --score

``--score`` compares the inference against the *declared* parents in the stored
topology, which is the only ground truth available.  It prints, per declared
child, whether the inference found the right level, an ambiguous set, or
nothing.  That score is the acceptance criterion for any change to the
detector: a rule that cannot reproduce the declared wiring is not a rule.

The replay calls the same ``infer_nesting`` the engine calls, with the same
channel mapping ``main.py`` builds, so a difference between this output and the
engine's is a difference in the data, not in the wiring of the call.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from wattmosaic_engine.contracts import Topology  # noqa: E402
from wattmosaic_engine.electrical.nesting import (  # noqa: E402
    build_nodes,
    infer_nesting,
)
from wattmosaic_engine.main import _direct_load_series, _forward_fill  # noqa: E402


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _aligner_key(channel: object) -> str:
    entity_id = str(getattr(channel, "entity_id", "") or "")
    return entity_id.split(".", 1)[-1] if "." in entity_id else entity_id


def load_aligned(db_path: Path, hours: float) -> tuple[Topology, dict[str, list[float]]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM topology_versions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    )
    topology = Topology.model_validate(payload)
    key_to_ct = {_aligner_key(ch): ch.ct_id for ch in topology.channels if ch.enabled}

    end = _as_utc(conn.execute("SELECT MAX(received_at) FROM ct_samples").fetchone()[0])
    assert end is not None
    start = end - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT ct_id, value_w, state_timestamp, received_at FROM ct_samples "
        "WHERE received_at >= ? AND received_at <= ? ORDER BY received_at",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    samples: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        value = row["value_w"]
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        stamp = _as_utc(row["state_timestamp"]) or _as_utc(row["received_at"])
        ct_id = key_to_ct.get(str(row["ct_id"]))
        if stamp is None or ct_id is None:
            continue
        samples.setdefault(ct_id, []).append((stamp, float(value)))

    grid = sorted({stamp for series in samples.values() for stamp, _ in series})
    aligned = {cid: _forward_fill(sorted(series), grid) for cid, series in samples.items()}
    return topology, aligned


def _node_series(
    topology: Topology, aligned: dict[str, list[float]]
) -> tuple[dict[str, list[float]], dict[str, tuple[str, ...]]]:
    channels = {
        ch.ct_id: {
            "ct_type": str(ch.ct_type),
            "phase": str(ch.phase),
            "paired_ct_id": ch.paired_ct_id,
            "parent_ct_id": ch.parent_ct_id,
        }
        for ch in topology.channels
    }
    nodes = build_nodes(channels)
    out: dict[str, list[float]] = {}
    for node_id, node in nodes.items():
        present = [m for m in node.members if m in aligned]
        if not present:
            continue
        length = len(aligned[present[0]])
        values: list[float] = []
        for i in range(length):
            total = 0.0
            seen = False
            for member in present:
                raw = aligned[member][i]
                if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
                    total += float(raw)
                    seen = True
            values.append(total if seen else math.nan)
        out[node_id] = values
    return out, {n: node.members for n, node in nodes.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "wattmosaic.db")
    parser.add_argument(
        "--score", action="store_true", help="score against declared parents"
    )
    args = parser.parse_args()

    topology, aligned = load_aligned(args.db, args.hours)
    print(f"topology version {topology.version}: {len(topology.channels)} channels")
    print(f"compared channels: {len(aligned)}")

    by_id = {ch.ct_id: ch for ch in topology.channels}
    containers = [
        ch.ct_id
        for ch in topology.channels
        if ch.enabled and str(ch.ct_type) in ("main", "branch")
    ]
    direct_loads = _direct_load_series(topology, aligned)
    report: dict[str, object] = {}
    candidates = infer_nesting(
        {
            cid: {
                "ct_type": str(ch.ct_type),
                "phase": str(ch.phase),
                "paired_ct_id": ch.paired_ct_id,
                "parent_ct_id": ch.parent_ct_id,
            }
            for cid, ch in by_id.items()
        },
        {cid: values for cid, values in aligned.items() if cid in by_id},
        container_ct_ids=containers,
        direct_loads=direct_loads,
        report=report,
    )

    print("\nkinds:", dict(Counter(c.kind for c in candidates)))
    print("closure evidence available for:", sorted(direct_loads))
    print(
        "ambiguous:",
        len(report.get("ambiguous", [])),
        "suppressed:",
        len(report.get("suppressed", [])),
        "not_inferable:",
        len(report.get("not_inferable", [])),
    )
    print()
    for cand in candidates:
        ev = cand.evidence
        if cand.kind == "duplicate":
            print(
                f"  duplicate  {cand.child_ct_id:44s} == {cand.parent_ct_id:22s} "
                f"act={ev.get('activity_ratio')} rel={ev.get('median_relative_difference')} "
                f"ratio={ev.get('magnitude_ratio')}"
            )
        else:
            print(
                f"  nested     {cand.child_ct_id:44s} -> {cand.parent_ct_id:22s} "
                f"cont={ev.get('containment')} ambiguous={bool(ev.get('containment_ambiguous'))} "
                f"closure={ev.get('closure_fraction')} rem={ev.get('closure_median_remainder_w')}"
            )

    for entry in report.get("ambiguous", []):
        print(f"  AMBIGUOUS  {entry['ct_id']}: {entry['candidate_parent_ct_ids']}")

    if args.score:
        return _score(topology, candidates, report, aligned)
    return 0


def _score(
    topology: Topology,
    candidates: list,
    report: dict,
    aligned: dict[str, list[float]] | None = None,
) -> int:
    """Compare against the declared parents -- the only ground truth available.

    Two numbers matter, and they are reported separately because they answer
    different questions:

    * **measurable children** -- a ``main`` CT, whose level containment can
      establish.  This is the accuracy that counts.
    * **not_inferable** -- a load.  Reporting it as "missing" would be a lie
      dressed as a metric: a 9 W circuit is contained by every panel while idle,
      so no measurement rule can place it.
    """
    truth = {ch.ct_id: ch.parent_ct_id for ch in topology.channels if ch.parent_ct_id}
    by_id = {ch.ct_id: ch for ch in topology.channels}
    proposed = {c.child_ct_id: c.parent_ct_id for c in candidates if c.kind == "nested"}
    ambiguous = {
        entry["ct_id"]: entry["candidate_parent_ct_ids"]
        for entry in report.get("ambiguous", [])
    }
    not_inferable: set[str] = set()
    for entry in report.get("not_inferable", []):
        not_inferable.add(str(entry["ct_id"]))

    print(f"\n--- score against {len(truth)} declared parents ---")
    correct = wrong = ambiguous_n = load_n = missing = 0
    for child, declared in sorted(truth.items()):
        is_main = str(by_id[child].ct_type) == "main"
        if child in proposed:
            if proposed[child] == declared:
                correct += 1
            else:
                wrong += 1
                print(
                    f"  WRONG      {child:44s} declared={declared} "
                    f"proposed={proposed[child]}"
                )
            continue
        if child in ambiguous:
            ambiguous_n += 1
            continue
        if not is_main or child in not_inferable:
            load_n += 1
            continue
        missing += 1
        print(f"  MISSING    {child:44s} declared={declared}")

    measurable = correct + wrong + ambiguous_n + missing
    print(
        f"\nmeasurable children: {measurable}  "
        f"correct={correct} wrong={wrong} ambiguous={ambiguous_n} missing={missing}"
    )
    if measurable:
        print(f"accuracy on measurable children: {correct / measurable:.3f}")
    print(f"loads correctly reported as not inferable: {load_n}")

    _feed_check(topology, candidates, aligned)
    return 0


def _feed_check(
    topology: Topology,
    candidates: list,
    aligned: dict[str, list[float]] | None,
) -> None:
    """Apply the proposals to a copy and count whole-home feeds.

    The live topology declares **no** parents, so the engine currently sums six
    mains.  That is the defect this change exists to fix, so it is the one number
    the replay must show moving.
    """
    from wattmosaic_engine.contracts import Topology as _Topology
    from wattmosaic_engine.electrical.topology import TopologyGraph

    before = TopologyGraph(topology).effective_feeds()
    payload = topology.model_dump(mode="json")
    by_id = {ch["ct_id"]: ch for ch in payload["channels"]}
    for candidate in candidates:
        if candidate.kind == "duplicate":
            by_id[candidate.child_ct_id]["alias_of_ct_id"] = candidate.parent_ct_id
        else:
            by_id[candidate.child_ct_id]["parent_ct_id"] = candidate.parent_ct_id
    after = TopologyGraph(_Topology.model_validate(payload)).effective_feeds()

    print("\n--- whole-home feeds ---")
    print(f"  before applying proposals: {len(before)}  {before}")
    print(f"  after applying proposals:  {len(after)}  {after}")
    if len(after) < len(before):
        print("  => nested feeds and aliases are no longer summed twice.")
    else:
        print("  => unchanged; the proposals did not fix a double count.")

    if aligned:
        mains = [m for m in after if m in aligned]
        if mains:
            totals: list[float] = []
            for i in range(len(aligned[mains[0]])):
                values = [
                    float(aligned[m][i])
                    for m in mains
                    if isinstance(aligned[m][i], (int, float))
                    and math.isfinite(float(aligned[m][i]))
                ]
                if values:
                    totals.append(sum(values))
            if totals:
                totals.sort()
                print(
                    f"  median whole-home power with {len(mains)} feeds: "
                    f"{totals[len(totals) // 2]:.0f} W"
                )


if __name__ == "__main__":
    raise SystemExit(main())
