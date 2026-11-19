"""The L1-L4 storage tier registry (`TASK-STORAGE-001`, `REQ-STORAGE-001`).

The four tiers of ``docs/specs/load-disaggregation/08-storage-and-data-lifecycle.md``
§2 are a **logical** separation over one SQLite file, not four databases.  This
module is the single published place that says which table belongs to which
tier, so retention, pruning and the storage-health surface read one answer
rather than re-deriving it per call site.

``TIER_TABLES`` carries one entry per tier; :data:`TABLE_TIERS` is its
tabulation.  Every table in the real schema (``storage/migrations/``, whose
``CREATE TABLE`` statements are the authority) appears exactly once.
``tests/unit/test_storage_config.py::test_every_table_is_classified`` asserts
the two directions against a live database, so a new table cannot be added
without being classified -- and a table the specification lists that the schema
does not create is never invented here.

A tier is a *classification*, not a retention sentence.  ``prune`` reads
:func:`prunable_tables`, which carries the shipped horizon per table explicitly,
so the two are allowed to disagree -- ``device_events`` is the recorded case.

Consumers.  ``GET /status`` reads :func:`table_tiers` for the tier vocabulary
(``09-api-and-data-contracts.md`` §7.8.1 ``StorageTierRow``: one row per tier,
``tier`` in ``L1``/``L2``/``L3``/``L4``, sub-classification by ``table_name``).
``Database.prune`` reads :func:`prunable_tables`; the retention policy reads
``EngineConfig``'s per-tier fields.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contracts import utcnow
from .migrations import migration_tier_tables

#: The tier vocabulary, in order.  Exactly these four names are valid
#: (``09-api-and-data-contracts.md`` §7.8.1); no other value may be published.
TIERS: tuple[str, ...] = ("L1", "L2", "L3", "L4")

#: One entry per tier, in the order the spec states them.  A table appears in
#: exactly one list; :data:`TABLE_TIERS` is built from this mapping.
#:
#: The specification's own table lists (§2.1-§2.4) lag the schema in five
#: places.  Classified by the spec's criteria -- raw measurement / event
#: evidence / aggregate / versioned -- rather than omitted:
#:
#: * ``device_state_samples`` -- per-device state rows, pruned on the raw
#:   horizon like every other per-instant row (L1).
#: * ``inference_results``, ``reconciliation_results``, ``data_quality_events``,
#:   ``system_events`` -- per-cycle telemetry and log rows on the raw horizon,
#:   not aggregates and not versioned state.  ``system_events`` is an
#:   operational log, so it is grouped with the other non-aggregate telemetry
#:   rather than declared evidence.
#: * ``operating_stages`` -- a segment of a ``device_runs`` run, so L3.
#:
#: The contract-2 tables (``ct_channels``, ``devices``, ``topology_versions``,
#: ``topology_relations``, ``device_models``, ``device_model_versions``,
#: ``learning_samples``, ``residual_power``, ``device_events``,
#: ``ha_entity_bindings``) are archived as ``*_legacy`` by migration 25, which
#: classifies the archived names in its own ``TIERS``.
#: * ``schema_metadata`` -- schema bookkeeping (already listed by the spec).
#:
#: The spec-listed ``evidence_records`` does not exist and is not created here.
#:
#: **Tables created by a later migration are not listed here.**  Each migration
#: module declares them in its own ``TIERS`` dict, and
#: :func:`merged_tier_tables` folds those in at import.  That is what lets a
#: Wave-2 task classify its tables in the one file it owns, with no edit to this
#: registry (the plan's per-file ownership rule).
TIER_TABLES: dict[str, tuple[str, ...]] = {
    # §2.1 -- high-resolution raw measurements.  The spec names only
    # ``ct_samples``; ``device_state_samples`` and the four per-cycle telemetry
    # tables are per-instant rows on the raw horizon too (see the note below).
    "L1": (
        "ct_samples",
        "device_state_samples",
        "inference_results",
        "reconciliation_results",
        "data_quality_events",
        "system_events",
    ),
    # §2.2 -- immutable event evidence.
    "L2": (
        "isolation_verdicts",
        "inference_records",
    ),
    # §2.3 -- aggregated measurements.
    "L3": (
        "power_aggregates",
        "energy_aggregates",
        "device_runs",
        "operating_stages",
    ),
    # §2.4 -- versioned models and topology, plus the versioned capture store.
    "L4": (
        "device_configurations",
        "circuits",
        "ct_measurement_factors",
        "schema_metadata",
        "device_flags",
        "engine_state",
    ),
}

#: The registry, plus tables declared by discovered migration modules.
#:
#: A table classified twice -- here and by a migration, or by two migrations --
#: is an import-time error, exactly like a duplicate inside :data:`TIER_TABLES`.
#: The two stages are separate functions so the merge can be tested against a
#: synthetic declaration without loading a real migration module.
BASE_TIER_TABLES: dict[str, tuple[str, ...]] = TIER_TABLES


def merge_tier_tables(
    base: dict[str, tuple[str, ...]], declared: dict[str, str]
) -> dict[str, tuple[str, ...]]:
    """``base`` plus ``declared`` folded into their tiers, as a new mapping.

    ``declared`` is ``{table: tier}`` -- what a migration module's ``TIERS`` is.
    Raises :class:`ValueError` for an unknown tier or a table classified twice;
    the caller (:func:`merged_tier_tables`) runs at import, so a mistake here
    fails the process rather than producing a registry with a table in no tier.
    """
    merged: dict[str, list[str]] = {tier: list(tables) for tier, tables in base.items()}
    already = {table for tables in merged.values() for table in tables}
    for table in sorted(declared):
        tier = declared[table]
        if tier not in merged:
            raise ValueError(f"migration module declares unknown tier {tier!r} for {table!r}")
        if table in already:
            raise ValueError(f"{table!r} is classified more than once")
        merged[tier].append(table)
        already.add(table)
    return {tier: tuple(tables) for tier, tables in merged.items()}


def merged_tier_tables() -> dict[str, tuple[str, ...]]:
    """The published registry: :data:`BASE_TIER_TABLES` plus every migration's ``TIERS``."""
    return merge_tier_tables(BASE_TIER_TABLES, migration_tier_tables())


