"""The residual split must survive the SQLite round-trip, not just memory.

Release ``0.2.1-dev.12`` added ``Reconciliation.unmonitored_w`` and
``.meter_disagreement_w`` (contract 1.4.0).  The engine computed both correctly
— its own ``explanation`` said
``residual_includes_meter_disagreement:354.8W_of:120.8W`` — yet ``/snapshot``
published ``0.0`` for both on every cycle, so
``unmonitored_w + meter_disagreement_w != unknown_residual_w`` by the whole
residual.

The unit suite was green because every existing test asserted the *in-memory*
object returned by the disaggregation engine.  The loss happened in storage:

* ``reconciliation_results`` had no columns for the two fields (schema 6);
* ``Database.insert_reconciliation`` wrote an explicit column list without them;
* ``row_to_reconciliation`` never read them, so the Pydantic ``0.0`` defaults
  were served on the ``/snapshot`` read path.

These tests therefore exercise the round-trip — write through the real
``Database``, read back through the same function the API uses — because that is
the only layer that was broken.

Contract 1.5.0 then corrected the *arithmetic* (the spread is unbounded, the
residual is not) and added ``meter_spread_w`` / ``meter_spread_exceeds_residual``.
Those two columns live in the **same schema-7 migration**, because schema 7 has
never been deployed and both changes are one logical fix to one read path.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import Reconciliation
from wattmosaic_engine.main import row_to_reconciliation
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)

TS = datetime(2026, 11, 11, 19, 35, 32, tzinfo=UTC)

#: The live dev.12 shape: a ~672 W residual of which ~354.8 W is two meters
#: disagreeing on the same conductors and ~317.2 W is genuinely unmonitored.
LIVE_RESIDUAL_W = 672.0
LIVE_METER_DISAGREEMENT_W = 354.8
LIVE_UNMONITORED_W = 317.2
#: Two other ``Reconciliation`` fields that storage dropped the same way.
LIVE_CONFIGURED_PRIOR_W = 55.0
LIVE_UNCERTAINTY_W = 12.5


def _live_split() -> Reconciliation:
    return Reconciliation(
        timestamp=TS,
        topology_version=2,
        main_power_w=13_512.0,
        direct_measured_w=12_442.7,
        inferred_w=397.3,
        unknown_residual_w=LIVE_RESIDUAL_W,
        unmonitored_w=LIVE_UNMONITORED_W,
        meter_disagreement_w=LIVE_METER_DISAGREEMENT_W,
        meter_spread_w=LIVE_METER_DISAGREEMENT_W,
        meter_spread_exceeds_residual=False,
        configured_prior_w=LIVE_CONFIGURED_PRIOR_W,
        measurement_uncertainty_w=LIVE_UNCERTAINTY_W,
        topology_confirmed=True,
        reconciliation_error_w=26.088,
        reconciliation_error_ratio=0.00193,
        accounted_fraction=0.9502,
        quality_status="degraded",
        explanation=["residual_includes_meter_disagreement:354.8W_of:672.0W"],
    )


def test_round_trip_preserves_the_split_and_the_invariant(tmp_path: Path) -> None:
    """THE regression: the two fields must survive a real write/read cycle.

    Fails against pre-fix code with ``0.0`` for both fields and an invariant
    error equal to the whole residual.
    """
    db = Database(tmp_path / "split.db")
    try:
        db.insert_reconciliation(_live_split())
        stored = db.latest_reconciliation()
        assert stored is not None

        # The columns exist and hold the numbers that were written.
        assert stored["unmonitored_w"] == pytest.approx(LIVE_UNMONITORED_W)
        assert stored["meter_disagreement_w"] == pytest.approx(LIVE_METER_DISAGREEMENT_W)

        # And the function the API uses rebuilds the same model.
        rec = row_to_reconciliation(stored)
        assert rec.unmonitored_w == pytest.approx(LIVE_UNMONITORED_W)
        assert rec.meter_disagreement_w == pytest.approx(LIVE_METER_DISAGREEMENT_W)
        assert rec.unknown_residual_w == pytest.approx(LIVE_RESIDUAL_W)

        # The contract identity the acceptance gate asserts.
        assert (
            rec.unmonitored_w + rec.meter_disagreement_w - rec.unknown_residual_w
        ) == pytest.approx(0.0, abs=0.01)
    finally:
        db.close()


def test_round_trip_preserves_every_contract_field(tmp_path: Path) -> None:
    """The whole model must survive storage, not just the newest two fields.

    ``configured_prior_w`` (1.1.0), ``measurement_uncertainty_w`` (1.2.0) and
    ``topology_confirmed`` (1.3.0) had the *same* defect: no column, no INSERT
    entry, no reader.  A split-only fix would leave those silently zeroed, so
    the regression covers every field the table must carry.
    """
    db = Database(tmp_path / "all_fields.db")
    try:
        written = _live_split()
        db.insert_reconciliation(written)
        rec = row_to_reconciliation(db.latest_reconciliation())
        for name in (
            "main_power_w",
            "direct_measured_w",
            "inferred_w",
            "configured_prior_w",
            "unknown_residual_w",
            "unmonitored_w",
            "meter_disagreement_w",
            "meter_spread_w",
            "reconciliation_error_w",
            "accounted_fraction",
            "measurement_uncertainty_w",
        ):
            assert getattr(rec, name) == pytest.approx(getattr(written, name)), name
        assert rec.topology_confirmed is True
        assert rec.topology_version == written.topology_version
        assert rec.explanation == written.explanation
    finally:
        db.close()


def test_topology_confirmed_survives_as_a_boolean(tmp_path: Path) -> None:
    """SQLite stores ``0``/``1``; both directions must round-trip."""
    db = Database(tmp_path / "confirmed.db")
    try:
        for value in (True, False):
            db.insert_reconciliation(
                Reconciliation(timestamp=TS, topology_version=1, topology_confirmed=value)
            )
            assert row_to_reconciliation(db.latest_reconciliation()).topology_confirmed is value
    finally:
        db.close()


def test_snapshot_reads_the_split_from_the_database(tmp_path: Path) -> None:
    """``EngineCore.snapshot()`` is what ``GET /snapshot`` serves.

    It reads ``db.latest_reconciliation()`` — the DB path, not
    ``latest_reconciliation`` in memory — so this is where the silent zero was
    published to the HA integration and to diagnostics.
    """
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore

    db = Database(tmp_path / "snapshot.db")
    try:
        db.insert_reconciliation(_live_split())
        config = EngineConfig(database_path=str(tmp_path / "snapshot.db"), ha_token="not-used")
        core = EngineCore(config, db)

        snapshot = core.snapshot().model_dump(mode="json")
        recon = snapshot["reconciliation"]
        assert recon["unmonitored_w"] == pytest.approx(LIVE_UNMONITORED_W)
        assert recon["meter_disagreement_w"] == pytest.approx(LIVE_METER_DISAGREEMENT_W)
        assert recon["meter_spread_w"] == pytest.approx(LIVE_METER_DISAGREEMENT_W)
        assert recon["meter_spread_exceeds_residual"] is False
        assert recon["unknown_residual_w"] == pytest.approx(LIVE_RESIDUAL_W)
        assert (
            recon["unmonitored_w"] + recon["meter_disagreement_w"]
            - recon["unknown_residual_w"]
        ) == pytest.approx(0.0, abs=0.01)
    finally:
        db.close()


def test_snapshot_endpoint_serves_a_non_zero_split(tmp_path: Path) -> None:
    """End-to-end over HTTP: the JSON body the integration actually parses."""
    import aiohttp
    from wattmosaic_engine.api.server import APIServer
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore

    db = Database(tmp_path / "api.db")
    try:
        db.insert_reconciliation(_live_split())
        config = EngineConfig(database_path=str(tmp_path / "api.db"), ha_token="not-used")
        core = EngineCore(config, db)

        async def scenario() -> dict:
            server = APIServer(core, host="127.0.0.1", port=0, api_token="t")
            await server.start()
            try:
                async with aiohttp.ClientSession(headers={"X-API-Token": "t"}) as session:
                    url = f"http://127.0.0.1:{server.port}/api/v1/snapshot"
                    async with session.get(url) as resp:
                        assert resp.status == 200
                        body = json.loads(await resp.text())
                return body["reconciliation"]
            finally:
                await server.stop()

        recon = asyncio.run(scenario())
        assert recon["unmonitored_w"] != 0.0
        assert recon["meter_disagreement_w"] != 0.0
        assert (
            recon["unmonitored_w"] + recon["meter_disagreement_w"]
            - recon["unknown_residual_w"]
        ) == pytest.approx(0.0, abs=0.01)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Backward compatibility: rows written before schema 7
# --------------------------------------------------------------------------- #


def test_row_from_before_the_columns_reads_as_zero(tmp_path: Path) -> None:
    """A NULL split predates the field and degrades to the model default.

    ``Reconciliation`` declares ``float = 0.0``, so NULL must read as ``0.0``
    rather than raise or fabricate a non-zero decomposition.
    """
    db = Database(tmp_path / "legacy.db")
    try:
        db._conn.execute(
            """INSERT INTO reconciliation_results(timestamp, topology_version, main_power_w,
            l1_power_w, l2_power_w, direct_measured_w, inferred_w, model_estimated_w,
            unknown_residual_w, reconciliation_error_w, reconciliation_error_ratio,
            accounted_fraction, quality_status, explanation_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                TS.isoformat(), 1, 1000.0, None, None, 700.0, 0.0, 0.0,
                300.0, 0.0, 0.0, 0.7, "good", "[]",
            ),
        )
        row = db.latest_reconciliation()
        assert row is not None
        assert row["unmonitored_w"] is None
        assert row["meter_disagreement_w"] is None
        assert row["meter_spread_w"] is None

        rec = row_to_reconciliation(row)
        assert rec.unmonitored_w == 0.0
        assert rec.meter_disagreement_w == 0.0
        assert rec.meter_spread_w == 0.0
        assert rec.meter_spread_exceeds_residual is False
        assert rec.unknown_residual_w == pytest.approx(300.0)
    finally:
        db.close()


