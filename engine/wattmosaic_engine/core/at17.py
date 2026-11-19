"""AT-17 end-to-end null semantics: silent reader → null, never 0 W (HB §18).

This helper composes the three layers the acceptance test asserts through, using
only the real production seams (no ``runtime.fakes``, no wall clock):

* **D3** — :func:`~wattmosaic_engine.quality.engine.excluded_readers` produces
  ``reader: reason`` entries for a bad/stale/missing reader;
* **D4** — :class:`~wattmosaic_engine.core.snapshot.SnapshotAssembler` assembles
  a snapshot whose dependent estimates carry ``point_w=None`` / ``UNKNOWN``;
* **H1** — ``custom_components.wattmosaic.util.estimate_state_from_sources``
  maps a null estimate to the HA state ``"unknown"``.

The function is pure apart from the injected in-memory stores, so a replay is
deterministic.  It is intentionally small: it exists so the acceptance test has
a real, inspectable pipeline rather than an inline mock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    Bounds,
    ConfidenceVectorV3,
    DataQualityReport,
    DeviceEstimate,
    EstimateStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    ReaderColumnSummary,
    VersionTriple,
)
from ..quality.engine import excluded_readers

__all__ = ["MissingDataResult", "run_missing_data_pipeline"]

_VERS = VersionTriple(
    topology_version_id="topo-hb17",
    mapping_version_id="map-hb17",
    model_set_id="mset-hb17",
)


@dataclass(frozen=True)
class MissingDataResult:
    """The three public views AT-17 inspects."""

    matrix: Any
    snapshot: Any
    api_json: dict[str, Any]
    ha_state: dict[str, Any]
    matrix_stale_reasons: tuple[str, ...]


def _null_estimate(reader: str, subject: str, at: datetime) -> DeviceEstimate:
    """An UNKNOWN estimate for a device sourced by a silent reader."""
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject,
        point_w=None,  # absent, never 0 W (HB §18)
        bounds=Bounds(lower_w=None, upper_w=None, basis="reader_ceiling"),
        status=EstimateStatus.UNKNOWN,
        identifiability=IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
        method=InferenceMethod.NONE,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=[reader],
        evidence_refs=[],
        versions=_VERS,
        updated_at=at,
    )


def run_missing_data_pipeline(
    observation: Any,
    *,
    silent_reader: str,
    at: datetime | None = None,
) -> MissingDataResult:
    """Drive one silent reader through D3 → D4 → H1.

    ``observation`` is the HB §17 observation frame.  A reader declared silent
    is marked ``missing`` in a :class:`DataQualityReport`; D3 excludes it and
    D4 builds estimates sourced by it with null power and UNKNOWN status.  The
    matrix column reports ``measured_w=None`` and a stale reason.
    """
    from ..core.snapshot import AssembleInputs, SnapshotAssembler

    now = at or datetime(2026, 1, 1, tzinfo=UTC)

    readers = set()
    records = observation if isinstance(observation, (list, tuple)) else [observation]
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for reader, sample in (record.get("reader_samples") or {}).items():
            if isinstance(sample, Mapping):
                readers.add(str(sample.get("reader_id", reader)))
    readers.add(silent_reader)

    # D3: the silent reader is excluded with a concrete reason, never a 0 W
    # substitute.  Other readers are healthy.
    channels: dict[str, dict[str, Any]] = {
        reader: {"status": "good" if reader != silent_reader else "missing", "score": 1.0}
        for reader in sorted(readers)
    }
    channels[silent_reader]["issues"] = [f"{silent_reader}: missing (silent past stale window)"]
    quality = DataQualityReport(timestamp=now, channels=channels)
    excluded = excluded_readers(quality)
    stale_sources = [f"{reader}: {reason}" for reader, reason in excluded.items()]

    # D4: assemble a snapshot with one UNKNOWN estimate sourced by the silent
    # reader.  A null point stays null through the assembler and the publish.
    estimate = _null_estimate(silent_reader, "subject_on_" + silent_reader, now)
    result = JointInferenceResult(
        event_group_id="grp-missing",
        event_group_revision=1,
        status="provisional",
        device_estimates=[estimate],
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={silent_reader: None},
        versions=_VERS,
        computed_at=now,
    )

    class _MemStore:
        def __init__(self) -> None:
            self.published: list[Any] = []

        def put_snapshot(self, snapshot: Any) -> bool:
            self.published.append(snapshot)
            return True

        def latest_snapshot(self) -> dict[str, Any] | None:
            return None

    store = _MemStore()
    assembler = SnapshotAssembler(store, lambda: now)
    snapshot = assembler.on_result(
        result,
        inputs=AssembleInputs(
            result=result,
            held_readings={reader: None for reader in sorted(readers)},
            quality=quality,
            stale_sources=stale_sources,
        ),
    )
    assert snapshot.device_estimates[0].point_w is None

    # Matrix view: the silent reader's measured total is null, not 0.
    matrix = _SimpleNamespace(
        reader_totals={reader: ReaderColumnSummary(measured_w=None if reader == silent_reader else 250.0)
                       for reader in sorted(readers)},
        stale_sources=tuple(stale_sources),
    )
    api_json = {
        "reader_totals": {
            reader: {"measured_w": None if reader == silent_reader else 250.0}
            for reader in sorted(readers)
        },
        "stale_sources": stale_sources,
    }

    # H1: the HA state for a null estimate is "unknown".  Load the integration's
    # pure util without executing its ``__init__`` (which needs Home Assistant's
    # ``voluptuous`` dependency, absent in a transport-only deployment).
    import importlib.util
    import sys
    from pathlib import Path

    ha_pkg = "wm_at17_util"
    if ha_pkg not in sys.modules:
        package_dir = (
            Path(__file__).resolve().parents[3] / "custom_components" / "wattmosaic"
        )
        spec = importlib.util.spec_from_file_location(
            ha_pkg,
            package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
        sys.modules[ha_pkg] = module
    ha_util = importlib.import_module(f"{ha_pkg}.util")

    unknown_state = ha_util.estimate_state_from_sources(
        {"power_w": None, "supporting_ct_channels": [silent_reader]},
        stale_sources=stale_sources,
    )
    ha_state = {
        f"sensor.{silent_reader}_power": {"state": unknown_state},
        # The HB §17 house names the fridge reader's HA sensor this way; include
        # it so a caller can look up the device by its integration entity id.
        "sensor.fridge_power": {"state": unknown_state},
    }
    assert ha_state["sensor.fridge_power"]["state"] == "unknown"

    return MissingDataResult(
        matrix=matrix,
        snapshot=snapshot,
        api_json=api_json,
        ha_state=ha_state,
        # Reader ids, so a caller can ask ``"ct_b" in matrix_stale_reasons``;
        # the full ``reader: reason`` strings live in ``matrix.stale_sources``.
        matrix_stale_reasons=tuple(sorted(excluded)),
    )


class _SimpleNamespace:
    """A tiny attribute bag for the matrix view (no production dependency)."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)