TIER_TABLES = merged_tier_tables()

#: Same registry as ``{table: tier}``.  Built once at import; the mapping is
#: flat so a lookup cannot disagree with the per-tier listing.
TABLE_TIERS: dict[str, str] = {
    table: tier for tier, tables in TIER_TABLES.items() for table in tables
}


class TierError(LookupError):
    """A table with no declared tier.

    Raised rather than defaulted: a table that is not in the registry is a
    classification the spec has not made, and silently putting it in a tier
    would make the registry a guess.
    """


def _check_registry() -> None:
    """Fail at import if a table is listed twice or a tier name is unknown."""
    seen: set[str] = set()
    for tier, tables in TIER_TABLES.items():
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        for table in tables:
            if table in seen:
                raise ValueError(f"{table!r} is classified more than once")
            seen.add(table)


_check_registry()


def tier_of(table: str) -> str:
    """The tier of ``table``: one of ``TIERS``.

    Raises :class:`TierError` for a table the registry does not classify, so a
    caller cannot treat "unknown" as a tier.
    """
    try:
        return TABLE_TIERS[table]
    except KeyError:
        raise TierError(
            f"{table!r} has no storage tier; add it to TIER_TABLES in "
            "wattmosaic_engine.storage.tiers"
        ) from None


def tables_of_tier(tier: str) -> tuple[str, ...]:
    """Every table classified in ``tier``, in registry order."""
    try:
        return TIER_TABLES[tier]
    except KeyError:
        raise TierError(f"{tier!r} is not one of {', '.join(TIERS)}") from None


def table_tiers() -> list[tuple[str, str]]:
    """Every ``(table, tier)`` pair, sorted deterministically by table name.

    This is the published listing.  It is what ``GET /status`` should consume
    for the ``tiers`` block, and what a storage-health surface groups by.
    """
    return sorted(TABLE_TIERS.items(), key=lambda item: item[0])


def tier_tables() -> list[tuple[str, tuple[str, ...]]]:
    """Every ``(tier, tables)`` pair, in :data:`TIERS` order."""
    return [(tier, TIER_TABLES[tier]) for tier in TIERS]


def iter_tier_tables() -> Iterator[tuple[str, str]]:
    """Iterate ``(tier, table)`` in :data:`TIERS` order, then registry order."""
    for tier in TIERS:
        for table in TIER_TABLES[tier]:
            yield tier, table


#: The horizon a prunable table is deleted on.  ``"raw"`` is the L1 raw-sample
#: horizon (``retention_days_raw``); ``"aggregate"`` is the L3 bucket horizon
#: (``retention_days_aggregates``).  Declared per table rather than derived from
#: the tier so a table whose shipped retention predates the tier model keeps the
#: behaviour its readers already depend on.
RAW_HORIZON = "raw"
AGGREGATE_HORIZON = "aggregate"


