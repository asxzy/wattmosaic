"""Integration: the guarded full ``reset-db`` backend endpoint.

The owner decision for this feature is "Everything (true fresh start)": a reset
must wipe *all* app data (topology, readers, models, inference history, events,
residuals, settings) back to a brand-new install, but only after a safety backup
and only with an explicit confirmation token.

These tests run a real ``aiohttp`` server around the real ``EngineCore`` (the
concrete facade) and a real, isolated SQLite database in ``tmp_path``.  The
repo's own ``data/wattmosaic.db`` is never touched.  They prove:

* an unconfirmed/bare call returns HTTP 400 and changes nothing;
* a confirmed call creates a timestamped backup, wipes all data, re-migrates to
  ``LATEST_VERSION`` and leaves a writable fresh schema;
* a second (confirmed) call still succeeds on an already-fresh DB (idempotent);
* auth is not weakened -- the sibling ``/commands`` token gate applies here too.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web as web
from wattmosaic_engine.api.server import RESET_DB_PATH, create_app
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import MeasurementType, ReaderMeasurement
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage import Database
from wattmosaic_engine.storage.migrations import LATEST_VERSION

TOKEN = "db-reset-token"


class _Harness:
    def __init__(self, db: Database, core: EngineCore, runner: web.AppRunner, base: str) -> None:
        self.db = db
        self.core = core
        self.runner = runner
        self.base = base

    async def aclose(self) -> None:
        await self.runner.cleanup()


async def _build_harness(tmp_path: Path) -> _Harness:
    db_path = tmp_path / "reset.db"
    db = Database(db_path)
    config = EngineConfig(database_path=str(db_path), ha_token="not-used", api_token=TOKEN)
    core = EngineCore(config, db)
    app = create_app(facade=core, api_token=TOKEN)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return _Harness(db, core, runner, f"http://127.0.0.1:{port}{RESET_DB_PATH}")


def _with_harness(tmp_path: Path, scenario: Any) -> Any:
    async def main() -> Any:
        harness = await _build_harness(tmp_path)
        try:
            return await scenario(harness)
        finally:
            await harness.aclose()
            harness.db.close()

    return asyncio.run(main())


def _seed_data(db: Database) -> None:
    """Write a marker into several app tables so the wipe is observable."""
    db.log_system_event("info", "test", "before-reset-marker")
    db.set_state("reset_marker", {"seeded": True})
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    db.repos.samples.insert_readings(
        [
            ReaderMeasurement(
                reader_id="ct_main",
                observed_at=stamp,
                received_at=stamp,
                active_power_w=100.0,
                phase="l1",
                measurement_type=MeasurementType.INSTANTANEOUS,
                calibration_version="cal-1",
                measurement_basis="ct_measured",
                sequence=1,
            )
        ]
    )


def _table_count(db: Database, table: str) -> int:
    with db._lock:
        return int(db._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


async def _post(session: aiohttp.ClientSession, url: str, body: Any, *, token: str | None = TOKEN) -> Any:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-API-Token"] = token
    async with session.post(url, data=json.dumps(body), headers=headers) as response:
        return response.status, await response.json()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_unconfirmed_call_is_rejected_and_changes_nothing(tmp_path: Path) -> None:
    """A bare/unconfirmed call returns 400 and leaves every byte of data intact."""

    async def scenario(harness: _Harness) -> Any:
        _seed_data(harness.db)
        before = {
            "events": _table_count(harness.db, "system_events"),
            "state": _table_count(harness.db, "engine_state"),
            "samples": _table_count(harness.db, "ct_samples"),
        }
        backups_before = list(tmp_path.glob("*.reset-backup.*"))
        async with aiohttp.ClientSession() as session:
            bare_status, bare_body = await _post(session, harness.base, {})
            wrong_status, wrong_body = await _post(
                session, harness.base, {"confirm": "YES"}
            )
        after = {
            "events": _table_count(harness.db, "system_events"),
            "state": _table_count(harness.db, "engine_state"),
            "samples": _table_count(harness.db, "ct_samples"),
        }
        backups_after = list(tmp_path.glob("*.reset-backup.*"))
        return (
            bare_status, bare_body, wrong_status, wrong_body,
            before, after, backups_before, backups_after,
        )

    (
        bare_status, bare_body, wrong_status, wrong_body,
        before, after, backups_before, backups_after,
    ) = _with_harness(tmp_path, scenario)

    assert bare_status == 400 and bare_body["error"] == "bad_request"
    assert wrong_status == 400 and wrong_body["error"] == "bad_request"
    assert before == after, "an unconfirmed call must not wipe anything"
    assert len(backups_after) == len(backups_before), "no backup on an unconfirmed call"


def test_confirmed_call_backs_up_wipes_and_remigrates(tmp_path: Path) -> None:
    """The core guarantee: backup first, then a fresh LATEST_VERSION schema."""

    async def scenario(harness: _Harness) -> Any:
        _seed_data(harness.db)
        seeded_events = _table_count(harness.db, "system_events")
        async with aiohttp.ClientSession() as session:
            status, body = await _post(session, harness.base, {"confirm": "RESET"})
        backup_path = Path(body["backup_path"])
        # All app-data tables empty except the one row migrations seed.
        counts = {t: _table_count(harness.db, t) for t in (
            "ct_samples", "ha_events_v3", "event_evidence", "inference_results",
            "system_events", "engine_state", "measurement_endpoints", "physical_devices",
            "reader_residual_series", "learning_observations", "power_model_versions_v3",
            # The read-only legacy archives come back empty on a fresh schema too.
            "ct_channels_legacy", "devices_legacy", "device_events_legacy",
            "residual_power_legacy",
        )}
        schema_version = harness.db.schema_version
        # A fresh record can be written through the very same object the engine
        # components hold, proving the connection was rebound in place.
        harness.db.log_system_event("info", "test", "after-reset-marker")
        fresh_write_visible = _table_count(harness.db, "system_events")
        # The backup is a self-contained copy of the pre-reset DB.
        backup_counts: dict[str, int] = {}
        import sqlite3

        with sqlite3.connect(backup_path) as conn:
            backup_counts["system_events"] = int(
                conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0]
            )
        return (
            status, body, seeded_events, backup_path, counts,
            schema_version, fresh_write_visible, backup_counts,
        )

    (
        status, body, seeded_events, backup_path, counts,
        schema_version, fresh_write_visible, backup_counts,
    ) = _with_harness(tmp_path, scenario)

    assert status == 200, body
    assert body["reset"] is True
    assert backup_path.exists(), "timestamped backup file must exist"
    assert ".reset-backup." in backup_path.name
    assert seeded_events > 0, "the seed must have written a marker"
    assert backup_counts["system_events"] >= seeded_events, (
        "the backup must contain the pre-reset data"
    )
    assert all(n == 0 for n in counts.values()), f"all app tables must be empty: {counts}"
    assert schema_version == LATEST_VERSION
    assert body["schema_version"] == LATEST_VERSION
    assert body["latest_schema_version"] == LATEST_VERSION
    assert fresh_write_visible == 1, "the fresh schema must accept new writes"


def test_second_confirmed_call_succeeds_on_fresh_db(tmp_path: Path) -> None:
    """Idempotent-safe: resetting an already-fresh DB still succeeds."""

    async def scenario(harness: _Harness) -> Any:
        async with aiohttp.ClientSession() as session:
            first_status, first_body = await _post(session, harness.base, {"confirm": "RESET"})
            second_status, second_body = await _post(session, harness.base, {"confirm": "RESET"})
        return (
            first_status, first_body, second_status, second_body,
            Path(first_body["backup_path"]), Path(second_body["backup_path"]),
            harness.db.schema_version,
        )

    (
        first_status, first_body, second_status, second_body,
        backup1, backup2, schema_version,
    ) = _with_harness(tmp_path, scenario)

    assert first_status == 200 and second_status == 200, (first_body, second_body)
    assert backup1.exists() and backup2.exists(), "each reset makes its own backup"
    assert backup1 != backup2, "backups must be timestamped, not overwritten"
    assert schema_version == LATEST_VERSION


def test_reset_route_requires_the_sibling_auth_token(tmp_path: Path) -> None:
    """Auth is not weakened: without the token the reset route is a 401."""

    async def scenario(harness: _Harness) -> Any:
        _seed_data(harness.db)
        before = _table_count(harness.db, "system_events")
        async with aiohttp.ClientSession() as session:
            no_token_status, _ = await _post(session, harness.base, {"confirm": "RESET"}, token=None)
            bad_token_status, _ = await _post(
                session, harness.base, {"confirm": "RESET"}, token="wrong"
            )
        after = _table_count(harness.db, "system_events")
        return no_token_status, bad_token_status, before, after

    no_token_status, bad_token_status, before, after = _with_harness(tmp_path, scenario)

    assert no_token_status == 401
    assert bad_token_status == 401
    assert before == after, "an unauthenticated call must not wipe anything"


def test_other_databases_are_unaffected(tmp_path: Path) -> None:
    """Isolation: another DB in the same temp dir keeps all of its data."""

    other_path = tmp_path / "other.db"
    other = Database(other_path)
    try:
        other.log_system_event("info", "test", "untouched")

        async def scenario(harness: _Harness) -> Any:
            async with aiohttp.ClientSession() as session:
                status, _ = await _post(session, harness.base, {"confirm": "RESET"})
            return status

        status = _with_harness(tmp_path, scenario)
        assert status == 200
        assert _table_count(other, "system_events") == 1, "a sibling DB must not be wiped"
    finally:
        other.close()
