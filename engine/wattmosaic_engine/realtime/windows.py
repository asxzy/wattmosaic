r"""Event-window collector and immutable event evidence (P4).

Stage 5 of the real-time pipeline.  When P3 emits a
:class:`~wattmosaic_engine.contracts.MeasurementEvent`, this module:

* snapshots the pre-window from the P2
  :class:`~wattmosaic_engine.realtime.reader_state.ReaderStateStore` ring and
  keeps a post-window open until its configured expiry;
* maintains **one live** :class:`~wattmosaic_engine.contracts.EventEvidence`
  row per ``(measurement event, reader)``: the open window revises that row in
  place (``upsert``) as post-samples arrive, then marks it FINAL when the window
  closes.  Only a *late* sample after FINAL creates a superseding row (with the
  old row's samples cleared so the payload is never duplicated);
* batches writes through a caller-supplied sink (S16 ``put_evidence`` via the
  persist queue in P6; a plain callback before then);
* exposes ``on_provisional(evidence)`` and ``on_final(evidence)`` for P5.

Design §4 / HB §6.5 (DEC-18, DEC-30): the pre-window is 30 s or at least two
samples, whichever is longer; the post-window is 120 s per load class, resolved
global -> class -> device like :class:`~wattmosaic_engine.disaggregation.isolation.IsolationConfig`.
Baseline and σ come from P2 (long-run per-reader statistics), **never** from the
two samples inside the 30 s pre-window.  A late sample creates a new evidence
record that ``supersedes=<old id>``; the old summary stays as the audit record.

The module is **pure**: it has no database access and no wall clock.  Every
"now" is an explicit argument.  Production code never imports
:mod:`wattmosaic_engine.runtime.fakes`.

A note on ``metadata``: the K02 ``EventEvidence`` contract has no ``metadata``
field and forbids extra fields, so supersession metadata is carried *alongside*
the record in the :class:`EvidenceBatch` handoff — an item pairs an evidence
record with its metadata.  This keeps the contract untouched (no cross-task
edit) while still making ``supersedes`` / revise-finalize queryable by the
consumer.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contracts import (
    EventEvidence,
    MeasurementEvent,
    QualityFlag,
    StepClass,
)
from .reader_state import ReaderStateStore

__all__ = [
    "BATCH_SIZE",
    "DEFAULT_POST_S",
    "DEFAULT_PRE_S",
    "DEFAULT_REQUIRED_PRE_SAMPLES",
    "INSUFFICIENT_PRE",
    "OP_FINALIZE",
    "OP_KEY",
    "OP_REVISE",
    "OP_SUPERSEDE",
    "SUPERSEDES_KEY",
    "EvidenceBatch",
    "EventWindow",
    "ResolvedWindowConfig",
    "WindowConfig",
    "WindowCollector",
    "batch_evidence",
    "build_evidence",
    "replay_delta",
]

# Design §4 / DEC-18.
DEFAULT_PRE_S = 30.0
DEFAULT_POST_S = 120.0
DEFAULT_REQUIRED_PRE_SAMPLES = 2
#: Number of evidence records flushed together through the persist queue.
BATCH_SIZE = 64
#: Metadata key of the supersession link (task step 3).
SUPERSEDES_KEY = "supersedes"
#: Operation keys on an ::class:`EvidenceBatch`: an open window revises its one
#: live row in place; closing sets ``finalized``; late data supersedes.
OP_KEY = "op"
OP_REVISE = "revise"
OP_FINALIZE = "finalize"
OP_SUPERSEDE = "supersede"
#: A window-collection flag (not a K02 measurement QualityFlag), carried in the
#: window's / batch's metadata (``EventEvidence.quality_flags`` is a closed enum).
INSUFFICIENT_PRE = "insufficient_pre"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _check_seconds(name: str, value: float) -> float:
    seconds = float(value)
    if seconds <= 0 or not math.isfinite(seconds):
        raise ValueError(f"{name} must be a positive, finite number of seconds")
    return seconds


# --------------------------------------------------------------------------- #
# Configuration (resolved like IsolationConfig: global -> class -> device)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResolvedWindowConfig:
    """The effective timings for one reader.  ``None`` fields inherit globals."""

    pre_s: float | None = None
    post_s: float | None = None
    min_pre_samples: int | None = None

    def __post_init__(self) -> None:
        if self.pre_s is not None:
            object.__setattr__(self, "pre_s", _check_seconds("pre_s", self.pre_s))
        if self.post_s is not None:
            object.__setattr__(self, "post_s", _check_seconds("post_s", self.post_s))
        if self.min_pre_samples is not None and int(self.min_pre_samples) < 1:
            raise ValueError("min_pre_samples must be >= 1")

    def merged(self, defaults: ResolvedWindowConfig) -> ResolvedWindowConfig:
        """Return a fully resolved config, filling unset fields from ``defaults``."""
        return ResolvedWindowConfig(
            pre_s=self.pre_s if self.pre_s is not None else defaults.pre_s,
            post_s=self.post_s if self.post_s is not None else defaults.post_s,
            min_pre_samples=(
                self.min_pre_samples
                if self.min_pre_samples is not None
                else defaults.min_pre_samples
            ),
        )


@dataclass(slots=True)
class WindowConfig:
    """Global pre/post windows with optional per-class and per-reader overrides.

    Resolution matches :meth:`IsolationConfig.resolve`: device first, then the
    first matching class label, then the global values.  An override names only
    the fields it changes; the rest inherit the global defaults.
    """

    pre_s: float = DEFAULT_PRE_S
    post_s: float = DEFAULT_POST_S
    min_pre_samples: int = DEFAULT_REQUIRED_PRE_SAMPLES
    by_class: Mapping[str, ResolvedWindowConfig] = field(default_factory=dict)
    by_reader: Mapping[str, ResolvedWindowConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.pre_s = _check_seconds("pre_s", self.pre_s)
        self.post_s = _check_seconds("post_s", self.post_s)
        if int(self.min_pre_samples) < 1:
            raise ValueError("min_pre_samples must be >= 1")
        self.min_pre_samples = int(self.min_pre_samples)
        for layer, overrides in (
            ("by_class", self.by_class),
            ("by_reader", self.by_reader),
        ):
            for key, value in dict(overrides).items():
                if not isinstance(value, ResolvedWindowConfig):
                    raise ValueError(f"{layer}[{key!r}] must be a ResolvedWindowConfig")

    @property
    def global_config(self) -> ResolvedWindowConfig:
        return ResolvedWindowConfig(self.pre_s, self.post_s, self.min_pre_samples)

    def resolve(
        self, reader_id: str, reader_classes: Sequence[str] = ()
    ) -> tuple[str, ResolvedWindowConfig]:
        """Return ``(source, config)``; ``source`` is ``"device"``/``"class"``/``"global"``."""
        override = dict(self.by_reader).get(reader_id)
        if override is not None:
            return "device", override.merged(self.global_config)
        for label in reader_classes:
            found = dict(self.by_class).get(str(label))
            if found is not None:
                return "class", found.merged(self.global_config)
        return "global", self.global_config

    def min_pre_samples_for(self, resolved: ResolvedWindowConfig) -> int:
        return int(
            resolved.min_pre_samples
            if resolved.min_pre_samples is not None
            else self.min_pre_samples
        )


# --------------------------------------------------------------------------- #
# Evidence helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceBatch:
    """One pending write: an immutable evidence record and its metadata.

    ``metadata`` is kept alongside the contract record because K02
    ``EventEvidence`` has no ``metadata`` field (and forbids extra fields).  The
    storage/consumer layer reads ``metadata[SUPERSEDES_KEY]`` to discover the
    supersession link without mutating the immutable record.
    """

    evidence: EventEvidence
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def supersedes(self) -> str | None:
        value = self.metadata.get(SUPERSEDES_KEY)
        return None if value is None else str(value)

    @property
    def op(self) -> str:
        """The persistence operation: revise / finalize / supersede / insert."""
        return str(self.metadata.get(OP_KEY, "insert"))


@dataclass(frozen=True, slots=True)
class EventWindow:
    """The live window for one detected reader change."""

    evidence: EventEvidence
    pre_samples: tuple[tuple[datetime, float | None], ...]
    flags: frozenset[str]
    config: ResolvedWindowConfig
    source: str
    close_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _deterministic_evidence_id(event: MeasurementEvent, window_start: datetime) -> str:
    """Stable evidence id: one window per detected event (P3 identity)."""
    return f"evd-{event.event_id}-{_aware(window_start).strftime('%Y%m%dT%H%M%S%fZ')}"


def _stage_id(previous_id: str, suffix: str) -> str:
    """Derive a distinct id for the next immutable stage of one window."""
    digest = hashlib.sha256(f"{previous_id}|{suffix}".encode()).hexdigest()[:8]
    return f"{previous_id}-{suffix}-{digest}"


def _revised_evidence_id(previous_id: str, reason: str) -> str:
    """A fresh id for a superseding record (append, never update)."""
    digest = hashlib.sha256(f"{previous_id}|{reason}".encode()).hexdigest()[:12]
    return f"{previous_id}-rev-{digest}"


def build_evidence(
    *,
    event: MeasurementEvent,
    samples: Sequence[tuple[datetime, float | None]],
    baseline_w: float | None,
    sigma_w: float | None,
    window_start: datetime,
    window_end: datetime,
    quality_flags: Sequence[QualityFlag | str],
    topology_version_id: str,
    evidence_id: str,
    finalized: bool = False,
) -> EventEvidence:
    """Build one immutable :class:`EventEvidence` record.

    ``baseline_w`` and ``sigma_w`` are passed in from P2 (long-run statistics),
    never derived from the pre-window.  An absent delta (a data gap) stays
    ``None``; it is never turned into ``0.0``.
    """
    return EventEvidence(
        evidence_id=str(evidence_id),
        reader_id=event.reader_id,
        measurement_event_id=event.event_id,
        window_start=_aware(window_start),
        window_end=_aware(window_end),
        samples=[(_aware(at), None if value is None else float(value)) for at, value in samples],
        baseline_w=baseline_w,
        sigma_w=sigma_w,
        delta_w=event.delta_w,
        quality_flags=list(quality_flags),
        topology_version_id=topology_version_id,
        finalized=bool(finalized),
    )


def replay_delta(evidence: EventEvidence) -> float | None:
    """Replay the event's Δ from stored evidence, after raw samples are pruned.

    The signed Δ is captured in the evidence itself at write time, so replay is
    exact and independent of the ring's retention (DEC-30).  ``None`` (a reader
    with no usable post-window data / a data gap) is preserved as ``None``; it is
    never conflated with a zero delta.
    """
    return None if evidence.delta_w is None else float(evidence.delta_w)


def batch_evidence(
    batch: Sequence[EvidenceBatch], size: int = BATCH_SIZE
) -> list[list[EvidenceBatch]]:
    """Split pending writes into fixed-size batches for the persist queue."""
    if int(size) < 1:
        raise ValueError("batch size must be >= 1")
    return [list(batch[i : i + size]) for i in range(0, len(batch), int(size))]


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #

EvidenceSink = Callable[[Sequence[EvidenceBatch]], None]
EvidenceCallback = Callable[[EventEvidence], None]


class WindowCollector:
    """Open, progress, finalize and revise event windows (pure; injected clock).

    Parameters
    ----------
    store:
        The P2 reader-state store, used only to read baseline, σ and the
        pre-window ring.  No DB and no wall clock are involved.
    config:
        Per-load-class window configuration (global -> class -> device).
    sink:
        Optional batch writer receiving ``EvidenceBatch`` items, e.g. a wrapper
        around S16 ``put_evidence`` (P6 supplies the persist queue).
    on_provisional, on_final:
        Callbacks for P5, invoked with each immutable record as it is written.
    """

    def __init__(
        self,
        store: ReaderStateStore,
        config: WindowConfig | None = None,
        *,
        sink: EvidenceSink | None = None,
        on_provisional: EvidenceCallback | None = None,
        on_final: EvidenceCallback | None = None,
        topology_version_id: str = "topo-0",
    ) -> None:
        self.store = store
        self.config = config or WindowConfig()
        self.sink = sink
        self.on_provisional = on_provisional
        self.on_final = on_final
        self.topology_version_id = topology_version_id
        self._windows: dict[str, EventWindow] = {}
        self._pending: list[EvidenceBatch] = []
        self._by_event: dict[str, str] = {}
        self._closed: dict[str, EvidenceBatch] = {}

    # -- introspection ----------------------------------------------------- #
    @property
    def windows(self) -> dict[str, EventWindow]:
        """Live windows keyed by reader id (P5/P6 read-only inspection)."""
        return self._windows

    @property
    def closed(self) -> dict[str, EvidenceBatch]:
        """The latest finalized evidence per reader, for late-sample revision."""
        return self._closed

    # -- opening ----------------------------------------------------------- #
    def open(
        self,
        event: MeasurementEvent,
        *,
        at: datetime,
        reader_classes: Sequence[str] = (),
        topology_version_id: str | None = None,
    ) -> EventWindow | None:
        """Open a window for one detected event from the P2 pre-window ring."""
        at = _aware(at)
        # A data gap is not a step: P3b makes its delta None; it never opens a
        # window (design §3, DEC-18).
        if str(getattr(event.step_class, "value", event.step_class)) == StepClass.DATA_GAP.value:
            return None
        if event.event_id in self._by_event:
            return self._windows.get(self._by_event[event.event_id])

        source, resolved = self.config.resolve(event.reader_id, reader_classes)
        pre_s = float(resolved.pre_s if resolved.pre_s is not None else self.config.pre_s)
        min_pre = self.config.min_pre_samples_for(resolved)
        pre_start = event.onset_lo - timedelta(seconds=pre_s)
        ring = [
            (s.observed_at, s.active_power_w)
            for s in self.store.window(event.reader_id, pre_start, event.onset_lo)
        ]
        # Prefer >= min_pre samples: with a 15.7 s cadence a 30 s window holds
        # two samples; otherwise extend backwards until we have them ("30 s, or
        # >= 2 samples, whichever is longer").
        if len(ring) < min_pre:
            # Same ``(observed_at, power)`` shape as ``ring``: the store returns
            # ``WindowSample`` objects, and mixing them in made ``ring[0][0]``
            # raise for every sparse pre-window (the window never opened).
            earlier = [
                (s.observed_at, s.active_power_w)
                for s in self.store.window(
                    event.reader_id, event.onset_lo - timedelta(days=1), pre_start
                )
            ]
            ring = [*earlier, *ring]
            if len(ring) > min_pre:
                ring = ring[-min_pre:]

        flags: set[str] = set()
        if len(ring) < min_pre:
            flags.add(INSUFFICIENT_PRE)
        # Propagate the measurement-quality flags of the pre-window samples
        # (MeasurementEvent itself carries no quality flags).  Absent = None.
        pre_flags = self._flags_at(event.reader_id, event.onset_lo)

        baseline = self.store.baseline(event.reader_id)
        if baseline is None:
            baseline = event.pre_level_w
        # sigma comes from P2's long-run estimate, never from the pre-window.
        sigma = self.store.sigma(event.reader_id)
        window_start = ring[0][0] if ring else _aware(event.onset_lo)
        window_end = at + timedelta(
            seconds=float(resolved.post_s if resolved.post_s is not None else self.config.post_s)
        )
        version = topology_version_id or self.topology_version_id
        evidence = build_evidence(
            event=event,
            samples=[],
            baseline_w=baseline,
            sigma_w=sigma,
            window_start=window_start,
            window_end=window_end,
            quality_flags=pre_flags,
            topology_version_id=version,
            evidence_id=_deterministic_evidence_id(event, window_start),
            finalized=False,
        )
        metadata: dict[str, Any] = {}
        if INSUFFICIENT_PRE in flags:
            metadata[INSUFFICIENT_PRE] = True
        window = EventWindow(
            evidence=evidence,
            pre_samples=tuple(ring),
            flags=frozenset(flags),
            config=resolved,
            source=source,
            close_at=window_end,
            metadata=metadata,
        )
        self._windows[event.reader_id] = window
        self._by_event[event.event_id] = event.reader_id
        return window

    def _flags_at(self, reader_id: str, until: datetime) -> list[QualityFlag | str]:
        """Distinct quality flags on the P2 samples up to ``until``."""
        seen: dict[str, QualityFlag | str] = {}
        for sample in self.store.window(reader_id, until - timedelta(days=1), until):
            for flag in sample.quality_flags:
                key = str(getattr(flag, "value", flag))
                seen.setdefault(key, flag)
        return list(seen.values())

    def on_detected(
        self,
        event: MeasurementEvent,
        store: ReaderStateStore | None = None,
        *,
        at: datetime,
        reader_classes: Sequence[str] = (),
        topology_version_id: str | None = None,
    ) -> EventWindow | None:
        """Convenience: P3 emits an event, the collector opens it from P2."""
        if store is not None and store is not self.store:
            raise ValueError("the detector's reader-state store must be the collector's store")
        return self.open(
            event,
            at=at,
            reader_classes=reader_classes,
            topology_version_id=topology_version_id,
        )

    # -- progressing ------------------------------------------------------- #
    def on_sample(
        self,
        reader_id: str,
        at: datetime,
        value_w: float | None,
    ) -> EventEvidence | None:
        """Add an in-window post-sample by revising the one live evidence row.

        Z8: the evidence id is the deterministic one minted at :meth:`open`, so
        every post-sample rewrites the *same* row (its bounded window samples
        replaced, not duplicated).  Only the first post-sample announces
        PROVISIONAL to P5 (design §4); subsequent samples keep its id.
        """
        at = _aware(at)
        window = self._windows.get(reader_id)
        if window is None or not (window.evidence.window_start <= at <= window.close_at):
            return None
        if any(existing_at == at for existing_at, _ in window.evidence.samples):
            return None
        is_first = len(window.evidence.samples) == 0
        evidence = window.evidence.model_copy(
            update={
                "samples": [
                    *window.evidence.samples,
                    (at, None if value_w is None else float(value_w)),
                ],
            }
        )
        window = replace(window, evidence=evidence)
        self._windows[reader_id] = window
        metadata = dict(window.metadata)
        metadata[OP_KEY] = OP_REVISE
        self._enqueue(evidence, metadata, provisional=is_first)
        return evidence

    def tick(self, now: datetime) -> list[EventEvidence]:
        """Advance time; finalize every window whose post-window has expired."""
        now = _aware(now)
        finalized: list[EventEvidence] = []
        for reader_id, window in list(self._windows.items()):
            if now >= window.close_at:
                evidence = self._finalize(reader_id)
                if evidence is not None:
                    finalized.append(evidence)
        return finalized

    def close(self, reader_id: str) -> EventEvidence | None:
        """Close one window immediately (caller-driven, e.g. group quiet period)."""
        return self._finalize(reader_id)

    def _finalize(self, reader_id: str) -> EventEvidence | None:
        window = self._windows.pop(reader_id, None)
        if window is None:
            return None
        # FINAL sets ``finalized`` on the SAME live row (Z8): no new row is
        # created just because the window closed.  The id stays stable so P4's
        # sub-events and every evidence ref keep resolving.
        evidence = window.evidence.model_copy(
            update={
                "finalized": True,
                "window_end": window.close_at,
            }
        )
        metadata = dict(window.metadata)
        metadata[OP_KEY] = OP_FINALIZE
        self._enqueue(evidence, metadata, provisional=False)
        self._closed[reader_id] = EvidenceBatch(evidence, metadata)
        return evidence

    # -- late samples / revisions ----------------------------------------- #
    def on_late_sample(
        self,
        reader_id: str,
        at: datetime,
        value_w: float | None,
    ) -> EventEvidence | None:
        """A sample arriving after FINAL creates a superseding immutable record."""
        previous_item = self._closed.get(reader_id)
        if previous_item is None or not previous_item.evidence.finalized:
            return None
        previous = previous_item.evidence
        at = _aware(at)
        # A late sample belongs to this window when its observed time falls at
        # or after the window's first sample.  Samples older than the window
        # belong to a different (earlier) event and are not our concern here.
        if at < previous.window_start:
            return None
        return self.revise(previous, reason="late_sample", sample=(at, value_w))

    def revise(
        self,
        previous: EventEvidence,
        *,
        reason: str,
        sample: tuple[datetime, float | None] | None = None,
    ) -> EventEvidence:
        """Create a late-data revision that supersedes ``previous``.

        HB requires an audit trail but DEC-30 forbids duplicating the window
        samples.  The predecessor's *summary* row (Δ, baseline, σ, quality,
        bounds) stays → ``supersedes_evidence_id`` still resolves → but its
        ``samples_json`` is reduced to a bounded first/last pair via an in-place
        ``clear_samples`` handoff.  The new row carries the full revised samples.
        """
        metadata = {
            SUPERSEDES_KEY: previous.evidence_id,
            "reason": reason,
            OP_KEY: OP_SUPERSEDE,
            "clear_samples_of": previous.evidence_id,
        }
        samples = list(previous.samples)
        if sample is not None:
            at, value = sample
            samples.append((_aware(at), None if value is None else float(value)))
        revised = previous.model_copy(
            update={
                "evidence_id": _revised_evidence_id(previous.evidence_id, reason),
                "samples": samples,
                "finalized": True,
            }
        )
        # Track the revision as the latest closed record for this reader.
        for reader_id, item in list(self._closed.items()):
            if item.evidence.evidence_id == previous.evidence_id:
                self._closed[reader_id] = EvidenceBatch(revised, metadata)
                break
        self._enqueue(revised, metadata, provisional=False)
        return revised

    # -- persistence ------------------------------------------------------- #
    def _enqueue(
        self,
        evidence: EventEvidence,
        metadata: Mapping[str, Any],
        *,
        provisional: bool,
    ) -> None:
        item = EvidenceBatch(evidence, dict(metadata))
        self._pending.append(item)
        self._flush()
        callback = self.on_provisional if provisional else self.on_final
        if callback is not None:
            callback(evidence)

    def _flush(self) -> None:
        """Hand pending records to the sink as one batch (S16 `put_evidence`).

        The sink is the persist-queue writer; batching across *unrelated* events
        is its job.  ``batch_evidence`` / ``BATCH_SIZE`` are exposed for callers
        that need to chunk a backlog explicitly (e.g. replay).
        """
        if self.sink is None or not self._pending:
            return
        pending, self._pending = self._pending, []
        self.sink(pending)

    def flush(self) -> None:
        """Explicitly flush any remaining pending evidence batches."""
        self._flush()
