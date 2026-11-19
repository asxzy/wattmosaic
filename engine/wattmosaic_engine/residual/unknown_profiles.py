"""T6 — Unknown Load Profile engine (design §6, analysis C WP-C6).

Unexplained FINAL reader steps become persistent *Unknown Load Profiles*: one
per scope, located at the deepest reader that saw the step, with a lifecycle
``observing → characterised → candidate`` and no auto-CONFIRM.  Profiles are
never devices and never reader-residual rows (rule 5): a profile is a recurring
step *signature*, kept separate from the signed residual series produced by T3.

The module is pure.  It reads no clock, no database and no wall time: every
time value comes from the injected observation stream or the injected
``clock``.  Persistence is an explicit, injected sink call, so this module can
run in the P6 background executor without touching the event loop.  Production
code here never imports :mod:`wattmosaic_engine.runtime.fakes`.

Absent values are ``None``, never 0 W (HB §18).  A reader whose step cannot be
located gets no profile rather than a fabricated one.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    CandidateSource,
    EdgeEvidence,
    EdgeLifecycleV3,
    EventGroup,
    EventGroupStatus,
    JointInferenceResult,
    MappingEdge,
    Observability,
    PhysicalDevice,
    UnknownLoadProfileV3,
)

__all__: list[str] = [
    "CLUSTER_TOLERANCE",
    "CONFIDENCE_THRESHOLD",
    "DEFAULT_STEP_THRESHOLD_W",
    "DURATION_BUCKET_S",
    "MIN_CHARACTERISED_OCCURRENCES",
    "MIN_PERIODIC_STEPS",
    "PERIODIC_CV_MAX",
    "RETIRE_AFTER_DAYS",
    "UNKNOWN_EXPLANATION_THRESHOLD",
    "DeviceSink",
    "MappingEdgeSink",
    "ProfilePromotion",
    "ProfileSink",
    "ProfileStore",
    "UnknownDetectionResult",
    "UnknownOccurrence",
    "UnknownProfileView",
    "promote_profile",
    "run_unknown_detection",
]

#: An unknown-scope candidate must explain at least this share of a reader step
#: for that step to feed a profile (design §6 step 1).
UNKNOWN_EXPLANATION_THRESHOLD = 0.80

#: A group is UNRESOLVED when no device-level explanation carries it (design §4).
CONFIDENCE_THRESHOLD = 0.80

#: Occurrences of the same signature needed to reach CHARACTERISED (design §6).
MIN_CHARACTERISED_OCCURRENCES = 5

#: A profile is retired after this many days unseen; retired rows are retained.
RETIRE_AFTER_DAYS = 30.0

#: Periodicity (the P3 tag) needs >= 4 same-sign steps with CV(period) < 0.3.
MIN_PERIODIC_STEPS = 4
PERIODIC_CV_MAX = 0.3

#: Clustering bands: sign, |Δ| ±20 %, and a duration bucket.
CLUSTER_TOLERANCE = 0.20
DURATION_BUCKET_S = 30.0

#: Minimum |ΔY| treated as a step.  A caller may pass a tighter detector value.
DEFAULT_STEP_THRESHOLD_W = 5.0

#: Grouping tolerance for co-occurring reader steps (design §4, max 5 s).
DEFAULT_GROUP_WINDOW_S = 60.0


# --------------------------------------------------------------------------- #
# Optional injected sinks (no import of any other task's module).
# --------------------------------------------------------------------------- #


@runtime_checkable
class ProfileSink(Protocol):
    """The S19 batch writer T6 needs (``ResidualRepository.put_profile``)."""

    def put_profile(self, profile: UnknownLoadProfileV3) -> None: ...


@runtime_checkable
class ProfileStore(Protocol):
    """A read side for profiles (S19 ``ResidualRepository.profiles``)."""

    def profiles(
        self, scope_endpoint_id: str | None = None, lifecycle: str | None = None
    ) -> Sequence[Any]: ...


@runtime_checkable
class DeviceSink(Protocol):
    """D2's ``DeviceService.create_device``."""

    def create_device(self, device: PhysicalDevice) -> Any: ...


@runtime_checkable
class MappingEdgeSink(Protocol):
    """A sink that can persist one PROVISIONAL mapping edge."""

    def add_mapping_edge(self, edge: MappingEdge) -> Any: ...