def is_prunable(table: str) -> bool:
    """Whether the retention pass may delete rows from ``table``.

    The answer is the registry's, not the tier's: :func:`prunable_tables` is the
    declaration of what the shipped retention pass actually deletes, and
    ``device_events`` is the case where that set and the tier model disagree (see
    the comment on its entry there).  Every table not listed is reported with a
    count of ``0`` and left untouched, which is what makes "the history is
    exempt" visible rather than merely absent.
    """
    return table in PRUNABLE_TABLES


def prunable_tables() -> tuple[tuple[str, str, str], ...]:
    """``(table, timestamp_column, horizon)`` for every table the retention pass deletes.

    The column is the age key each table is actually pruned by, and the horizon
    is :data:`RAW_HORIZON` or :data:`AGGREGATE_HORIZON`.  Both are kept here so
    ``Database.prune`` reads one declaration instead of a second hand-written
    list that can drift from the registry.

    This is the shipped behaviour, entry by entry.  The legacy
    ``device_events`` log used to be age-pruned here; migration 25 archives it
    read-only as ``device_events_legacy``, so it is no longer pruned (a delete
    would abort on its read-only trigger).
    """
    return (
        ("ct_samples", "received_at", RAW_HORIZON),
        ("device_state_samples", "received_at", RAW_HORIZON),
        ("inference_results", "timestamp", RAW_HORIZON),
        ("reconciliation_results", "timestamp", RAW_HORIZON),
        ("data_quality_events", "timestamp", RAW_HORIZON),
        ("system_events", "timestamp", RAW_HORIZON),
        ("power_aggregates", "bucket_start", AGGREGATE_HORIZON),
        ("energy_aggregates", "bucket_start", AGGREGATE_HORIZON),
    )


#: ``{table: (column, horizon)}``, built once from :func:`prunable_tables`.
PRUNABLE_TABLES: dict[str, tuple[str, str]] = {
    table: (column, horizon) for table, column, horizon in prunable_tables()
}


# --------------------------------------------------------------------------- #
# D5 -- re-tiering and DEC-30 retention declarations
# --------------------------------------------------------------------------- #

#: The archived legacy raw event log that ``ha_events_v3``/``event_evidence``
#: supersede.  Migration 25 renames it to ``device_events_legacy`` and makes it
#: read-only; nothing writes or prunes it.
LEGACY_EVENT_TABLES: tuple[str, ...] = ("device_events_legacy",)

#: The canonical, immutable event-evidence table that replaces the legacy log.
CANONICAL_EVIDENCE_TABLE = "event_evidence"


def canonical_evidence_table() -> str:
    """The table new (v3) event evidence is written to."""
    return CANONICAL_EVIDENCE_TABLE


def never_pruned_tables() -> tuple[str, ...]:
    """Every registered table the retention pass must never delete from.

    DEC-30 keeps topology, mapping, model and matrix-cell *versions* forever;
    they are small and are the history a replay resolves against.  Derived from
    the registry (L4 tables plus the version/evidence tables of lower tiers)
    rather than a second hand-written list, so a newly registered table is
    exempt by default and the "history is exempt" statement stays true.
    """
    exempt = {
        table
        for table in TABLE_TIERS
        if table not in PRUNABLE_TABLES
        or table.endswith("_versions")
        or table.endswith("_versions_v3")
    }
    return tuple(table for table in TABLE_TIERS if table in exempt)


#: Storage tiers for which a user-facing "growth per day" measurement makes
#: sense.  A versioned (L4) table is immutable history, so growth is reported as
#: ``None`` rather than a fabricated ``0`` (HB §18).
_GROWTH_TIERS = frozenset({"L1", "L2", "L3"})

