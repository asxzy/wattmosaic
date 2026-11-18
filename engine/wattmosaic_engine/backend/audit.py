"""Analysis (audit) pass wiring for the backend.

Owned by T5/T6 after X03."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..contracts import InferenceResult, Topology

log = logging.getLogger("wattmosaic.integration")


class AuditMixin:
    """Behaviour for one responsibility of :class:`WattMosaicBackend`."""

    def _analyze(
        self,
        window: Any,
        topology: Topology,
        results: Sequence[InferenceResult],
        events: Sequence[Any],
    ) -> None:
        """One incremental pass: isolation → association → circuits → residual → hierarchy → record.

        Guarded whole-call *and* internally: the pass reports a per-item error in
        ``AuditReport.errors`` rather than raising, so one bad event or circuit
        can never abort the inference loop.  A failure here cannot change the
        inference results main.py already has -- the artifacts are written
        beside them, never instead of them.
        """
        if not self.audit_config.write_records and not any(
            (
                self.audit_config.write_isolation,
                self.audit_config.write_association,
                self.audit_config.write_circuits,
                self.audit_config.write_residual,
                self.audit_config.write_hierarchy,
            )
        ):
            return
        try:
            report = self.audit.analyze(
                window,
                topology=topology,
                results=results,
                events=events,
                mode="incremental",
            )
        except Exception as exc:  # noqa: BLE001 - the audit never kills inference
            self.last_error = f"audit: {type(exc).__name__}: {exc}"
            log.warning("analysis pass failed: %s", exc)
            return
        self.last_audit_report = report
        if report.errors:
            log.debug("analysis pass reported %d error(s)", len(report.errors))

    def _audit_summary(self) -> dict[str, Any]:
        report = self.audit_report()
        summary: dict[str, Any] = {
            "enabled": self.audit_config.write_records
            or self.audit_config.write_isolation
            or self.audit_config.write_association
            or self.audit_config.write_circuits
            or self.audit_config.write_residual
            or self.audit_config.write_hierarchy,
            "algorithm_version": self.audit_config.algorithm_version,
            "reanalysis_interval_s": self.reanalysis_interval_s,
            "reanalysis_attempts": self.reanalysis_attempts,
            "last_reanalysis_at": (
                None if self.last_reanalysis_at is None else self.last_reanalysis_at.isoformat()
            ),
        }
        if report is None:
            summary["last_pass"] = None
            return summary
        summary["last_pass"] = report.as_dict()
        return summary

    def audit_report(self) -> Any:
        """The last pass's report, for the read-only audit endpoints.

        Reads through to the pass rather than caching a second copy: two
        "last report" attributes would be able to disagree about what was
        analysed, and the API would then serve the stale one.
        """
        report = self.audit.last_report
        return report if report is not None else self.last_audit_report

    def isolation_status(self) -> dict[str, Any]:
        """Verdict counts over the retained analysis window, or an honest empty answer.

        Reads the *pass* rather than the last report: a live pass matures a
        handful of events per hour, so the most recent cycle's report is usually
        empty and would render as "0 analysed" even though analysis is running.
        """
        status = getattr(self.audit, "isolation_status", None)
        if status is not None:
            try:
                return status()
            except Exception:  # noqa: BLE001 - the endpoint must still answer
                pass
        report = self.audit_report()
        if report is None:
            return {
                "source": "no_analysis_pass_yet",
                "algorithm_version": self.audit_config.algorithm_version,
                "counts": {
                    "analysed": 0,
                    "isolated": 0,
                    "contaminated": 0,
                    "insufficient_data": 0,
                },
                "verdicts": [],
            }
        return report.isolation_status()
