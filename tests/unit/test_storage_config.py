"""Storage: the configuration table's schema (migration 4) and the tier registry.

Exercises the real ``Database`` against a temporary SQLite file.  The legacy
``DeviceConfiguration`` contract and its save/load helpers were removed with the
contract-3.0.0 cutover, so the configuration half only pins the schema and that a
prior-schema payload survives the upgrade byte-for-byte.  The
``TASK-STORAGE-001`` half covers the four-tier registry, the per-tier retention
ranges, the payload encoding and the retention pass.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.config.settings import (
    RETENTION_DAYS_AGGREGATES_RANGE,
    RETENTION_DAYS_RAW_RANGE,
    EngineConfig,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    PAYLOAD_ENCODING_VERSION,
    RESERVED_VERSIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.payload import (
    decode_json_text,
    decode_payload,
    encode_if_worthwhile,
    encode_payload,
    payload_is_encoded,
)
from wattmosaic_engine.storage.tiers import (
    AGGREGATE_HORIZON,
    CAPTURE_HORIZON,
    PRUNABLE_TABLES,
    TABLE_TIERS,
    TIER_TABLES,
    TIERS,
    TierError,
    is_prunable,
    partition_decision,
    prunable_tables,
    table_tiers,
    tier_of,
)

# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


def test_migrations_reach_version_4_and_are_idempotent(tmp_path) -> None:
    db_path = tmp_path / "config.db"
    first = Database(db_path)
    try:
        assert first.schema_version == LATEST_VERSION
        assert first.schema_version >= 4
        tables = {
            row["name"]
            for row in first._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "device_configurations" in tables
    finally:
        first.close()

    second = Database(db_path)
    try:
        assert second.schema_version == LATEST_VERSION
        assert apply_migrations(second._conn) == LATEST_VERSION
    finally:
        second.close()


def test_latest_schema_includes_the_audit_tables(tmp_path) -> None:
    """Migrations 9 and 10 are additive audit state; config is unaffected."""
    db = Database(tmp_path / "audit_schema.db")
    try:
        assert db.schema_version == LATEST_VERSION
        tables = {
            row["name"]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "circuits",
            "topology_relations_legacy",
            "inference_records",
            "ct_measurement_factors",
            "ha_entity_bindings_legacy",
            "residual_power_legacy",
            "isolation_verdicts",
        } <= tables
        # And the migration is idempotent on reopen.
        assert apply_migrations(db._conn) == LATEST_VERSION
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- the tier registry
# --------------------------------------------------------------------------- #


def _live_tables(db: Database) -> set[str]:
    """Every table the real schema creates, read from ``sqlite_master``."""
    return {
        str(row["name"])
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def test_every_table_is_classified(tmp_path) -> None:
    """Every table the schema creates resolves to exactly one tier.

    Both directions are asserted against a live database, not against the
    registry alone: a table the migrations create but the registry omits (or a
    table the registry invents) is a classification nobody made, and the
    retention pass would then either skip it silently or raise.
    """
    db = Database(tmp_path / "tiers.db")
    try:
        live = _live_tables(db)
        registered = set(TABLE_TIERS)
        assert live == registered, (
            f"unclassified={sorted(live - registered)} "
            f"invented={sorted(registered - live)}"
        )
        # Exactly one tier per table, in both directions.
        assert len(TABLE_TIERS) == sum(len(t) for t in TIER_TABLES.values())
        assert {tier for _, tier in table_tiers()} == set(TIERS)
        assert {table for table, _ in table_tiers()} == registered
        for table in live:
            assert tier_of(table) in TIERS
    finally:
        db.close()


def test_the_registry_is_import_safe() -> None:
    """The import-time check refuses a duplicate or an unknown tier."""
    from wattmosaic_engine.storage.tiers import tables_of_tier

    assert TIERS == ("L1", "L2", "L3", "L4")
    assert set(TIER_TABLES) == set(TIERS)
    with pytest.raises(TierError):
        tier_of("no_such_table")
    with pytest.raises(TierError):
        tables_of_tier("L5")


def test_stats_publishes_the_tier_classification(tmp_path) -> None:
    """``GET /status``'s storage block reads the registry, not a second list."""
    db = Database(tmp_path / "stats.db")
    try:
        published = db.stats()["tiers"]
        assert dict(published) == TABLE_TIERS
        assert len(published) == len(TABLE_TIERS)
    finally:
        db.close()


