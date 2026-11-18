"""Snapshot and read-time device views.

Owned by D4 after X03 (snapshot assembly and provenance)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ..api.facade import InvalidRequest, NotFound, confidence_tier_vocabulary
from ..contracts import InferenceResult, MonitoringLevel, SnapshotResponse, Topology
from ..electrical.panel_tree import TREE_VERSION, build_panel_tree
from .rows import row_to_inference_result, row_to_reconciliation

log = logging.getLogger("wattmosaic.main")


class SnapshotMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def snapshot(self) -> SnapshotResponse:
        rows = self.db.latest_inference_results()
        results = [row_to_inference_result(r) for r in rows]
        rec_row = self.db.latest_reconciliation()
        return SnapshotResponse(
            timestamp=self._clock(),
            reconciliation=row_to_reconciliation(rec_row, clock=self._clock),
            results=results,
            quality=self.latest_quality,
        )

    def devices(self) -> list[dict[str, Any]]:
        topology = self._active_topology()
        devices = topology.devices if topology else []
        configurations = self._effective_configurations(topology)
        recorded_results = self._latest_results_map()
        out: list[dict[str, Any]] = []
        for dev in devices:
            recorded = recorded_results.get(dev.device_id)
            out.append(
                {
                    "device_id": dev.device_id,
                    "name": dev.name,
                    "phase": dev.phase,
                    "double_pole": dev.double_pole,
                    "topology_status": dev.topology_status,
                    "branch_ct_ids": list(dev.branch_ct_ids),
                    "latest_result": (
                        recorded.model_dump(mode="json") if recorded is not None else None
                    ),
                    "confidence_tier": self._tier_text(recorded),
                    "configuration": self._configuration_payload(
                        configurations.get(dev.device_id)
                    ),
                    "model": self._active_model_summary(dev.device_id),
                }
            )
        return out

    def device(self, device_id: str) -> dict[str, Any]:
        topology = self._active_topology()
        dev = topology.device_map().get(device_id) if topology else None
        result = self.latest_results.get(device_id) or self._latest_result_from_db(device_id)
        if dev is None and result is None:
            raise NotFound(f"device {device_id!r} not found")
        configuration = None
        if dev is not None:
            configurations = self._effective_configurations(topology)
            configuration = self._configuration_payload(configurations.get(device_id))
        return {
            "device_id": device_id,
            "name": dev.name if dev else None,
            "topology": dev.model_dump(mode="json") if dev else None,
            "latest_result": result.model_dump(mode="json") if result else None,
            "confidence_tier": self._tier_text(result),
            "configuration": configuration,
            "model": self._active_model_summary(device_id),
            "history": self._device_history(device_id),
        }

    def _device_history(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent inference results for one device, newest first.

        ``Database.inference_history`` filters with ``timestamp >= since``.  The
        pre-1.6.0 call passed ``since=self._clock()``, which excludes every row that
        has already been written -- and a stored result is always in the past.
        The endpoint therefore always reported ``history: []`` while the table
        held rows (``docs/assessment_gap_analysis.md`` G17).  The lower bound is
        now an explicit, configured lookback.
        """
        try:
            since = self._clock() - timedelta(
                seconds=max(1.0, float(self.config.history_lookback_s))
            )
            rows = self.db.inference_history(device_id, since=since, limit=limit)
        except Exception:  # noqa: BLE001
            return []
        return [_safe_history_row(r) for r in rows]

    def _latest_results_map(self) -> dict[str, Any]:
        """Latest known result per device: in-memory first, stored rows second.

        ``latest_inference_results()`` is a whole-table GROUP BY over
        ``inference_results``.  Asking it once per device is an N+1 query:
        measured at ~670 ms for 28 devices against a 343 560-row table, which
        would stall the event loop on every caller.  Fetch it once, index it,
        and let the in-memory map win, so a result computed since the last
        persist is not shadowed by an older row.

        The database fallback matters for more than speed.  Right after a
        restart ``latest_results`` is empty until the first inference cycle, so
        a reader that consulted only the in-memory map would report every
        device as unknown/0 W while ``/devices`` reported real power -- the
        same install showing two different answers on two tabs.

        Cost note: ``latest_inference_results`` is a covering-index scan whose
        price grows with the number of retained rows (~24 ms warm and ~590 ms
        cold at the production 343k).  It is deliberately not bounded by
        timestamp: a device that stopped reporting a month ago still has a
        legitimate latest row, and dropping it would be a correctness change,
        not an optimisation.  Callers that can be slow therefore run this in a
        thread -- see the ``devices`` and ``topology_tree`` routes.
        """
        try:
            latest_rows = self.db.latest_inference_results()
        except Exception:  # noqa: BLE001
            # Not silent: right after a restart the in-memory map is empty, so a
            # swallowed error here makes every device read as unknown on both
            # ``/devices`` and ``/topology/tree`` with nothing in the log to
            # explain it.  Degrade, but say so.
            log.warning(
                "latest results unavailable; serving in-memory results only",
                exc_info=True,
            )
            latest_rows = []
        merged: dict[str, Any] = {}
        for row in latest_rows:
            device_key = row.get("device_id")
            if not device_key:
                continue
            try:
                merged[str(device_key)] = row_to_inference_result(row)
            except Exception:  # noqa: BLE001
                continue
        merged.update(self.latest_results)
        return merged

    def _latest_result_from_db(self, device_id: str) -> InferenceResult | None:
        try:
            rows = self.db.latest_inference_results()
        except Exception:  # noqa: BLE001
            return None
        for row in rows:
            if row.get("device_id") == device_id:
                try:
                    return row_to_inference_result(row)
                except Exception:  # noqa: BLE001
                    return None
        return None

    def _effective_configurations(
        self, topology: Topology | None
    ) -> dict[str, Any]:
        """Resolve every device's effective configuration once per call.

        The configuration layer is part of the optional inference backend, so the
        import is guarded: a plain engine (no disaggregation package) still serves
        ``/devices`` and simply omits the resolved configuration.
        """
        if topology is None:
            return {}
        try:
            from ..disaggregation.configuration import effective_configs

            return effective_configs(topology)
        except Exception:  # noqa: BLE001 - optional backend must not break reads
            return {}

    @staticmethod
    def _monitoring_text(value: Any) -> str:
        """Publish the monitoring axis only as a value the contract defines.

        Nothing produces the retired ``dedicated`` level any more, but the
        payload must not lean on that implicitly: a stored declaration that
        still carries it is read back from metadata, so an unrecognised value
        degrades to ``unknown`` instead of being served verbatim.
        """
        raw = getattr(value, "value", value)
        try:
            return MonitoringLevel(str(raw)).value
        except ValueError:
            return MonitoringLevel.UNKNOWN.value

    @classmethod
    def _configuration_payload(cls, config: Any) -> dict[str, Any] | None:
        """JSON-ready view of one :class:`EffectiveConfig` (enums as strings)."""
        if config is None:
            return None

        def _text(value: Any) -> Any:
            return getattr(value, "value", value)

        def _list(value: Any) -> list[Any]:
            return [str(item) for item in (value or ())]

        return {
            "device_type": _text(getattr(config, "device_type", None)),
            "connectivity": _text(getattr(config, "connectivity", None)),
            "monitoring": cls._monitoring_text(getattr(config, "monitoring", None)),
            "behavior": _text(getattr(config, "behavior", None)),
            "monitored_by_ct_ids": _list(getattr(config, "monitored_by_ct_ids", ())),
            "load_entity_ids": _list(getattr(config, "load_entity_ids", ())),
            "mode_entity_ids": _list(getattr(config, "mode_entity_ids", ())),
            "expected_on_w": getattr(config, "expected_on_w", None),
            "expected_standby_w": getattr(config, "expected_standby_w", None),
            "shared_unmodelled": bool(getattr(config, "shared_unmodelled", False)),
            "notes": _list(getattr(config, "notes", ())),
        }

    @staticmethod
    def _tier_text(result: InferenceResult | None) -> str | None:
        if result is None:
            return None
        tier = getattr(result, "confidence_tier", None)
        if tier is None:
            return None
        return str(getattr(tier, "value", tier))

    def confidence_tiers(self) -> dict[str, Any]:
        """Static ordered tier vocabulary (labels clients must not hard-code)."""
        return confidence_tier_vocabulary()

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        """Read-time panel → circuits → devices hierarchy (contract §1).

        Cheap by construction: it reads the already-cached topology and one
        indexed query for the latest result per device (the same ~25 ms query
        ``/devices`` makes), and it never scans CT samples.  The expensive
        nesting inference runs **only** when ``include_nesting`` is explicitly
        requested; even then it can only merge duplicate panels or add advisory
        suggestions, never change leaf counts.
        """
        topology = self._active_topology()
        if topology is None:
            return {
                "version": TREE_VERSION,
                "status": "candidate",
                "generated_at": self._clock().isoformat().replace("+00:00", "Z"),
                "topology_version": 0,
                "stats": {
                    "panels": 0,
                    "circuits": 0,
                    "devices": 0,
                    "unplaced_devices": 0,
                    "unplaced_circuits": 0,
                    "max_depth": 0,
                    "duplicate_feeds": 0,
                },
                "roots": [],
                "flat": {"panels": [], "circuits": [], "devices": []},
                "notes": ["no topology configured"],
            }
        nesting = self.nesting_candidates() if include_nesting else None
        return build_panel_tree(
            topology,
            latest_results=self._latest_results_map(),
            nesting=nesting,
        )

    def export(self, device_id: str) -> dict[str, Any]:
        try:
            data = self.db.export_device_data(device_id)
        except Exception as exc:  # noqa: BLE001
            raise InvalidRequest(f"export failed: {exc}") from exc
        if not data.get("inference_results") and not data.get("learning_samples") and not data.get("model_versions"):
            topology = self._active_topology()
            known = topology.device_map() if topology else {}
            if device_id not in known:
                raise NotFound(f"device {device_id!r} not found")
        return data


def _safe_history_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": row.get("timestamp"),
        "power_w": row.get("power_w"),
        "source": row.get("source"),
        "confidence": row.get("confidence"),
        "quality_status": row.get("quality_status"),
        "operating_stage": row.get("operating_stage"),
    }
