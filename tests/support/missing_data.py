"""AT-17 pipeline through the live runtime: a reader goes silent, nothing becomes 0 W.

Z3b replaced the engine's ``core/at17.py`` stand-in (a hand-built snapshot, a
fake matrix namespace and a hard-coded HA entity) with this test builder, which
only *drives* real code and reads its public outputs:

1. the HB §17 observation is replayed through ``EngineHarness`` (the real
   ``EngineCore`` three loops), with the user's declarations made up front;
2. from ``silent_from_s`` on, the silent reader's samples are simply not
   delivered (a CT that stops reporting) -- the runtime's own D3 quality and
   reader hold decide it is stale/missing;
3. ``snapshot`` is the published v3 ``InferenceSnapshot``; ``matrix`` is the
   real ``MatrixPayload`` from the mapping service (``core.matrix_payload()``)
   and ``api_json`` is exactly what ``GET /matrix`` serialises it to;
4. ``ha_state`` comes from the HA integration's pure modules
   (``util.coerce_v3_snapshot`` + ``entity_values.device_values``), keyed by the
   entity id Home Assistant derives for a ``has_entity_name`` sensor
   (``sensor.<device name>_<translated entity name>``).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
import types
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from wattmosaic_engine.api.server import _jsonable
from wattmosaic_engine.contracts.entities import PhysicalDevice

from tests.support.hb17_seed import start_hb17_harness

__all__ = ["MissingDataResult", "run_missing_reader"]

_PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
_PACKAGE_NAME = "wm_missing_data_under_test"


@dataclass(frozen=True)
class MissingDataResult:
    """The public views AT-17 inspects, all read from the live runtime."""

    matrix: Any
    snapshot: Any
    api_json: dict[str, Any]
    ha_state: dict[str, dict[str, Any]]
    matrix_stale_reasons: tuple[str, ...]


def _integration() -> tuple[types.ModuleType, types.ModuleType]:
    """The integration's pure ``util``/``entity_values`` without its HA ``__init__``."""
    if _PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PACKAGE_NAME,
            _PACKAGE_DIR / "__init__.py",
            submodule_search_locations=[str(_PACKAGE_DIR)],
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(_PACKAGE_DIR)]  # type: ignore[attr-defined]
        sys.modules[_PACKAGE_NAME] = module
    return (
        importlib.import_module(f"{_PACKAGE_NAME}.util"),
        importlib.import_module(f"{_PACKAGE_NAME}.entity_values"),
    )


def _slug(text: str) -> str:
    """Home Assistant's ``slugify`` for entity object ids (ASCII subset)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _sensor_names() -> dict[str, str]:
    translations = json.loads((_PACKAGE_DIR / "translations" / "en.json").read_text())
    return {key: str(spec["name"]) for key, spec in translations["entity"]["sensor"].items()}


def _ha_state(snapshot: Any, devices: Mapping[str, str], now: Any, stale_after_s: float) -> dict:
    util, entity_values = _integration()
    const = importlib.import_module(f"{_PACKAGE_NAME}.const")
    coerced = util.coerce_v3_snapshot(snapshot.model_dump(mode="json"))
    names = _sensor_names()
    states: dict[str, dict[str, Any]] = {}
    for device_id, name in devices.items():
        values = entity_values.device_values(
            coerced["results_by_id"].get(device_id),
            engine_available=True,
            now=now,
            stale_after=stale_after_s,
        )
        for key in const.DEVICE_SENSOR_KEYS:
            if key not in values or key not in names:
                continue
            if values.get("available") is False:
                state: Any = "unavailable"
            else:
                state = "unknown" if values[key] is None else values[key]
            states[f"sensor.{_slug(name)}_{_slug(names[key])}"] = {"state": state}
    return states


def run_missing_reader(
    hb17: Mapping[str, Any],
    *,
    silent_reader: str,
    silent_from_s: float,
    devices: Mapping[str, str],
    single_subject: Mapping[str, str],
    stale_after_s: float = 3600.0,
) -> MissingDataResult:
    """Replay ``hb17`` with ``silent_reader`` going quiet at ``silent_from_s``.

    ``devices`` is the declared ``device -> endpoint`` mapping (each device is
    also upserted as a user-declared physical device, named by its id);
    ``single_subject`` declares ``endpoint -> device`` single-subject scopes
    (DEC-05).  The stream after ``silent_from_s`` is played to its end, which is
    past the runtime's ``missing_after_s``.
    """
    house = hb17["house"]
    start = house.start_time
    records = []
    for record in hb17["observation"]:
        record = dict(record)
        if float(record.get("t_s") or 0.0) >= silent_from_s:
            record["reader_samples"] = {
                reader: sample
                for reader, sample in (record.get("reader_samples") or {}).items()
                if reader != silent_reader
            }
        records.append(record)

    def configure(core: Any) -> None:
        at = start - timedelta(days=1)
        for device_id in devices:
            core.db.repos.entities.upsert_device(
                PhysicalDevice(device_id=device_id, name=device_id, origin="user", created_at=at)
            )
        for endpoint_id, subject in single_subject.items():
            core.db.repos.entities.upsert_endpoint(
                {"endpoint_id": endpoint_id, "kind": "ct", "entity_id": None,
                 "scope": "single_subject", "scope_status": "confirmed",
                 "scope_subject_id": subject, "enabled": True, "created_at": at}
            )

    harness = start_hb17_harness(
        {"house": house, "observation": records}, devices=devices, configure=configure, run=False
    )
    try:
        harness.run(records)
        harness.settle()
        snapshot = harness.snapshot
        matrix = harness.core.matrix_payload()
        api_json = json.loads(json.dumps(_jsonable(matrix)))
        ha_state = _ha_state(snapshot, {d: d for d in devices}, harness.now, stale_after_s)
    finally:
        harness.close()
    reasons = tuple(sorted({str(entry).split(":", 1)[0].strip() for entry in matrix.stale_sources}))
    return MissingDataResult(
        matrix=matrix,
        snapshot=snapshot,
        api_json=api_json,
        ha_state=ha_state,
        matrix_stale_reasons=reasons,
    )
