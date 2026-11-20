"""Replay pulled HA history through the real v3 runtime and score the topology.

This tool is Deliverable B of ``docs/specs/ha-history-replay``.  It reads the
on-disk history produced by ``ha_history_pull.py`` plus a machine-readable
ground truth, seeds a *fresh* engine database with the GT readers (no CONTAINS
edges), replays the samples/events on an injected clock, and scores the inferred
topology with ``tools/dev/topology_score.py``.

Hard rules: no network, no live-install data in tracked files, no declared
CONTAINS edges, absent states are dropped (never 0), and the report contains
counts/scores only -- never raw samples.

Usage::

    .venv/bin/python tools/dev/replay_ground_truth.py \\
        --history data/raw/ha_history --gt data/raw/ground_truth/topology_gt.json \\
        --json-out data/raw/replay/report.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))
if str(ROOT / "tools" / "dev") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools" / "dev"))

from tools.dev import (  # noqa: E402 - sys.path set up above
    replay_pipeline,
    topology_score,
)

#: A sample-to-sample change at least this large counts as meaningful activity.
ACTIVITY_DELTA_W = 30.0
PROGRESS_EVERY = 100_000
DEFAULT_CHECKPOINT_HOURS = 12.0


# --------------------------------------------------------------------------- #
# Generic helpers (reuse replay_pipeline's where available)
# --------------------------------------------------------------------------- #


FACTOR_HISTORY_START = datetime(2000, 1, 1, tzinfo=UTC)


def _as_utc(value: Any) -> datetime:
    return replay_pipeline.as_utc(value)


def _iso(value: datetime) -> str:
    return replay_pipeline.iso(value)


def _parse_time(value: str | None) -> datetime | None:
    return replay_pipeline.parse_time(value)


def _value(item: Any) -> Any:
    return replay_pipeline.value(item)


def _iter_jsonl_gz(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a gzipped JSONL file (one object per line)."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _numeric(value: Any) -> float | None:
    """Absent is None, never 0: non-numeric states are dropped (HB §18)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _load_gt(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("readers"), list):
        raise SystemExit(f"invalid ground truth (no readers list): {path}")
    return payload