# --------------------------------------------------------------------------- #
# Value types.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnknownOccurrence:
    """One unexplained FINAL step attributed to a scope (immutable)."""

    at: datetime
    delta_w: float
    scope_endpoint_id: str
    duration_s: float | None = None
    event_group_id: str | None = None
    periodic: bool = False
    delta_y_w: float | None = None


@dataclass(frozen=True)
class UnknownDetectionResult:
    """Everything T6 publishes from one pass over the observation stream."""

    event_groups: tuple[EventGroup, ...] = ()
    candidates: tuple[CandidateHypothesis, ...] = ()
    unknown_profiles: tuple[UnknownLoadProfileV3, ...] = ()
    unexplained: tuple[UnknownOccurrence, ...] = ()

    def __iter__(self):
        """Iterating the result yields its profiles (harness threading convenience)."""
        return iter(self.unknown_profiles)


@dataclass(frozen=True)
class ProfilePromotion:
    """The artefacts created when a user promotes a profile to a device."""

    device: PhysicalDevice
    mapping_edge: MappingEdge
    profile: UnknownLoadProfileV3


# --------------------------------------------------------------------------- #
# The I1 source-6 view: profiles for one reader scope.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnknownProfileView:
    """A read-only view satisfying I1's ``ProfileView.profiles_for_scope``.

    Promoted and retired profiles are not offered as fresh candidates: a
    promoted profile has become a device, and a retired one is kept only for
    history.  ``include_retired`` lets a caller inspect the retained rows.
    """

    profiles: tuple[UnknownLoadProfileV3, ...] = ()
    include_promoted: bool = False
    include_retired: bool = False

    def profiles_for_scope(self, reader_id: str) -> tuple[UnknownLoadProfileV3, ...]:
        return tuple(
            p
            for p in self.profiles
            if p.scope_endpoint_id == reader_id
            and (self.include_retired or p.lifecycle != "retired")
            and (self.include_promoted or p.lifecycle != "promoted")
        )

    def all_profiles(self) -> tuple[UnknownLoadProfileV3, ...]:
        return self.profiles

    def characterising(self, scope: str) -> tuple[UnknownLoadProfileV3, ...]:
        """Profiles at ``scope`` that have reached CHARACTERISED or beyond."""
        wanted = frozenset({"characterised", "candidate"})
        return tuple(
            p
            for p in self.profiles_for_scope(scope)
            if str(p.lifecycle) in wanted
        )


# --------------------------------------------------------------------------- #
# Observation parsing — used only to build the input to the pure clusterer.
# --------------------------------------------------------------------------- #


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _records(observation: Any) -> list[Mapping[str, Any]]:
    """Normalise the accepted observation shapes into a list of records."""
    if isinstance(observation, Mapping):
        for key in ("records", "observation", "observations"):
            value = observation.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [r for r in value if isinstance(r, Mapping)]
        return [observation]
    if isinstance(observation, Sequence) and not isinstance(observation, (str, bytes)):
        return [r for r in observation if isinstance(r, Mapping)]
    raise TypeError(f"unsupported observation: {type(observation).__name__}")


