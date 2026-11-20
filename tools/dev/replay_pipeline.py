"""Offline replay diagnostic: where does a live database stop in the v3 pipeline?

The live engine can ingest readings and HA events yet discover no topology edges.
This tool answers *which stage* stops that pipeline by replaying a **copy** of a
live WattMosaic database through the **real** v3 runtime:

* the endpoint declarations are copied into a fresh temporary database (the
  source file is only ever read, never modified);
* an ``EngineCore`` is wired with the real services (P2/P3/P4/P5, T5 ledger,
  D1 lifecycles) on an **injected clock** -- no HA source, no network, no sleeps;
* reader readings (``ct_samples``) and HA events (``ha_events_v3``) are merged in
  ``observed_at`` order and fed through the production fast path
  (``EngineCore.on_reading`` / ``EngineCore.on_ha_event``);
* after each record the event loop, the steady loop and the background executor
  (T5 ledger folds) are driven synchronously, reusing ``EngineHarness``'s pump /
  heartbeat / settle seams rather than duplicating them;
* a JSON + text report counts what reached each stage: readings, P3 steps and
  multi-reader coincidences, P4 windows, P5 groups, ``on_group_ready`` deliveries,
  ``infer_group`` calls, T5 ledger counters per pair, and edges proposed by the
  lifecycle.  The T5 thresholds in force and, for the top pairs, the threshold
  each is still short of are reported too.

Usage::

    .venv/bin/python tools/dev/replay_pipeline.py --db /path/to/wattmosaic.db
    .venv/bin/python tools/dev/replay_pipeline.py --db copy.db --since 2026-11-01T00:00:00Z \
        --until 2026-11-02T00:00:00Z --json-out /tmp/replay.json

No real data is written to the repository: a temporary copy is made and removed
on exit, and the report contains counts only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))

#: Tables whose rows are the user's *declarations*, copied into the fresh DB.
#: No samples, evidence, groups, snapshots or models are copied: those are the
#: outputs this replay re-derives from the source observations.
DECLARATION_TABLES = (
    "measurement_endpoints",
    "endpoint_config_revisions",
    "ha_entities",
    "ha_entity_links",
)

#: Two P3 steps count as a multi-reader coincidence within this window.
COINCIDENCE_WINDOW_S = 10.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"invalid ISO time: {value!r}") from exc


def _value(item: Any) -> Any:
    """Pydantic may store an enum object or its value."""
    return getattr(item, "value", item)


# Public aliases for the small time/value helpers, so sibling replay tools can
# reuse the exact same parsing without duplicating it.  Behaviour is unchanged;
# the private names above remain the canonical definitions used by this module.
as_utc = _as_utc
iso = _iso
parse_time = _parse_time
value = _value


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# --------------------------------------------------------------------------- #
# Source reads (strictly read-only) and temp-database preparation
# --------------------------------------------------------------------------- #


def _prepare_work_db(source: Path, target: Path) -> dict[str, int]:
    """Create a fresh engine-managed DB with only the declaration rows copied.

    The source is opened read-only (``mode=ro``); ``target`` starts empty so the
    engine's ``Database`` applies the real migrations before the rows are added.
    """
    from wattmosaic_engine.storage.database import Database

    copied: dict[str, int] = {}
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    db = Database(str(target))
    try:
        for table in DECLARATION_TABLES:
            if not _table_columns(src, table):
                copied[table] = 0
                continue
            rows = [dict(row) for row in src.execute(f"SELECT * FROM {table}").fetchall()]
            columns = sorted(_table_columns(db._conn, table))  # noqa: SLF001 - storage seam
            present = [column for column in columns if rows and column in rows[0]]
            if rows and present:
                placeholders = ",".join("?" for _ in present)
                db._conn.executemany(  # noqa: SLF001 - storage seam, single writer
                    f"INSERT OR REPLACE INTO {table}({','.join(present)}) VALUES({placeholders})",
                    [tuple(row.get(column) for column in present) for row in rows],
                )
            copied[table] = len(rows)
    finally:
        db.close()
        src.close()
    return copied


def _read_observations(
    source: Path,
    since: datetime | None,
    until: datetime | None,
) -> list[tuple[datetime, str, dict[str, Any]]]:
    """Return ``(observed_at, kind, payload)`` triples for the replay stream."""
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    records: list[tuple[datetime, str, dict[str, Any]]] = []
    try:
        sample_cols = _table_columns(conn, "ct_samples")
        event_cols = _table_columns(conn, "ha_events_v3")
        lo = _iso(since) if since else None
        hi = _iso(until) if until else None

        if sample_cols:
            query = "SELECT * FROM ct_samples WHERE 1=1"
            params: list[Any] = []
            if lo:
                query += " AND COALESCE(observed_at, state_timestamp) >= ?"
                params.append(lo)
            if hi:
                query += " AND COALESCE(observed_at, state_timestamp) <= ?"
                params.append(hi)
            query += " ORDER BY COALESCE(observed_at, state_timestamp), sequence, id"
            for row in conn.execute(query, params).fetchall():
                data = dict(row)
                observed = _as_utc(data.get("observed_at") or data["state_timestamp"])
                value = (
                    data.get("active_power_v2_w")
                    if "active_power_v2_w" in data
                    else data["value_w"]
                )
                records.append(
                    (
                        observed,
                        "reading",
                        {
                            "reader_id": str(data["ct_id"]),
                            "observed_at": observed,
                            "received_at": _as_utc(data["received_at"]),
                            "value_w": value,
                            "sequence": int(data.get("sequence") or 0),
                            "measurement_window_ms": data.get("measurement_window_ms"),
                            "quality_flags_json": data.get("quality_flags_json"),
                        },
                    )
                )

        if event_cols:
            query = "SELECT * FROM ha_events_v3 WHERE 1=1"
            params = []
            if lo:
                query += " AND observed_at >= ?"
                params.append(lo)
            if hi:
                query += " AND observed_at <= ?"
                params.append(hi)
            query += " ORDER BY observed_at, event_id"
            for row in conn.execute(query, params).fetchall():
                data = dict(row)
                observed = _as_utc(data["observed_at"])
                try:
                    attributes = json.loads(data.get("attributes_delta_json") or "{}")
                except (TypeError, ValueError):
                    attributes = {}
                if not isinstance(attributes, dict):
                    attributes = {}
                records.append(
                    (
                        observed,
                        "ha_event",
                        {
                            "entity_id": str(data["entity_id"]),
                            "observed_at": observed,
                            "received_at": _as_utc(data["received_at"]),
                            "from_state": data.get("from_state"),
                            "to_state": data.get("to_state"),
                            "attributes_delta": attributes,
                            "context_id": data.get("context_id"),
                            "context_parent_id": data.get("context_parent_id"),
                            "context_user_id": data.get("context_user_id"),
                            "linked": bool(data.get("linked")),
                        },
                    )
                )
    finally:
        conn.close()
    records.sort(key=lambda item: (item[0], 0 if item[1] == "reading" else 1))
    return records


# --------------------------------------------------------------------------- #
# Instrumentation: observe the real runtime without reimplementing decisions
# --------------------------------------------------------------------------- #


class _Probe:
    """Wrap runtime seams; each wrapper delegates to the real method first."""

    def __init__(self, core: Any, entity_sources: set[str]) -> None:
        self.core = core
        self.entity_sources = entity_sources
        self.step_events: list[tuple[datetime, str, float, str]] = []
        self.windows_opened: set[str] = set()
        self.evidence_finalized: set[str] = set()
        self.group_deliveries = 0
        self.ledger_folds = 0
        self.entity_events_seen = 0
        self.entity_events_filtered = 0
        self.readings_fed = 0

        self._original_detector = core.change_detector.emit
        self._original_group_ready = core.on_group_ready
        self._original_sink = core.window_collector.sink
        self._original_fold = core._fold_group_ledger

        core.change_detector.emit = self._on_step
        core.on_group_ready = self._on_group_ready
        core.window_collector.sink = self._on_evidence_batch
        core._fold_group_ledger = self._count_fold

    def _on_step(self, event: Any) -> None:
        reader = str(event.reader_id)
        step_class = str(_value(event.step_class))
        delta = getattr(event, "delta_w", None)
        if delta is not None and step_class != "data_gap":
            self.step_events.append(
                (
                    getattr(event, "detected_at", None) or self.core._clock(),
                    reader,
                    float(delta),
                    step_class,
                )
            )
        return self._original_detector(event)

    def _on_evidence_batch(self, batch: Any) -> None:
        result = self._original_sink(batch)
        for item in batch:
            evidence = getattr(item, "evidence", item)
            if getattr(evidence, "finalized", False):
                self.evidence_finalized.add(str(getattr(evidence, "evidence_id", "")))
        return result

    def _on_group_ready(self, group: Any) -> None:
        self.group_deliveries += 1
        return self._original_group_ready(group)

    def _count_fold(self, group: Any) -> int:
        self.ledger_folds += 1
        return self._original_fold(group)

    def restore(self) -> None:
        core = self.core
        core.change_detector.emit = self._original_detector
        core.on_group_ready = self._original_group_ready
        core.window_collector.sink = self._original_sink
        core._fold_group_ledger = self._original_fold


def _compute_coincidences(steps: list[tuple[datetime, str, float, str]]) -> list[tuple[datetime, tuple[str, ...]]]:
    """Distinct readers with P3 steps inside a rolling 10 s window."""
    if not steps:
        return []
    ordered = sorted(steps, key=lambda item: item[0])
    coincidences: list[tuple[datetime, tuple[str, ...]]] = []
    window: list[tuple[datetime, str]] = []
    for at, reader, _delta, _step_class in ordered:
        if window and (at - window[0][0]).total_seconds() > COINCIDENCE_WINDOW_S:
            window = []
        window.append((at, reader))
        readers = tuple(sorted({r for _, r in window}))
        if len(readers) >= 2 and (not coincidences or coincidences[-1][1] != readers):
            coincidences.append((at, readers))
    return coincidences


# --------------------------------------------------------------------------- #
# Replay driver
# --------------------------------------------------------------------------- #


def replay(
    work_db: Path,
    *,
    observation_source: Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    speed: float = 1.0,
) -> dict[str, Any]:
    """Replay the prepared database copy through the real runtime.

    ``work_db`` holds only the copied declarations and engine outputs; the raw
    observations are read strictly read-only from ``observation_source`` (the
    original database), which the replay never modifies.
    """
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.electrical import topology_inference as ti
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.realtime.harness import EngineHarness
    from wattmosaic_engine.storage.database import Database

    records = _read_observations(observation_source or work_db, since, until)
    if not records:
        raise SystemExit("no ct_samples/ha_events_v3 rows to replay (check --since/--until)")
    origin = records[0][0]

    # Build the core over the prepared (copied) DB with an injected clock, exactly
    # as the deterministic harness does, but with our file-backed DB.
    now = [origin]
    db = Database(str(work_db))
    config = EngineConfig(
        database_path=str(work_db),
        ha_token="x",
        api_token="replay",
        learning_enabled=False,
    )
    core = EngineCore(config, db, clock=lambda: now[0])
    core._wire_v3_services()  # noqa: SLF001 - composition seam, no HA source started
    harness = EngineHarness(core, db, now, origin)

    # Which HA entities would the live HASource emit?  Reuse its own predicate
    # (it calls is_event_domain, then consults ha_entity_links for a behavioural
    # role) -- do not reimplement the domain list or the link-role rule here.
    entity_sources: set[str] = set()
    for row in db.repos.entities.ha_entities():
        entity_id = str(row["entity_id"])
        if core.hasource._is_ha_event_source(entity_id, {}, origin):  # noqa: SLF001 - reuse the source predicate
            entity_sources.add(entity_id)

    probe = _Probe(core, entity_sources)
    consumer = harness._consumer  # noqa: SLF001 - harness's production reader/event bridge
    _original_consumer_sample = consumer.on_sample
    _original_consumer_state = consumer.on_state_changed

    def _on_sample(payload: dict[str, Any]) -> None:
        # The harness consumer builds the canonical ReaderMeasurement (polarity
        # and multiplier once) and calls the production fast path; count only.
        probe.readings_fed += 1
        return _original_consumer_sample(payload)

    def _on_state_changed(msg: dict[str, Any]) -> None:
        # The harness consumer builds the device-free HAEvent with top-level
        # context and links, exactly as HASource._emit_ha_event does.
        return _original_consumer_state(msg)

    consumer.on_sample = _on_sample
    consumer.on_state_changed = _on_state_changed

    opened_windows: set[str] = set()
    last_heartbeat = origin
    try:
        for observed, kind, payload in records:
            harness._advance(observed)  # noqa: SLF001 - harness clock seam
            if kind == "reading":
                if payload["value_w"] is None:
                    continue
                _on_sample(payload)
            else:
                entity_id = payload["entity_id"]
                if entity_id not in entity_sources:
                    probe.entity_events_filtered += 1
                    continue
                probe.entity_events_seen += 1
                _on_state_changed(
                    {
                        "entity_id": entity_id,
                        "observed_at": payload["observed_at"],
                        "received_at": payload["received_at"],
                        "state": payload["to_state"],
                        "from_state": payload["from_state"],
                        "attributes": payload["attributes_delta"],
                        "context": {
                            "id": payload["context_id"],
                            "parent_id": payload["context_parent_id"],
                            "user_id": payload["context_user_id"],
                        },
                    }
                )
            for window in core.window_collector.windows.values():
                opened_windows.add(str(window.evidence.evidence_id))
            harness.pump()
            heartbeat_s = float(getattr(core.config, "steady_heartbeat_s", 60.0))
            if (harness.now - last_heartbeat).total_seconds() >= heartbeat_s:
                harness.heartbeat()
                last_heartbeat = harness.now
        harness.settle()
        harness.pump()
        for item in core.window_collector.closed.values():
            if getattr(item.evidence, "finalized", False):
                probe.evidence_finalized.add(str(item.evidence.evidence_id))
        # Build the report while the DB is still open: it reads stored groups.
        report = _build_report(core, probe, opened_windows, origin, len(records), since, until, speed, ti)
    finally:
        consumer.on_sample = _original_consumer_sample
        consumer.on_state_changed = _original_consumer_state
        probe.restore()
        harness.close()

    return report


def _build_report(
    core: Any,
    probe: _Probe,
    opened_windows: set[str],
    origin: datetime,
    n_records: int,
    since: datetime | None,
    until: datetime | None,
    speed: float,
    ti: Any,
) -> dict[str, Any]:
    steps_by_reader: Counter[str] = Counter(reader for _, reader, _, _ in probe.step_events)
    coincidences = _compute_coincidences(probe.step_events)

    groups_by_status: Counter[str] = Counter()
    groups_with_multi_reader: list[str] = []
    for row in core.db.repos.evidence.groups():
        groups_by_status[str(_value(row.status))] += 1
        deltas = row.reader_deltas or []
        if len({str(_value(getattr(d, "reader_id", None))) for d in deltas}) >= 2:
            groups_with_multi_reader.append(str(row.group_id))

    view = core.topology_inference
    pairs: list[dict[str, Any]] = []
    for edge in view.edges:
        evidence = edge.evidence
        pairs.append(
            {
                "parent": str(edge.source_endpoint_id),
                "child": str(edge.target_endpoint_id),
                "support": int(evidence.support),
                "conflict": int(evidence.conflict),
                "parent_only": int(evidence.parent_only),
                "independent_causes": int(evidence.independent_causes),
                "lifecycle": str(_value(edge.lifecycle)),
            }
        )
    pairs.sort(key=lambda item: (-item["support"], item["parent"], item["child"]))

    proposed_by_lifecycle: Counter[str] = Counter(str(_value(e.lifecycle)) for e in view.edges)

    thresholds = {
        "MIN_SUPPORT_CANDIDATE": ti.MIN_SUPPORT_CANDIDATE,
        "MIN_SUPPORT_PROVISIONAL": ti.MIN_SUPPORT_PROVISIONAL,
        "MIN_SUPPORT_CONFIRMED": ti.MIN_SUPPORT_CONFIRMED,
        "MIN_INDEPENDENT_CAUSES": ti.MIN_INDEPENDENT_CAUSES,
        "CONFLICT_RATIO_PROVISIONAL": ti.CONFLICT_RATIO_PROVISIONAL,
        "CONFLICT_RATIO_CONFIRMED": ti.CONFLICT_RATIO_CONFIRMED,
        "COINCIDENCE_WINDOW_S": COINCIDENCE_WINDOW_S,
    }
    top_pairs: list[dict[str, Any]] = []
    for pair in pairs[:10]:
        short: list[str] = []
        decisive = pair["support"] + pair["conflict"]
        ratio = (pair["conflict"] / decisive) if decisive else 0.0
        if pair["independent_causes"] < ti.MIN_INDEPENDENT_CAUSES:
            short.append(f"independent_causes {pair['independent_causes']}/{ti.MIN_INDEPENDENT_CAUSES}")
        if pair["support"] < ti.MIN_SUPPORT_PROVISIONAL:
            short.append(f"support {pair['support']}/{ti.MIN_SUPPORT_PROVISIONAL} (PROVISIONAL)")
        if ratio >= ti.CONFLICT_RATIO_PROVISIONAL:
            short.append(f"conflict_ratio {ratio:.2f} >= {ti.CONFLICT_RATIO_PROVISIONAL}")
        if pair["support"] < ti.MIN_SUPPORT_CONFIRMED:
            short.append(f"support {pair['support']}/{ti.MIN_SUPPORT_CONFIRMED} (CONFIRMED)")
        top_pairs.append({**pair, "conflict_ratio": round(ratio, 4), "short_of": short})

    snapshot = core._v3_snapshot()
    snapshot_counts = (
        {
            "reader_measurements": len(snapshot.reader_measurements),
            "device_estimates": len(snapshot.device_estimates),
            "group_estimates": len(snapshot.group_estimates),
            "residual_estimates": len(snapshot.residual_estimates),
        }
        if snapshot is not None
        else {"reader_measurements": 0, "device_estimates": 0, "group_estimates": 0, "residual_estimates": 0}
    )

    return {
        "input": {
            "since": _iso(since) if since else None,
            "until": _iso(until) if until else None,
            "speed": speed,
            "origin": _iso(origin),
            "records": n_records,
        },
        "readings_fed": probe.readings_fed,
        "ha_events": {
            "emitted_by_source": probe.entity_events_seen,
            "filtered_out": probe.entity_events_filtered,
            "delivered": core.events_ingested,
        },
        "detector": {
            "step_events": len(probe.step_events),
            "steps_per_reader": dict(sorted(steps_by_reader.items())),
            "multi_reader_coincidences": len(coincidences),
            "coincidence_examples": [
                {"at": _iso(at), "readers": list(readers)} for at, readers in coincidences[:10]
            ],
        },
        "windows": {
            "opened": len(opened_windows | probe.evidence_finalized),
            "finalized": len(probe.evidence_finalized),
        },
        "groups": {
            "by_status": dict(sorted(groups_by_status.items())),
            "with_at_least_two_reader_deltas": len(groups_with_multi_reader),
            "delivered_to_on_group_ready": probe.group_deliveries,
        },
        "edges": {
            "infer_group_calls": core.infer_group_calls,
            "snapshot_estimate_counts": snapshot_counts,
            "proposed_by_lifecycle": dict(sorted(proposed_by_lifecycle.items())),
        },
        "t5": {
            "ledger_folds": probe.ledger_folds,
            "thresholds": thresholds,
            "pairs": pairs,
            "top_pairs": top_pairs,
        },
        "stop_reason": _stop_reason(probe, coincidences, pairs, core.infer_group_calls),
    }


def _stop_reason(
    probe: _Probe,
    coincidences: list[Any],
    pairs: list[dict[str, Any]],
    infer_group_calls: int,
) -> str:
    """Name the first stage with no observable output (the diagnostic answer)."""
    if probe.readings_fed == 0:
        return "P1/no readings fed: no ct_samples in the selected window"
    if not probe.step_events:
        return "P3/no detector steps: readings never crossed the step threshold (check sigma warm-up)"
    if not coincidences:
        return "P3/no multi-reader coincidences within 10 s: steps never co-occurred"
    if probe.group_deliveries == 0:
        return "P5/no groups delivered to on_group_ready: co-steps never formed a group"
    if infer_group_calls == 0:
        return "event loop/infer_group never ran for a ready group"
    if not pairs:
        return "T5/ledger empty: no (parent, child) pair received a delta observation"
    if not any(pair["support"] > 0 for pair in pairs):
        return "T5/no support on any pair: co-steps did not agree within 3 sigma"
    if not any(pair["lifecycle"] == "provisional" for pair in pairs):
        return "T5/no PROVISIONAL edge: support or independent causes below threshold"
    return "pipeline reached PROVISIONAL edges; stop is later (confirmation/H/P5 publish)"


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def format_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("WattMosaic offline replay pipeline diagnostic")
    lines.append("=" * 44)
    info = report["input"]
    lines.append(
        f"window: {info['since'] or '-'} .. {info['until'] or '-'} "
        f"({info['records']} records, origin {info['origin']}, speed {info['speed']})"
    )
    lines.append(f"STOP: {report['stop_reason']}")
    lines.append("")
    lines.append(f"readings fed:        {report['readings_fed']}")
    ha = report["ha_events"]
    lines.append(
        f"HA events:           {ha['emitted_by_source']} emitted, "
        f"{ha['filtered_out']} filtered, {ha['delivered']} delivered"
    )
    det = report["detector"]
    lines.append(f"P3 step events:      {det['step_events']} {det['steps_per_reader']}")
    lines.append(f"P3 coincidences:     {det['multi_reader_coincidences']} (within 10 s)")
    win = report["windows"]
    lines.append(f"P4 windows:          {win['opened']} opened, {win['finalized']} finalized")
    groups = report["groups"]
    lines.append(f"P5 groups:           {groups['by_status']}")
    lines.append(
        f"P5 multi-reader:     {groups['with_at_least_two_reader_deltas']} groups "
        f"(>= 2 reader deltas)"
    )
    lines.append(f"on_group_ready:      {groups['delivered_to_on_group_ready']} deliveries")
    edges = report["edges"]
    lines.append(f"infer_group calls:   {edges['infer_group_calls']}")
    lines.append(f"snapshot estimates:  {edges['snapshot_estimate_counts']}")
    lines.append(f"proposed lifecycles: {edges['proposed_by_lifecycle']}")
    t5 = report["t5"]
    lines.append("")
    lines.append(f"T5 ledger folds:     {t5['ledger_folds']}")
    lines.append(f"T5 thresholds:       {t5['thresholds']}")
    lines.append("T5 (parent, child) pairs, top 10 by support:")
    for pair in t5["top_pairs"]:
        short = "; ".join(pair["short_of"]) or "meets PROVISIONAL"
        lines.append(
            f"  {pair['parent']:>16} -> {pair['child']:<16} "
            f"support={pair['support']:<3} conflict={pair['conflict']:<3} "
            f"indep={pair['independent_causes']:<3} lifecycle={pair['lifecycle']:<12} "
            f"short_of: {short}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path, help="copy of a live wattmosaic.db (never modified)")
    parser.add_argument("--since", help="only replay observations at/after this ISO time")
    parser.add_argument("--until", help="only replay observations at/before this ISO time")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="replay speed multiplier (informational: the injected clock never sleeps)",
    )
    parser.add_argument("--json-out", type=Path, help="write the JSON report here")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    source = args.db.resolve()
    if not source.exists():
        parser.error(f"database not found: {source}")
    since = _parse_time(args.since)
    until = _parse_time(args.until)

    with tempfile.TemporaryDirectory(prefix="wm-replay-") as tmp:
        work = Path(tmp) / "wattmosaic.db"
        # The engine works on a fresh DB with only the declarations copied over;
        # the source is opened strictly read-only and is never modified.
        _prepare_work_db(source, work)
        report = replay(work, observation_source=source, since=since, until=until, speed=args.speed)

    payload = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.json_out:
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual tool
    raise SystemExit(main())
