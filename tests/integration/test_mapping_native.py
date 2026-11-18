"""Integration coverage for EngineCore's native bulk ``mapping_matrix()``.

Commit b959997 made the production ``/mapping`` route resolve to
``EngineCore.mapping_matrix()``, which feeds a single bulk model read into the
shared read-time projection instead of the facade's per-device ``models()`` N+1.
This module pins the three guarantees that landed unverified:

* the response keeps the standard matrix shape (cells, coverage and the
  device/reader axes);
* the matrix performs exactly ONE bulk model read and never falls back to the
  per-device ``Database.active_model`` lookup (no N+1);
* the active-model provenance matches ``models()`` (registry-first) — the native
  projection is byte-identical to the fallback ``derive_mapping_matrix(core)``
  that reads through ``core.models()``.

``ModelRegistry.active()`` is itself DB-backed (it calls
``Database.active_model`` on every lookup and caches only *instantiated* models,
never the active row), and both paths deserialise the row with
``version_from_row(...).model_dump(mode="json")``.  The parity tests below make
that structural guarantee executable so a future source or dump-mode change
cannot silently diverge.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.api.facade import derive_mapping_matrix
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceModelParameters,
    DeviceModelVersion,
    DeviceTopology,
    ModelStatus,
    ModelType,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.models.registry import ModelRegistry
from wattmosaic_engine.models.serde import version_from_row
from wattmosaic_engine.storage.database import Database


def _build_core(tmp_path: Path) -> tuple[EngineCore, Database]:
    """A real EngineCore on a temp DB with one device on one branch CT.

    The registry is attached the way ``EngineCore.start()`` attaches the
    inference backend's registry, so ``models()`` takes its registry-first
    branch exactly as it does in production.
    """
    path = tmp_path / "mapping_native.db"
    db = Database(path)
    config = EngineConfig(database_path=str(path), ha_token="unused", api_token="token")
    core = EngineCore(config, db)
    core.model_registry = ModelRegistry(db)

    topology = Topology(
        version=1,
        status=TopologyStatus.CONFIRMED,
        channels=[
            CTChannel(
                ct_id="ct1",
                entity_id="sensor.ct1",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                device_id="dev1",
                enabled=True,
                topology_status=TopologyStatus.CONFIRMED,
            )
        ],
        devices=[
            DeviceTopology(
                device_id="dev1",
                branch_ct_ids=["ct1"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            )
        ],
    )
    db.save_topology(topology)
    core.topology_cache = db.load_topology()
    return core, db


def _save_active(db: Database, version: int, on_power_w: float) -> None:
    db.save_model_version(
        DeviceModelVersion(
            device_id="dev1",
            model_type=ModelType.BINARY,
            version=version,
            status=ModelStatus.ACTIVE,
            sample_count=100,
            training_quality=0.9,
            validation_metrics={"r2": 0.9, "mae_w": 5.0},
            parameters=DeviceModelParameters(
                on_power_w=on_power_w, standby_power_w=0.0, off_power_w=0.0
            ),
            confidence=0.8,
        ),
        activate=True,
    )


def test_native_mapping_matrix_shape_and_axes(tmp_path: Path) -> None:
    """The native method returns the standard matrix envelope and axes."""
    core, db = _build_core(tmp_path)
    try:
        _save_active(db, 1, 80.0)
        matrix = asyncio.run(core.mapping_matrix())

        assert {"cells", "coverage", "devices", "readers"} <= set(matrix)
        assert matrix["devices"] == ["dev1"]
        assert matrix["readers"] == ["ct1"]
        assert len(matrix["cells"]) == 1
        cell = matrix["cells"][0]
        assert cell["device_id"] == "dev1"
        assert cell["reader_id"] == "ct1"
        assert isinstance(matrix["coverage"], dict)
    finally:
        db.close()


def test_native_mapping_matrix_does_one_bulk_read_and_no_per_device_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly one bulk model read, zero per-device ``active_model`` calls.

    Patching ``_bulk_active_models`` with a counting wrapper proves the single
    bulk operation; making ``Database.active_model`` raise proves the native
    path never reintroduces the per-device N+1 even when a device is present.
    """
    core, db = _build_core(tmp_path)
    try:
        _save_active(db, 1, 80.0)

        real_bulk = core._bulk_active_models
        counts: dict[str, int] = {"bulk": 0}

        def counting_bulk() -> dict[str, dict[str, Any]]:
            counts["bulk"] += 1
            return real_bulk()

        monkeypatch.setattr(core, "_bulk_active_models", counting_bulk)

        def forbid_per_device(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("N+1: per-device Database.active_model was called")

        monkeypatch.setattr(db, "active_model", forbid_per_device)

        matrix = asyncio.run(core.mapping_matrix())

        assert counts["bulk"] == 1, f"expected one bulk read, got {counts['bulk']}"
        assert matrix["devices"] == ["dev1"]
    finally:
        db.close()


def test_native_mapping_matrix_matches_registry_first_fallback(tmp_path: Path) -> None:
    """Native projection is byte-identical to the ``models()``-based fallback.

    ``derive_mapping_matrix(core)`` calls ``core.models(device_id)`` per device,
    which serves the active model from the registry first.  If the bulk path's
    model provenance ever diverged (source priority or dump mode), this equality
    would break.
    """
    core, db = _build_core(tmp_path)
    try:
        _save_active(db, 1, 80.0)
        _save_active(db, 2, 160.0)  # promote a second version

        native = asyncio.run(core.mapping_matrix())
        fallback = asyncio.run(derive_mapping_matrix(core))

        assert native == fallback
        # The active version shown by models() is the promoted one.
        assert core.models("dev1")["active"]["version"] == 2
    finally:
        db.close()


def test_bulk_active_model_payload_matches_registry_active(tmp_path: Path) -> None:
    """DB bulk read and registry-first ``models()`` publish the same active dict.

    This is the direct provenance-parity assertion: the exact dict the bulk path
    feeds to ``resolve_model`` equals the ``active`` payload ``models()`` serves
    via ``_model_dump(registry.active(device_id))``.  A change in either source
    or serialisation mode fails here.
    """
    core, db = _build_core(tmp_path)
    try:
        _save_active(db, 1, 80.0)
        _save_active(db, 2, 160.0)

        from_registry = core.models("dev1")["active"]
        bulk_row = core._bulk_active_models()["dev1"]
        parsed = version_from_row(bulk_row)
        assert parsed is not None
        from_bulk = parsed.model_dump(mode="json")

        assert from_bulk == from_registry
        assert from_bulk["version"] == 2
        assert from_bulk["parameters"]["on_power_w"] == 160.0
    finally:
        db.close()


def test_bulk_read_ignores_a_non_active_candidate_row(tmp_path: Path) -> None:
    """A saved-but-not-activated candidate must not change either path's active.

    Regression guard for source/shape divergence: the bulk query joins on
    ``active_version`` (not merely ``created_at``), so a newer candidate row
    that was never promoted is invisible to both the bulk read and the
    registry-first ``models()``.
    """
    core, db = _build_core(tmp_path)
    try:
        _save_active(db, 1, 80.0)
        # Save version 2 without activating it: it is a candidate, not active.
        db.save_model_version(
            DeviceModelVersion(
                device_id="dev1",
                model_type=ModelType.BINARY,
                version=2,
                status=ModelStatus.PENDING,
                parameters=DeviceModelParameters(on_power_w=999.0),
            ),
            activate=False,
        )

        assert core.models("dev1")["active"]["version"] == 1
        bulk_row = core._bulk_active_models()["dev1"]
        assert int(bulk_row["version"]) == 1
    finally:
        db.close()
