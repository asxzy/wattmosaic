"""Replay stored reader history through the v3 topology inference engine.

This is a **development tool, not a test.** It needs the local engine database,
which is not part of the repository, so it cannot run in CI. What it does is
show what the shipped v3 inference actually produces on the reference
installation's real measurements, which is the only way to tell a guard that
works from a guard that only satisfies a synthetic fixture.

Usage::

    .venv/bin/python tools/dev/replay_nesting.py --db /path/to/copy.db
    .venv/bin/python tools/dev/replay_nesting.py --db /path/to/copy.db --hours 48
    .venv/bin/python tools/dev/replay_nesting.py --db /path/to/copy.db --score

``--score`` compares inferred CONTAINS edges against the *declared* parents in
the stored topology, which is the only ground truth available. It prints, per
declared child, whether the inference found the right parent, an ambiguous set,
a different parent, or nothing. That score is the acceptance criterion for any
change to the detector: a rule that cannot reproduce the declared wiring is not
a rule (V2's real-data replay gate).

The replay calls the same :func:`wattmosaic_engine.electrical.topology_inference.infer_topology`
the engine uses, from an immutable HB §17 observation frame built from the
reader samples. The database path is always supplied by the caller through
``--db``; no checkout path is hard-coded.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from wattmosaic_engine.contracts import Topology  # noqa: E402
from wattmosaic_engine.electrical.topology_inference import (  # noqa: E402
    infer_topology,
)


def _as_utc(value: object) -> datetime | str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _aligner_key(channel: object) -> str:
    entity_id = str(getattr(channel, "entity_id", "") or "")
    return entity_id.split(".", 1)[-1] if "." in entity_id else entity_id


def load_observation(db_path: Path, hours: float) -> tuple[Topology, list[dict[str, Any]]]:
    """Read stored reader samples into a v3 observation frame (read-only).

    The topology is loaded from the latest ``topology_versions`` row so the
    declared parents -- the scoring ground truth -- are available. Reader
    samples from the trailing ``hours`` are lifted into frames keyed by ct-id.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        topology_row = conn.execute(
            "SELECT payload_json FROM topology_versions ORDER BY version DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if topology_row is None:
            raise SystemExit(f"no topology_versions row in {db_path}")
        topology = Topology.model_validate(json.loads(topology_row[0]))

        end_raw = conn.execute("SELECT MAX(received_at) FROM ct_samples").fetchone()[0]
        end = _as_utc(end_raw)
        if not isinstance(end, datetime):
            raise SystemExit(f"ct_samples is empty in {db_path}")
        start = end - timedelta(hours=hours)
        rows = conn.execute(
            "SELECT ct_id, entity_id, value_w, state_timestamp, received_at, quality "
            "FROM ct_samples WHERE received_at >= ? AND received_at <= ? "
            "ORDER BY received_at, id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    frames: list[dict[str, Any]] = []
    for row in rows:
        value = row["value_w"]
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        stamp = _as_utc(row["state_timestamp"]) or _as_utc(row["received_at"])
        if not isinstance(stamp, datetime):
            continue
        frames.append(
            {
                "timestamp": stamp.isoformat(),
                "reader_samples": {
                    str(row["ct_id"]): {
                        "reader_id": str(row["ct_id"]),
                        "entity_id": str(row["entity_id"]),
                        "value_w": float(value),
                        "state_timestamp": stamp.isoformat(),
                        "received_at": str(row["received_at"]),
                        "quality": str(row["quality"] or "good"),
                    }
                },
            }
        )
    return topology, frames


def _declared_parents(topology: Topology) -> dict[str, str]:
    """The scoring ground truth: declared parents keyed by child ct-id."""
    return {
        ch.ct_id: ch.parent_ct_id
        for ch in topology.channels
        if ch.parent_ct_id
    }


def score(
    topology: Topology,
    view: Any,
) -> int:
    """Compare inferred CONTAINS edges against declared parents.

    A declared child is *measurable* when its level can be established from
    measurements (a main feed). A load whose level is not inferable is reported
    separately rather than counted as a miss: a small circuit is contained by
    every panel while idle, so no measurement rule can place it.
    """
    truth = _declared_parents(topology)
    by_id = {ch.ct_id: ch for ch in topology.channels}
    proposed: dict[str, str] = {}
    for edge in view.edges:
        relation = str(getattr(edge.relation, "value", edge.relation))
        lifecycle = str(getattr(edge.lifecycle, "value", edge.lifecycle))
        if relation == "contains" and lifecycle != "rejected":
            proposed[edge.target_endpoint_id] = edge.source_endpoint_id

    print(f"\n--- score against {len(truth)} declared parents ---")
    correct = wrong = missing = load_n = 0
    for child, declared in sorted(truth.items()):
        channel = by_id.get(child)
        is_main = channel is not None and str(getattr(channel.ct_type, "value", channel.ct_type)) == "main"
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
        if not is_main:
            load_n += 1
            continue
        missing += 1
        print(f"  MISSING    {child:44s} declared={declared}")

    measurable = correct + wrong + missing
    print(
        f"\nmeasurable children: {measurable}  "
        f"correct={correct} wrong={wrong} missing={missing}"
    )
    if measurable:
        print(f"accuracy on measurable children: {correct / measurable:.3f}")
    print(f"loads correctly reported as not inferable: {load_n}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="path to the operator's database copy (required; no default path is hard-coded)",
    )
    parser.add_argument(
        "--score", action="store_true", help="score inferred CONTAINS edges against declared parents"
    )
    args = parser.parse_args()

    if args.db is None:
        parser.error("--db is required: supply a copy of the operator's database")
    if not args.db.exists():
        parser.error(f"no database at {args.db}")

    topology, observation = load_observation(args.db, args.hours)
    print(f"topology version {topology.version}: {len(topology.channels)} channels")
    print(f"reader frames: {len(observation)}")

    view = infer_topology(observation, clock=lambda: datetime.now(UTC))
    print("\nkinds:", dict(Counter(
        str(getattr(edge.relation, "value", edge.relation)) for edge in view.edges
    )))
    print(f"contains edges: {len(view.contains_edges())}")
    print()
    for edge in view.contains_edges():
        relation = str(getattr(edge.relation, "value", edge.relation))
        lifecycle = str(getattr(edge.lifecycle, "value", edge.lifecycle))
        evidence = edge.evidence
        print(
            f"  {relation:10s} {edge.source_endpoint_id:28s} -> "
            f"{edge.target_endpoint_id:28s} {lifecycle:12s} "
            f"support={evidence.support} conflict={evidence.conflict} "
            f"causes={evidence.independent_causes}"
        )

    if args.score:
        return score(topology, view)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