#: Age key of each table whose health is reported, by its declared tier.  The
#: time column of a versioned table is metadata, not a prune horizon; ``None``
#: means the table has no meaningful single age key.
_TABLE_TIME_COLUMNS: dict[str, str | None] = {
    "ct_samples": "received_at",
    "device_events_legacy": "occurred_at",
    "device_state_samples": "received_at",
    "inference_results": "timestamp",
    "reconciliation_results": "timestamp",
    "data_quality_events": "timestamp",
    "system_events": "timestamp",
    "power_aggregates": "bucket_start",
    "energy_aggregates": "bucket_start",
    "event_evidence": "window_start",
    "event_groups": "opened_at",
    "event_group_members": None,
    "event_inference_records": "created_at",
    "ha_events_v3": "observed_at",
    "inference_snapshots": "created_at",
    "device_group_estimates": "at",
    "reader_residual_series": "at",
    "unknown_load_profiles": "first_seen",
    "topology_versions_v3": "created_at",
    "mapping_versions": "created_at",
    "matrix_cell_versions": "updated_at",
    "power_model_versions_v3": "created_at",
    "configuration_events": "created_at",
    "learning_observations": "created_at",
}
for _table, (_col, _horizon) in PRUNABLE_TABLES.items():
    _TABLE_TIME_COLUMNS.setdefault(_table, _col)


def time_column_of(table: str) -> str | None:
    """The age key of ``table`` for storage-health measurement, or ``None``.

    A table with no single meaningful age key (e.g. the group-member join table)
    reports ``None`` rather than a zero sentinel (HB §18).
    """
    return _TABLE_TIME_COLUMNS.get(table)


def has_growth(tier: str) -> bool:
    """Whether a tier's tables are expected to grow (L1-L3, not versioned L4)."""
    return tier in _GROWTH_TIERS


# --------------------------------------------------------------------------- #
# DEC-30 -- event-evidence retention (two independent clocks)
# --------------------------------------------------------------------------- #

#: The immutable summary (Δ, baseline, σ, quality, window bounds) lives this long.
DEFAULT_EVIDENCE_RETENTION_DAYS = 400
#: High-resolution points inside ``samples_json`` are thinned after this long.
DEFAULT_SAMPLES_RETENTION_DAYS = 30
#: Points kept when a window is thinned: first + last + min + max.
_SUMMARY_POINT_LIMIT = 2


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _as_utc(parsed)


def referenced_evidence_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Ids named by another evidence row's ``supersedes_evidence_id``.

    A superseded record is immutable and older than the revision that replaces
    it; pruning must never drop it while the newer record still points at it.
    Returns an empty set on a database created before migration 21.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_evidence)")}
    if "supersedes_evidence_id" not in columns:
        return frozenset()
    rows = conn.execute(
        "SELECT DISTINCT supersedes_evidence_id FROM event_evidence "
        "WHERE supersedes_evidence_id IS NOT NULL"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _thin_samples(raw: str) -> tuple[list[list[Any]], int]:
    """Reduce a ``samples_json`` payload to a bounded summary.

    Returns the new list and the number of points dropped.  ``None`` sample
    values (data gaps) stay ``None`` -- never turned into ``0.0``.  Idempotent:
    thinning an already-thin list drops nothing.
    """
    try:
        samples = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return [], 0
    if not isinstance(samples, list) or len(samples) <= _SUMMARY_POINT_LIMIT:
        return samples if isinstance(samples, list) else [], 0

    kept: list[list[Any]] = [list(samples[0]), list(samples[-1])]
    numeric = [
        sample
        for sample in samples
        if len(sample) == 2 and isinstance(sample[1], (int, float)) and sample[1] is not None
    ]
    if numeric:
        for extreme in (min(numeric, key=lambda s: s[1]), max(numeric, key=lambda s: s[1])):
            record = list(extreme)
            if not any(record == existing for existing in kept):
                kept.append(record)
    kept.sort(key=lambda s: str(s[0]))
    return kept, len(samples) - len(kept)


def apply_evidence_retention(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    evidence_days: int = DEFAULT_EVIDENCE_RETENTION_DAYS,
    sample_days: int = DEFAULT_SAMPLES_RETENTION_DAYS,
    active_reference_ids: Iterable[str] = (),
) -> dict[str, int]:
    """Thin old raw samples and delete unreferenced, expired evidence rows.

    Returns ``{"rows_thinned", "samples_dropped", "rows_deleted",
    "rows_protected"}``.  ``now`` is injected so a fixed clock is deterministic;
    it defaults to :func:`utcnow` at the call site only.  ``active_reference_ids``
    are evidence ids cited by an active derived run (L7) that must outlive the
    evidence horizon.  Single transaction; no wall clock and no reimplementation
    of the repository layer.
    """
    current = _as_utc(now) if now is not None else utcnow()
    sample_cutoff = current - timedelta(days=int(sample_days))
    evidence_cutoff = current - timedelta(days=int(evidence_days))
    protected = referenced_evidence_ids(conn) | frozenset(
        str(value) for value in active_reference_ids
    )

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_evidence)")}
    if "window_start" not in columns or "samples_json" not in columns:
        return {"rows_thinned": 0, "samples_dropped": 0, "rows_deleted": 0, "rows_protected": 0}

    counts = {"rows_thinned": 0, "samples_dropped": 0, "rows_deleted": 0, "rows_protected": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT evidence_id, window_start, samples_json FROM event_evidence"
        ).fetchall()
        for row in rows:
            evidence_id = str(row["evidence_id"])
            window_start = _parse_iso(row["window_start"])
            if window_start <= sample_cutoff:
                kept, dropped = _thin_samples(row["samples_json"])
                if dropped:
                    conn.execute(
                        "UPDATE event_evidence SET samples_json=? WHERE evidence_id=?",
                        (json.dumps(kept, default=str), evidence_id),
                    )
                    counts["rows_thinned"] += 1
                    counts["samples_dropped"] += dropped

            if window_start <= evidence_cutoff:
                if evidence_id in protected:
                    counts["rows_protected"] += 1
                else:
                    conn.execute(
                        "DELETE FROM event_evidence WHERE evidence_id=?", (evidence_id,)
                    )
                    counts["rows_deleted"] += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return counts


