"""Live factor inference from monitor companion quantities (ADR-0044, E5 §3).

A monitor-applied multiplier is invisible to the engine because HA publishes no
factor entity: the published power already includes it.  When a reader declares
voltage/current/power-factor companions, ``f = |P| / (V·|I|·|PF|)`` recovers the
multiplier exactly.  This module is the single implementation of that algorithm.
``tools/dev/ha_history_pull.py`` imports it for offline factor derivation, and
the engine's background job (:func:`infer_factors_job`) uses it live.

The algorithm (all series aligned by holding the last value, no per-fold cost):

* qualify instants with ``|I| ≥ 0.5 A``, ``|PF| ≥ 0.3``, ``|P| ≥ 50 W`` and
  companion values no more than 30 s older than the power sample;
* snap the ratio to ``{1, 2}`` within 15 %;
* take the per-hour mode (at least 5 qualifying instants per hour);
* require 2 consecutive hours of agreement before a change is adopted.

Readers without qualifying data are returned as ``unknown``, never guessed
(HB §18).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "FACTOR_BUCKET_SECONDS",
    "FACTOR_CHANGE_BUCKETS",
    "FACTOR_MAX_AGE_S",
    "FACTOR_MIN_CURRENT_A",
    "FACTOR_MIN_INSTANTS_PER_BUCKET",
    "FACTOR_MIN_POWER_W",
    "FACTOR_MIN_PF",
    "FACTOR_SNAP_CANDIDATES",
    "FACTOR_SNAP_TOLERANCE",
    "derive_factors",
    "infer_factors_job",
    "merge_buckets",
    "parse_numeric",
    "parse_ts",
    "snap_factor",
]

FACTOR_SNAP_CANDIDATES = (1.0, 2.0)
FACTOR_SNAP_TOLERANCE = 0.15
FACTOR_MAX_AGE_S = 30.0
FACTOR_MIN_CURRENT_A = 0.5
FACTOR_MIN_PF = 0.3
FACTOR_MIN_POWER_W = 50.0
FACTOR_BUCKET_SECONDS = 3600
FACTOR_MIN_INSTANTS_PER_BUCKET = 5
FACTOR_CHANGE_BUCKETS = 2

#: Schema tag kept identical to the pull tool's historical output.
SCHEMA_FACTORS = "wattmosaic.measurement_factors/1"


def parse_numeric(raw: object) -> float | None:
    """Finite float from an HA state value, or ``None`` (absent, never 0)."""
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


def snap_factor(value: float) -> float | None:
    """Snap ``value`` to the nearest candidate within the tolerance, else None."""
    best = min(FACTOR_SNAP_CANDIDATES, key=lambda candidate: abs(value - candidate))
    if abs(value - best) <= FACTOR_SNAP_TOLERANCE * best:
        return best
    return None


def _series(rows: Sequence[Mapping[str, Any]] | None) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for raw in rows or ():
        ts = parse_ts(raw.get("t"))
        value = parse_numeric(raw.get("v"))
        if ts is not None and value is not None:
            out.append((ts, value))
    out.sort(key=lambda pair: pair[0])
    return out


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

    for reader in readers:
        power_entity = reader.get("power_entity")
        inputs = _factor_inputs(reader)
        if not isinstance(power_entity, str) or not power_entity or not inputs:
            continue

        channel_series = {
            "p": _series(rows_by_entity.get(power_entity)),
            "v": _series(rows_by_entity.get(inputs["v"])),
            "i": _series(rows_by_entity.get(inputs["i"])),
            "pf": _series(rows_by_entity.get(inputs["pf"])),
        }
        if any(not s for s in channel_series.values()):
            unknown.append(power_entity)
            continue

        instants = sorted({ts for rows in channel_series.values() for ts, _ in rows})
        last: dict[str, tuple[datetime, float] | None] = {name: None for name in channel_series}
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
            snapped = snap_factor(abs(p) / (v * abs(i) * abs(pf)))
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
        factors[power_entity] = merge_buckets(bucket_factor)

    return {
        "schema": SCHEMA_FACTORS,
        "method": "P/(V*I*PF) snapped",
        "factors": factors,
        "unknown": sorted(set(unknown)),
    }


def _factor_inputs(reader: Mapping[str, Any]) -> dict[str, str]:
    """Positional voltage/current/power-factor entity ids of a reader."""
    inputs = list(reader.get("factor_inputs") or ())
    if len(inputs) < 3:
        return {}
    return {"v": inputs[0], "i": inputs[1], "pf": inputs[2]}


def merge_buckets(bucket_factor: Mapping[int, float]) -> list[dict[str, Any]]:
    """Merge hourly factors into intervals with a two-bucket hysteresis.

    A change is adopted only after ``FACTOR_CHANGE_BUCKETS`` consecutive buckets
    with the new value; a single stray bucket never splits an interval.  Missing
    buckets do not split an interval.  ``to`` is ``None`` for the final interval.
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
            continue
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

    runs: list[tuple[int, int, float]] = []
    for bucket in sorted(adopted):
        factor = adopted[bucket]
        if runs and runs[-1][2] == factor:
            first, _, _ = runs[-1]
            runs[-1] = (first, bucket, factor)
        else:
            runs.append((bucket, bucket, factor))

    intervals: list[dict[str, Any]] = []
    for index, (first, last, factor) in enumerate(runs):
        end = bucket_start(runs[index + 1][0]) if index + 1 < len(runs) else None
        intervals.append(
            {
                "from": bucket_start(first).isoformat(),
                "to": end.isoformat() if end is not None else None,
                "factor": factor,
                "samples": sum(bucket_factor[b] for b in bucket_factor if first <= b <= last),
            }
        )
    return intervals


