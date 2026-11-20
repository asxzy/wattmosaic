"""Check whether Home Assistant's recorder holds CT entities (read-only).

A production install keeps history in HA's recorder, not in the engine's
``ct_samples`` table (ADR-0042).  This probe answers one question before anyone
relies on that history: for each CT entity, do the last N days actually exist in
the recorder?  Entities excluded from the recorder, or a recorder whose
``purge_keep_days`` is shorter than the window, are reported explicitly rather
than silently appearing as an empty replay.

It talks to HA only through the read-only :mod:`ha_client` layer and issues only
``history/history_during_period`` queries (a read command).  No service call is
possible from this tool.

Entity ids come from the latest ``topology_versions`` payload in a database copy
(``channels[].entity_id`` where ``ct_id`` is set), or from ``--entities``.

Usage::

    .venv/bin/python tools/audit/check_recorder_coverage.py --db data/wattmosaic.db --days 10
    .venv/bin/python tools/audit/check_recorder_coverage.py --entities sensor.ct1_power sensor.ct2_power
    .venv/bin/python tools/audit/check_recorder_coverage.py --db data/wattmosaic.db --days 10 --json

Exit status is 1 if any entity is ``missing``, else 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ha_client import HAClient, fetch_history  # noqa: E402

Status = Literal["ok", "short", "missing"]


@dataclass(frozen=True, slots=True)
class EntityCoverage:
    entity_id: str
    rows: int
    first: datetime | None
    last: datetime | None
    max_gap_s: float | None
    status: Status


def _parse_ts(row: Mapping[str, Any]) -> datetime | None:
    """Return an aware UTC timestamp from a compressed history row, or ``None``.

    HA's minimal history uses ``lu`` (last_updated) and, when that is absent,
    ``lc`` (last_changed).  Both are epoch seconds (float).  Non-numeric states
    and malformed rows are treated as no observation.
    """
    try:
        state = row.get("s")
        if state is None or str(state).strip() == "":
            return None
        float(str(state))  # validate numeric state; value itself is not kept
        raw_ts = row.get("lu") if row.get("lu") is not None else row.get("lc")
        ts = float(raw_ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, UTC)


def _max_gap(times: Sequence[datetime], end: datetime) -> float | None:
    """Largest gap between consecutive timestamps, including last -> ``end``."""
    if not times:
        return None
    ordered = sorted(times)
    gaps = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False)]
    gaps.append((end - ordered[-1]).total_seconds())
    return max(gaps, default=0.0)


def coverage_report(
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
    entity_ids: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    short_tolerance: timedelta = timedelta(hours=1),
) -> list[EntityCoverage]:
    """Classify recorder coverage for each requested entity.

    ``missing``: no numeric rows.  ``short``: the first row is later than
    ``start + short_tolerance``.  ``ok``: otherwise.  ``max_gap_s`` is reported
    but never changes the status.
    """
    out: list[EntityCoverage] = []
    for eid in entity_ids:
        times = [ts for ts in (_parse_ts(row) for row in payload.get(eid) or []) if ts is not None]
        if not times:
            out.append(EntityCoverage(eid, 0, None, None, None, "missing"))
            continue
        first = min(times)
        last = max(times)
        status: Status = "short" if first > start + short_tolerance else "ok"
        out.append(EntityCoverage(eid, len(times), first, last, _max_gap(times, end), status))
    return out


@dataclass(slots=True)
class _Accumulator:
    """Running fold state for one entity (only counts/timestamps are kept)."""

    entity_id: str
    rows: int = 0
    first: datetime | None = None
    last: datetime | None = None
    previous: datetime | None = None
    max_gap_s: float | None = None

    def add_chunk(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            ts = _parse_ts(row)
            if ts is None:
                continue
            self.rows += 1
            if self.first is None or ts < self.first:
                self.first = ts
            if self.last is None or ts > self.last:
                self.last = ts
            if self.previous is not None:
                gap = (ts - self.previous).total_seconds()
                if gap > 0 and (self.max_gap_s is None or gap > self.max_gap_s):
                    self.max_gap_s = gap
            self.previous = ts

    def finish(
        self,
        *,
        start: datetime,
        end: datetime,
        short_tolerance: timedelta = timedelta(hours=1),
    ) -> EntityCoverage:
        if self.rows == 0 or self.first is None:
            return EntityCoverage(self.entity_id, 0, None, None, None, "missing")
        # Include the gap from the last row to the end of the window.
        if self.last is not None:
            tail = (end - self.last).total_seconds()
            if tail > 0 and (self.max_gap_s is None or tail > self.max_gap_s):
                self.max_gap_s = tail
        status: Status = "short" if self.first > start + short_tolerance else "ok"
        return EntityCoverage(
            self.entity_id,
            self.rows,
            self.first,
            self.last,
            self.max_gap_s,
            status,
        )


def fold_chunk(
    accumulators: Mapping[str, _Accumulator],
    chunk: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    """Fold one fetched chunk into the running per-entity accumulators."""
    for eid, acc in accumulators.items():
        acc.add_chunk(chunk.get(eid) or [])


async def stream_coverage(
    entity_ids: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    accumulators: Mapping[str, _Accumulator] | None = None,
    chunk: timedelta = timedelta(hours=1),
) -> dict[str, _Accumulator]:
    """Fetch the window in 1-hour chunks and fold rows into running counters.

    Only a client's read-only ``fetch_history`` is used.  Fetching per chunk
    keeps a 10-day window at 1 Hz from producing one huge response; only counts
    and timestamps are retained.
    """
    if accumulators is None:
        accumulators = {eid: _Accumulator(eid) for eid in entity_ids}
    async with HAClient.from_env() as client:
        cursor = start
        while cursor < end:
            stop = min(cursor + chunk, end)
            payload = await fetch_history(
                client,
                list(entity_ids),
                start=cursor.isoformat(),
                end=stop.isoformat(),
                minimal=True,
            )
            fold_chunk(accumulators, payload)
            cursor = stop
    return dict(accumulators)


def entity_ids_from_topology_db(db_path: Path) -> list[str]:
    """Read CT entity ids from the latest topology version, read-only.

    Mirrors the query pattern in ``replay_capture.py``: ``channels[].entity_id``
    where ``ct_id`` is set.  The database is opened ``mode=ro`` and never
    written.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload_json FROM topology_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    if row is None:
        return []
    payload = json.loads(row[0])
    return [
        str(ch["entity_id"])
        for ch in payload.get("channels") or []
        if ch.get("ct_id") and ch.get("entity_id")
    ]


