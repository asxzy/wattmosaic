"""Pull Home Assistant recorder history to disk for ground-truth replay (read-only).

The on-disk format is the shared contract in
``docs/specs/ha-history-replay/README.md`` section 1: a ``manifest.json`` plus
gzip-compressed JSONL files of numeric samples and state events, one file per
UTC day, and a ``factors.json`` derived from voltage/current/power-factor rows.

This tool never writes to Home Assistant.  It only uses the read-only
WebSocket command ``history/history_during_period`` and the REST ``GET
/api/states`` exposed by :mod:`tools.audit.ha_client`, and it refuses to write
outside ``<repo>/data/raw/`` unless ``--force-out`` is given so pulled data
cannot land in a tracked directory.  It never prints the token or state values.

Usage::

    .venv/bin/python tools/dev/ha_history_pull.py \\
        --readers data/raw/ground_truth/topology_gt.json \\
        [--days 10 | --start ISO --end ISO] \\
        [--events auto | --events-file FILE | --no-events] \\
        [--out data/raw/ha_history] [--refresh] [--derive-factors-only]
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))

from tools.audit.ha_client import HAClient, connection_settings, load_env  # noqa: E402

SCHEMA_HISTORY = "wattmosaic.ha_history/1"
SCHEMA_FACTORS = "wattmosaic.measurement_factors/1"

#: HA domains considered load drivers for ``--events auto``.  ``is_event_domain``
#: admits a wider set (enum sensors, vacuum, ...); the spec narrows it here.
EVENT_DOMAINS = frozenset(
    {
        "switch",
        "climate",
        "humidifier",
        "fan",
        "water_heater",
        "binary_sensor",
        "light",
        "input_boolean",
        "media_player",
    }
)
MAX_EVENT_ENTITIES = 400
MAX_BATCH = 50
FACTOR_SNAP_CANDIDATES = (1.0, 2.0)
FACTOR_SNAP_TOLERANCE = 0.15
FACTOR_MAX_AGE_S = 30.0
FACTOR_MIN_CURRENT_A = 0.5
FACTOR_MIN_PF = 0.3
FACTOR_MIN_POWER_W = 50.0
FACTOR_BUCKET_SECONDS = 3600
FACTOR_MIN_INSTANTS_PER_BUCKET = 5
FACTOR_CHANGE_BUCKETS = 2


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def parse_numeric(raw: object) -> float | None:
    """Finite float from an HA state value, or ``None``.

    Non-numeric states (``unavailable``, ``unknown``, ``""``), booleans and
    non-finite numbers are dropped, never coerced to 0 (HB §18).  Numeric
    strings become floats.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_ts(raw: object) -> datetime | None:
    """Parse a recorder timestamp (ISO-8601 string or epoch seconds) to UTC."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        return datetime.fromtimestamp(value, UTC) if math.isfinite(value) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    """UTC ISO-8601 string for ``dt``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def day_key(dt: datetime) -> str:
    """UTC ``YYYY-MM-DD`` key of ``dt``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).date().isoformat()


def day_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Inclusive ``[start, end)`` UTC bounds of the day containing ``dt``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def chunk_days(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """Split ``[start, end)`` into UTC-day ``(key, day_start, day_end)`` chunks."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end < start:
        raise ValueError("end must not be before start")
    chunks: list[tuple[str, datetime, datetime]] = []
    cursor = start
    while cursor < end:
        day_start, next_day = day_bounds(cursor)
        stop = min(next_day, end)
        chunks.append((day_key(day_start), day_start, stop))
        cursor = stop
    return chunks


def batches(items: Sequence[str], size: int = MAX_BATCH) -> list[list[str]]:
    """Split entity ids into batches of at most ``size``."""
    if size < 1:
        raise ValueError("batch size must be positive")
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def resolve_out(out: str | Path, *, force: bool = False) -> Path:
    """Resolve ``out`` and refuse paths outside ``<repo>/data/raw/``.

    Relative paths are interpreted relative to the repository root (the CLI is
    expected to be invoked from anywhere).  An absolute path is accepted only
    when it is under ``data/raw`` or ``--force-out`` is given.
    """
    path = Path(out).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    raw_root = (ROOT / "data" / "raw").resolve()
    if not force and path != raw_root and raw_root not in path.parents:
        raise ValueError(
            f"--out {path} is outside {raw_root}; pass --force-out to override"
        )
    return path


