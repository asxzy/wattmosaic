"""The health probe must stay cheap enough to run every few seconds.

Defect #38: ``/api/v1/health`` ran ``PRAGMA integrity_check`` on every call.
That pragma reads the *whole* database file — measured at ~1.2 s on a 392 MB
database and ~6.8 s cold — and it runs synchronously on the event loop.

This only became fatal once the App health probe was fixed (defect #35): the
container ``HEALTHCHECK`` now succeeds and therefore polls ``/healthz`` every
5 s, and ``/healthz`` proxies ``/api/v1/health``.  Each poll blocked ingestion
for over a second, and on slow App storage it could exceed the 8 s probe
timeout, flipping a perfectly healthy engine to ``unhealthy`` and letting the
Supervisor watchdog restart it forever.

The fix separates the two concerns:

* ``Database.ping()`` — ``SELECT 1``, cheap, safe on a probe path.
* ``Database.integrity_check()`` — the full scan, called once at startup and
  from diagnostics, with the verdict cached for ``health()``.

Each test fails if the expensive path is put back on the probe.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database


def _core(tmp_path: Path) -> tuple[EngineCore, Database]:
    db = Database(tmp_path / "health.db")
    config = EngineConfig(database_path=str(tmp_path / "health.db"), ha_token="not-used")
    return EngineCore(config, db), db


def _traced_health(db: Database) -> tuple[object, list[str]]:
    """Run one health() call and return the SQL statements it executed."""
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    try:
        health = _core_health(db)
    finally:
        db._conn.set_trace_callback(None)
    return health, statements


def _core_health(db: Database) -> object:
    # ``health()`` is a method on the core that owns this database.
    core = getattr(db, "_test_core", None)
    assert core is not None
    return core.health()


def test_health_does_not_run_integrity_check(tmp_path: Path) -> None:
    """health() must never execute a whole-file integrity scan.

    Regression: it called ``db.integrity_check()``, which issues both
    ``PRAGMA integrity_check`` and ``PRAGMA quick_check``.
    """
    core, db = _core(tmp_path)
    try:
        db._test_core = core  # type: ignore[attr-defined]
        health, statements = _traced_health(db)
        assert health.database == "ok"  # type: ignore[attr-defined]
        lowered = " ".join(statements).lower()
        assert "integrity_check" not in lowered, (
            "health() executed PRAGMA integrity_check; that is a whole-file scan "
            f"and must not run on a probe path. Statements: {statements}"
        )
        assert "quick_check" not in lowered, (
            f"health() executed PRAGMA quick_check. Statements: {statements}"
        )
    finally:
        db.close()


def test_health_issues_a_cheap_probe(tmp_path: Path) -> None:
    """A positive control: health() still touches the database, cheaply."""
    core, db = _core(tmp_path)
    try:
        db._test_core = core  # type: ignore[attr-defined]
        _, statements = _traced_health(db)
        assert any("select 1" in s.lower() for s in statements), statements
    finally:
        db.close()


def test_ping_is_cheap_and_raises_on_dead_connection(tmp_path: Path) -> None:
    """ping() must be a liveness check, not a verification pass."""
    db = Database(tmp_path / "ping.db")
    try:
        db.ping()  # open connection: no error
        db.close()
        with pytest.raises(Exception):  # noqa: B017 - sqlite raises ProgrammingError
            db.ping()
    finally:
        pass


def test_verify_integrity_caches_a_failure(tmp_path: Path) -> None:
    """A failed startup scan must keep being reported by health().

    Otherwise a corrupt file would look healthy forever, because health() no
    longer rescans it.
    """
    core, db = _core(tmp_path)
    try:
        db._test_core = core  # type: ignore[attr-defined]
        core._integrity_ok = False
        core._integrity_error = "malformed database"
        health = core.health()
        assert health.database == "malformed database"
        assert health.status == "error"
    finally:
        db.close()


def test_verify_integrity_passes_on_a_healthy_database(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    try:
        report = core.verify_integrity()
        assert report["integrity"] == "ok"
        assert core._integrity_ok is True
        assert core.health().database == "ok"
    finally:
        db.close()
