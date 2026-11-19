"""Shared helpers for the V2 developer tools (synthetic load / migration rehearsal).

This module lives beside the tools it serves and is importable both as a script
(``sys.path`` is set up by each tool) and as ``tools.dev._perfsupport`` from the
unit tests.  It deliberately imports no engine code at module import time: every
engine import is local to a function so ``tools.dev.rehearse_migration --help``
works even before the engine has been installed.

Nothing here reads a wall clock for *simulated* time: the benchmark drives an
injected clock, and the only wall-clock reads are the ``perf_counter`` calls that
measure real CPU/response latency.
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (matches the project's p99 probes).

    Returns ``0.0`` for an empty sample rather than raising, so a benchmark whose
    quick mode produced no requests still emits a well-shaped report.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = int(math.ceil(min(1.0, max(0.0, fraction)) * len(ordered))) - 1
    return ordered[max(0, min(len(ordered) - 1, rank))]


def latency_summary(values_ms: list[float]) -> dict[str, float]:
    """p50/p95/p99 of a millisecond sample (plus the sample count)."""
    return {
        "p50_ms": round(percentile(values_ms, 0.50), 4),
        "p95_ms": round(percentile(values_ms, 0.95), 4),
        "p99_ms": round(percentile(values_ms, 0.99), 4),
        "count": len(values_ms),
    }


# --------------------------------------------------------------------------- #
# Deterministic synthetic load
# --------------------------------------------------------------------------- #

#: Default benchmark scale (the task's Pi-class budget).
DEFAULT_READERS = 40
DEFAULT_ENTITIES = 500
DEFAULT_EVENTS_PER_S = 30.0
DEFAULT_DURATION_S = 3600.0
QUICK_DURATION_S = 60.0


def reader_ids(n: int) -> list[str]:
    return [f"ct_{i:03d}" for i in range(n)]


def entity_ids(n: int) -> list[str]:
    return [f"ha_entity_{i:04d}" for i in range(n)]


def make_revision(endpoint_id: str, at: datetime) -> Any:
    """A neutral, migration-declared endpoint revision for a synthetic reader."""
    from wattmosaic_engine.contracts import EndpointConfigRevision

    return EndpointConfigRevision(
        endpoint_id=endpoint_id,
        revision=1,
        valid_from=at - timedelta(days=1),
        phase="l1",
        leg=None,
        voltage_reference_v=None,
        polarity="normal",
        multiplier=1.0,
        calibration_version="synthetic@1",
        measurement_type="instantaneous",
        measurement_window_ms=1000,
        expected_interval_s=None,
        resolution_w=None,
        declared_by="migration",
    )


class _RevisionLookup:
    """Fixed lookup over the synthetic reader revisions, shared with normalizer."""

    def __init__(self, revisions: dict[str, Any]) -> None:
        self._revisions = revisions

    def endpoint_revision_at(self, endpoint_id: str, at: datetime) -> Any | None:
        return self._revisions.get(endpoint_id)


def build_load(
    *, readers: int, entities: int, events_per_s: float, seed: int, origin: datetime
) -> dict[str, Any]:
    """Build all deterministic inputs for the synthetic run (no side effects)."""
    rng = random.Random(seed)
    rids = reader_ids(readers)
    eids = entity_ids(entities)
    revisions = {rid: make_revision(rid, origin) for rid in rids}
    # Pre-generate the power walk so a repeated run is byte-identical.
    levels = {rid: 100.0 + i * 1.5 for i, rid in enumerate(rids)}
    return {
        "rng": rng,
        "readers": rids,
        "entities": eids,
        "revisions": revisions,
        "levels": levels,
        "events_per_s": float(events_per_s),
        "origin": origin,
    }


# --------------------------------------------------------------------------- #
# Migration rehearsal schema snapshot helpers
# --------------------------------------------------------------------------- #

#: Tables whose ids / HA unique ids must be preserved across the migration.
#: HA-unique-id-bearing tables are the canonical v3 HA entity tables plus the
#: legacy binding table (archived read-only by v025 but still readable).
ID_TABLES: dict[str, str] = {
    # pre-3.0 legacy sources
    "devices": "device_id",
    "devices_legacy": "device_id",
    "ha_entity_bindings": "entity_id",
    # canonical v3 tables (backfilled from the legacy ones)
    "endpoint_config_revisions": "endpoint_id",
    "physical_devices": "device_id",
    "measurement_endpoints": "endpoint_id",
    "ha_entities": "entity_id",
    "ha_entity_links": "entity_id",
    "ha_entity_bindings_legacy": "entity_id",
    "topology_versions_v3": "version_id",
    "mapping_versions": "version_id",
}


def tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count for every table (so archived tables can be checked by name)."""
    return {
        name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name in tables(conn)
    }