def test_null_split_is_ambiguous_but_never_a_crash(tmp_path: Path) -> None:
    """An entirely absent reconciliation still yields a valid default model."""
    rec = row_to_reconciliation(None)
    assert rec.unmonitored_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    rec = row_to_reconciliation({})
    assert rec.unmonitored_w == 0.0
    assert rec.meter_disagreement_w == 0.0


# --------------------------------------------------------------------------- #
# The migration itself
# --------------------------------------------------------------------------- #


def _columns(conn: sqlite3.Connection, table: str = "reconciliation_results") -> list[str]:
    """Column names of ``table``, duplicates included (a duplicate would be a bug)."""
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _build_schema_6(path: Path) -> None:
    """Create a database exactly as release 0.2.1-dev.12 left it (schema 6)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for migration in MIGRATIONS:
            if migration.version > 6:
                break
            for stmt in migration.statements:
                conn.execute(stmt)
            # ``apply`` is the schema-7 hook; older migration objects do not
            # have it, so the helper must tolerate its absence.
            hook = getattr(migration, "apply", None)
            if hook is not None:
                hook(conn)
        conn.execute("PRAGMA user_version = 6")
    finally:
        conn.close()


def test_migration_7_adds_the_split_columns_in_place(tmp_path: Path) -> None:
    """A real schema-6 DB upgrades additively; existing rows are untouched."""
    path = tmp_path / "prod_like.db"
    _build_schema_6(path)

    # A reconciliation row written by the old code: no split columns existed.
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert current_version(conn) == 6
        assert "unmonitored_w" not in _columns(conn)
        conn.execute(
            """INSERT INTO reconciliation_results(timestamp, topology_version, main_power_w,
            l1_power_w, l2_power_w, direct_measured_w, inferred_w, model_estimated_w,
            unknown_residual_w, reconciliation_error_w, reconciliation_error_ratio,
            accounted_fraction, quality_status, explanation_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                TS.isoformat(), 1, 2_000.0, None, None, 1_400.0, 0.0, 0.0,
                600.0, 0.0, 0.0, 0.7, "good", "[]",
            ),
        )
        old_id = conn.execute("SELECT id FROM reconciliation_results").fetchone()[0]
    finally:
        conn.close()

    # Opening with the current code applies migration 7.
    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION == 7
        cols = _columns(db._conn)
        assert {
            "unmonitored_w",
            "meter_disagreement_w",
            "meter_spread_w",
            "meter_spread_exceeds_residual",
            "configured_prior_w",
            "measurement_uncertainty_w",
            "topology_confirmed",
        } <= set(cols)

        # The old row survived and reads its split as NULL -> 0.0.
        old = db._conn.execute(
            "SELECT * FROM reconciliation_results WHERE id=?", (old_id,)
        ).fetchone()
        assert dict(old)["unknown_residual_w"] == pytest.approx(600.0)
        assert dict(old)["unmonitored_w"] is None
        assert dict(old)["topology_confirmed"] is None
        old_model = row_to_reconciliation(dict(old))
        assert old_model.unmonitored_w == 0.0
        assert old_model.topology_confirmed is False

        # And a new row round-trips with a real split and its spread.
        db.insert_reconciliation(_live_split())
        rec = row_to_reconciliation(db.latest_reconciliation())
        assert rec.unmonitored_w == pytest.approx(LIVE_UNMONITORED_W)
        assert rec.meter_disagreement_w == pytest.approx(LIVE_METER_DISAGREEMENT_W)
        assert rec.meter_spread_w == pytest.approx(LIVE_METER_DISAGREEMENT_W)
    finally:
        db.close()


