"""Z5 — incremental cadence equivalence and event-loop coalescing.

Two properties, both exercised against the production modules:

* P2's incrementally-maintained cadence sketch returns exactly the same
  :class:`ResolvedCadence` as a fresh ``resolve_channel_cadence`` over the ring,
  for random interval streams (including out-of-order receive times, duplicate
  observations and non-evidence flags).  It is O(1) amortized per sample, so P3
  can ask on every sample without a full re-sort of the ring.

* The event loop coalesces several queued revisions of one group into a single
  inference of the newest revision.  Superseded revisions are counted, the
  newest revision of every group is always kept, ordering between distinct
  groups is preserved, and the published record references the newest revision.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    EventGroup,
    EventGroupStatus,
    MeasurementBasis,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
    VersionTriple,
)
from wattmosaic_engine.electrical.window import DEFAULT_CADENCE_S, clamp_cadence
from wattmosaic_engine.realtime.reader_state import ReaderStateStore
from wattmosaic_engine.synchronization.aligner import resolve_channel_cadence

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VERSIONS = VersionTriple(
    topology_version_id="t1", mapping_version_id="m1", model_set_id="ms1"
)


class _Entry:
    """The one field ``resolve_channel_cadence`` reads off a ring sample."""

    __slots__ = ("received_at",)

    def __init__(self, received_at: datetime) -> None:
        self.received_at = received_at


def _measurement(
    observed_at: datetime,
    received_at: datetime,
    sequence: int,
    *,
    flags: list[QualityFlag] | None = None,
) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id="r",
        observed_at=observed_at,
        received_at=received_at,
        active_power_w=100.0,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="c@1",
        measurement_basis=MeasurementBasis.CT_MEASURED.value,
        quality_flags=flags or [],
        sequence=sequence,
    )


def test_incremental_cadence_matches_resolve_channel_cadence_random_streams() -> None:
    """Exact equivalence over random streams, after every single add."""
    rng = random.Random(20261119)
    for _trial in range(50):
        store = ReaderStateStore()
        received = T0
        for i, sequence in enumerate(range(rng.randint(2, 200))):
            # Mostly regular cadence with jitter, occasional clock skew
            # (negative gap -> ignored by the resolver) and duplicate receive
            # times (gap 0 -> ignored).
            received += timedelta(
                seconds=rng.choice([0.0, 0.5, 1.0, 1.0, 1.0, 2.0, 15.7, -2.0])
            )
            flags: list[QualityFlag] = []
            if rng.random() < 0.1:
                flags.append(QualityFlag.OUT_OF_ORDER)
            store.add(_measurement(T0 + timedelta(seconds=i), received, sequence, flags=flags))

            ring = store._rings["r"]  # noqa: SLF001 - test inspects the sketch
            expected = resolve_channel_cadence(
                [_Entry(s.received_at) for s in ring.samples],
                default=clamp_cadence(DEFAULT_CADENCE_S),
            )
            got = store.cadence("r")
            assert (got.seconds, got.source, got.intervals) == (
                expected.seconds,
                expected.source,
                expected.intervals,
            )


def test_incremental_cadence_survives_late_inserts_and_prune() -> None:
    """Structural changes (late receive time, prune) rebuild the sketch exactly."""
    store = ReaderStateStore()
    # In-order regular gaps.
    for i in range(30):
        at = T0 + timedelta(seconds=i * 15.7)
        store.add(_measurement(at, at, i))
    ring = store._rings["r"]  # noqa: SLF001
    expected = resolve_channel_cadence(
        [_Entry(s.received_at) for s in ring.samples],
        default=clamp_cadence(DEFAULT_CADENCE_S),
    )
    assert (store.cadence("r").seconds, store.cadence("r").intervals) == (
        expected.seconds,
        expected.intervals,
    )

    # Prune the front of the ring; the removed gap must leave the sketch too.
    store.prune(now=T0 + timedelta(seconds=30 * 15.7 + 60.0))
    ring = store._rings["r"]  # noqa: SLF001
    expected = resolve_channel_cadence(
        [_Entry(s.received_at) for s in ring.samples],
        default=clamp_cadence(DEFAULT_CADENCE_S),
    )
    got = store.cadence("r")
    assert (got.seconds, got.source, got.intervals) == (
        expected.seconds,
        expected.source,
        expected.intervals,
    )


# --------------------------------------------------------------------------- #
# Event-loop coalescing
# --------------------------------------------------------------------------- #


def _group(name: str, revision: int) -> EventGroup:
    return EventGroup(
        group_id=f"grp-{name}",
        revision=revision,
        status=EventGroupStatus.MATCHING,
        opened_at=T0,
        sub_events=(),
        reader_deltas=(),
        grouping_reasons=[],
        versions=VERSIONS,
    )


class _Core:
    """Minimal harness for the coalescing helper (no DB / no wiring needed)."""

    def __init__(self, queued: list[EventGroup]) -> None:
        self.groups_superseded = 0

        class _Q:
            def __init__(self, items: list[EventGroup]) -> None:
                self._items = list(items)

            def drain(self) -> list[EventGroup]:
                items, self._items = self._items, []
                return items

        self.groups_ready = _Q(queued)  # type: ignore[assignment]

    # Bind the real production method under test (a mixin method: no wiring).
    from wattmosaic_engine.core.runtime import RuntimeMixin

    _coalesce_ready_groups = RuntimeMixin._coalesce_ready_groups


def test_coalescing_keeps_only_newest_revision_and_counts_superseded() -> None:
    g1, g2, g3 = _group("a", 1), _group("a", 2), _group("a", 3)
    core = _Core([g1, g2, g3])
    batch = core._coalesce_ready_groups()
    assert len(batch) == 1
    assert batch[0].revision == 3
    assert core.groups_superseded == 2


def test_coalescing_never_drops_a_group_and_preserves_order() -> None:
    a1, a2 = _group("a", 1), _group("a", 2)
    b1, b4 = _group("b", 1), _group("b", 4)
    c1 = _group("c", 1)
    core = _Core([a1, b1, a2, c1, b4])
    batch = core._coalesce_ready_groups()
    # Distinct groups keep first-seen order (a, b, c); each is the newest rev.
    assert [g.group_id for g in batch] == ["grp-a", "grp-b", "grp-c"]
    assert [g.revision for g in batch] == [2, 4, 1]
    assert core.groups_superseded == 2


def test_coalescing_seed_handles_newer_revision_queued_behind_first() -> None:
    first = _group("a", 1)
    newer = _group("a", 5)
    core = _Core([newer])
    batch = core._coalesce_ready_groups(seed=first)
    assert len(batch) == 1
    assert batch[0].revision == 5
    assert core.groups_superseded == 1


def test_inferred_result_references_newest_coalesced_revision(tmp_path: Any) -> None:
    """The published inference (and its record) cites the newest revision.

    Three revisions of one group are queued; after coalescing only the newest
    (rev 3) is inferred.  ``infer_group`` stamps ``event_group_revision`` from
    the group it is handed, so the published result — and the record opened from
    it — reference rev 3, never the superseded rev 1/2.
    """
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "rt.db")
    core = EngineCore(
        EngineConfig(database_path=str(tmp_path / "rt.db"), ha_token="x"),
        db,
        clock=lambda: T0,
    )
    try:
        core._wire_v3_services()
        # Queue rev 1, rev 2, rev 3 of the same group.
        for revision in (1, 2, 3):
            core.groups_ready.put_nowait(_group("a", revision))

        first = core.groups_ready.get_nowait()
        batch = core._coalesce_ready_groups(seed=first)
        assert len(batch) == 1
        assert batch[0].revision == 3
        assert core.groups_superseded == 2

        # Inferring the coalesced group carries its revision into the result.
        # (The bundle builder may refresh to the live grouping-engine value, so
        # we assert against the built bundle's group revision explicitly.)
        core.grouping_engine = None  # avoid refresh; infer exactly the queued rev
        bundle = core._build_input_bundle(batch[0])
        assert bundle is not None
        result = core._run_infer_group(bundle)
        assert result.event_group_id == batch[0].group_id
        assert result.event_group_revision == 3
        # The record opened from the published result cites the same revision.
        record = core.record_store.open_provisional(
            tuple(d.reader_id for d in batch[0].reader_deltas),
            at=result.computed_at,
            versions=result.versions,
            group_id=result.event_group_id,
        )
        assert record.event_group_id == batch[0].group_id
    finally:
        db.close()
