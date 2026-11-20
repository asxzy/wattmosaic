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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ha_client import HAClient  # noqa: E402

Status = Literal["ok", "short", "missing"]


@dataclass(frozen=True, slots=True)
class EntityCoverage:
    entity_id: str
    rows: int
    first: datetime | None
    last: datetime | None
    max_gap_s: float | None
    status: Status


def _numeric_state(row: Mapping[str, Any]) -> float | None:
    """Finite numeric value of a compressed history row's state, or ``None``.

    Non-numeric ``unavailable``/``unknown``/empty states and non-finite values
    (``nan``, ``inf``) are not observations and must never be counted as rows.
    """
    if "s" not in row:
        return None
    try:
        value = float(str(row["s"]).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_ts(row: Mapping[str, Any]) -> datetime | None:
    """Aware UTC timestamp of a compressed history row, or ``None``.

    HA's minimal history uses ``lu`` (last_updated) and, when that is absent,
    ``lc`` (last_changed).  Both are epoch seconds (float).
    """
    if _numeric_state(row) is None:
        return None
    raw_ts = row.get("lu") if row.get("lu") is not None else row.get("lc")
    try:
        ts = float(raw_ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, UTC)


def _is_non_numeric(row: Mapping[str, Any]) -> bool:
    """Whether a state row carried a non-numeric (or non-finite) value."""
    return "s" in row and _numeric_state(row) is None


def non_numeric_counts(
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
    entity_ids: Sequence[str],
) -> dict[str, int]:
    """Count skipped non-numeric state rows per entity (pure counterpart)."""
    return {
        eid: sum(1 for row in payload.get(eid) or [] if _is_non_numeric(row))
        for eid in entity_ids
    }


def _summarize(
    entity_id: str,
    times: Sequence[datetime],
    *,
    start: datetime,
    end: datetime,
    short_tolerance: timedelta,
) -> EntityCoverage | None:
    """One shared rule for the pure report and the streaming fold.

    Sort, restrict to ``[start, end]``, take gaps between consecutive rows (a
    gap counts when ``>= 0``, so duplicate timestamps contribute ``0.0``) plus
    the tail gap to ``end``.  With any row, ``max_gap_s`` is a float, possibly
    ``0.0``; it never changes the status.
    """
    ordered = sorted(ts for ts in times if start <= ts <= end)
    if not ordered:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False)]
    gaps.append((end - ordered[-1]).total_seconds())
    gaps = [gap for gap in gaps if gap >= 0]
    status: Status = "short" if ordered[0] > start + short_tolerance else "ok"
    return EntityCoverage(
        entity_id,
        len(ordered),
        ordered[0],
        ordered[-1],
        max(gaps, default=0.0),
        status,
    )


def coverage_report(
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
    entity_ids: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    short_tolerance: timedelta = timedelta(hours=1),
) -> list[EntityCoverage]:
    """Classify recorder coverage for each requested entity (pure function)."""
    out: list[EntityCoverage] = []
    for eid in entity_ids:
        times = [ts for ts in (_parse_ts(row) for row in payload.get(eid) or []) if ts is not None]
        summary = _summarize(
            eid, times, start=start, end=end, short_tolerance=short_tolerance
        )
        out.append(
            summary
            if summary is not None
            else EntityCoverage(eid, 0, None, None, None, "missing")
        )
    return out


