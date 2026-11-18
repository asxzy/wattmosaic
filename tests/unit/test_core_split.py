"""The EngineCore / WattMosaicBackend split (X03) must not change the surface.

``main.py`` used to hold a 3 380-line ``EngineCore`` and ``integration.py`` a
1 112-line ``WattMosaicBackend``.  They are now compositions of responsibility
mixins under ``wattmosaic_engine/core/`` and ``wattmosaic_engine/backend/``, with
``integration.py`` kept as a re-export facade.

Two things are pinned here, because the split is explicitly a *no behaviour
change* refactor:

1. **The public surface survives.**  Every public attribute and method a caller
   could observe before the split still exists — on ``EngineCore`` and on the two
   modules themselves (``from wattmosaic_engine.main import _sample_grid`` and
   ``from wattmosaic_engine.integration import WattMosaicBackend`` still work).
   The lists below were captured from the pre-split files and are deliberately
   literal: a name dropped by a later edit fails here instead of in production.
2. **Time is injectable.**  ``run_inference_once`` used to call ``utcnow()``
   directly.  It now reads ``self._clock()``, so the timestamps it publishes come
   from the injected clock — the plumbing P6 and replay (X07) need.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    InferenceResult,
    Reconciliation,
    Source,
    Topology,
)
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "engine" / "wattmosaic_engine"

#: Public attributes and methods of ``EngineCore`` before the split, in name
#: order.  Captured from ``main.py`` at the parent of the X03 change.
PUBLIC_ENGINE_CORE_SURFACE = (
    "aligner",
    "bootstrap_topology",
    "circuits",
    "command",
    "component_status",
    "confidence_tiers",
    "config",
    "confirm_topology",
    "cycles",
    "db",
    "device",
    "device_levels",
    "device_states",
    "devices",
    "diagnostics",
    "discover_placements",
    "events_evicted_before_maturity",
    "events_ingested",
    "events_persisted",
    "export",
    "ha_device_registry",
    "hasource",
    "health",
    "inference_backend",
    "inference_backend_status",
    "inference_error",
    "inference_last_run_at",
    "inference_records",
    "isolation_status",
    "last_successful_processing_at",
    "latest_quality",
    "latest_reconciliation",
    "latest_results",
    "learning_active",
    "mapping_matrix",
    "maybe_bootstrap_topology",
    "metrics",
    "model_registry",
    "models",
    "models_summary",
    "nesting_candidates",
    "on_event",
    "on_sample",
    "panel_tree",
    "persist_queue",
    "persistence_errors",
    "power_events_not_state",
    "quality",
    "quality_engine",
    "quality_report",
    "relations",
    "residual_components",
    "run_inference_once",
    "runtime_errors",
    "runtime_stats",
    "samples_ingested",
    "samples_persisted",
    "set_topology",
    "snapshot",
    "start",
    "started_at",
    "started_wall",
    "status",
    "stop",
    "topology",
    "topology_bootstrap_error",
    "topology_cache",
    "unlinked_events_ignored",
    "verify_integrity",
)

#: Capture instant for the injected clock.  Deliberately not "now": a helper
#: that still called ``utcnow()`` would fail rather than accidentally match.
FIXED_NOW = datetime(2031, 5, 4, 3, 2, 1, tzinfo=UTC)


def _core(tmp_path: Path) -> tuple[EngineCore, Database]:
    db = Database(tmp_path / "split.db")
    config = EngineConfig(database_path=str(tmp_path / "split.db"), ha_token="not-used")
    return EngineCore(config, db, clock=lambda: FIXED_NOW), db


def _topology() -> Topology:
    return Topology(
        version=3,
        channels=[
            CTChannel(
                ct_id="ct_main",
                entity_id="sensor.mains_power",
                ct_type=CTType.MAIN,
                enabled=True,
            )
        ],
        devices=[DeviceTopology(device_id="lamp", name="Lamp")],
    )


class _StubBackend:
    """Minimal ``process()`` so a cycle runs without the model layer."""

    learning = None
    learning_active = False
    models = None
    registry = None

    def __init__(self, clock: datetime) -> None:
        self._now = clock

    async def process(self, window, *, device_states, topology, models=None, events=None):
        result = InferenceResult(
            device_id="lamp",
            timestamp=self._now,
            power_w=12.0,
            source=Source.MODEL_ESTIMATED,
            confidence=0.5,
            quality_status="good",
        )
        return {
            "results": [result],
            "reconciliation": Reconciliation(timestamp=self._now),
            "diagnostics": {},
        }


# --------------------------------------------------------------------------- #
# 1. The pre-split public surface still exists
# --------------------------------------------------------------------------- #


def test_every_public_engine_core_name_survives_the_split(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    try:
        missing = [name for name in PUBLIC_ENGINE_CORE_SURFACE if not hasattr(core, name)]
    finally:
        db.close()
    assert not missing, (
        "the EngineCore split dropped public names that callers can observe: "
        f"{missing}"
    )


def test_engine_core_is_composed_from_the_core_mixins() -> None:
    """The split is composition, not a copy: each responsibility is a base."""
    from wattmosaic_engine.core import (
        DiagnosticsApiMixin,
        DiscoveryApiMixin,
        MappingApiMixin,
        ModelsApiMixin,
        RuntimeMixin,
        SnapshotMixin,
        TopologyApiMixin,
    )

    expected = {
        RuntimeMixin,
        SnapshotMixin,
        TopologyApiMixin,
        MappingApiMixin,
        ModelsApiMixin,
        DiscoveryApiMixin,
        DiagnosticsApiMixin,
    }
    assert expected <= set(EngineCore.__mro__)

    # One module per responsibility, so later waves can own whole files.
    for module in (
        "core/runtime.py",
        "core/rows.py",
        "core/snapshot.py",
        "core/topology_api.py",
        "core/mapping_api.py",
        "core/models_api.py",
        "core/discovery_api.py",
        "core/diagnostics_api.py",
        "backend/__init__.py",
        "backend/inference.py",
        "backend/learning.py",
        "backend/audit.py",
        "backend/reanalysis.py",
    ):
        assert (PACKAGE_DIR / module).is_file(), f"{module} is missing from the split"


def test_integration_remains_a_reexport_facade() -> None:
    """``integration.py`` still answers the old import path."""
    from wattmosaic_engine import integration
    from wattmosaic_engine.backend import WattMosaicBackend as BackendClass
    from wattmosaic_engine.backend import build_backend as backend_factory

    assert integration.WattMosaicBackend is BackendClass
    assert integration.build_backend is backend_factory
    assert set(integration.__all__) >= {"BackendBatch", "WattMosaicBackend", "build_backend"}


#: Module-level names ``main.py`` defined before the split.  Moving them must not
#: break ``from wattmosaic_engine.main import <name>``.
MAIN_MODULE_LEVEL_NAMES = (
    "HISTORY_TIMEOUT_BASE_S",
    "HISTORY_TIMEOUT_PER_HOUR_S",
    "MAINTENANCE_INTERVAL_S",
    "NESTING_RESAMPLE_GRID",
    "SAMPLE_BATCH_SIZE",
    "SAMPLE_FLUSH_INTERVAL_S",
    "SUPERVISION_BACKOFF_BASE_S",
    "SUPERVISION_BACKOFF_MAX_S",
    "TOPOLOGY_BOOTSTRAP_WAIT_S",
    "TOPOLOGY_RETRY_INTERVAL_S",
    "ComponentStatus",
    "EngineCore",
    "InferenceBackendUnavailable",
    "JsonFormatter",
    "Supervisor",
    "_as_utc",
    "_event_time",
    "_forward_fill",
    "_is_numeric_state",
    "_json_list",
    "_json_obj",
    "_linkable_state_entities",
    "_prom_label",
    "_safe_float",
    "_safe_history_row",
    "_sample_grid",
    "_sampling_cadence_s",
    "build_parser",
    "configure_logging",
    "load_inference_backend",
    "log",
    "main",
    "row_to_inference_result",
    "row_to_reconciliation",
)

#: Same for the helpers ``integration.py`` defined at module level.
INTEGRATION_MODULE_LEVEL_NAMES = (
    "BackendBatch",
    "DEFAULT_EVENT_BUFFER",
    "DEFAULT_FLUSH_EVERY_CYCLES",
    "DEFAULT_REANALYSIS_HISTORY",
    "DEFAULT_TRAIN_INTERVAL_S",
    "RECOVERY_LOOKBACK_HOURS",
    "WattMosaicBackend",
    "_channel_type",
    "_event_attributes",
    "_event_entity",
    "_event_state",
    "_event_time",
    "_has_fresh_measurement",
    "_measurement_age",
    "_quality_text",
    "_source_text",
    "_stale_reason",
    "build_backend",
    "load_inference_backend",
    "log",
)


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("wattmosaic_engine.main", MAIN_MODULE_LEVEL_NAMES),
        ("wattmosaic_engine.integration", INTEGRATION_MODULE_LEVEL_NAMES),
    ],
)
def test_module_level_names_still_import_from_their_old_path(
    module_name: str, names: tuple[str, ...]
) -> None:
    """Callers import helpers *and* classes from these two modules by name."""
    module = importlib.import_module(module_name)
    missing = [name for name in names if not hasattr(module, name)]
    assert not missing, f"{module_name} no longer defines {missing}"


# --------------------------------------------------------------------------- #
# 2. ``run_inference_once`` timestamps come from the injected clock
# --------------------------------------------------------------------------- #


def test_run_inference_once_uses_the_injected_clock(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    try:
        db.save_topology(_topology())
        core.topology_cache = db.load_topology()
        core.inference_backend = _StubBackend(FIXED_NOW)

        summary = asyncio.run(core.run_inference_once())
        snapshot_at = core.snapshot().timestamp
    finally:
        db.close()

    assert summary["results"] == 1
    assert core.inference_last_run_at == FIXED_NOW
    assert core.last_successful_processing_at == FIXED_NOW
    # The constructor timestamp and the snapshot timestamp share the same source.
    assert core.started_wall == FIXED_NOW
    assert snapshot_at == FIXED_NOW
    assert all(
        result.timestamp == FIXED_NOW for result in core.latest_results.values()
    )


def test_a_missing_topology_does_not_consume_the_clock(tmp_path: Path) -> None:
    """The skipped path never timestamps anything, so no clock call is observed."""
    calls: list[datetime] = []

    def clock() -> datetime:
        calls.append(FIXED_NOW)
        return FIXED_NOW

    db = Database(tmp_path / "split.db")
    config = EngineConfig(database_path=str(tmp_path / "split.db"), ha_token="not-used")
    core = EngineCore(config, db, clock=clock)
    try:
        before = len(calls)
        summary = asyncio.run(core.run_inference_once())
        assert summary == {"skipped": "no topology configured"}
        assert len(calls) == before, "the skip path must not stamp a time"
    finally:
        db.close()


def test_wattmosaic_backend_accepts_a_clock() -> None:
    """Clock injection is plumbing for the backend too (P6, X07 replay)."""
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(database=None, learning_enabled=False, clock=lambda: FIXED_NOW)
    try:
        assert backend._clock() == FIXED_NOW
        # The failure path in ``process`` is the one timestamp it owns.
        batch = backend.process(
            None,
            device_states={},
            topology=_topology(),
        )
    finally:
        backend.close()
    assert batch.reconciliation.timestamp == FIXED_NOW


def test_backend_defaults_to_wall_clock() -> None:
    from wattmosaic_engine.contracts import utcnow
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(database=None, learning_enabled=False)
    try:
        assert backend._clock is utcnow
    finally:
        backend.close()


@pytest.mark.parametrize(
    "name",
    ["backend/inference.py", "backend/learning.py", "backend/reanalysis.py"],
)
def test_moved_backend_modules_have_no_direct_wall_clock_calls(name: str) -> None:
    """``utcnow()`` in moved code became ``self._clock()`` (X03 step 3)."""
    source = (PACKAGE_DIR / name).read_text(encoding="utf-8")
    assert "utcnow()" not in source, f"{name} still reads the wall clock directly"


@pytest.mark.parametrize(
    "name",
    [
        "core/runtime.py",
        "core/snapshot.py",
        "core/topology_api.py",
        "core/models_api.py",
        "core/discovery_api.py",
        "core/diagnostics_api.py",
    ],
)
def test_moved_core_modules_have_no_direct_wall_clock_calls(name: str) -> None:
    source = (PACKAGE_DIR / name).read_text(encoding="utf-8")
    assert "utcnow()" not in source, f"{name} still reads the wall clock directly"