# --------------------------------------------------------------------------- #
# Background job
# --------------------------------------------------------------------------- #


def infer_factors_job(
    companion_store: Any,
    reader_rows: Sequence[Mapping[str, Any]],
    *,
    factors_repo: Any,
    now: datetime,
    last_run: datetime | None = None,
    min_interval_s: float = 3600.0,
    budget: Any = None,
) -> dict[str, Any]:
    """Infer factors from companion rings and append changed intervals.

    Runs on the BackgroundExecutor with a CPU budget and at most hourly.  For
    each reader with companion samples, it derives the factor from the aligned
    ring, and appends a ``MeasurementFactor(applied_by="monitor",
    declared_by="inferred")`` interval when the factor changes.  It never
    overwrites a user-declared factor and leaves readers with no qualifying data
    alone (no row).  New rows are picked up by P3's factor row-count invalidation.
    """
    report: dict[str, Any] = {"appended": 0, "unchanged": 0, "unknown": [], "skipped": 0}
    if last_run is not None and 0.0 <= (now - last_run).total_seconds() < min_interval_s:
        report["skipped"] = 1
        return report
    if companion_store is None or factors_repo is None:
        return report

    from ..contracts import MeasurementFactor

    # Build the same P/V/I/PF row shape derive_factors expects from the rings.
    rows_by_entity: dict[str, list[dict[str, Any]]] = {}
    readers: list[dict[str, Any]] = []
    for row in reader_rows:
        rid = str(row.get("endpoint_id", row.get("reader_id", "")))
        ring = companion_store.ring(rid)
        if not ring:
            continue
        # Power comes from the reader's own ring if available; the companion
        # ring holds (ts, V, I, PF) only, so live inference uses the power
        # samples recorded in the structure memory via the caller's rows.
        power = list(row.get("power_rows") or ())
        if not power:
            continue
        v_rows, i_rows, pf_rows = [], [], []
        for ts, v, i, pf in ring:
            iso = datetime.fromtimestamp(ts, UTC).isoformat()
            if v is not None:
                v_rows.append({"t": iso, "v": v})
            if i is not None:
                i_rows.append({"t": iso, "v": i})
            if pf is not None:
                pf_rows.append({"t": iso, "v": pf})
        v_entity, i_entity, pf_entity = f"v::{rid}", f"i::{rid}", f"pf::{rid}"
        rows_by_entity[v_entity] = v_rows
        rows_by_entity[i_entity] = i_rows
        rows_by_entity[pf_entity] = pf_rows
        rows_by_entity[f"p::{rid}"] = power
        readers.append(
            {
                "power_entity": f"p::{rid}",
                "factor_inputs": [v_entity, i_entity, pf_entity],
                "_reader_id": rid,
            }
        )

    derived = derive_factors(rows_by_entity, readers)
    entity_to_rid = {r["power_entity"]: r["_reader_id"] for r in readers}
    for entity, intervals in derived["factors"].items():
        rid = entity_to_rid.get(entity)
        if rid is None:
            continue
        existing = factors_repo.factors_for_ct(rid)
        user_declared = any(str(f.get("applied_by")) == "user" for f in existing)
        if user_declared:
            # A user declaration is authoritative; never write over it.
            report["skipped"] += 1
            continue
        inferred = [
            f for f in existing if str(f.get("declared_by")) == "inferred"
        ]
        for position, interval in enumerate(intervals):
            effective_from = (
                datetime(2000, 1, 1, tzinfo=UTC)
                if position == 0 and not inferred
                else parse_ts(interval["from"])
            )
            if effective_from is None:
                continue
            # Skip an interval already present with the same multiplier.  The
            # first interval is backdated to cover the pre-qualification history,
            # so its stored ``effective_from`` differs from the derived hour;
            # compare against the latest inferred row's multiplier instead of
            # its exact start to keep the job idempotent.
            latest_open = [f for f in inferred if f.get("effective_to") is None]
            duplicate = any(
                float(f["multiplier"]) == float(interval["factor"])
                for f in (latest_open if position == 0 else inferred)
            )
            if duplicate:
                report["unchanged"] += 1
                continue
            factors_repo.insert_measurement_factor(
                MeasurementFactor(
                    ct_id=rid,
                    multiplier=float(interval["factor"]),
                    effective_from=effective_from,
                    effective_to=parse_ts(interval["to"]) if interval.get("to") else None,
                    applied_by="monitor",
                    declared_by="inferred",
                    note="inferred live from companion V/I/PF: P/(V*I*PF) snapped",
                )
            )
            report["appended"] += 1
    report["unknown"] = [entity_to_rid[e] for e in derived["unknown"] if e in entity_to_rid]
    return report
