"""Z11 — age retention for the derived tables (V2 DB-growth finding).

The verification report (§6) measured ~69 MB/day of unbounded growth in
``inference_snapshots``, ``event_inference_records``, ``ha_events_v3``,
``event_groups`` (+ members) and ``reader_residual_series`` (≈25 GB/year), and
a 30-day raw-sample default implying 21-28 GB steady state.  This module pins:

* the shared decision layer prunes old derived rows on independent horizons;
* provenance wins over age: a row cited by the current snapshot, a non-finalized
  group (or a group with a newer revision), and an active record are kept;
* pruning is chunked and honours the DEC-37 CPU budget;
* config validation (new settings + 7-day raw default) and the add-on/engine
  ``retention_days_raw`` bounds parity.

The decision layer is driven by an injected ``conn`` and ``now``; integration
checks run it against a real :class:`~wattmosaic_engine.storage.database.Database`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from wattmosaic_engine.config.settings import (
    RETENTION_DAYS_DERIVED_RANGE,
    RETENTION_DAYS_RAW_RANGE,
    EngineConfig,
)
from wattmosaic_engine.storage.migrations import LATEST_VERSION
from wattmosaic_engine.storage.tiers import (
    DEFAULT_DERIVED_RETENTION_DAYS,
    DERIVED_PRUNE_CHUNK_SIZE,
    DERIVED_RETENTION_TABLES,
    apply_derived_retention,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


def _iso(at: datetime) -> str:
    return at.isoformat()


def _snapshot_row(conn: sqlite3.Connection, snapshot_id: str, at: datetime, payload: dict) -> int:
    cur = conn.execute(
        """INSERT INTO inference_snapshots(
            snapshot_id, observed_at, computed_at, topology_version_id,
            mapping_version_id, model_set_id, content_hash, payload_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            snapshot_id,
            _iso(at),
            _iso(at),
            "topo-1",
            "map-1",
            "models-1",
            f"hash-{snapshot_id}",
            json.dumps(payload),
            _iso(at),
        ),
    )
    return int(cur.lastrowid)


def _group_row(
    conn: sqlite3.Connection,
    group_id: str,
    revision: int,
    at: datetime,
    status: str = "confirmed",
    closed_at: datetime | None = None,
) -> None:
    conn.execute(
        """INSERT INTO event_groups(
            group_id, revision, status, opened_at, closed_at, versions_json
        ) VALUES(?,?,?,?,?,?)""",
        (group_id, revision, status, _iso(at), _iso(closed_at) if closed_at else None, "{}"),
    )
    conn.execute(
        """INSERT INTO event_group_members(group_id, revision, subject_kind, subject_id)
        VALUES(?,?,?,?)""",
        (group_id, revision, "device", f"dev-{group_id}"),
    )


