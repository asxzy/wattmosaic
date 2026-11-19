"""AT-14 — one physical load visible to many Readers counted once (HB §21).

Closes: I5. The observable TV (a single-subject plug reporting 3 W standby in
HB §17 step 8) is seen by the plug, Circuit A, Subpanel and Main.  The snapshot
publishes the TV exactly once with ``MEASURED`` status, and the reconciliation
counts that one physical load once in ``identified_w`` — not four times from
the four rows that observe it.

The snapshot is the one the live runtime publishes (``engine_harness``: the
real fast/event/steady loops over the HB §17 observation stream).  The user's
declarations are the only configuration: the TV on its smart plug, the plug a
confirmed single-subject endpoint (DEC-05) plugged into Circuit A, and the
plug's HA switch linked to the TV.  No ground truth is read.

The full HB §17 stream is replayed.  The plug's samples carry the simulator's
sub-watt perturbation, so the published TV value is not a constant 3 W: the
assertions are measurement-based instead.  The TV's point must be exactly the
plug reading the runtime stored when it published that MEASURED value; it may
lag the plug's current reading only inside the steady loop's 3σ re-publish
deadband (σ = the runtime's noise estimate for the plug); and it must lie
within 3σ of the 3 W standby.  The reconciliation's ``identified_w`` must
equal the TV's point exactly -- the TV is the only identified load, counted
once, not once per observing reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import EstimateStatus, InferenceMethod

CLOSED_BY = "I5"


def _declare_tv_on_plug(start: Any):
    def configure(core: Any) -> None:
        at = start - timedelta(days=1)
        core.db.repos.entities.upsert_endpoint(
            {"endpoint_id": "ct_plug", "kind": "smart_plug", "entity_id": "sensor.tv_plug_power",
             "scope": "single_subject", "scope_status": "confirmed", "scope_subject_id": "tv",
             "enabled": True, "created_at": at}
        )
        core.db.repos.entities.add_entity_link(
            {"entity_id": "switch.tv_plug", "target_kind": "device", "target_id": "tv",
             "role": "state", "valid_from": at, "declared_by": "user"}
        )

    return configure


@dataclass(frozen=True)
class _Published:
    """The published snapshot plus the runtime's own view of the plug reader."""

    snapshot: Any
    plug_at_publish_w: float | None  # stored ct_plug reading when the TV was published
    plug_now_w: float | None  # ct_plug's held reading at the snapshot's observed_at
    plug_sigma_w: float | None  # the runtime's noise estimate for ct_plug


def _published_snapshot(hb17: Any, engine_harness: Any) -> _Published:
    """The snapshot the live runtime publishes for the full HB §17 stream."""
    harness = engine_harness(
        hb17,
        devices={"tv": "ct_plug"},
        parents={"ct_plug": "ct_a"},  # the plug is plugged into Circuit A
        configure=_declare_tv_on_plug(hb17["house"].start_time),
    )
    try:
        snapshot = harness.snapshot
        tv = next(e for e in snapshot.device_estimates if e.subject_id == "tv")
        stored = harness.db.repos.inference.snapshot_as_of(tv.updated_at)
        at_publish = next(
            (
                m["active_power_w"]
                for m in (stored["payload"]["reader_measurements"] if stored else ())
                if m["reader_id"] == "ct_plug"
            ),
            None,
        )
        state = harness.core.reader_state
        return _Published(
            snapshot=snapshot,
            plug_at_publish_w=at_publish,
            plug_now_w=state.held("ct_plug", snapshot.observed_at).value,
            plug_sigma_w=state.sigma("ct_plug"),
        )
    finally:
        harness.close()


def test_tv_counted_once_in_snapshot_reconciliation(hb17, engine_harness) -> None:
    """Four readers observing one TV still yield one physical contribution."""
    published = _published_snapshot(hb17, engine_harness)
    snapshot = published.snapshot
    tv_estimates = [e for e in snapshot.device_estimates if e.subject_id == "tv"]
    assert len(tv_estimates) == 1

    tv = tv_estimates[0]
    # MEASURED comes only from a confirmed single-subject endpoint (DEC-05):
    # the plug's 3 W standby, never collapsed to 0 (HB §17 step 8).
    assert tv.status == EstimateStatus.MEASURED
    assert tv.method == InferenceMethod.DIRECT_READING
    # The point is the plug's own reading, as stored when it was published ...
    assert published.plug_at_publish_w is not None
    assert tv.point_w == pytest.approx(published.plug_at_publish_w, abs=1e-6)
    # ... still current within the steady loop's 3σ re-publish deadband ...
    sigma = published.plug_sigma_w
    assert sigma is not None and sigma > 0.0
    assert published.plug_now_w is not None
    assert tv.point_w == pytest.approx(published.plug_now_w, abs=3 * sigma)
    # ... and the 3 W standby within the plug's noise.
    assert tv.point_w == pytest.approx(3.0, abs=3 * sigma)
    assert tv.point_w > 0.0
    assert tv.source_reader_ids, "the TV must list at least one contributing reader"

    # The nested CTs observe the same physical load as EXPECTED entries, but
    # provenance never duplicates one reader and the reconciliation counts the
    # TV once, not once per observing reader.
    assert len(tv.source_reader_ids) == len(set(tv.source_reader_ids))
    recon_tv = snapshot.reconciliation.identified_w
    assert recon_tv is not None
    identified = [
        e for e in snapshot.device_estimates
        if e.status in (EstimateStatus.MEASURED, EstimateStatus.ESTIMATED) and e.point_w is not None
    ]
    assert [e.subject_id for e in identified] == ["tv"]  # the only identified load
    assert recon_tv == pytest.approx(tv.point_w, abs=1e-6)
    # Not 4× the plug reading (the DEF-04 / AT-14 double-count defect).
    assert recon_tv != pytest.approx(12.0)


def test_reconciliation_terms_use_null_semantics(hb17, engine_harness) -> None:
    """Absent terms are None, never 0.0; the identity is never fabricated."""
    snapshot = _published_snapshot(hb17, engine_harness).snapshot
    recon = snapshot.reconciliation
    # main and identified are real; group/unknown may be absent (HB §18).
    assert recon.main_w is not None
    assert recon.identified_w is not None
    if recon.group_w is None or recon.unknown_w is None:
        assert recon.balance_error_w is None