def row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    """Timestamp of a compressed HA history row (``lu`` then ``lc``)."""
    raw = row.get("lu")
    if raw is None:
        raw = row.get("lc")
    return parse_ts(raw)


def row_state(row: Mapping[str, Any]) -> object:
    """State of a compressed history row; full rows use ``state``."""
    if "s" in row:
        return row["s"]
    return row.get("state")


def sample_rows_from_history(
    payload: Mapping[str, Any] | None, entity_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Flatten a history payload into sorted ``{entity_id, t, v}`` sample rows.

    Rows whose state is not numeric, or which carry no usable timestamp, are
    dropped (absent is ``None``, never 0).  Output is sorted by ``t``.
    """
    rows: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        for raw in (payload or {}).get(entity_id) or ():
            if not isinstance(raw, Mapping):
                continue
            value = parse_numeric(row_state(raw))
            ts = row_timestamp(raw)
            if value is None or ts is None:
                continue
            rows.append({"entity_id": entity_id, "t": iso(ts), "v": value})
    rows.sort(key=lambda row: (row["t"], row["entity_id"]))
    return rows


def event_rows_from_history(
    payload: Mapping[str, Any] | None, entity_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Flatten a full (attributes-bearing) history payload into event rows.

    Attributes are kept (climate ``hvac_action`` lives there).  Rows without a
    usable timestamp are dropped.  Output is sorted by ``t``.
    """
    rows: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        for raw in (payload or {}).get(entity_id) or ():
            if not isinstance(raw, Mapping):
                continue
            ts = row_timestamp(raw)
            if ts is None:
                continue
            attributes = raw.get("attributes")
            if not isinstance(attributes, dict):
                attributes = raw.get("a") if isinstance(raw.get("a"), dict) else {}
            rows.append(
                {
                    "entity_id": entity_id,
                    "t": iso(ts),
                    "state": str(raw.get("s", raw.get("state", ""))),
                    "attributes": attributes,
                }
            )
    rows.sort(key=lambda row: (row["t"], row["entity_id"]))
    return rows


def _default_is_event_domain(entity_id: str, attributes: Any) -> bool:
    """Production rule for event domains, imported lazily from the engine."""
    from wattmosaic_engine.ingestion.ha_source import is_event_domain  # noqa: PLC0415

    return bool(is_event_domain(entity_id, attributes))


def event_candidates(
    states: Sequence[Mapping[str, Any]],
    *,
    is_event_domain: Any,
) -> list[str]:
    """Load-driver entities from ``GET /api/states`` (those in EVENT_DOMAINS).

    WattMosaic's own entities (``sensor.wattmosaic_*`` and the ``wattmosaic``
    domain) are excluded.  ``is_event_domain`` is injected so the rule is reused
    from ``ingestion/ha_source.py`` in production while tests need no engine
    import.  Sorted for determinism; the caller ranks by change count.
    """
    candidates: set[str] = set()
    for state in states:
        entity_id = str(state.get("entity_id") or "")
        if not entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain == "wattmosaic" or entity_id.startswith("sensor.wattmosaic_"):
            continue
        if domain not in EVENT_DOMAINS:
            continue
        if not is_event_domain(entity_id, state.get("attributes")):
            continue
        candidates.add(entity_id)
    return sorted(candidates)


def select_event_entities(
    candidates: Sequence[str],
    change_counts: Mapping[str, int] | None = None,
    *,
    limit: int = MAX_EVENT_ENTITIES,
) -> list[str]:
    """Cap load-driver entities at ``limit``, most-changed in 24 h first.

    A stable secondary ordering keeps ties deterministic.  Entities without a
    change count sort last.
    """
    counts = change_counts or {}
    ordered = sorted(candidates, key=lambda eid: (-counts.get(eid, 0), eid))
    return ordered[:limit]


def load_readers(path: str | Path) -> list[dict[str, Any]]:
    """Load the ``readers`` list of a ground-truth file."""
    payload = json.loads(Path(path).read_text())
    readers = payload.get("readers")
    if not isinstance(readers, list):
        raise ValueError(f"{path}: expected a 'readers' list")
    return [reader for reader in readers if isinstance(reader, Mapping)]


def reader_sample_entities(readers: Sequence[Mapping[str, Any]]) -> list[str]:
    """All power entities and factor inputs, de-duplicated and sorted."""
    ids: set[str] = set()
    for reader in readers:
        power = reader.get("power_entity")
        if isinstance(power, str) and power:
            ids.add(power)
        for item in reader.get("factor_inputs") or ():
            if isinstance(item, str) and item:
                ids.add(item)
    return sorted(ids)


def factor_inputs(reader: Mapping[str, Any]) -> dict[str, str]:
    """Return the voltage/current/power-factor entity ids of a reader.

    factor_inputs are positional in the ground-truth file: voltage, current,
    power-factor.  Extra inputs are ignored.
    """
    inputs = list(reader.get("factor_inputs") or ())
    if len(inputs) < 3:
        return {}
    return {"v": inputs[0], "i": inputs[1], "pf": inputs[2]}


# --------------------------------------------------------------------------- #
# Factor derivation (section 2.3, pure)
# --------------------------------------------------------------------------- #
def _snap_factor(value: float) -> float | None:
    """Snap ``value`` to the nearest candidate within the tolerance, else None."""
    best = min(FACTOR_SNAP_CANDIDATES, key=lambda candidate: abs(value - candidate))
    if abs(value - best) <= FACTOR_SNAP_TOLERANCE * best:
        return best
    return None


def derive_factors(
    rows_by_entity: Mapping[str, Sequence[Mapping[str, Any]]],
    readers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive measurement factors from aligned P/V/I/PF samples.

    For every reader with all three factor inputs, align P, V, I and PF by
    holding the last value, keep only instants where all four are at most 30 s
    old and strong enough, snap the ratio to {1.0, 2.0}, take the per-hour mode
    and merge consecutive equal hours with a two-bucket hysteresis.  Readers
    with no qualifying bucket go to ``unknown`` (never guessed).
    """
    factors: dict[str, Any] = {}
    unknown: list[str] = []

    def series(entity_id: str) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        for raw in rows_by_entity.get(entity_id) or ():
            ts = parse_ts(raw.get("t"))
            value = parse_numeric(raw.get("v"))
            if ts is not None and value is not None:
                out.append((ts, value))
        out.sort(key=lambda pair: pair[0])
        return out

    for reader in readers:
        power_entity = reader.get("power_entity")
        inputs = factor_inputs(reader)
        if not isinstance(power_entity, str) or not power_entity or not inputs:
            continue

        p_series = series(power_entity)
        v_series = series(inputs["v"])
        i_series = series(inputs["i"])
        pf_series = series(inputs["pf"])
        if not p_series or not v_series or not i_series or not pf_series:
            unknown.append(power_entity)
            continue

        # Merge all instants, aligning each channel by holding its last value.
        instants = sorted(
            {ts for ts, _ in p_series}
            | {ts for ts, _ in v_series}
            | {ts for ts, _ in i_series}
            | {ts for ts, _ in pf_series}
        )
        last: dict[str, tuple[datetime, float] | None] = {
            "p": None,
            "v": None,
            "i": None,
            "pf": None,
        }
        channel_series = {
            "p": p_series,
            "v": v_series,
            "i": i_series,
            "pf": pf_series,
        }
        cursors = {name: 0 for name in channel_series}
        bucket_counts: dict[int, Counter[float]] = defaultdict(Counter)
        for ts in instants:
            for name, rows in channel_series.items():
                cursor = cursors[name]
                while cursor < len(rows) and rows[cursor][0] <= ts:
                    last[name] = rows[cursor]
                    cursor += 1
                cursors[name] = cursor
            held = [last[name] for name in ("p", "v", "i", "pf")]
            if any(value is None for value in held):
                continue
            p, v, i, pf = (value[1] for value in held if value is not None)
            if any((ts - value[0]).total_seconds() > FACTOR_MAX_AGE_S for value in held if value is not None):
                continue
            if abs(i) < FACTOR_MIN_CURRENT_A or abs(pf) < FACTOR_MIN_PF or abs(p) < FACTOR_MIN_POWER_W:
                continue
            if v <= 0:
                continue
            snapped = _snap_factor(abs(p) / (v * abs(i) * abs(pf)))
            if snapped is None:
                continue
            bucket = int(ts.timestamp()) // FACTOR_BUCKET_SECONDS
            bucket_counts[bucket][snapped] += 1

        bucket_factor: dict[int, float] = {}
        for bucket, counts in bucket_counts.items():
            if sum(counts.values()) < FACTOR_MIN_INSTANTS_PER_BUCKET:
                continue
            # Ties resolve to the smaller factor deterministically.
            bucket_factor[bucket] = min(counts, key=lambda value: (-counts[value], value))

        if not bucket_factor:
            unknown.append(power_entity)
            continue

        factors[power_entity] = _merge_buckets(bucket_factor)

    return {
        "schema": SCHEMA_FACTORS,
        "method": "P/(V*I*PF) snapped",
        "factors": factors,
        "unknown": sorted(set(unknown)),
    }


def _merge_buckets(bucket_factor: Mapping[int, float]) -> list[dict[str, Any]]:
    """Merge hourly factors into ``factors.json`` intervals with hysteresis.

    A change is adopted only after ``FACTOR_CHANGE_BUCKETS`` consecutive buckets
    with the new value; a single stray bucket never splits an interval.  Missing
    (no-value) buckets also do not split an interval.  Bucket indices are
    consecutive one-hour slots.  ``to`` is ``None`` for the final (open) interval.
    """
    if not bucket_factor:
        return []
    lo, hi = min(bucket_factor), max(bucket_factor)
    adopted: dict[int, float] = {}
    current: float | None = None
    pending: float | None = None
    pending_run = 0
    for bucket in range(lo, hi + 1):
        value = bucket_factor.get(bucket)
        if value is None:
            continue  # gap: hold the current value, do not split
        if current is None:
            current = value
            adopted[bucket] = current
            continue
        if value == current:
            pending, pending_run = None, 0
            adopted[bucket] = current
            continue
        if value == pending:
            pending_run += 1
        else:
            pending, pending_run = value, 1
        if pending_run >= FACTOR_CHANGE_BUCKETS:
            current = pending
            adopted[bucket] = current
            pending, pending_run = None, 0

    def bucket_start(bucket: int) -> datetime:
        return datetime.fromtimestamp(bucket * FACTOR_BUCKET_SECONDS, UTC)

    # Build runs of consecutive adopted buckets with an unchanged factor.  The
    # (small) gap buckets between runs are absorbed into the preceding interval,
    # since missing buckets do not split an interval.
    runs: list[tuple[int, int, float]] = []  # (first bucket, last bucket, factor)
    for bucket in sorted(adopted):
        factor = adopted[bucket]
        if runs and runs[-1][2] == factor and bucket == runs[-1][1] + 1:
            first, _, _ = runs[-1]
            runs[-1] = (first, bucket, factor)
        elif runs and runs[-1][2] == factor:
            # Gap without a value: extend the run, do not split.
            first, _, _ = runs[-1]
            runs[-1] = (first, bucket, factor)
        else:
            runs.append((bucket, bucket, factor))

    intervals: list[dict[str, Any]] = []
    for index, (first, last, factor) in enumerate(runs):
        end = bucket_start(runs[index + 1][0]) if index + 1 < len(runs) else None
        intervals.append(
            {
                "from": iso(bucket_start(first)),
                "to": iso(end) if end is not None else None,
                "factor": factor,
                "samples": sum(bucket_factor[b] for b in bucket_factor if first <= b <= last),
            }
        )
    return intervals


# --------------------------------------------------------------------------- #
# Fetching (section 2.2)
# --------------------------------------------------------------------------- #
async def fetch_history_events(
    client: HAClient,
    entity_ids: list[str],
    *,
    start: str,
    end: str,
    include_start_time_state: bool = True,
) -> dict[str, Any]:
    """Full-history (attributes-bearing) read for event entities.

    This is deliberately a separate helper so ``ha_client.fetch_history`` keeps
    its sample-oriented defaults (``minimal=True``, ``no_attributes=True``,
    ``significant_changes_only=False``).  The command is still the read-only
    ``history/history_during_period``.
    """
    return await client.ws_command(
        "history/history_during_period",
        start_time=start,
        end_time=end,
        entity_ids=list(entity_ids),
        minimal_response=False,
        no_attributes=False,
        significant_changes_only=False,
        include_start_time_state=include_start_time_state,
    )


def _history_request(
    client: HAClient,
    entity_ids: list[str],
    *,
    start: datetime,
    end: datetime,
    first_chunk: bool,
    is_event: bool,
) -> Any:
    """Issue one recorder query for a batch of entities and a time window."""
    from tools.audit.ha_client import fetch_history  # noqa: PLC0415

    if is_event:
        return fetch_history_events(
            client,
            entity_ids,
            start=iso(start),
            end=iso(end),
            include_start_time_state=first_chunk,
        )
    return fetch_history(
        client,
        entity_ids,
        start=iso(start),
        end=iso(end),
        minimal=True,
        include_start_time_state=first_chunk,
    )


async def fetch_window(
    client: HAClient,
    entity_ids: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    is_event: bool,
    first_chunk: bool = True,
) -> list[dict[str, Any]]:
    """Fetch all entities over one UTC-day window, batching at 50 entities.

    ``include_start_time_state`` is True only for the first batch of the first
    day so a synthetic state is not repeated at every chunk boundary.
    """
    rows: list[dict[str, Any]] = []
    for index, batch in enumerate(batches(list(entity_ids))):
        payload = await _history_request(
            client,
            batch,
            start=start,
            end=end,
            first_chunk=(first_chunk and index == 0),
            is_event=is_event,
        )
        if is_event:
            rows.extend(event_rows_from_history(payload, batch))
        else:
            rows.extend(sample_rows_from_history(payload, batch))
    return rows


def _write_jsonl_gz(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Write sorted JSONL rows to a gzip file, one JSON object per line."""
    ordered = sorted(rows, key=lambda row: (row["t"], row.get("entity_id", "")))
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in ordered:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _load_manifest(out: Path) -> dict[str, Any]:
    path = out / "manifest.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _atomic_write(path: Path, text: str) -> None:
    """Write text atomically so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_manifest(out: Path, manifest: dict[str, Any]) -> None:
    _atomic_write(out / "manifest.json", json.dumps(manifest, indent=2) + "\n")


def _derive_and_write_factors(out: Path, readers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rebuild ``factors.json`` from the sample files already on disk."""
    rows_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(out.glob("samples-*.jsonl.gz")):
        for row in _read_jsonl_gz(path):
            rows_by_entity[str(row.get("entity_id"))].append(row)
    factors = derive_factors(rows_by_entity, readers)
    _atomic_write(out / "factors.json", json.dumps(factors, indent=2) + "\n")
    return factors


def _env_with_fallback() -> dict[str, str]:
    """Env for ``connection_settings``: checkout ``.env``, then main checkout.

    When run from a linked worktree the credentials live in the main checkout's
    ``.env``; ``git rev-parse --git-common-dir`` points back to it.  Values are
    merged lowest-priority first: main checkout, worktree, process environment.
    No secret is ever printed or logged.
    """
    import subprocess  # noqa: PLC0415

    env: dict[str, str] = {}
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        main_root = Path(common).resolve().parent
        if main_root != ROOT:
            env.update(load_env(main_root / ".env"))
    except (OSError, subprocess.SubprocessError):
        pass
    env.update(load_env(ROOT / ".env"))
    env.update(os.environ)
    return env


def _client_from_env() -> HAClient:
    """Build a read-only client from checkout ``.env`` (main checkout fallback)."""
    return HAClient(*connection_settings(_env_with_fallback()))


async def pull(
    args: argparse.Namespace,
    *,
    client: HAClient | None = None,
    states: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the pull and return the manifest written to disk.

    ``client``, ``states`` and ``now`` may be injected by tests so no network or
    wall clock is needed.  With ``client`` given, the same instance is used for
    ``GET /api/states`` and all history queries.
    """
    out = resolve_out(args.out, force=args.force_out)
    out.mkdir(parents=True, exist_ok=True)
    readers = load_readers(args.readers)
    sample_entities = reader_sample_entities(readers)

    manifest = _load_manifest(out)
    if manifest.get("schema") != SCHEMA_HISTORY:
        manifest = {
            "schema": SCHEMA_HISTORY,
            "start": None,
            "end": None,
            "days": {},
            "sample_entities": sample_entities,
            "event_entities": [],
            "earliest_row": None,
            "pulled_at": None,
        }
    manifest["sample_entities"] = sample_entities

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if args.start:
        start = parse_ts(args.start)
        if start is None:
            raise ValueError(f"invalid --start: {args.start!r}")
    else:
        start = now - timedelta(days=args.days)
    if args.end:
        end = parse_ts(args.end)
        if end is None:
            raise ValueError(f"invalid --end: {args.end!r}")
    else:
        end = now
    chunks = chunk_days(start, end)
    manifest["start"], manifest["end"] = iso(start), iso(end)

    event_entities: list[str] = []
    change_counts: dict[str, int] = {}
    events_mode = args.events
    if getattr(args, "no_events", False):
        events_mode = "none"
    if getattr(args, "events_file", None):
        events_mode = "file"
    if events_mode == "file":
        event_entities = sorted(
            {line.strip() for line in Path(args.events_file).read_text().splitlines() if line.strip()}
        )
    elif args.events == "auto":
        if states is None:
            if client is not None:
                states = await client.states()
            else:
                async with _client_from_env() as own_client:
                    states = await own_client.states()
        candidates = event_candidates(states, is_event_domain=_default_is_event_domain)
        for state in states:
            eid = str(state.get("entity_id") or "")
            count = state.get("change_count_last_24h")
            if eid in candidates and isinstance(count, (int, float)) and not isinstance(count, bool):
                change_counts[eid] = int(count)
        event_entities = select_event_entities(candidates, change_counts)
    manifest["event_entities"] = event_entities

    earliest: datetime | None = parse_ts(manifest.get("earliest_row")) if manifest.get("earliest_row") else None

    async def run(pull_client: HAClient) -> None:
        first_chunk = True
        nonlocal earliest
        for key, day_start, day_end in chunks:
            # The current UTC day is always partial: its rows can still grow.
            partial = day_start <= now < day_start + timedelta(days=1)
            day = manifest["days"].get(key, {})
            if day.get("complete") and not args.refresh:
                print(f"{key}: skipped (complete)", flush=True)
                continue
            t0 = time.monotonic()
            sample_file = out / f"samples-{key}.jsonl.gz"
            event_file = out / f"events-{key}.jsonl.gz"
            sample_rows: list[dict[str, Any]] = []
            event_rows: list[dict[str, Any]] = []
            if sample_entities:
                sample_rows = await fetch_window(
                    pull_client,
                    sample_entities,
                    start=day_start,
                    end=day_end,
                    is_event=False,
                    first_chunk=first_chunk,
                )
            if event_entities:
                event_rows = await fetch_window(
                    pull_client,
                    event_entities,
                    start=day_start,
                    end=day_end,
                    is_event=True,
                    first_chunk=first_chunk,
                )
            first_chunk = False
            _write_jsonl_gz(sample_file, sample_rows)
            _write_jsonl_gz(event_file, event_rows)
            for row in (*sample_rows, *event_rows):
                ts = parse_ts(row["t"])
                if ts is not None and (earliest is None or ts < earliest):
                    earliest = ts
            elapsed = time.monotonic() - t0
            manifest["days"][key] = {
                "complete": not partial,
                "samples": len(sample_rows),
                "events": len(event_rows),
            }
            print(
                f"{key}: {len(sample_rows)} samples, {len(event_rows)} events "
                f"in {elapsed:.1f}s",
                flush=True,
            )
            manifest["earliest_row"] = iso(earliest) if earliest else None
            manifest["pulled_at"] = iso(now)
            _write_manifest(out, manifest)

    if client is not None:
        await run(client)
    else:
        async with _client_from_env() as own_client:
            await run(own_client)

    if earliest is not None:
        manifest["earliest_row"] = iso(earliest)
    manifest["pulled_at"] = iso(now)
    _write_manifest(out, manifest)
    _derive_and_write_factors(out, readers)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--readers", required=True, help="JSON file with a readers list")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=float, default=10.0, help="days of history (default 10)")
    window.add_argument("--start", help="window start (ISO-8601); use with --end")
    parser.add_argument("--end", help="window end (ISO-8601); default now")
    parser.add_argument(
        "--events",
        choices=("auto", "file", "none"),
        default="auto",
        help="event entity selection (default auto)",
    )
    parser.add_argument("--events-file", help="newline-separated entity ids (implies --events file)")
    parser.add_argument("--no-events", action="store_true", help="pull no event entities (implies --events none)")
    parser.add_argument("--out", default="data/raw/ha_history", help="output directory")
    parser.add_argument("--refresh", action="store_true", help="re-fetch complete days")
    parser.add_argument("--force-out", action="store_true", help="allow --out outside data/raw")
    parser.add_argument(
        "--derive-factors-only",
        action="store_true",
        help="recompute factors.json from existing files without contacting HA",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    events_mode = args.events
    if getattr(args, "no_events", False):
        events_mode = "none"
    if events_mode == "file" and not args.events_file:
        raise SystemExit("--events file requires --events-file (or pass --events-file directly)")
    out = resolve_out(args.out, force=args.force_out)
    out.mkdir(parents=True, exist_ok=True)
    readers = load_readers(args.readers)
    if args.derive_factors_only:
        factors = _derive_and_write_factors(out, readers)
        print(
            f"factors derived: {len(factors['factors'])} readers, "
            f"{len(factors['unknown'])} unknown",
            flush=True,
        )
        return 0
    asyncio.run(pull(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
