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

from collections.abc import Iterator

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
#: * ``ct_channels``, ``devices`` -- versioned topology state written by
#:   ``save_topology`` beside ``topology_versions`` (L4).  The spec names only
#:   the ``topology_versions`` snapshot.
#: * ``device_state_samples`` -- per-device state rows, pruned on the raw
#:   horizon like every other per-instant row (L1).
#: * ``inference_results``, ``reconciliation_results``, ``data_quality_events``,
#:   ``system_events`` -- per-cycle telemetry and log rows on the raw horizon,
#:   not aggregates and not versioned state.  ``device_events`` is L2 (it is
#:   event evidence); ``system_events`` is an operational log, so it is grouped
#:   with the other non-aggregate telemetry rather than declared evidence.
#: * ``operating_stages`` -- a segment of a ``device_runs`` run, so L3.
#: * ``learning_samples`` -- captured observations that feed model fitting,
#:   versioned-model work, so L4.
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
        "device_events",
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
        "device_models",
        "device_model_versions",
        "device_configurations",
        "topology_versions",
        "circuits",
        "topology_relations",
        "ct_measurement_factors",
        "ha_entity_bindings",
        "residual_power",
        "schema_metadata",
        "device_flags",
        "engine_state",
        "ct_channels",
        "devices",
        "learning_samples",
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

    This is the shipped behaviour, entry by entry, not the tier model's
    inference.  The one recorded disagreement:

    * ``device_events`` is classified **L2** (event evidence, spec §2.2), which
      the tier model would make long-term.  The shipped retention pass has
      **always** age-pruned it on the raw horizon, and the spec itself names it
      as the ~1.5 GB/day growth driver (§5.2, `main.py:886-890`).  Preserving
      that behaviour is required by the task's "prune behaviour for existing
      callers is unchanged"; re-tiering it to match the classification would
      silently stop pruning the documented growth driver.  The disagreement is
      recorded here rather than resolved silently -- the spec's L2 classification
      of ``device_events`` needs a follow-up decision.
    """
    return (
        ("ct_samples", "received_at", RAW_HORIZON),
        # The conflict entry.  See the docstring: L2 by classification, raw
        # horizon by shipped behaviour.
        ("device_events", "occurred_at", RAW_HORIZON),
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
#: prose.  The default stays 30 (spec §2.1, ``OD-31``).
RAW_RETENTION_ENV = "WM_RETENTION_DAYS_RAW"


__all__ = [
    "AGGREGATE_HORIZON",
    "BASE_TIER_TABLES",
    "PRUNABLE_TABLES",
    "RAW_HORIZON",
    "RAW_RETENTION_ENV",
    "TABLE_TIERS",
    "TIERS",
    "TIER_TABLES",
    "TierError",
    "is_prunable",
    "iter_tier_tables",
    "merge_tier_tables",
    "merged_tier_tables",
    "partition_decision",
    "prunable_tables",
    "table_tiers",
    "tables_of_tier",
    "tier_of",
    "tier_tables",
]
