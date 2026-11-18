"""Event-driven reanalysis of stored history.

Owned by L7 after X03."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contracts import InferenceResult, Topology

log = logging.getLogger("wattmosaic.integration")


class ReanalysisMixin:
    """Behaviour for one responsibility of :class:`WattMosaicBackend`."""

    def _newest_stored_event_at(self) -> datetime | None:
        """Newest ``occurred_at`` among stored device events in the lookback.

        Returns ``None`` when there is no stored evidence at all.  This is the
        signal that drives realtime reanalysis: a pass runs only when this value
        advances, i.e. when genuinely new evidence has been persisted.
        """
        database = self.database
        if database is None:
            return None
        now = self._clock()
        try:
            rows = database.recent_device_events(
                now - timedelta(seconds=self.reanalysis_lookback_s), limit=2000
            )
        except Exception as exc:  # noqa: BLE001 - reanalysis is an optimisation
            log.debug("reanalysis evidence query failed: %s", exc)
            return None
        newest: datetime | None = None
        for row in rows:
            at = _event_time(row)
            if at is not None and (newest is None or at > newest):
                newest = at
        return newest

    def _maybe_reanalyze(
        self, results: Sequence[InferenceResult], topology: Topology
    ) -> None:
        """Realtime, event-driven re-derivation from stored raw history.

        There is no 3600 s timer.  A pass fires as soon as new matured evidence
        exists — the newest stored device-event occurrence advanced past the
        ``_reanalysis_evidence_seen`` watermark — and does nothing on cycles with
        no new evidence.  This is the minimal debounce that keeps the 5 s loop
        from re-deriving identical history every cycle.  ``reanalysis_interval_s``
        is now an *optional* minimum gap (0 = immediate, the default); a positive
        value is a short debounce, never a periodic trigger.

        A re-derivation from stored samples is what can reach ``ISOLATED`` for an
        event that was still inside its own analysis window when it arrived.  It
        runs on the existing inference loop and starts no thread or task.
        """
        if self.database is None:
            return
        newest = self._newest_stored_event_at()
        if newest is None:
            return  # no stored evidence yet — nothing to re-derive
        seen = self._reanalysis_evidence_seen
        if seen is not None and newest <= seen:
            return  # no new matured evidence since the last pass
        now = self._clock()
        interval = self.reanalysis_interval_s
        last = self.last_reanalysis_at
        if interval > 0.0 and last is not None and (now - last).total_seconds() < interval:
            return
        # Claim the evidence before the pass so a failing reanalysis cannot
        # trigger a retry storm on every 5 s cycle.  Reanalysis is an optimisation
        # whose failure is non-fatal; the next new event claims the next pass.
        self._reanalysis_evidence_seen = newest
        self.last_reanalysis_at = now
        self.reanalysis_attempts += 1
        self._reanalysis_history.append(now)
        try:
            report = self.audit.reanalyze(
                topology=topology,
                lookback_s=self.reanalysis_lookback_s,
                now=now,
                results=results,
            )
        except Exception as exc:  # noqa: BLE001 - reanalysis is an optimisation
            self.last_error = f"reanalysis: {type(exc).__name__}: {exc}"
            log.warning("reanalysis pass failed: %s", exc)
            return
        self.last_audit_report = report


def _event_time(event: Any) -> datetime | None:
    raw = (
        event.get("occurred_at")
        if isinstance(event, Mapping)
        else getattr(event, "occurred_at", None)
    )
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