def test_migrations_are_idempotent_across_reopens(tmp_path: Path) -> None:
    """Running the migration set twice must not fail on the columns it added."""
    path = tmp_path / "twice.db"
    first = Database(path)
    try:
        assert first.schema_version == 7
    finally:
        first.close()

    second = Database(path)
    try:
        # apply_migrations is a no-op at the current version ...
        assert apply_migrations(second._conn) == 7
        # ... and re-running the version-7 body explicitly is still safe.
        for migration in MIGRATIONS:
            if migration.version == 7:
                hook = getattr(migration, "apply", None)
                assert hook is not None
                hook(second._conn)
        cols = _columns(second._conn)
        assert "unmonitored_w" in cols
        assert "meter_disagreement_w" in cols
        # The table still works after the redundant run.
        second.insert_reconciliation(_live_split())
        assert row_to_reconciliation(
            second.latest_reconciliation()
        ).meter_disagreement_w == pytest.approx(LIVE_METER_DISAGREEMENT_W)
    finally:
        second.close()


def test_migration_7_noops_when_create_table_already_has_the_columns(tmp_path: Path) -> None:
    """A fresh DB whose ``CREATE TABLE`` already names the columns must not fail.

    This is the interrupted-migration / hand-built-DB case: the column exists
    but ``user_version`` is still 6.  ``ALTER TABLE ADD COLUMN`` would raise
    ``duplicate column name`` without the ``PRAGMA table_info`` guard.
    """
    path = tmp_path / "prepopulated.db"
    _build_schema_6(path)

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("ALTER TABLE reconciliation_results ADD COLUMN unmonitored_w REAL")
        conn.execute(
            "ALTER TABLE reconciliation_results ADD COLUMN meter_disagreement_w REAL"
        )
        conn.execute("ALTER TABLE reconciliation_results ADD COLUMN meter_spread_w REAL")
        conn.execute(
            "ALTER TABLE reconciliation_results ADD COLUMN meter_spread_exceeds_residual INTEGER"
        )
        assert current_version(conn) == 6
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == 7
        cols = [
            row[1]
            for row in db._conn.execute("PRAGMA table_info(reconciliation_results)")
        ]
        assert cols.count("unmonitored_w") == 1
        assert cols.count("meter_disagreement_w") == 1
        assert cols.count("meter_spread_w") == 1
        assert cols.count("meter_spread_exceeds_residual") == 1
        db.insert_reconciliation(_live_split())
        assert row_to_reconciliation(db.latest_reconciliation()).unmonitored_w == pytest.approx(
            LIVE_UNMONITORED_W
        )
    finally:
        db.close()