def _parse_end(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--db", type=Path, default=None, help="database copy for CT entity ids")
    group.add_argument("--entities", nargs="+", default=None, help="explicit CT entity ids")
    parser.add_argument("--days", type=float, default=10.0)
    parser.add_argument("--end", default=None, help="ISO-8601 end time (default: now)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    entity_ids = (
        list(args.entities)
        if args.entities is not None
        else entity_ids_from_topology_db(args.db)
    )
    if not entity_ids:
        if args.json:
            print(json.dumps({"error": "no CT entities found"}))
        else:
            print("check_recorder_coverage: no CT entities found", file=sys.stderr)
        return 2

    end = _parse_end(args.end)
    start = end - timedelta(days=args.days)
    accumulators = {eid: _Accumulator(eid) for eid in entity_ids}
    asyncio.run(
        stream_coverage(entity_ids, start=start, end=end, accumulators=accumulators)
    )
    report = [
        acc.finish(start=start, end=end) for acc in accumulators.values()
    ]

    counts = {status: sum(1 for c in report if c.status == status) for status in ("ok", "short", "missing")}
    if args.json:
        print(
            json.dumps(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "entities": [
                        {
                            "entity_id": c.entity_id,
                            "rows": c.rows,
                            "first": c.first.isoformat() if c.first else None,
                            "last": c.last.isoformat() if c.last else None,
                            "max_gap_s": c.max_gap_s,
                            "status": c.status,
                        }
                        for c in report
                    ],
                    "summary": counts,
                },
                indent=2,
            )
        )
    else:
        print(f"{'entity':40s} {'rows':>7s} {'status':>8s}  first / last / max_gap_s")
        for c in report:
            first = c.first.isoformat() if c.first else "-"
            last = c.last.isoformat() if c.last else "-"
            gap = f"{c.max_gap_s:.0f}" if c.max_gap_s is not None else "-"
            print(f"{c.entity_id:40s} {c.rows:7d} {c.status:>8s}  {first} / {last} / {gap}")
        print(
            f"{counts['ok']} ok · {counts['short']} short · {counts['missing']} missing"
        )

    missing = [c.entity_id for c in report if c.status == "missing"]
    if missing:
        print(
            "hint: entities missing from the recorder may be excluded from the recorder "
            "or have purge_keep_days too short; set raw_capture_days to capture locally.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