def _record_row(
    conn: sqlite3.Connection, record_id: str, at: datetime, previous: str | None = None
) -> None:
    conn.execute(
        """INSERT INTO event_inference_records(
            record_id, event_group_id, interpretation_revision, previous_record_id,
            result_json, reason, created_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (record_id, f"group-{record_id}", 1, previous, "{}", "test", _iso(at)),
    )


def _ha_event_row(conn: sqlite3.Connection, event_id: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO ha_events_v3(event_id, entity_id, observed_at, received_at) "
        "VALUES(?,?,?,?)",
        (event_id, "switch.lamp", _iso(at), _iso(at)),
    )


def _residual_row(conn: sqlite3.Connection, reader_id: str, at: datetime) -> None:
    conn.execute(
        """INSERT INTO reader_residual_series(
            reader_id, at, residual_w, topology_version_id, basis
        ) VALUES(?,?,?,?,?)""",
        (reader_id, _iso(at), 5.0, "topo-1", "DELTA"),
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    from wattmosaic_engine.storage.database import Database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "derived_retention.db")
        try:
            yield db._conn
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Chunked pruning of old rows
# --------------------------------------------------------------------------- #


def test_prunes_old_derived_rows(conn: sqlite3.Connection) -> None:
    """Every derived table drops rows older than its horizon."""
    old = NOW - timedelta(days=200)
    _snapshot_row(conn, "snap-old", old, {})
    _record_row(conn, "rec-old", old)
    _ha_event_row(conn, "evt-old", old)
    _group_row(conn, "grp-old", 1, old, closed_at=old + timedelta(seconds=10))
    _residual_row(conn, "Main", old)
    # A recent snapshot must exist so "snap-old" is not the current one.
    _snapshot_row(conn, "snap-current", NOW, {})

    counts = apply_derived_retention(conn, now=NOW)
    assert counts["inference_snapshots"] == 1
    assert counts["event_inference_records"] == 1
    assert counts["ha_events_v3"] == 1
    assert counts["event_groups"] == 1  # member count is folded into the group
    assert counts["reader_residual_series"] == 1
    assert conn.execute("SELECT COUNT(*) FROM inference_snapshots WHERE snapshot_id='snap-old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_inference_records WHERE record_id='rec-old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ha_events_v3 WHERE event_id='evt-old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_groups WHERE group_id='grp-old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_group_members WHERE group_id='grp-old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reader_residual_series WHERE reader_id='Main'").fetchone()[0] == 0


def test_recent_rows_are_kept(conn: sqlite3.Connection) -> None:
    """Rows inside every horizon survive."""
    recent = NOW - timedelta(days=2)
    _snapshot_row(conn, "snap-new", recent, {})
    _record_row(conn, "rec-new", recent)
    _ha_event_row(conn, "evt-new", recent)
    _group_row(conn, "grp-new", 1, recent, closed_at=recent + timedelta(seconds=10))
    _residual_row(conn, "Main", recent)
    counts = apply_derived_retention(conn, now=NOW)
    assert all(v == 0 for v in counts.values())
    assert conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_group_members").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Provenance wins over age
# --------------------------------------------------------------------------- #


def test_current_snapshot_is_never_pruned(conn: sqlite3.Connection) -> None:
    """The newest snapshot is live even if its timestamp is very old."""
    very_old = NOW - timedelta(days=800)
    _snapshot_row(conn, "snap-live", very_old, {})
    counts = apply_derived_retention(conn, now=NOW)
    assert counts["inference_snapshots"] == 0
    assert conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0] == 1


def test_snapshot_referenced_rows_are_kept(conn: sqlite3.Connection) -> None:
    """HA events and groups cited by the live snapshot outlive their horizon."""
    old = NOW - timedelta(days=200)
    # The current snapshot cites an old ha_event and an old event_group.
    _snapshot_row(
        conn,
        "snap-current",
        NOW,
        {
            "unresolved_events": ["evt-cited"],
            "group_estimates": [
                {"group_id": "grp-cited", "evidence_refs": []},
            ],
        },
    )
    _ha_event_row(conn, "evt-cited", old)
    _ha_event_row(conn, "evt-free", old)
    _group_row(conn, "grp-cited", 1, old, closed_at=old + timedelta(seconds=10))
    _group_row(conn, "grp-free", 1, old, closed_at=old + timedelta(seconds=10))

    counts = apply_derived_retention(conn, now=NOW)
    assert counts["ha_events_v3"] == 1  # only evt-free removed
    assert counts["event_groups"] == 1
    assert conn.execute("SELECT event_id FROM ha_events_v3").fetchall()[0][0] == "evt-cited"
    assert conn.execute("SELECT group_id FROM event_groups").fetchall()[0][0] == "grp-cited"
    assert conn.execute("SELECT group_id FROM event_group_members").fetchall()[0][0] == "grp-cited"


def test_non_finalized_group_and_members_are_kept(conn: sqlite3.Connection) -> None:
    """A PROVISIONAL/DETECTED group (no closed_at) is never pruned."""
    old = NOW - timedelta(days=400)
    _group_row(conn, "grp-live", 1, old, status="detected", closed_at=None)
    _group_row(conn, "grp-final", 1, old, status="confirmed", closed_at=old + timedelta(seconds=10))
    counts = apply_derived_retention(conn, now=NOW)
    assert counts["event_groups"] == 1
    remaining = {r[0] for r in conn.execute("SELECT group_id FROM event_groups").fetchall()}
    assert remaining == {"grp-live"}
    remaining_members = {r[0] for r in conn.execute("SELECT group_id FROM event_group_members").fetchall()}
    assert remaining_members == {"grp-live"}


def test_superseded_revision_protects_older_group(conn: sqlite3.Connection) -> None:
    """A group with a newer revision is immutable history, never deleted."""
    old = NOW - timedelta(days=400)
    _group_row(conn, "grp-rev", 1, old, closed_at=old + timedelta(seconds=10))
    _group_row(conn, "grp-rev", 2, NOW - timedelta(days=1), closed_at=None)
    counts = apply_derived_retention(conn, now=NOW)
    assert counts["event_groups"] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_groups WHERE group_id='grp-rev'").fetchone()[0] == 2


def test_referenced_record_is_kept(conn: sqlite3.Connection) -> None:
    """A record cited by a newer record (revision chain) is immutable history."""
    old = NOW - timedelta(days=400)
    _record_row(conn, "rec-previous", old)
    _record_row(conn, "rec-newer", NOW - timedelta(days=1), previous="rec-previous")
    counts = apply_derived_retention(conn, now=NOW)
    assert counts["event_inference_records"] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_inference_records").fetchone()[0] == 2


def test_snapshot_referenced_residual_series_are_kept(conn: sqlite3.Connection) -> None:
    """A reader whose residual the current snapshot publishes is never pruned."""
    old = NOW - timedelta(days=200)
    _snapshot_row(
        conn,
        "snap-current",
        NOW,
        {"residual_estimates": [{"reader_id": "Main", "residual_w": 5.0}], "group_estimates": []},
    )
    _residual_row(conn, "Main", old)
    _residual_row(conn, "Branch", old)
    counts = apply_derived_retention(conn, now=NOW)
    assert counts["reader_residual_series"] == 1  # only Branch pruned
    assert [r[0] for r in conn.execute("SELECT reader_id FROM reader_residual_series").fetchall()] == [
        "Main"
    ]


def test_provenance_sets_are_empty_on_fresh_db(conn: sqlite3.Connection) -> None:
    """No snapshot / groups / active records is a valid, empty protection set."""
    counts = apply_derived_retention(conn, now=NOW)
    assert set(counts) == {name for name, _col, _key in DERIVED_RETENTION_TABLES}
    assert all(v == 0 for v in counts.values())


# --------------------------------------------------------------------------- #
# Bounded chunks / DEC-37 budget
# --------------------------------------------------------------------------- #


def test_prune_is_chunked(conn: sqlite3.Connection) -> None:
    """One pass deletes at most `chunk_size` rows per table."""
    old = NOW - timedelta(days=200)
    for i in range(DERIVED_PRUNE_CHUNK_SIZE + 5):
        _ha_event_row(conn, f"evt-{i}", old)
    counts = apply_derived_retention(conn, now=NOW, chunk_size=DERIVED_PRUNE_CHUNK_SIZE)
    assert counts["ha_events_v3"] == DERIVED_PRUNE_CHUNK_SIZE
    remaining = conn.execute("SELECT COUNT(*) FROM ha_events_v3").fetchone()[0]
    assert remaining == 5
    # A second pass resumes where this one stopped.
    counts2 = apply_derived_retention(conn, now=NOW, chunk_size=DERIVED_PRUNE_CHUNK_SIZE)
    assert counts2["ha_events_v3"] == 5
    assert conn.execute("SELECT COUNT(*) FROM ha_events_v3").fetchone()[0] == 0


def test_prune_honours_a_spent_budget(conn: sqlite3.Connection) -> None:
    """A spent DEC-37 budget stops the pass without deleting anything."""
    old = NOW - timedelta(days=200)
    for i in range(5):
        _ha_event_row(conn, f"evt-{i}", old)

    class SpentBudget:
        def exceeded(self) -> bool:
            return True

    counts = apply_derived_retention(conn, now=NOW, budget=SpentBudget())
    assert counts["ha_events_v3"] == 0
    assert conn.execute("SELECT COUNT(*) FROM ha_events_v3").fetchone()[0] == 5


# --------------------------------------------------------------------------- #
# Per-table horizons
# --------------------------------------------------------------------------- #


def test_per_table_horizons_are_independent(conn: sqlite3.Connection) -> None:
    """A short residual horizon can prune while a long snapshot horizon keeps."""
    at = NOW - timedelta(days=60)
    _residual_row(conn, "Main", at)
    _snapshot_row(conn, "snap-60d", at, {})
    _snapshot_row(conn, "snap-current", NOW, {})
    counts = apply_derived_retention(
        conn,
        now=NOW,
        horizons={"reader_residual_series": 30, "inference_snapshots": 90},
    )
    assert counts["reader_residual_series"] == 1
    assert counts["inference_snapshots"] == 0
    assert DEFAULT_DERIVED_RETENTION_DAYS["reader_residual_series"] == 30
    assert DEFAULT_DERIVED_RETENTION_DAYS["inference_snapshots"] == 90


def test_latest_schema_still_applies(conn: sqlite3.Connection) -> None:
    """The decision layer runs against the latest schema."""
    from wattmosaic_engine.storage.migrations import current_version

    assert current_version(conn) == LATEST_VERSION


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


def _config(**env: str) -> EngineConfig:
    base = {"WM_HA_URL": "http://homeassistant:8123", "WM_HA_TOKEN": "test-token"}
    base.update(env)
    return EngineConfig.from_env(base, dotenv_path="/nonexistent.env")


def test_raw_retention_default_is_seven_days() -> None:
    assert EngineConfig().retention_days_raw == 7
    assert _config().retention_days_raw == 7
    assert _config().redacted()["retention_days_raw"] == 7


def test_derived_retention_defaults() -> None:
    cfg = _config()
    assert cfg.retention_days_snapshots == 90
    assert cfg.retention_days_records == 90
    assert cfg.retention_days_ha_events == 90
    assert cfg.retention_days_groups == 90
    assert cfg.retention_days_residual_series == 30


def test_derived_retention_env_overrides_and_validation() -> None:
    cfg = _config(
        WM_RETENTION_DAYS_SNAPSHOTS="120",
        WM_RETENTION_DAYS_RECORDS="60",
        WM_RETENTION_DAYS_HA_EVENTS="45",
        WM_RETENTION_DAYS_GROUPS="180",
        WM_RETENTION_DAYS_RESIDUAL_SERIES="14",
    )
    assert cfg.retention_days_snapshots == 120
    assert cfg.retention_days_records == 60
    assert cfg.retention_days_ha_events == 45
    assert cfg.retention_days_groups == 180
    assert cfg.retention_days_residual_series == 14
    assert not [p for p in cfg.validate() if "retention_days_" in p]
    assert cfg.redacted()["retention_days_snapshots"] == 120

    low, high = RETENTION_DAYS_DERIVED_RANGE
    bad = _config(WM_RETENTION_DAYS_SNAPSHOTS=str(high + 1))
    assert any("retention_days_snapshots" in p for p in bad.validate())
    bad = _config(WM_RETENTION_DAYS_GROUPS=str(low - 1))
    assert any("retention_days_groups" in p for p in bad.validate())


# --------------------------------------------------------------------------- #
# Add-on / engine bounds parity
# --------------------------------------------------------------------------- #


def test_addon_and_engine_raw_retention_bounds_match() -> None:
    """The App option must accept exactly what the engine validator accepts."""
    cfg = yaml.safe_load((REPO_ROOT / "addon" / "config.yaml").read_text(encoding="utf-8"))
    declaration = str(cfg["schema"]["retention_days_raw"])
    match = re.search(r"int\((\d+),\s*(\d+)\)", declaration)
    assert match is not None, f"unexpected schema declaration: {declaration!r}"
    addon_low, addon_high = int(match.group(1)), int(match.group(2))
    engine_low, engine_high = RETENTION_DAYS_RAW_RANGE
    assert (addon_low, addon_high) == (engine_low, engine_high)


def test_addon_raw_retention_default_matches_engine() -> None:
    cfg = yaml.safe_load((REPO_ROOT / "addon" / "config.yaml").read_text(encoding="utf-8"))
    run_sh = (REPO_ROOT / "addon" / "run.sh").read_text(encoding="utf-8")
    assert cfg["options"]["retention_days_raw"] == EngineConfig().retention_days_raw == 7
    assert "retention_days_raw|WM_RETENTION_DAYS_RAW|7" in run_sh


def test_engine_runtime_prunes_derived_tables(conn: sqlite3.Connection) -> None:
    """The real EngineCore background job drives the decision layer."""
    from wattmosaic_engine.main import EngineCore

    old = NOW - timedelta(days=200)
    _ha_event_row(conn, "evt-job", old)
    _snapshot_row(conn, "snap-current", NOW, {})
    # A minimal core: only config, db and _clock are used by _prune_derived.
    core = object.__new__(EngineCore)
    core.config = _config()
    core.db = SimpleNamespace(_conn=conn, _lock=threading.RLock())
    core._clock = lambda: NOW
    core._prune_derived()
    assert conn.execute("SELECT COUNT(*) FROM ha_events_v3 WHERE event_id='evt-job'").fetchone()[0] == 0