@dataclass(slots=True)
class _Accumulator:
    """Running fold state: only timestamps and counts, never the values."""

    entity_id: str
    times: list[datetime] = field(default_factory=list)
    non_numeric: int = 0

    def add_chunk(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            if _is_non_numeric(row):
                self.non_numeric += 1
            ts = _parse_ts(row)
            if ts is not None:
                self.times.append(ts)

    def finish(
        self,
        *,
        start: datetime,
        end: datetime,
        short_tolerance: timedelta = timedelta(hours=1),
    ) -> EntityCoverage:
        summary = _summarize(
            self.entity_id,
            self.times,
            start=start,
            end=end,
            short_tolerance=short_tolerance,
        )
        return (
            summary
            if summary is not None
            else EntityCoverage(self.entity_id, 0, None, None, None, "missing")
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

    The start-time state is requested **only for the first chunk**.  HA's
    ``history_during_period`` defaults to ``include_start_time_state=True`` and
    then prepends a synthetic row at ``start_time`` to every per-entity result;
    doing that for every hourly chunk would cap ``max_gap_s`` at about one hour
    and hide an entity that stopped recording mid-window.
    """
    if accumulators is None:
        accumulators = {eid: _Accumulator(eid) for eid in entity_ids}
    async with HAClient.from_env() as client:
        cursor = start
        while cursor < end:
            stop = min(cursor + chunk, end)
            payload = await client.ws_command(
                "history/history_during_period",
                start_time=cursor.isoformat(),
                end_time=stop.isoformat(),
                entity_ids=list(entity_ids),
                minimal_response=True,
                no_attributes=True,
                significant_changes_only=False,
                include_start_time_state=(cursor == start),
            )
            fold_chunk(accumulators, payload)
            cursor = stop
    return dict(accumulators)


def entity_ids_from_topology_db(db_path: Path) -> list[str]:
    """Read CT entity ids from the latest topology version, read-only.

    Mirrors the query pattern in ``replay_capture.py``: ``channels[].entity_id``
    where ``ct_id`` is set.  The database is opened ``mode=ro`` and never
    written.  Corrupt JSON propagates so the CLI can report it clearly.
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

    if not math.isfinite(args.days) or args.days <= 0:
        parser.error("--days must be a positive number")
    try:
        end = _parse_end(args.end)
    except ValueError:
        parser.error(f"invalid --end {args.end!r}: expected an ISO-8601 timestamp")

    if args.db is not None and not args.db.exists():
        print(f"check_recorder_coverage: database file not found: {args.db}", file=sys.stderr)
        return 2
    try:
        entity_ids = (
            list(args.entities)
            if args.entities is not None
            else entity_ids_from_topology_db(args.db)
        )
    except (ValueError, sqlite3.Error) as exc:
        print(
            f"check_recorder_coverage: cannot read CT entities from {args.db}: {exc}",
            file=sys.stderr,
        )
        return 2
    if not entity_ids:
        if args.json:
            print(json.dumps({"error": "no CT entities found"}))
        else:
            print("check_recorder_coverage: no CT entities found", file=sys.stderr)
        return 2

    start = end - timedelta(days=args.days)
    accumulators = {eid: _Accumulator(eid) for eid in entity_ids}
    try:
        asyncio.run(
            stream_coverage(entity_ids, start=start, end=end, accumulators=accumulators)
        )
    except (RuntimeError, OSError, aiohttp.ClientError, TimeoutError) as exc:
        print(
            f"check_recorder_coverage: Home Assistant history request failed: {exc}",
            file=sys.stderr,
        )
        return 2

    report = [acc.finish(start=start, end=end) for acc in accumulators.values()]
    non_numeric = {eid: acc.non_numeric for eid, acc in accumulators.items()}
    counts = {
        status: sum(1 for c in report if c.status == status)
        for status in ("ok", "short", "missing")
    }
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
                            "non_numeric": non_numeric.get(c.entity_id, 0),
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
        print(
            f"{'entity':40s} {'rows':>7s} {'nonnum':>7s} {'status':>8s}"
            "  first / last / max_gap_s"
        )
        for c in report:
            first = c.first.isoformat() if c.first else "-"
            last = c.last.isoformat() if c.last else "-"
            gap = f"{c.max_gap_s:.0f}" if c.max_gap_s is not None else "-"
            print(
                f"{c.entity_id:40s} {c.rows:7d} {non_numeric.get(c.entity_id, 0):7d} "
                f"{c.status:>8s}  {first} / {last} / {gap}"
            )
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
