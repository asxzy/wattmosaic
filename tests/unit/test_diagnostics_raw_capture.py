"""Diagnostics surface for raw CT capture (T09, ADR-0042).

The support conversation must start from a known answer to "does this engine
keep raw CT readings?".  The answer is published in three places, and this
module pins all three:

* ``/api/v1/health``'s additive ``details`` object
  (:meth:`DiagnosticsApiMixin.health`);
* the diagnostics report's ``engine_health`` map
  (:func:`~wattmosaic_engine.diagnostics.report.collect_diagnostics`);
* the panel's Engine card (a static source check, the same style used by
  ``tests/unit/test_panel.py`` for shipped ES-module copy).

No contract change: ``details`` is schema-free, so an older integration keeps
parsing the health response unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import HealthResponse
from wattmosaic_engine.diagnostics.report import collect_diagnostics
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

REPO_ROOT = Path(__file__).resolve().parents[2]
VIEW_DIAGNOSTICS = REPO_ROOT / "addon" / "ui" / "view-diagnostics.js"


def _core(tmp_path: Path, *, raw_capture_days: int = 0) -> tuple[EngineCore, Database]:
    db = Database(tmp_path / "diagnostics.db")
    config = EngineConfig(
        database_path=str(tmp_path / "diagnostics.db"),
        ha_token="not-used",
        raw_capture_days=raw_capture_days,
    )
    return EngineCore(config, db), db


def _stub_core(health: HealthResponse) -> SimpleNamespace:
    """The lightest core a report can be built from: a health() callable."""
    return SimpleNamespace(health=lambda: health)


def test_health_details_report_capture_off(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    try:
        assert core.health().details["raw_capture"] == {"days": 0, "enabled": False}
    finally:
        db.close()


def test_health_details_report_capture_on(tmp_path: Path) -> None:
    core, db = _core(tmp_path, raw_capture_days=3)
    try:
        assert core.health().details["raw_capture"] == {"days": 3, "enabled": True}
    finally:
        db.close()


def test_report_engine_health_threads_raw_capture() -> None:
    present = HealthResponse(details={"raw_capture": {"days": 3, "enabled": True}})
    report = collect_diagnostics(core=_stub_core(present))
    assert report["engine_health"]["raw_capture"] == {"days": 3, "enabled": True}

    absent = HealthResponse(details={})
    report = collect_diagnostics(core=_stub_core(absent))
    assert report["engine_health"]["raw_capture"] is None


def test_panel_renders_raw_capture_row() -> None:
    source = VIEW_DIAGNOSTICS.read_text(encoding="utf-8")
    assert "'Raw CT capture'" in source
    assert "rawCaptureText(health.raw_capture)" in source
    # The three states the helper must render, pinned so an engine update cannot
    # silently degrade the row into an em dash on every installation.
    assert "'—'" in source
    assert "Off (history comes from the Home Assistant recorder)" in source
    assert "On, keeping ${days} day${days === 1 ? '' : 's'}" in source