def load_history(
    history_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    entities: set[str] | None = None,
) -> tuple[list[tuple[datetime, dict[str, Any]]], list[tuple[datetime, dict[str, Any]]]]:
    """Load sample and event rows from the history directory, time-ordered.

    Returns ``(samples, events)`` where each item is ``(t, row)``.  Rows outside
    ``[since, until]`` are ignored; samples with a non-numeric ``v`` are dropped.
    """
    samples: list[tuple[datetime, dict[str, Any]]] = []
    events: list[tuple[datetime, dict[str, Any]]] = []
    def _day_in_range(path: Path) -> bool:
        # Files are named <kind>-YYYY-MM-DD.jsonl.gz (UTC days): skip whole days
        # outside [since, until] instead of parsing every row of the history.
        try:
            day = datetime.strptime(path.name.split("-", 1)[1][:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return True
        if since and day + timedelta(days=1) <= since:
            return False
        return not (until and day > until)

    for path in sorted(history_dir.glob("samples-*.jsonl.gz")):
        if not _day_in_range(path):
            continue
        for row in _iter_jsonl_gz(path):
            if entities is not None and row.get("entity_id") not in entities:
                continue
            value = _numeric(row.get("v"))
            if value is None:
                continue
            sample = {
                "entity_id": str(row.get("entity_id", "")),
                "t": _as_utc(row["t"]),
                "v": value,
            }
            if since and sample["t"] < since:
                continue
            if until and sample["t"] > until:
                continue
            samples.append((sample["t"], sample))
    for path in sorted(history_dir.glob("events-*.jsonl.gz")):
        if not _day_in_range(path):
            continue
        for row in _iter_jsonl_gz(path):
            event = {
                "entity_id": str(row.get("entity_id", "")),
                "t": _as_utc(row["t"]),
                "state": row.get("state"),
                "attributes": row.get("attributes") if isinstance(row.get("attributes"), dict) else {},
            }
            if since and event["t"] < since:
                continue
            if until and event["t"] > until:
                continue
            events.append((event["t"], event))
    samples.sort(key=lambda item: item[0])
    events.sort(key=lambda item: item[0])
    return samples, events


# --------------------------------------------------------------------------- #
# Seeding the fresh database
# --------------------------------------------------------------------------- #


def seed_readers(db: Any, gt: dict[str, Any]) -> list[dict[str, Any]]:
    """Create one CT endpoint per GT reader plus its multiplier=1.0 revision.

    No CONTAINS edges are declared: the topology must be inferred from the
    replayed observations.
    """
    entities = db.repos.entities
    readers: list[dict[str, Any]] = []
    for reader in gt["readers"]:
        rid = str(reader["reader_id"])
        power_entity = str(reader.get("power_entity") or "")
        entities.upsert_endpoint(
            {
                "endpoint_id": rid,
                "kind": "ct",
                "entity_id": power_entity or None,
                "display_name": str(reader.get("name") or rid),
                "scope": "circuit",
                "scope_status": "candidate",
                "enabled": True,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )
        entities.append_endpoint_revision(
            {
                "endpoint_id": rid,
                "revision": 1,
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                "multiplier": 1.0,
                "phase": "l1",
                "polarity": "normal",
                "calibration_version": "replay@1",
                "measurement_type": "instantaneous",
                "declared_by": "import",
            }
        )
        readers.append(reader)
    return readers


def declare_installer_knowledge(core: Any, gt: dict[str, Any], declare: str) -> dict[str, int]:
    """Declare PAIRED_LEG / SAME_CONDUCTOR user edges (never CONTAINS)."""
    declared = {"pairs": 0, "aliases": 0}
    if declare == "none":
        return declared
    if declare in ("pairs", "pairs+aliases"):
        for pair in gt.get("paired_leg") or ():
            if isinstance(pair, Mapping):
                left = pair.get("left") or pair.get("a") or pair.get("source")
                right = pair.get("right") or pair.get("b") or pair.get("target")
            elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
                left, right = pair[0], pair[1]
            else:
                continue
            if left is None or right is None:
                continue
            core.declare_topology_edge(
                relation="paired_leg", source=str(left), target=str(right), declared_by="user"
            )
            declared["pairs"] += 1
    if declare == "pairs+aliases":
        for entry in gt.get("same_conductor") or ():
            if isinstance(entry, Mapping):
                alias, canonical = entry.get("alias"), entry.get("canonical")
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                alias, canonical = entry[0], entry[1]
            else:
                continue
            if alias is None or canonical is None:
                continue
            core.declare_topology_edge(
                relation="same_conductor",
                source=str(alias),
                target=str(canonical),
                declared_by="user",
            )
            declared["aliases"] += 1
    return declared


def store_factors(history_dir: Path, db: Any, gt: dict[str, Any], factors: str) -> dict[str, Any]:
    """Store each derived factor interval in the engine's factor store.

    The endpoint multiplier stays at 1.0: a factor row is a time-scoped
    declaration consulted by the engine at the reading's timestamp, so the
    historical change is reconstructed without renormalising the endpoint.
    """
    report: dict[str, Any] = {
        "stored": 0,
        "by_reader": {},
        "unknown": [],
        "file": None,
    }
    if factors == "none":
        return report
    path = history_dir / "factors.json"
    if not path.exists():
        return report
    report["file"] = str(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    entity_to_reader = {
        str(reader.get("power_entity")): str(reader["reader_id"]) for reader in gt["readers"]
    }
    from wattmosaic_engine.contracts import MeasurementFactor

    for entity_id, intervals in (payload.get("factors") or {}).items():
        reader_id = entity_to_reader.get(str(entity_id))
        if reader_id is None:
            continue
        for position, interval in enumerate(intervals):
            effective_to = interval.get("to")
            # The first derived interval starts at the first *qualifying* hour,
            # not at the start of the data.  With no earlier contrary evidence
            # the factor in force before it is the same one, so extend it back;
            # otherwise a x2 reader's early steps would be compared unscaled.
            effective_from = FACTOR_HISTORY_START if position == 0 else _as_utc(interval["from"])
            db.repos.factors.insert_measurement_factor(
                MeasurementFactor(
                    ct_id=reader_id,
                    multiplier=float(interval["factor"]),
                    effective_from=effective_from,
                    effective_to=_as_utc(effective_to) if effective_to else None,
                    applied_by="monitor",
                    declared_by="import",
                    note="derived from HA history: P/(V*I*PF) snapped",
                )
            )
            report["stored"] += 1
            report["by_reader"][reader_id] = report["by_reader"].get(reader_id, 0) + 1
    for entity_id in payload.get("unknown") or ():
        reader_id = entity_to_reader.get(str(entity_id))
        if reader_id is not None:
            report["unknown"].append(reader_id)
    return report


# --------------------------------------------------------------------------- #
# Scoring bridge
# --------------------------------------------------------------------------- #


def _row_evidence(row: Any) -> dict[str, Any]:
    raw = row.get("evidence") if isinstance(row, dict) else None
    if raw is None and isinstance(row, dict):
        raw = row.get("evidence_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def edge_dicts(core: Any, db: Any | None = None) -> list[dict[str, Any]]:
    """Convert the engine's topology edges to the scorer's shape.

    The T5 ledger (``core.topology_inference.edges``) holds the inferred edges;
    user declarations (PAIRED_LEG / SAME_CONDUCTOR from ``--declare``) live in
    the stored ``topology_edges`` rows.  Both are merged; for the same
    (relation, source, target) the ledger's live copy wins.
    """
    view = core.topology_inference
    if db is None:
        db = getattr(core, "db", None)
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in getattr(view, "edges", ()) or ():
        evidence = edge.evidence
        item = {
            "source": str(edge.source_endpoint_id),
            "target": str(edge.target_endpoint_id),
            "relation": _value(edge.relation),
            "lifecycle": _value(edge.lifecycle),
            "support": int(getattr(evidence, "support", 0) or 0),
            "conflict": int(getattr(evidence, "conflict", 0) or 0),
            "parent_only": int(getattr(evidence, "parent_only", 0) or 0),
            "declared_by": str(getattr(edge, "declared_by", "")),
        }
        merged[(item["relation"], item["source"], item["target"])] = item
    repo = getattr(getattr(db, "repos", None), "topology_v3", None) if db is not None else None
    if repo is not None:
        for row in repo.edges():
            if row.get("valid_until"):
                continue
            key = (
                _value(row.get("relation")),
                str(row.get("source_endpoint_id")),
                str(row.get("target_endpoint_id")),
            )
            if key in merged:
                continue
            evidence = _row_evidence(row)
            merged[key] = {
                "source": key[1],
                "target": key[2],
                "relation": key[0],
                "lifecycle": _value(row.get("lifecycle")),
                "support": int(evidence.get("support", 0) or 0),
                "conflict": int(evidence.get("conflict", 0) or 0),
                "parent_only": int(evidence.get("parent_only", 0) or 0),
                "declared_by": str(row.get("declared_by", "")),
            }
    return list(merged.values())


def score_now(
    core: Any,
    gt: dict[str, Any],
    activity: dict[str, int],
    *,
    structural: Iterable[str],
    soft: Iterable[str],
) -> dict[str, Any]:
    return topology_score.score(
        edge_dicts(core),
        gt,
        structural=structural,
        soft=soft,
        activity_by_reader=activity,
    )


# --------------------------------------------------------------------------- #
# Replay driver
# --------------------------------------------------------------------------- #


def replay(
    history_dir: Path,
    gt: dict[str, Any],
    db_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    declare: str = "pairs+aliases",
    factors: str = "auto",
    checkpoint_hours: float = DEFAULT_CHECKPOINT_HOURS,
    max_records: int | None = None,
    structural: Iterable[str] = ("confirmed", "structural"),
    soft: Iterable[str] = ("provisional",),
) -> dict[str, Any]:
    """Replay the history through a real EngineCore and return the JSON report."""
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.realtime.harness import EngineHarness
    from wattmosaic_engine.storage.database import Database

    reader_by_entity = {
        str(reader["power_entity"]): str(reader["reader_id"])
        for reader in gt["readers"]
        if reader.get("power_entity")
    }
    # Only reader power entities are replayed; skipping factor inputs (V/I/PF)
    # at load keeps a 10-day history to about a quarter of the memory.
    samples, events = load_history(
        history_dir, since=since, until=until, entities=set(reader_by_entity)
    )
    # Merge samples of GT power entities with events, in time order.  Samples
    # for factor inputs (V/I/PF) are deliberately not fed as readings.
    records: list[tuple[datetime, str, dict[str, Any]]] = []
    for t, row in samples:
        rid = reader_by_entity.get(row["entity_id"])
        if rid is None:
            continue
        records.append(
            (
                t,
                "reading",
                {
                    "reader_id": rid,
                    "observed_at": t,
                    "received_at": t,
                    "value_w": row["v"],
                    "sequence": 0,
                    "measurement_window_ms": None,
                    "quality_flags_json": None,
                },
            )
        )
    for t, row in events:
        records.append(
            (
                t,
                "ha_event",
                {
                    "entity_id": row["entity_id"],
                    "observed_at": t,
                    "received_at": t,
                    "state": row["state"],
                    "attributes": row["attributes"],
                },
            )
        )
    records.sort(key=lambda item: (item[0], 0 if item[1] == "reading" else 1))
    if max_records is not None:
        records = records[:max_records]
    if not records:
        raise SystemExit("no replayable records (check --history/--since/--until)")

    origin = records[0][0]
    now = [origin]
    db = Database(str(db_path))
    config = EngineConfig(
        database_path=str(db_path),
        ha_token="x",
        api_token="replay",
        learning_enabled=False,
    )
    core = EngineCore(config, db, clock=lambda: now[0])
    core._wire_v3_services()  # noqa: SLF001 - composition seam, no HA source started

    declared = declare_installer_knowledge(core, gt, declare)
    factor_report = store_factors(history_dir, db, gt, factors)

    harness = EngineHarness(core, db, now, origin)
    consumer = harness._consumer  # noqa: SLF001 - production reader/event bridge
    original_sample = consumer.on_sample
    original_state = consumer.on_state_changed
    readings_fed = 0
    events_seen = 0

    def on_sample(payload: dict[str, Any]) -> None:
        nonlocal readings_fed
        readings_fed += 1
        return original_sample(payload)

    def on_state_changed(message: dict[str, Any]) -> None:
        nonlocal events_seen
        events_seen += 1
        return original_state(message)

    consumer.on_sample = on_sample
    consumer.on_state_changed = on_state_changed

    # Which entities would the live HASource emit as HA events?  Reuse its own
    # predicate rather than reimplementing the domain list (as replay_pipeline does).
    entity_sources: set[str] = set()
    for row in db.repos.entities.ha_entities():
        entity_id = str(row["entity_id"])
        if core.hasource._is_ha_event_source(entity_id, {}, origin):  # noqa: SLF001 - reuse source predicate
            entity_sources.add(entity_id)

    activity: Counter[str] = Counter()
    last_value: dict[str, float] = {}
    checkpoints: list[dict[str, Any]] = []
    next_checkpoint = origin + timedelta(hours=checkpoint_hours) if checkpoint_hours > 0 else origin
    checkpoint_s = checkpoint_hours * 3600.0
    last_heartbeat = origin
    started_wall = time.monotonic()

    try:
        for index, (observed, kind, payload) in enumerate(records, start=1):
            harness._advance(observed)  # noqa: SLF001 - harness clock seam
            if kind == "reading":
                rid = str(payload["reader_id"])
                value = payload["value_w"]
                previous = last_value.get(rid)
                if previous is not None and abs(value - previous) >= ACTIVITY_DELTA_W:
                    activity[rid] += 1
                last_value[rid] = value
                on_sample(payload)
            else:
                entity_id = str(payload["entity_id"])
                if entity_id in entity_sources:
                    on_state_changed(
                        {
                            "entity_id": entity_id,
                            "observed_at": payload["observed_at"],
                            "received_at": payload["received_at"],
                            "state": payload["state"],
                            "attributes": payload["attributes"],
                        }
                    )
            harness.pump()
            heartbeat_s = float(getattr(core.config, "steady_heartbeat_s", 60.0))
            if (harness.now - last_heartbeat).total_seconds() >= heartbeat_s:
                harness.heartbeat()
                last_heartbeat = harness.now

            if checkpoint_s > 0 and observed >= next_checkpoint:
                checkpoints.append(
                    {
                        "at": _iso(observed),
                        "replayed_hours": round((observed - origin).total_seconds() / 3600.0, 3),
                        "records": index,
                        "score": score_now(
                            core, gt, dict(activity), structural=structural, soft=soft
                        ),
                    }
                )
                next_checkpoint = observed + timedelta(hours=checkpoint_hours)
            if index % PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - started_wall
                rate = index / elapsed if elapsed else 0.0
                print(
                    f"replayed {index} records, {rate:.0f} records/s, "
                    f"time {_iso(harness.now)}",
                    flush=True,
                )

        harness.settle()
        harness.pump()
        final_score = score_now(core, gt, dict(activity), structural=structural, soft=soft)
        report = {
            "run": {
                "history": str(history_dir),
                "gt": None,  # filled by main()
                "since": _iso(since) if since else None,
                "until": _iso(until) if until else None,
                "origin": _iso(origin),
                "declare": declare,
                "factors": factors,
                "max_records": max_records,
                "checkpoint_hours": checkpoint_hours,
                "wall_seconds": round(time.monotonic() - started_wall, 3),
            },
            "records": {
                "total": len(records),
                "samples": sum(1 for _, kind, _ in records if kind == "reading"),
                "events": sum(1 for _, kind, _ in records if kind == "ha_event"),
                "readings_fed": readings_fed,
                "events_seen": events_seen,
            },
            "declarations": declared,
            "factors_stored": factor_report,
            "activity_by_reader": dict(sorted(activity.items())),
            "checkpoints": checkpoints,
            "final": {
                "at": _iso(harness.now),
                "records": len(records),
                "score": final_score,
                "edges": edge_dicts(core),
            },
        }
    finally:
        consumer.on_sample = original_sample
        consumer.on_state_changed = original_state
        harness.close()

    return report


def format_summary(report: dict[str, Any]) -> str:
    """A compact human summary using GT reader names (no raw samples)."""
    score = report["final"]["score"]
    names = score.get("gt_reader_names") or {}
    direct = score["contains_direct"]["structural"]
    lines = [
        "WattMosaic ground-truth replay",
        "=" * 30,
        f"records: {report['records']['total']} "
        f"({report['records']['samples']} samples, {report['records']['events']} events)",
        f"declared: {report['declarations']}",
        f"factors stored: {report['factors_stored']['stored']}",
        f"direct CONTAINS: precision={direct['precision']:.3f} "
        f"recall={direct['recall']:.3f} f1={direct['f1']:.3f}",
        f"recall (scorable): {score['recall_scorable']['recall']:.3f}",
        f"checkpoints: {len(report['checkpoints'])}",
        "",
        "Per child:",
    ]
    for child in score["per_child"]:
        inferred = ", ".join(
            f"{names.get(item['parent'], item['parent'])} [{item['lifecycle']}]"
            for item in child["inferred_parents"]
        ) or "-"
        lines.append(
            f"  {child['child_name']}: {child['status']} "
            f"(expected {child['gt_parent_name']}; got {inferred})"
        )
    lines.append("")
    lines.append("Violations:")
    for kind, items in score["violations"].items():
        lines.append(f"  {kind}: {len(items)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", required=True, type=Path, help="on-disk HA history directory")
    parser.add_argument("--gt", required=True, type=Path, help="topology ground-truth JSON")
    parser.add_argument("--since", help="only replay rows at/after this ISO time")
    parser.add_argument("--until", help="only replay rows at/before this ISO time")
    parser.add_argument(
        "--declare",
        choices=("none", "pairs", "pairs+aliases"),
        default="pairs+aliases",
        help="installer declarations (never CONTAINS); default pairs+aliases",
    )
    parser.add_argument(
        "--factors", choices=("auto", "none"), default="auto", help="store derived factor intervals"
    )
    parser.add_argument(
        "--checkpoint-hours",
        type=float,
        default=DEFAULT_CHECKPOINT_HOURS,
        help="score every N replayed hours (0 disables checkpoints)",
    )
    parser.add_argument("--json-out", type=Path, help="write the JSON report here")
    parser.add_argument("--db-out", type=Path, help="keep the replay database at this path")
    parser.add_argument("--max-records", type=int, help="stop after this many records")
    parser.add_argument("--json", action="store_true", help="print the JSON report instead of a summary")
    args = parser.parse_args(argv)

    history_dir = args.history.resolve()
    if not history_dir.is_dir():
        parser.error(f"history directory not found: {history_dir}")
    gt_path = args.gt.resolve()
    if not gt_path.is_file():
        parser.error(f"ground-truth file not found: {gt_path}")
    gt = _load_gt(gt_path)
    since, until = _parse_time(args.since), _parse_time(args.until)
    if args.checkpoint_hours < 0:
        parser.error("--checkpoint-hours must be >= 0")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.db_out:
        db_path = args.db_out.resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="wm-replay-gt-")
        db_path = Path(temp_dir.name) / "replay.db"

    try:
        report = replay(
            history_dir,
            gt,
            db_path,
            since=since,
            until=until,
            declare=args.declare,
            factors=args.factors,
            checkpoint_hours=args.checkpoint_hours,
            max_records=args.max_records,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    report["run"]["gt"] = str(gt_path)
    payload = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print(format_summary(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual tool
    raise SystemExit(main())
