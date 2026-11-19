"""Measurement-inferred configuration-change detectors (T7, HB §14).

The detectors here are **pure**: they read reader steps, on-state evidence,
association history and reader silence, and *report* :class:`InferredChange`
proposals.  They never promote a change, advance a version or read a wall clock
(``now`` is injected).  The workflow that turns a proposal into a SUSPECTED
:class:`~wattmosaic_engine.contracts.ConfigurationEvent` and, when confirmed,
a promoted topology version lives in :mod:`wattmosaic_engine.versioning.workflow`.

The contract-3.0 cutover removed the declared-vs-observed topology diff that
used to live here (it compared two legacy pointer topologies); topology changes
are now per-edge lifecycle transitions owned by the version service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from wattmosaic_engine.contracts import ConfigurationChangeKind

__all__: list[str] = [
    "InferredChange",
    "detect_inferred_changes",
    "detect_reader_removals",
]


@dataclass(frozen=True)
class InferredChange:
    """One measurement-inferred change proposal (design §5).

    The detector only *reports*; the T7 workflow decides whether to record a
    SUSPECTED :class:`ConfigurationEvent` and whether to promote it.  ``subject``
    is the reader or device id, ``kind`` is the configuration-change kind, and
    ``evidence`` is an opaque, deterministic set of supporting tokens.
    """

    kind: ConfigurationChangeKind
    subject: str
    from_id: str | None = None
    to_id: str | None = None
    evidence: tuple[str, ...] = ()


def detect_inferred_changes(
    *,
    reader_steps: Mapping[str, Sequence[float]] | None = None,
    on_states: Mapping[str, Sequence[bool]] | None = None,
    association_history: Mapping[str, Sequence[str]] | None = None,
    now: datetime | None = None,
) -> tuple[InferredChange, ...]:
    """Pure inferred detectors of design §5 / HB §14 step 2.

    Four independent detectors, each with the threshold from design §5:

    * persistent sign inversion — ≥ 80 % of a consuming reader's last 20 steps
      negative-on-ON → ``CT_DIRECTION_CHANGE``;
    * child/parent β-ratio step over ≥ 10 events → ``CALIBRATION_CHANGE``;
    * a subject's matches vanish on reader A and appear on reader B over ≥ 5
      events → ``SMART_PLUG_RELOCATION`` / ``CT_READER_RELOCATION``;
    * silence > ``missing_after_s`` × 6 (see the workflow's ``missing_after_s``)
      → ``READER_REMOVAL``.

    No wall clock is read: ``now`` is injected, as are all observation series.
    Absent values stay absent — a reader with no steps contributes no evidence.
    """
    out: list[InferredChange] = []
    steps = reader_steps or {}
    on = on_states or {}

    # 1. Persistent sign inversion on a consuming reader.
    for reader in sorted(set(steps) & set(on)):
        series = list(steps[reader])
        states = list(on[reader])
        n = min(20, len(series), len(states))
        if n == 0:
            continue
        recent = list(zip(series[-n:], states[-n:], strict=False))
        on_steps = [delta for delta, state in recent if state]
        if len(on_steps) < 5:
            continue
        negative = sum(1 for delta in on_steps if delta < 0)
        if negative / len(on_steps) >= 0.8:
            out.append(
                InferredChange(
                    ConfigurationChangeKind.CT_DIRECTION_CHANGE,
                    reader,
                    evidence=(f"negative-on-on:{negative}/{len(on_steps)}",),
                )
            )

    # 2. Calibration / β-ratio step: a sustained ratio shift over >= 10 events.
    #    The caller supplies per-reader steps; a paired (parent, child) encoded
    #    as ``"child/parent"`` is compared against the unity baseline.
    for key in sorted(k for k in steps if "/" in k):
        child, parent = key.split("/", 1)
        if parent not in steps:
            continue
        child_series, parent_series = list(steps[key]), list(steps[parent])
        n = min(10, len(child_series), len(parent_series))
        if n < 10:
            continue
        ratios = [
            c / p
            for c, p in zip(child_series[-n:], parent_series[-n:], strict=False)
            if p != 0
        ]
        if len(ratios) < 10:
            continue
        baseline = ratios[0]
        if all(abs(r - baseline) <= 0.02 for r in ratios[1:]):
            # Constant ratio is one conductor with a multiplier, not drift; the
            # scaled-copy guard in T5 already refuses to promote that edge.
            continue
        if all(r > 1.0 for r in ratios) or all(r < 1.0 for r in ratios):
            out.append(
                InferredChange(
                    ConfigurationChangeKind.CALIBRATION_CHANGE,
                    child,
                    from_id=parent,
                    evidence=(f"beta-ratio-step:{min(ratios):.3f}..{max(ratios):.3f}",),
                )
            )

    # 3. Relocation: matches vanish on A and appear on B over >= 5 events.
    #    The last 5 observations must all be on a single new reader, and the
    #    immediately-preceding reader must be different (the subject left it).
    for subject in sorted(association_history or {}):
        history = list(association_history[subject])
        if len(history) < 6:
            continue
        recent = history[-5:]
        new_reader = recent[-1]
        if any(reader != new_reader for reader in recent):
            continue
        old_reader = history[-6]
        if old_reader == new_reader:
            continue
        kind = (
            ConfigurationChangeKind.SMART_PLUG_RELOCATION
            if subject.startswith("plug:") or subject.startswith("device:")
            else ConfigurationChangeKind.CT_READER_RELOCATION
        )
        out.append(
            InferredChange(
                kind,
                subject,
                from_id=old_reader,
                to_id=new_reader,
                evidence=(f"association:{old_reader}->{new_reader}",),
            )
        )

    return tuple(out)


def detect_reader_removals(
    *,
    reader_last_seen: Mapping[str, datetime | None],
    now: datetime,
    missing_after_s: float,
) -> tuple[InferredChange, ...]:
    """Reader-silence detector (design §5: silence > ``missing_after_s`` × 6)."""
    out: list[InferredChange] = []
    cutoff = missing_after_s * 6.0
    for reader in sorted(reader_last_seen):
        last = reader_last_seen[reader]
        if last is None:
            continue

        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        stamp = last if last.tzinfo is not None else last.replace(tzinfo=UTC)
        if (reference - stamp).total_seconds() > cutoff:
            out.append(
                InferredChange(
                    ConfigurationChangeKind.READER_REMOVAL,
                    reader,
                    evidence=(f"silent-s>{cutoff:.0f}s",),
                )
            )
    return tuple(out)