def primary_ids(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Primary/unique ids of the identity-bearing tables, sorted per table."""
    ids: dict[str, list[str]] = {}
    for table, column in ID_TABLES.items():
        if table not in tables(conn):
            continue
        cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if column not in cols:
            continue
        ids[table] = [str(r[0]) for r in conn.execute(
            f'SELECT DISTINCT "{column}" FROM "{table}" ORDER BY "{column}"'
        ).fetchall()]
    return ids


def ha_unique_ids(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every HA entity id and its link targets (the stable HA unique ids)."""
    result: dict[str, list[str]] = {}
    if "ha_entities" in tables(conn):
        result["ha_entities"] = [
            str(r[0]) for r in conn.execute("SELECT entity_id FROM ha_entities ORDER BY entity_id")
        ]
    if "ha_entity_links" in tables(conn):
        result["ha_entity_links"] = [
            f"{r[0]}|{r[1]}|{r[2]}"
            for r in conn.execute(
                "SELECT entity_id, target_kind, target_id FROM ha_entity_links "
                "ORDER BY entity_id, target_kind, target_id"
            )
        ]
    for name in ("ha_entity_bindings", "ha_entity_bindings_legacy"):
        if name in tables(conn):
            result[name] = [
                f"{r[0]}|{r[1]}"
                for r in conn.execute(
                    f'SELECT entity_id, device_id FROM "{name}" ORDER BY entity_id, device_id'
                )
            ]
    return result


#: (legacy table, legacy id column) -> canonical table whose ids must match.
#: Migrations 13-15 backfill these one-for-one from the legacy rows (DEC-04:
#: device ids are kept verbatim; DEC-08: HA entity bindings are copied).
LEGACY_TO_CANONICAL: dict[tuple[str, str], tuple[str, str]] = {
    ("devices_legacy", "device_id"): ("physical_devices", "device_id"),
    ("ha_entity_bindings_legacy", "entity_id"): ("ha_entities", "entity_id"),
}


def _ids_for(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in cols:
        return []
    return [str(r[0]) for r in conn.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" ORDER BY "{column}"'
    ).fetchall()]


def legacy_to_canonical_id_failures(
    before_ids: dict[str, list[str]], after_ids: dict[str, list[str]]
) -> list[str]:
    """Check every pre-existing id survived the backfill into its new table.

    ``before_ids`` is keyed by the *pre-migration* table name (so the legacy
    source ids are available), ``after_ids`` by the *post-migration* name.  A
    canonical table absent from the input cannot be compared and is skipped;
    when both sides exist the id sets must be identical.
    """
    failures: list[str] = []
    for (legacy, _column), (canonical, _canon_column) in LEGACY_TO_CANONICAL.items():
        # before snapshot used the un-archived name (no _legacy suffix yet).
        legacy_plain = legacy.removesuffix("_legacy")
        source = before_ids.get(legacy_plain, [])
        if not source:
            continue
        target = after_ids.get(canonical, [])
        if not target:
            failures.append(f"{canonical}: no ids (expected the ids from {legacy_plain})")
            continue
        missing = sorted(set(source) - set(target))
        if missing:
            failures.append(f"{canonical}: lost ids {missing[:5]} from {legacy_plain}")
    # Every id that existed in a canonical table before the migration (i.e. a
    # re-run of an already-canonical DB) must still exist afterwards.
    for table in set(before_ids) & set(after_ids):
        if set(before_ids[table]) - set(after_ids[table]):
            failures.append(f"{table}: lost pre-existing ids")
    return failures


def ha_unique_id_failures(
    before: dict[str, list[str]], after: dict[str, list[str]]
) -> list[str]:
    """Check HA unique ids are unchanged across the migration.

    The pre-migration ``ha_entity_bindings`` rows must survive verbatim in the
    archived ``ha_entity_bindings_legacy`` table, and every legacy entity id
    must also be present in the new ``ha_entities`` table.
    """
    failures: list[str] = []
    legacy_bindings = before.get("ha_entity_bindings", [])
    archived = after.get("ha_entity_bindings_legacy", [])
    if legacy_bindings and archived != legacy_bindings:
        failures.append("ha_entity_bindings: archived rows differ from the originals")
    legacy_entities = {row.split("|", 1)[0] for row in legacy_bindings}
    new_entities = set(after.get("ha_entities", []))
    if legacy_entities - new_entities:
        failures.append(
            f"ha_entities: lost ids {sorted(legacy_entities - new_entities)[:5]}"
        )
    return failures


def reconcile_counts(before: dict[str, int], after: dict[str, int]) -> tuple[bool, list[str]]:
    """Check that every pre-existing row count survived the migration.

    v025 renames legacy tables to ``<name>_legacy``; a table that disappeared is
    accepted only when its archived counterpart exists with the same count.  New
    tables introduced by the migration are ignored (they start empty or are
    backfilled from the legacy rows).  Returns ``(ok, list of failures)``.
    """
    failures: list[str] = []
    for name, count in before.items():
        if name in after:
            if after[name] != count:
                failures.append(f"{name}: {count} -> {after[name]} rows")
            continue
        archived = f"{name}_legacy"
        if archived in after:
            if after[archived] != count:
                failures.append(f"{name} -> {archived}: {count} -> {after[archived]} rows")
            continue
        failures.append(f"{name}: table vanished ({count} rows)")
    return (not failures), failures


def input_path_scrub(path: Path) -> str:
    """A report-safe name for an operator path: basename only, no directories."""
    return path.name