def _seen_events(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """HA events present in the observation frame (reader-only evidence has none)."""
    events: list[Mapping[str, Any]] = []
    for record in records:
        for event in record.get("events") or ():
            if isinstance(event, Mapping):
                events.append(event)
        for message in record.get("ha_messages") or ():
            if isinstance(message, Mapping):
                events.append(message)
    return events


def _is_single_subject(sample: Mapping[str, Any]) -> bool:
    """Whether a reader is a confirmed single-subject endpoint (DEC-05).

    Such an endpoint's steps are MEASURED device steps, not unknown load.  The
    simulator marks it with a zero declared averaging window; a caller may also
    pass the explicit ``single_subject`` flag.  We never guess from a device
    name.
    """
    if sample.get("single_subject") is True:
        return True
    window = sample.get("measurement_window_ms")
    return window == 0  # noqa: PLR2004 - the declared zero-window sentinel


def _reader_step_series(
    records: Sequence[Mapping[str, Any]],
    *,
    step_threshold_w: float,
) -> tuple[dict[str, list[tuple[datetime, float, float | None, str | None]]], dict[str, str]]:
    """Per-reader signed steps ``(at, delta, duration, entity_id)`` and parents.

    Only FINAL (settled) steps are used: a step is emitted when a reader's
    value changes by at least ``step_threshold_w`` and the new level holds for
    the next sample.  Single-subject endpoints are excluded (their steps are
    device evidence, not unknown load).
    """
    values: dict[str, list[tuple[datetime, float]]] = {}
    parents: dict[str, str] = {}
    entities: dict[str, str] = {}
    single: set[str] = set()
    for record in records:
        samples = record.get("reader_samples") or record.get("measurements") or {}
        if not isinstance(samples, Mapping):
            continue
        fallback = _parse_dt(record.get("timestamp"))
        for reader, sample in samples.items():
            if not isinstance(sample, Mapping):
                continue
            value = sample.get("value_w")
            if not _finite(value):
                value = sample.get("value")
            if not _finite(value):
                continue
            at = (
                _parse_dt(sample.get("state_timestamp"))
                or _parse_dt(sample.get("observed_at"))
                or fallback
            )
            if at is None:
                continue
            values.setdefault(str(reader), []).append((at, float(value)))
            parent = sample.get("parent")
            if isinstance(parent, str) and parent:
                parents[str(reader)] = parent
            entity_id = sample.get("entity_id")
            if isinstance(entity_id, str):
                entities[str(reader)] = entity_id
            if _is_single_subject(sample):
                single.add(str(reader))

    steps: dict[str, list[tuple[datetime, float, float | None, str | None]]] = {}
    for reader, points in values.items():
        if reader in single:
            continue
        ordered = sorted(points)
        reader_steps: list[tuple[datetime, float, float | None, str | None]] = []
        for index in range(1, len(ordered) - 1):
            (t0, v0), (t1, v1), (t2, v2) = ordered[index - 1 : index + 2]
            delta = v1 - v0
            if abs(delta) < step_threshold_w:
                continue
            if abs(v2 - v1) >= step_threshold_w:
                continue  # still settling: the step is not FINAL yet
            duration = max((t1 - t0).total_seconds(), (t2 - t1).total_seconds())
            reader_steps.append((t1, delta, duration, entities.get(reader)))
        steps[reader] = reader_steps
    return steps, parents


def _deepest(readers: Sequence[str], parents: Mapping[str, str]) -> tuple[str, ...]:
    """Readers with no stepped descendant (the deepest reader that saw a step)."""
    stepped = set(readers)
    result = []
    for reader in readers:
        if any(parents.get(other) == reader for other in stepped):
            continue
        result.append(reader)
    return tuple(result)


def _group_steps(
    steps: Mapping[str, Sequence[tuple[datetime, float, float | None, str | None]]],
    *,
    group_window_s: float,
) -> list[dict[str, tuple[datetime, float, float | None, str | None]]]:
    """Bucket near-simultaneous reader steps into candidate event groups.

    A bucket is anchored on its *latest* step so that a step chain spanning a
    settling window (ct_main → ct_a, 30 s apart) stays in one group while an
    unrelated step minutes later starts a fresh one.
    """
    buckets: list[dict[str, tuple[datetime, float, float | None, str | None]]] = []
    anchors: list[datetime] = []
    for reader in sorted(steps):
        for step in sorted(steps[reader], key=lambda s: s[0]):
            if not buckets or (step[0] - anchors[-1]).total_seconds() > group_window_s:
                buckets.append({reader: step})
                anchors.append(step[0])
            else:
                buckets[-1][reader] = step
                anchors[-1] = max(anchors[-1], step[0])
    return buckets


def _event_groups(
    buckets: Sequence[Mapping[str, tuple[datetime, float, float | None, str | None]]],
    haevents: Sequence[Mapping[str, Any]],
    *,
    versions: Any | None,
    group_window_s: float,
) -> tuple[tuple[EventGroup, ...], dict[int, bool]]:
    """Build EventGroup contracts and a per-group 'unresolved' flag.

    A group is UNRESOLVED when no HA state change sits near its onset: an
    HA-linked step has a device-level explanation and must not feed a profile.
    """
    from wattmosaic_engine.contracts import ReaderDelta  # local: keeps imports light

    groups: list[EventGroup] = []
    unresolved: dict[int, bool] = {}
    for index, bucket in enumerate(buckets):
        all_steps = sorted(bucket.items(), key=lambda item: item[1][0])
        opened = all_steps[0][1][0]
        closed = max(step[0] for _, step in all_steps)
        resolved = False
        for event in haevents:
            observed = _parse_dt(event.get("observed_at") or event.get("last_changed"))
            if observed is None:
                continue
            if abs((observed - opened).total_seconds()) <= group_window_s:
                resolved = True
                break
        unresolved[index] = not resolved
        deltas = []
        for reader, step in all_steps:
            deltas.append(
                ReaderDelta(
                    reader_id=reader,
                    delta_w=step[1],
                    sigma_w=None,
                    completeness="complete",
                )
            )
        status = (
            EventGroupStatus.UNRESOLVED if not resolved else EventGroupStatus.DETECTED
        )
        group = EventGroup(
            group_id=f"ug-{index}",
            revision=1,
            status=status,
            opened_at=opened,
            closed_at=closed,
            reader_deltas=deltas,
            grouping_reasons=("reader_step_co_occurrence",),
            versions=(
                versions
                if versions is not None
                else _default_versions()
            ),
        )
        # The Pydantic base uses ``use_enum_values=True``, so the validated
        # ``status`` is stored as its string value.  Restore the enum object so
        # consumers may read ``group.status.value`` (HB §12.3 vocabulary).
        object.__setattr__(group, "status", status)
        groups.append(group)
    return tuple(groups), unresolved


def _default_versions() -> Any:
    from wattmosaic_engine.contracts import VersionTriple

    return VersionTriple(
        topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-t6"
    )


def _candidates(
    groups: Sequence[EventGroup],
    unresolved: Mapping[int, bool],
) -> tuple[CandidateHypothesis, ...]:
    """One ``unknown_scope`` candidate per affected reader scope (HB §7.5)."""
    out: list[CandidateHypothesis] = []
    seen: set[tuple[str, str]] = set()
    for index, group in enumerate(groups):
        for delta in group.reader_deltas:
            key = ("unknown_scope", delta.reader_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                CandidateHypothesis(
                    candidate_id=f"unk-{delta.reader_id}",
                    subject_kind="unknown_scope",
                    subject_id=delta.reader_id,
                    sources=[CandidateSource.UNKNOWN_SCOPE],
                    evidence_terms={
                        "unresolved": 1.0 if unresolved.get(index, False) else 0.0,
                    },
                    transition="unknown_step",
                )
            )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Clustering and lifecycle (pure).
# --------------------------------------------------------------------------- #


def _duration_bucket(duration_s: float | None) -> int:
    if duration_s is None or not math.isfinite(duration_s):
        return -1
    return int(math.floor(duration_s / DURATION_BUCKET_S))


def _cluster_key(occurrence: UnknownOccurrence) -> tuple[int, int, int]:
    """Cluster identity ``(sign, |Δ| band ±20 %, duration bucket)``.

    The magnitude band is the rounded log of |Δ| so a ±20 % neighbour lands in
    the same band while a clearly different amplitude does not.  The profile
    id is derived from this key, so recurring steps share one persistent id.
    """
    magnitude = abs(occurrence.delta_w)
    band = 0 if magnitude <= 0.0 else int(math.floor(math.log10(magnitude) * 10.0))
    return (
        1 if occurrence.delta_w >= 0.0 else -1,
        band,
        _duration_bucket(occurrence.duration_s),
    )


def _matches(profile: UnknownLoadProfileV3, occurrence: UnknownOccurrence) -> bool:
    """Whether ``occurrence`` belongs to ``profile``'s signature cluster.

    Sign, |Δ| within ±20 % of the stored median and the same duration bucket
    must all agree (design §6 step 2).  A retired profile is never revived.
    """
    if profile.lifecycle == "retired":
        return False
    median_w = profile.signature.get("delta_w_median")
    if median_w is None or not math.isfinite(float(median_w)):
        return False
    if (occurrence.delta_w >= 0.0) != (float(median_w) >= 0.0):
        return False
    reference = abs(float(median_w))
    if reference <= 0.0:
        return abs(occurrence.delta_w) <= CLUSTER_TOLERANCE
    ratio = abs(abs(occurrence.delta_w) - reference) / reference
    if ratio > CLUSTER_TOLERANCE:
        return False
    return profile.signature.get("duration_bucket") == float(
        _duration_bucket(occurrence.duration_s)
    )


def _signature(
    profile: UnknownLoadProfileV3, occurrence: UnknownOccurrence
) -> dict[str, float]:
    """Append one occurrence to a profile signature via a running median."""
    median_w = float(profile.signature.get("delta_w_median", occurrence.delta_w))
    count = max(int(profile.event_count), 1)
    # An approximate median is enough for clustering: blend the new sample into
    # the stored centre with equal weight, then let later occurrences pull it.
    updated = median_w + (occurrence.delta_w - median_w) / (count + 1)
    signature = dict(profile.signature)
    signature["delta_w_median"] = float(updated)
    signature["peak_w"] = float(max(abs(float(signature.get("peak_w", 0.0))), abs(occurrence.delta_w)))
    signature["duration_bucket"] = float(_duration_bucket(occurrence.duration_s))
    return signature


def _is_periodic(occurrences: Sequence[UnknownOccurrence]) -> bool:
    """The P3 tag: >= 4 same-sign steps with CV(period) < 0.3."""
    if len(occurrences) < MIN_PERIODIC_STEPS:
        return False
    if len({1 if o.delta_w >= 0.0 else -1 for o in occurrences}) != 1:
        return False
    stamps = sorted(o.at for o in occurrences)
    periods = [
        (later - earlier).total_seconds()
        for earlier, later in zip(stamps, stamps[1:], strict=False)
    ]
    periods = [p for p in periods if p > 0.0]
    if len(periods) < MIN_PERIODIC_STEPS - 1:
        return False
    mean = sum(periods) / len(periods)
    if mean <= 0.0:
        return False
    variance = sum((p - mean) ** 2 for p in periods) / len(periods)
    return (math.sqrt(variance) / mean) < PERIODIC_CV_MAX


def _profile_id(scope: str, occurrence: UnknownOccurrence) -> str:
    key = _cluster_key(occurrence)
    canonical = f"{scope}|{key[0]}|{key[1]}|{key[2]}"
    import hashlib

    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"ulp-{digest[:16]}"


def _new_profile(scope: str, occurrence: UnknownOccurrence) -> UnknownLoadProfileV3:
    from wattmosaic_engine.contracts import UnknownLoadProfileV3

    signature = {
        "delta_w_median": float(occurrence.delta_w),
        "peak_w": float(abs(occurrence.delta_w)),
        "duration_bucket": float(_duration_bucket(occurrence.duration_s)),
        "periodic": 1.0 if occurrence.periodic else 0.0,
    }
    return UnknownLoadProfileV3(
        profile_id=_profile_id(scope, occurrence),
        scope_endpoint_id=scope,
        lifecycle="observing",
        signature=signature,
        event_count=0,
        independent_event_count=0,
        first_seen=occurrence.at,
        last_seen=occurrence.at,
    )


def _retire_stale(
    profiles: Sequence[UnknownLoadProfileV3], *, now: datetime
) -> tuple[UnknownLoadProfileV3, ...]:
    """Retire (but keep) profiles unseen for ``RETIRE_AFTER_DAYS``."""
    cutoff = now - timedelta(days=RETIRE_AFTER_DAYS)
    out: list[UnknownLoadProfileV3] = []
    for profile in profiles:
        if profile.lifecycle in ("retired", "promoted"):
            out.append(profile)
            continue
        if profile.last_seen < cutoff:
            out.append(profile.model_copy(update={"lifecycle": "retired"}))
        else:
            out.append(profile)
    return tuple(out)


def unexplained_scopes(result: JointInferenceResult) -> tuple[str, ...]:
    """Scopes where the unknown-scope competitor explains a FINAL step.

    Step 1 of T6: a scope qualifies when a device estimate carries no point for
    it (so the unknown alternative owns the delta, share 1.0 >= 80 %) or the
    inference result itself reports an UNRESOLVED event group.  ``None`` is an
    absent estimate, never 0 W (HB §18).
    """
    scopes: list[str] = []
    # An unknown estimate marks a scope where the unknown competitor was
    # retained.  Every one of those scopes qualifies (share >= 80 % when the
    # unknown column carries the unexplained step).
    for estimate in result.unknown_estimates:
        if estimate.scope_reader_id not in scopes:
            scopes.append(estimate.scope_reader_id)
    status = str(getattr(result.status, "value", result.status))
    if status == "unresolved":
        # With no device explanation, a reader whose residual actually carries
        # a step is unexplained; ``None`` means missing/absent, not a step.
        for scope, delta in result.residual_w_by_reader.items():
            if delta is not None and scope not in scopes:
                scopes.append(scope)
    return tuple(scopes)


def apply_occurrences(
    profiles: Sequence[UnknownLoadProfileV3],
    occurrences: Sequence[UnknownOccurrence],
    *,
    now: datetime,
) -> tuple[UnknownLoadProfileV3, ...]:
    """Fold new occurrences into the persistent profile set (pure).

    Lifecycle: OBSERVING → CHARACTERISED at
    ``MIN_CHARACTERISED_OCCURRENCES`` independent occurrences → CANDIDATE once
    characterised (offered to I1 through :class:`UnknownProfileView`).  A
    profile is **never** auto-CONFIRMED; the contract rejects it too.
    """
    current = list(_retire_stale(profiles, now=now))
    # Working set: (profile, its signature amplitudes) so periodicity of a
    # cluster is judged from every occurrence that landed in it, not just the
    # handful arriving in this one pass.
    history: dict[str, list[UnknownOccurrence]] = {}
    for profile in current:
        centre = float(profile.signature.get("delta_w_median", 0.0))
        history[profile.profile_id] = [
            UnknownOccurrence(
                at=profile.first_seen,
                delta_w=centre,
                scope_endpoint_id=profile.scope_endpoint_id,
            )
        ] * int(profile.event_count)

    for occurrence in sorted(occurrences, key=lambda o: (o.at, o.scope_endpoint_id)):
        active = next(
            (
                p
                for p in current
                if p.scope_endpoint_id == occurrence.scope_endpoint_id
                and _matches(p, occurrence)
            ),
            None,
        )
        if active is None:
            active = _new_profile(occurrence.scope_endpoint_id, occurrence)
            current.append(active)
            history[active.profile_id] = []

        cluster_history = [*history[active.profile_id], occurrence]
        signature = _signature(active, occurrence)
        if _is_periodic(cluster_history):
            signature["periodic"] = 1.0
        count = int(active.event_count) + 1
        lifecycle = str(active.lifecycle)
        if lifecycle == "observing" and count >= MIN_CHARACTERISED_OCCURRENCES:
            # CHARACTERISED is the terminal automatic state.  CANDIDATE is an
            # *offer* to the user (a separate, explicit step), never something
            # the background detector does by itself.
            lifecycle = "characterised"
        updated = active.model_copy(
            update={
                "lifecycle": lifecycle,
                "signature": signature,
                "event_count": count,
                "independent_event_count": int(active.independent_event_count) + 1,
                "last_seen": max(active.last_seen, occurrence.at),
            }
        )
        current[current.index(active)] = updated
        history[updated.profile_id] = cluster_history
    return tuple(current)


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def run_unknown_detection(
    observation: Any,
    *,
    profiles: Sequence[UnknownLoadProfileV3] | None = None,
    clock: Callable[[], datetime] | None = None,
    versions: Any | None = None,
    sink: ProfileSink | None = None,
    step_threshold_w: float = DEFAULT_STEP_THRESHOLD_W,
    group_window_s: float = DEFAULT_GROUP_WINDOW_S,
) -> UnknownDetectionResult:
    """Detect unexplained reader steps and update the persistent profile set.

    ``observation`` is the HB §17 observation frame (a list of records carrying
    ``reader_samples`` plus optional HA messages/events), or a single record.
    Returned profiles are *not* mutated in place: a caller threads them back in
    on the next pass to build independent occurrence counts.  When ``sink`` is
    supplied every changed profile is persisted in one S19-style batch.
    """
    now = clock() if clock is not None else None
    records = _records(observation)
    haevents = _seen_events(records)
    steps, parents = _reader_step_series(
        records, step_threshold_w=float(step_threshold_w)
    )
    buckets = _group_steps(steps, group_window_s=float(group_window_s))
    groups, unresolved = _event_groups(
        buckets, haevents, versions=versions, group_window_s=float(group_window_s)
    )

    occurrences: list[UnknownOccurrence] = []
    for index, bucket in enumerate(buckets):
        # An UNRESOLVED group is explained entirely by the unknown alternative
        # (share 1.0); a group with nearby HA evidence is device-explained
        # (share 0.0).  Only the former feeds a profile (design §6 step 1).
        unknown_share = 1.0 if unresolved.get(index, False) else 0.0
        if unknown_share < UNKNOWN_EXPLANATION_THRESHOLD:
            continue
        deepest = _deepest(list(bucket), parents)
        for reader in deepest:
            step = bucket[reader]
            delta = step[1]
            occurrences.append(
                UnknownOccurrence(
                    at=step[0],
                    delta_w=delta,
                    scope_endpoint_id=reader,
                    duration_s=step[2],
                    event_group_id=groups[index].group_id,
                    delta_y_w=delta,
                )
            )
            # With no device-level HA evidence attached to the group the
            # unknown-scope candidate explains the whole step (share 1.0,
            # >= the 80 % threshold).  Periodic tagging happens in
            # ``apply_occurrences`` from each cluster's accumulated history.

    # The caller may thread back either the profile list or the full previous
    # result (the acceptance harness does the latter).
    if isinstance(profiles, UnknownDetectionResult):
        prior_profiles = profiles.unknown_profiles
    else:
        prior_profiles = tuple(profiles or ())
    flat = tuple(
        sorted(occurrences, key=lambda o: (o.at, o.scope_endpoint_id))
    )

    if now is None:
        now = max((o.at for o in flat), default=None) or _latest_timestamp(records)
    updated = apply_occurrences(prior_profiles, flat, now=now)
    if sink is not None:
        for profile in updated:
            sink.put_profile(profile)
    return UnknownDetectionResult(
        event_groups=groups,
        candidates=_candidates(groups, unresolved),
        unknown_profiles=updated,
        unexplained=tuple(flat),
    )


def _latest_timestamp(records: Sequence[Mapping[str, Any]]) -> datetime:
    stamps = [
        parsed
        for record in records
        for parsed in [_parse_dt(record.get("timestamp"))]
        if parsed is not None
    ]
    if stamps:
        return max(stamps)
    # Deterministic epoch fallback: no wall clock.  A caller with real time
    # should inject ``clock`` explicitly (P6 does).
    return datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Promotion (user-driven, never automatic).
# --------------------------------------------------------------------------- #


def promote_profile(
    profile: UnknownLoadProfileV3,
    *,
    devices: DeviceSink,
    mapping_edges: Any,
    profiles: ProfileSink,
    clock: Callable[[], datetime] | None = None,
    device_id: str | None = None,
    mapping_edge_id: str | None = None,
    name: str | None = None,
) -> ProfilePromotion | None:
    """Promote a CHARACTERISED/CANDIDATE profile to a Physical Device.

    A device is created through D2's service with
    ``origin=unknown_profile_promotion`` and one PROVISIONAL mapping edge to the
    profile's scope.  The profile is left ``promoted`` (never CONFIRMED) and
    persisted through the injected profile sink.  Returns ``None`` when the
    profile is not promotable (already promoted/retired, or still observing).
    """
    lifecycle = str(profile.lifecycle)
    if lifecycle not in ("characterised", "candidate"):
        return None
    now = clock() if clock is not None else profile.last_seen
    target = device_id or f"pd-ulp-{profile.profile_id}"
    device = PhysicalDevice(
        device_id=target,
        name=name or f"Unknown load at {profile.scope_endpoint_id}",
        sign_class=(
            "producer" if float(profile.signature.get("delta_w_median", 0.0)) < 0.0 else "consumer"
        ),
        origin="unknown_profile_promotion",
        created_at=now,
    )
    created = devices.create_device(device)
    if created is not None and isinstance(created, PhysicalDevice):
        device = created

    edge = MappingEdge(
        mapping_edge_id=mapping_edge_id or f"m::unknown_profile::{profile.profile_id}",
        subject_kind="unknown_profile",
        subject_id=profile.profile_id,
        endpoint_id=profile.scope_endpoint_id,
        observability=Observability.INFERRED,
        lifecycle=EdgeLifecycleV3.PROVISIONAL,
        declared_by="inference",
        evidence=EdgeEvidence(),
        valid_from=now,
    )
    _publish_mapping_edge(mapping_edges, edge)

    promoted = profile.model_copy(update={"lifecycle": "promoted"})
    profiles.put_profile(promoted)
    return ProfilePromotion(device=device, mapping_edge=edge, profile=promoted)


def _publish_mapping_edge(mapping_edges: Any, edge: MappingEdge) -> None:
    """Persist an edge through whichever injected seam the caller provides."""
    adder = getattr(mapping_edges, "add_mapping_edge", None)
    if callable(adder):
        adder(edge)
        return
    propose = getattr(mapping_edges, "propose_mapping", None)
    promote = getattr(mapping_edges, "promote_mapping", None)
    if callable(propose):
        version = propose([edge], None)
        if callable(promote) and version is not None:
            promote(str(getattr(version, "value", version)), "unknown profile promotion")
        return
    raise TypeError("mapping_edges sink has no add_mapping_edge/propose_mapping")