def test_latest_schema_version_is_7() -> None:
    """The residual split is the first schema bump since 0.1.6's schema 6."""
    assert LATEST_VERSION == 7
    assert [m.version for m in MIGRATIONS] == list(range(1, 8))


# --------------------------------------------------------------------------- #
# The engine-side decomposition, now that it is additive (was xfail here)
# --------------------------------------------------------------------------- #


def test_engine_split_is_additive_when_spread_exceeds_residual() -> None:
    """The split adds up even when the meters disagree by more than the residual.

    This used to be a strict ``xfail``: the engine reported the *unbounded*
    meter-to-meter spread as a part of a bounded residual, so
    ``unmonitored_w + meter_disagreement_w`` overshot
    ``unknown_residual_w`` whenever the instruments disagreed by more than the
    whole gap.  Contract 1.5.0 clips the spread's *contribution* into the
    residual and reports the spread separately, so the identity now holds for
    every input shape.
    """
    from tests.unit.test_reconciliation_split import _aliased_topology, _infer, _window

    # canonical 1000 W, alias 600 W -> spread 400 W; heater 900 W leaves a
    # 100 W residual, so the spread exceeds it by 300 W.
    rec = _infer(
        _aliased_topology(),
        _window(
            {
                "load_manager_l1": 1000.0,
                "load_manager_l2": 0.0,
                "main_panel_l1": 600.0,
                "main_panel_l2": 0.0,
                "ct_heater": 900.0,
            }
        ),
    ).reconciliation
    assert rec.unknown_residual_w == pytest.approx(100.0)
    # The honest, unbounded diagnostic is still reported in full ...
    assert rec.meter_spread_w == pytest.approx(400.0)
    # ... while its contribution to a 100 W residual can only be 100 W.
    assert rec.meter_disagreement_w == pytest.approx(100.0)
    assert rec.unmonitored_w == pytest.approx(0.0, abs=1e-9)
    assert rec.meter_spread_exceeds_residual is True
    assert (
        rec.unmonitored_w + rec.meter_disagreement_w - rec.unknown_residual_w
    ) == pytest.approx(0.0, abs=0.01)