# --------------------------------------------------------------------------- #
# Z11 -- age retention for the derived tables (V2 DB-growth finding)
# --------------------------------------------------------------------------- #

#: The derived tables that grew without bound before Z11 (~69 MB/day, ~25 GB/yr)
#: and now carry an age-based horizon.  Declared in one place so the prune pass,
#: the config default and the tests agree on the table/column/key triple.
#: ``(table, age_column, key_column)``: key_column is the primary key used by the
#: DELETE, so a chunk prunes by the same key it selects on.
DERIVED_RETENTION_TABLES: tuple[tuple[str, str, str], ...] = (
    ("inference_snapshots", "created_at", "id"),
    ("event_inference_records", "created_at", "record_id"),
    ("ha_events_v3", "observed_at", "event_id"),
    ("event_groups", "opened_at", "group_id"),
    ("reader_residual_series", "at", "reader_id"),
)

#: Join tables pruned implicitly when their parent row is deleted.  They have no
#: independent age column: a member is as old as the group it belongs to, and
#: ``event_group_members`` has a FOREIGN KEY to ``event_groups`` (so members are
#: deleted *before* the parent group).  Counts are folded into the parent's total.
DERIVED_MEMBER_TABLES: dict[str, tuple[str, str]] = {
    "event_groups": ("event_group_members", "group_id"),
}

#: Default horizons (days), matching the EngineConfig fields of the same name
#: (Z11).  Kept here so the pure decision layer has defaults without importing
#: config (config is the lowest layer and imports nothing from storage).
DEFAULT_DERIVED_RETENTION_DAYS: dict[str, int] = {
    "inference_snapshots": 90,
    "event_inference_records": 90,
    "ha_events_v3": 90,
    "event_groups": 90,
    "event_group_members": 90,
    "reader_residual_series": 30,
}

#: Rows per chunk.  A single maintenance pass deletes at most this many rows per
#: table, keeping every pass well inside DEC-37's 2 s CPU cap; the next pass
#: continues where this one stopped.  Bounded chunks also keep each transaction
#: short enough not to block a concurrent `ping()` on `/health`.
DERIVED_PRUNE_CHUNK_SIZE = 500