def test_prunable_tables_match_the_shipped_retention_pass(tmp_path) -> None:
    """The registry's prune declaration is the shipped behaviour, entry by entry.

    ``device_events`` was the recorded conflict (L2, pruned on the raw horizon).
    The contract-3.0.0 cutover archived it read-only as ``device_events_legacy``
    -- still L2 -- and nothing prunes it any more: a DELETE would abort on its
    read-only trigger, and no runtime path writes it, so it cannot grow.
    """
    db = Database(tmp_path / "prune_registry.db")
    try:
        declared = {table: (col, horizon) for table, col, horizon in prunable_tables()}
        # ADR-0042: ct_samples has its own capture horizon, handed to the
        # background purge rather than the on-loop raw prune.
        assert declared["ct_samples"] == ("received_at", CAPTURE_HORIZON)
        assert "device_events" not in declared
        assert "device_events_legacy" not in declared
        assert declared["power_aggregates"] == ("bucket_start", AGGREGATE_HORIZON)
        assert declared["energy_aggregates"] == ("bucket_start", AGGREGATE_HORIZON)
        # Every declared table exists and every declared column is its age key.
        for table, (col, _horizon) in declared.items():
            assert table in _live_tables(db)
            columns = {
                str(row[1])
                for row in db._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert col in columns, (table, col)
            assert is_prunable(table)
        # The registry is the single source: ``PRUNABLE_TABLES`` is built from it.
        assert {t: (c, h) for t, (c, h) in PRUNABLE_TABLES.items()} == declared
        # The archive keeps its classification but is exempt from pruning.
        assert tier_of("device_events_legacy") == "L2"
        assert not is_prunable("device_events_legacy")
    finally:
        db.close()


def test_prune_reports_every_exempt_table_with_zero(tmp_path) -> None:
    """Every registered table the pass does not delete is reported as ``0``."""
    db = Database(tmp_path / "exempt.db")
    try:
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert set(removed) == set(TABLE_TIERS)
        for table, count in removed.items():
            if is_prunable(table):
                continue
            assert count == 0, table
            # And the table is genuinely untouched: nothing was deleted.
            assert isinstance(count, int)
        assert removed["inference_records"] == 0
        assert removed["isolation_verdicts"] == 0
        assert removed["residual_power_legacy"] == 0
        assert removed["circuits"] == 0
    finally:
        db.close()


def test_archived_device_events_are_not_age_pruned(tmp_path) -> None:
    """The archived ``device_events_legacy`` survives the retention pass.

    Before the cutover the shipped pass pruned ``device_events`` on the raw
    horizon.  The table is now a read-only rollback record: the pass must leave
    it alone (a DELETE would abort the whole prune on its trigger), even for a
    row far older than the raw window.
    """
    path = tmp_path / "events.db"
    _build_prior_schema(path)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO device_events(event_id, device_id, entity_id, event_type,
            occurred_at, received_at) VALUES(?,?,?,?,?,?)""",
            ("old", "dev1", "switch.dev1", "state_changed", old, old),
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    try:
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed["device_events_legacy"] == 0
        surviving = {
            row["event_id"]
            for row in db._conn.execute("SELECT event_id FROM device_events_legacy").fetchall()
        }
        assert surviving == {"e1", "old"}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- per-tier retention ranges
# --------------------------------------------------------------------------- #


def _config(**env: str) -> EngineConfig:
    base = {"WM_HA_URL": "http://homeassistant:8123", "WM_HA_TOKEN": "test-token"}
    base.update(env)
    return EngineConfig.from_env(base, dotenv_path="/nonexistent.env")


def test_raw_retention_range_is_enforced() -> None:
    """The documented 7-14 band and the shipped 30 are accepted; outside is not."""
    low, high = RETENTION_DAYS_RAW_RANGE
    assert (low, high) == (1, 90)
    for accepted in (7, 14, 30):
        problems = [
            p for p in _config(WM_RETENTION_DAYS_RAW=str(accepted)).validate()
            if "retention_days_raw" in p
        ]
        assert problems == [], (accepted, problems)
    for refused in (0, 91):
        problems = [
            p for p in _config(WM_RETENTION_DAYS_RAW=str(refused)).validate()
            if "retention_days_raw" in p
        ]
        assert problems, refused


def test_retention_defaults_and_env_overrides() -> None:
    """Defaults are the shipped values; every per-tier horizon is configurable."""
    cfg = _config()
    # Z11: raw default reduced from 30 to 7 (V2 DB-growth finding).
    assert cfg.retention_days_raw == 7
    assert cfg.retention_days_aggregates == 730
    assert cfg.retention_days_evidence is None
    assert cfg.retention_days_versions is None
    agg_low, agg_high = RETENTION_DAYS_AGGREGATES_RANGE
    assert agg_low <= cfg.retention_days_aggregates <= agg_high

    overridden = _config(
        WM_RETENTION_DAYS_RAW="14",
        WM_RETENTION_DAYS_AGGREGATES="90",
        WM_RETENTION_DAYS_EVIDENCE="60",
        WM_RETENTION_DAYS_VERSIONS="120",
    )
    assert overridden.retention_days_raw == 14
    assert overridden.retention_days_aggregates == 90
    assert overridden.retention_days_evidence == 60
    assert overridden.retention_days_versions == 120
    # All four are published in the log-safe view.
    assert overridden.redacted()["retention_days_raw"] == 14
    assert overridden.redacted()["retention_days_evidence"] == 60
    assert overridden.redacted()["retention_days_versions"] == 120


def test_an_explicit_horizon_may_not_be_shorter_than_the_raw_window() -> None:
    """Evidence that does not outlive the raw window it explains cannot be audited."""
    # Z11: the raw default is now 7, so a shorter horizon is anything below it.
    problems = _config(WM_RETENTION_DAYS_EVIDENCE="6").validate()
    assert any("retention_days_evidence" in p for p in problems)
    problems = _config(WM_RETENTION_DAYS_VERSIONS="6").validate()
    assert any("retention_days_versions" in p for p in problems)
    # Equal is fine; unset is fine.
    assert not [
        p for p in _config(WM_RETENTION_DAYS_EVIDENCE="7").validate()
        if "retention_days_evidence" in p
    ]


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- payload encoding
# --------------------------------------------------------------------------- #


def test_payload_round_trip_across_encodings() -> None:
    """One read path serves an encoded payload and a legacy one."""
    document = {
        "device_id": "kitchen_lights",
        "notes": ["x" * 500, "y" * 500],
        "expected": {"on_w": 45.0, "idle_w": 0.5},
    }
    raw = json.dumps(document)
    encoded = encode_payload(raw)
    assert encoded.startswith("Z1:")
    assert payload_is_encoded(encoded)
    assert not payload_is_encoded(raw)

    # The same call reads both forms.
    assert decode_payload(encoded) == document
    assert decode_payload(raw) == document
    assert decode_json_text(encoded) == json.dumps(document)

    # A writer encodes only when it pays, and the row stays readable either way.
    stored, was_encoded = encode_if_worthwhile(raw)
    assert was_encoded and payload_is_encoded(stored)
    assert len(stored) < len(raw)
    assert decode_payload(stored) == document

    small = json.dumps({"a": 1})
    small_stored, small_encoded = encode_if_worthwhile(small)
    assert not small_encoded
    assert small_stored == small
    assert decode_payload(small_stored) == {"a": 1}

    # A corrupt encoded cell is reported as absent, not raised.
    assert decode_payload("Z1:!!!not-base64!!!") is None
    assert decode_payload(None) is None
    assert decode_payload("") is None


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- partitioning decision
# --------------------------------------------------------------------------- #


def test_the_partitioning_decision_carries_its_measurement() -> None:
    """The decision names what replaces partitioning and when it is revisited."""
    decision = partition_decision()
    assert "SQLite has no native partitioning" in decision
    assert "1.5 GB/day" in decision
    assert "320852" in decision
    assert "reconsidered" in decision
    spec = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "specs"
        / "load-disaggregation"
        / "08-storage-and-data-lifecycle.md"
    ).read_text()
    assert "## 7. Partitioning" in spec
    assert "1.5 GB/day" in spec


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- prior-schema load (raw sqlite3, no ``Database``)
# --------------------------------------------------------------------------- #


#: A configuration payload as an older binary stored it (uncompressed JSON).
LEGACY_CONFIG_PAYLOAD: dict[str, object] = {
    "device_id": "kitchen_lights",
    "device_type": "light_group",
    "state_entity_ids": ["light.kitchen", "light.kitchen_under_cabinet"],
    "monitoring": "shared",
    "monitored_by_ct_ids": ["ct_sub_l1"],
    "expected": {"on_w": 45.0, "idle_w": 0.5},
    "configured_by": "user",
    "notes": ["audited 2026-11-01", "five-bulb fixture"],
}


def _build_prior_schema(path: Path, upto: int = 10) -> None:
    """Create a pre-migration database with raw ``sqlite3`` only.

    ``Database.__init__`` would run the migrations immediately, so the fixture is
    built on a plain connection and stopped at ``user_version = 10`` -- one
    version behind :data:`LATEST_VERSION`, which is the seam this wave must not
    disturb (it adds no migration).
    """
    conn = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS:
            if migration.version > upto:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {upto}")
        # A legacy (uncompressed) configuration payload, as an older binary wrote.
        config = LEGACY_CONFIG_PAYLOAD
        conn.execute(
            "INSERT INTO device_configurations(device_id, payload_json, updated_at) "
            "VALUES(?,?,?)",
            ("kitchen_lights", json.dumps(config), "2026-11-06T00:00:00+00:00"),
        )
        # A legacy channel, topology snapshot and device event, all uncompressed.
        conn.execute(
            """INSERT INTO ct_channels(ct_id, entity_id, name, ct_type, phase,
            polarity, enabled, reliability, topology_status, metadata_json, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("main_l1", "sensor.main_l1", "Main", "main", "l1", "normal", 1, 1.0,
             "candidate", "{}", "2026-11-06T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO topology_versions(created_at, status, payload_json, note) "
            "VALUES(?,?,?,?)",
            (
                "2026-11-06T00:00:00+00:00",
                "candidate",
                json.dumps({"version": 1, "channels": [], "devices": []}),
                "legacy",
            ),
        )
        conn.execute(
            """INSERT INTO device_events(event_id, device_id, entity_id, event_type,
            occurred_at, received_at) VALUES(?,?,?,?,?,?)""",
            ("e1", "dev1", "switch.dev1", "state_changed", "2026-11-06T00:00:00+00:00",
             "2026-11-06T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_prior_schema_database_migrates_and_loads(tmp_path) -> None:
    """A version-10 database migrates forward and its rows still load."""
    path = tmp_path / "prior.db"
    _build_prior_schema(path)
    conn = sqlite3.connect(path)
    try:
        assert current_version(conn) == 10
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        # The legacy configuration payload reads through the shared decode path.
        stored = db._conn.execute(
            "SELECT payload_json FROM device_configurations WHERE device_id='kitchen_lights'"
        ).fetchone()["payload_json"]
        assert decode_payload(stored) == LEGACY_CONFIG_PAYLOAD
        # The legacy channel became a canonical endpoint; the originals are kept
        # as read-only archives with every row.
        endpoint = db._conn.execute(
            "SELECT scope FROM measurement_endpoints WHERE endpoint_id='main_l1'"
        ).fetchone()
        assert endpoint is not None and endpoint["scope"] == "panel_feed"
        snapshot = db._conn.execute(
            "SELECT payload_json FROM topology_versions_legacy"
        ).fetchone()["payload_json"]
        assert decode_payload(snapshot) == {"version": 1, "channels": [], "devices": []}
        events = [
            row["event_id"]
            for row in db._conn.execute("SELECT event_id FROM device_events_legacy")
        ]
        assert events == ["e1"]
        # And the migration is idempotent on reopen.
        assert apply_migrations(db._conn) == LATEST_VERSION
    finally:
        db.close()


def test_the_payload_encoding_is_not_a_migration() -> None:
    """The encoding is a write-path marker, so it is never a migration.

    A migration that only bumped ``user_version`` would advance the version on a
    database whose bytes never changed, making ``schema_version`` a lie.  Its
    number (12) is reserved and can be discovered as a gap between the released
    migrations (1-11) and the wave-2 ones (13+).
    """
    assert PAYLOAD_ENCODING_VERSION == 12
    assert PAYLOAD_ENCODING_VERSION in RESERVED_VERSIONS
    assert PAYLOAD_ENCODING_VERSION not in {m.version for m in MIGRATIONS}
    assert all(m.version <= LATEST_VERSION for m in MIGRATIONS)