def test_meter_spread_survives_the_round_trip(tmp_path: Path) -> None:
    """Contract 1.5.0's spread and flag must persist like the rest of the split.

    The 1.4.0 lane added the split columns; the spread is a *different*
    quantity (unbounded, diagnostic only) and needs its own column, otherwise
    ``meter_spread_w`` reads back as ``0.0`` and the operator is told the
    meters agree when they do not.
    """
    db = Database(tmp_path / "spread.db")
    try:
        written = Reconciliation(
            timestamp=TS,
            topology_version=2,
            main_power_w=1000.0,
            direct_measured_w=833.6,
            unknown_residual_w=166.4,
            unmonitored_w=0.0,
            meter_disagreement_w=166.4,
            meter_spread_w=348.9,
            meter_spread_exceeds_residual=True,
        )
        db.insert_reconciliation(written)
        stored = db.latest_reconciliation()
        assert stored is not None
        assert stored["meter_spread_w"] == pytest.approx(348.9)
        assert stored["meter_spread_exceeds_residual"] == 1

        rec = row_to_reconciliation(stored)
        assert rec.meter_spread_w == pytest.approx(348.9)
        assert rec.meter_spread_exceeds_residual is True
        assert rec.meter_disagreement_w == pytest.approx(166.4)
        assert rec.unknown_residual_w == pytest.approx(166.4)
    finally:
        db.close()