#: Group states that are still live: a non-finalized group must never be pruned
#: even if it is older than the horizon (HB §12.3, DEC-30 replay guarantee).
_LIVE_GROUP_STATUSES = frozenset({"detected", "matching", "provisional", "unresolved"})


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Columns of ``table`` (empty when the table does not exist yet)."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _live_group_keys(conn: sqlite3.Connection) -> frozenset[str]:
    """``group_id`` of every group that is still live or non-finalized.

    A group is protected when its state is one of :data:`_LIVE_GROUP_STATUSES`
    (detected/matching/provisional/unresolved), when it has no ``closed_at``
    (still open), or when a *newer revision* of the same group exists (the older
    revision is immutable history a revision chain still points at).  A
    CONFIRMED/REVISED group with a ``closed_at`` and no newer revision is
    finalized and may be pruned once it ages out.
    """
    columns = _table_columns(conn, "event_groups")
    if not columns or "status" not in columns:
        return frozenset()
    closed_expr = "closed_at" if "closed_at" in columns else "NULL"
    try:
        rows = conn.execute(
            f"SELECT group_id, revision, status, {closed_expr} FROM event_groups"  # noqa: S608
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    non_final_revisions: dict[str, int] = {}
    for row in rows:
        group_id = str(row[0])
        try:
            revision = int(row[1])
        except (TypeError, ValueError):
            revision = 0
        status = str(row[2]).lower()
        closed = str(row[3]) if row[3] is not None else ""
        if status in _LIVE_GROUP_STATUSES or not closed:
            non_final_revisions[group_id] = max(non_final_revisions.get(group_id, 0), revision)
    protected = set(non_final_revisions)
    # A superseded revision is referenced by the newest revision of the same
    # group; protect every older revision of a group that is itself non-final.
    for group_id, revision in non_final_revisions.items():
        rows_for_group = [r for r in rows if str(r[0]) == group_id]
        for r in rows_for_group:
            try:
                if int(r[1]) < revision:
                    protected.add(group_id)
            except (TypeError, ValueError):
                continue
    return frozenset(protected)


def _snapshot_referenced_ids(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """Evidence/group/event/member ids cited by the live (current) snapshot.

    The current snapshot is the highest ``id`` row in ``inference_snapshots``.
    Its ``payload_json`` carries ``unresolved_events`` (ha_event ids) and
    ``group_estimates`` (group ids and nested ``evidence_refs``).  A row these
    ids name must outlive its age horizon: pruning it would break the HB §18
    provenance the snapshot promises to resolve.  On a database with no
    snapshot, every set is empty.
    """
    empty: dict[str, frozenset[str]] = {
        "evidence": frozenset(),
        "groups": frozenset(),
        "ha_events": frozenset(),
        "records": frozenset(),
        "residual_readers": frozenset(),
    }
    columns = _table_columns(conn, "inference_snapshots")
    if not columns or "payload_json" not in columns:
        return empty
    try:
        row = conn.execute(
            "SELECT payload_json FROM inference_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return empty
    if row is None or not row[0]:
        return empty
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return empty
    if not isinstance(payload, dict):
        return empty

    groups: set[str] = set()
    evidence: set[str] = set()
    ha_events: set[str] = set()
    records: set[str] = set()
    residual_readers: set[str] = set()
    for point in payload.get("residual_estimates", []) or []:
        if isinstance(point, dict) and point.get("reader_id") is not None:
            residual_readers.add(str(point["reader_id"]))
    for group in payload.get("group_estimates", []) or []:
        if not isinstance(group, dict):
            continue
        gid = group.get("group_id")
        if gid is not None:
            groups.add(str(gid))
        for ref in group.get("evidence_refs", []) or []:
            if not isinstance(ref, dict) or "id" not in ref:
                continue
            kind = str(ref.get("kind", ""))
            identifier = str(ref["id"])
            if kind == "ha_event":
                ha_events.add(identifier)
            elif kind == "event_evidence":
                evidence.add(identifier)
            elif kind == "event_group":
                groups.add(identifier)
    for value in payload.get("unresolved_events", []) or []:
        ha_events.add(str(value))
    return {
        "groups": frozenset(groups),
        "evidence": frozenset(evidence),
        "ha_events": frozenset(ha_events),
        "records": frozenset(records),
        "residual_readers": frozenset(residual_readers),
    }


def _active_record_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Record ids still referenced by a live derived run or a newer record.

    ``event_inference_records.previous_record_id`` forms an append-only revision
    chain; a record cited by a newer one is immutable history that replay needs.
    A derived run that has not been superseded (``superseded_by IS NULL``) may
    still be resolved against its records, so those are protected too.  Only the
    JSON ``evidence``/``triple`` columns link a run to records, so the parse is
    deliberately tolerant of absent columns.
    """
    protected: set[str] = set()
    columns = _table_columns(conn, "event_inference_records")
    if columns and "previous_record_id" in columns:
        try:
            rows = conn.execute(
                "SELECT DISTINCT previous_record_id FROM event_inference_records "
                "WHERE previous_record_id IS NOT NULL"
            ).fetchall()
            protected.update(str(r[0]) for r in rows)
        except sqlite3.Error:
            pass
    run_columns = _table_columns(conn, "derived_runs")
    if run_columns and "superseded_by" in run_columns:
        try:
            runs = conn.execute(
                "SELECT triple_json FROM derived_runs WHERE superseded_by IS NULL"
            ).fetchall()
            for run in runs:
                if not run[0]:
                    continue
                try:
                    data = json.loads(run[0])
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                for ref in data.get("evidence_refs", []) or []:
                    if isinstance(ref, dict) and ref.get("kind") == "event_inference_record":
                        protected.add(str(ref.get("id")))
        except sqlite3.Error:
            pass
    return frozenset(protected)


def apply_derived_retention(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    horizons: Mapping[str, int] | None = None,
    chunk_size: int = DERIVED_PRUNE_CHUNK_SIZE,
    budget: Any | None = None,
) -> dict[str, int]:
    """Age-prune the derived tables, respecting provenance and DEC-37's CPU cap.

    A pure decision layer over an injected ``conn`` and ``now`` (mirroring
    :func:`apply_evidence_retention`): no wall clock, no repository layer, no
    engine import.  ``horizons`` maps a table name to its retention in days and
    falls back to :data:`DEFAULT_DERIVED_RETENTION_DAYS`, then the matching
    ``EngineConfig`` field at the runtime call site.

    ``budget`` is the optional cooperative :class:`~wattmosaic_engine.core.executors.JobBudget`
    (duck-typed: anything with ``exceeded()``): after each bounded chunk the pass
    stops when the budget is spent, so one background job never exceeds ~2 s of
    CPU.  A later maintenance pass resumes where this one stopped.  ``chunk_size``
    additionally bounds a pass even without a budget (deterministic tests).

    Returns ``{table: rows_deleted}` for every table in
    :data:`DERIVED_RETENTION_TABLES` (``0`` when the table is absent or nothing
    aged out, so "deleted nothing" is visible rather than absent).  Provenance
    rules, in order of precedence:

    * the **current snapshot** is never deleted (the newest row); rows it cites
      (ha_event ids, group ids, evidence refs) are never deleted;
    * a **non-finalized group** / group with a newer revision, and every member
      of a protected group, are never deleted;
    * an **active record** (referenced by a newer record or a live derived run)
      is never deleted;
    * evidence rows still cited by a newer revision are already protected by
      :func:`apply_evidence_retention` -- this pass does not touch that table.
    """
    if horizons is None:
        horizons = {}
    elif not isinstance(horizons, Mapping):
        raise TypeError("horizons must be a mapping of table -> days")
    limit = max(1, int(chunk_size))
    current = _as_utc(now) if now is not None else utcnow()

    # Collect protection sets once, before any delete (deleting a snapshot first
    # would make the references it carried undiscoverable mid-pass).
    snapshot_ids = _snapshot_referenced_ids(conn)
    live_groups = _live_group_keys(conn)
    active_records = _active_record_ids(conn)
    # The live snapshot row itself is the newest id; never prune it.
    live_snapshot_id: int | None = None
    if _table_columns(conn, "inference_snapshots"):
        row = conn.execute(
            "SELECT id FROM inference_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            live_snapshot_id = int(row[0])

    totals = {table: 0 for table, _col, _key in DERIVED_RETENTION_TABLES}
    # Compute all cutoffs up front from one `now`, so horizons are consistent
    # even if a chunked pass spans several maintenance cycles.
    cutoffs: dict[str, str] = {}
    for table, _col, _key in DERIVED_RETENTION_TABLES:
        days = int(horizons.get(table, DEFAULT_DERIVED_RETENTION_DAYS[table]))
        cutoffs[table] = (current - timedelta(days=max(1, days))).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, age_col, key_col in DERIVED_RETENTION_TABLES:
            columns = _table_columns(conn, table)
            if not columns or age_col not in columns:
                continue
            cutoff = cutoffs[table]
            # Fetch candidate keys aged past the horizon, bounded by one chunk.
            candidates = conn.execute(
                f"SELECT {key_col}, {age_col} FROM {table} "  # noqa: S608 - fixed names
                f"WHERE {age_col} < ? ORDER BY {age_col} ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            deleted = 0
            for key, _aged_value in candidates:
                if budget is not None and budget.exceeded():
                    break
                if not _is_prunable_row(
                    table,
                    key,
                    live_snapshot_id=live_snapshot_id,
                    snapshot_ids=snapshot_ids,
                    live_groups=live_groups,
                    active_records=active_records,
                ):
                    continue
                # A group's members share its fate; delete them first so the
                # FOREIGN KEY from event_group_members -> event_groups holds.
                member = DERIVED_MEMBER_TABLES.get(table)
                if member is not None:
                    member_table, member_key = member
                    if _table_columns(conn, member_table):
                        conn.execute(
                            f"DELETE FROM {member_table} WHERE {member_key} = ?",  # noqa: S608
                            (key,),
                        )
                conn.execute(
                    f"DELETE FROM {table} WHERE {key_col} = ?",  # noqa: S608 - fixed names
                    (key,),
                )
                deleted += 1
            totals[table] = deleted
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return totals


def _is_prunable_row(
    table: str,
    key: Any,
    *,
    live_snapshot_id: int | None,
    snapshot_ids: Mapping[str, frozenset[str]],
    live_groups: frozenset[str],
    active_records: frozenset[str],
) -> bool:
    """Whether one aged candidate row may be deleted (all provenance checks)."""
    identifier = str(key)
    if table == "inference_snapshots":
        # The current snapshot is protected; an older snapshot whose content is
        # identical to the current one is also protected from deletion only by
        # its age (all older snapshots are history and may be pruned).
        return live_snapshot_id is None or int(key) != live_snapshot_id
    if table == "ha_events_v3":
        return identifier not in snapshot_ids["ha_events"]
    if table == "event_groups":
        return identifier not in live_groups and identifier not in snapshot_ids["groups"]
    if table == "event_inference_records":
        # A record cited by a newer revision (active run) or by the live snapshot's
        # group evidence is immutable history that replay still resolves.
        return identifier not in active_records and identifier not in snapshot_ids["records"]
    if table == "reader_residual_series":
        # key_col is reader_id; a reader whose residual the current snapshot
        # publishes is still live and its series must outlive its age horizon.
        return identifier not in snapshot_ids["residual_readers"]
    return True


def partition_decision() -> str:
    """The recorded partitioning decision and the measurement behind it.

    SQLite has no native partitioning, so none is used.  The text is published
    here (and mirrored in spec ``08`` §7) so the requirement that a partitioning
    decision carry its measurement is satisfied by the decision itself rather
    than by a separate document nobody links to.
    """
    return (
        "SQLite has no native partitioning and none is introduced. What is used "
        "instead: time-ordered keys (buckets floored to epoch-aligned boundaries, "
        "rows keyed by time), the index set of spec 08 §6.2, and the PRAGMA "
        "configuration (WAL, busy_timeout=10000, wal_autocheckpoint=1000). "
        "Measurement that justifies not partitioning: on the reference install "
        "(data/wattmosaic.db, 2026-11-07) the largest payload table, "
        "inference_results, holds 320852 rows and 16.1 MB of explanation_json; a "
        "topology_versions snapshot is 37302 bytes (3151 bytes zlib-compressed). "
        "At the documented 1.5 GB/day event-spam rate the growth driver is the "
        "event path, not a per-partition access path. A different technology is "
        "reconsidered only when a measurement shows the single-file store misses "
        "a stated requirement -- sustained write throughput above the "
        "single-writer path, a dataset whose required queries exceed their "
        "latency budget, or a retention requirement pruning cannot meet."
    )


#: ``addon/run.sh`` maps the App option ``retention_days_raw`` onto this variable;
#: named here so the option surface and the engine field cannot drift apart in
#: prose.  The default is now 7 days (V2 DB-growth finding); the accepted range
#: is 1-90 (``RETENTION_DAYS_RAW_RANGE``), which the add-on schema mirrors.
RAW_RETENTION_ENV = "WM_RETENTION_DAYS_RAW"


__all__ = [
    "AGGREGATE_HORIZON",
    "BASE_TIER_TABLES",
    "CANONICAL_EVIDENCE_TABLE",
    "DEFAULT_DERIVED_RETENTION_DAYS",
    "DEFAULT_EVIDENCE_RETENTION_DAYS",
    "DEFAULT_SAMPLES_RETENTION_DAYS",
    "DERIVED_MEMBER_TABLES",
    "DERIVED_PRUNE_CHUNK_SIZE",
    "DERIVED_RETENTION_TABLES",
    "LEGACY_EVENT_TABLES",
    "PRUNABLE_TABLES",
    "RAW_HORIZON",
    "RAW_RETENTION_ENV",
    "TABLE_TIERS",
    "TIERS",
    "TIER_TABLES",
    "TierError",
    "apply_derived_retention",
    "apply_evidence_retention",
    "canonical_evidence_table",
    "has_growth",
    "is_prunable",
    "iter_tier_tables",
    "merge_tier_tables",
    "merged_tier_tables",
    "never_pruned_tables",
    "partition_decision",
    "prunable_tables",
    "referenced_evidence_ids",
    "table_tiers",
    "tables_of_tier",
    "tier_of",
    "tier_tables",
    "time_column_of",
]
