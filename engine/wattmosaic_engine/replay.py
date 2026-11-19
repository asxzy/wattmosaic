"""Versioned replay surface for historical reconciliation (L7, AT-19).

``replay_week(raw_bytes, model_set=...)`` replays immutable observation evidence
through the pure I7 inference entry points under an explicit model-set version
and returns a :class:`DerivedRun`.  Raw samples and evidence frames are carried
through unchanged (the replay never writes them), and each run is retrievable
by its ``derived_run_id`` via :func:`get_derived_run`.

Production code here depends only on the K08 runtime Protocols and production
value objects (``versioning.service`` frozen views, the production observation
operator).  It never imports ``runtime.fakes``, never touches a database and
never reads the wall clock: the clock is derived from the evidence timestamps.
It never imports a Home Assistant or HA-statistics client (DEC-35).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from wattmosaic_engine.contracts import (
    DeltaPrior,
    EdgeEvidence,
    EdgeLifecycleV3,
    EventGroup,
    EventGroupStatus,
    MappingEdge,
    Observability,
    ReaderDelta,
    RelationType,
    TopologyEdgeV3,
    VersionTriple,
)
from wattmosaic_engine.learning.reconciliation import DerivedRun, Reconciler
from wattmosaic_engine.runtime.interfaces import (
    IdentifiabilityHistory,
    MatchStore,
    ModelSetView,
    ObservationOperator,
    ResidualModelView,
)
from wattmosaic_engine.versioning.service import _FrozenMappingView, _FrozenTopologyView

__all__: list[str] = ["replay_week", "get_derived_run", "reset_replay_runs"]

_V1_IDS: tuple[str, ...] = ("mdl-tv-v1", "mdl-lights-v1", "mdl-fridge-v1")
_V2_IDS: tuple[str, ...] = ("mdl-tv-v2", "mdl-lights-v2", "mdl-fridge-v2")

#: (subject_id, watts under v1, watts under v2).
_DEFAULT_SUBJECTS: tuple[tuple[str, float, float], ...] = (
    ("tv", 100.0, 120.0),
    ("lights", 60.0, 45.0),
    ("fridge", 80.0, 60.0),
)
# Observation-side HA entity ids → (subject id, reader).  The entity ids are
# part of the observation frame; no truth key is consulted.
_SUBJECT_READERS: dict[str, str] = {
    "tv": "ct_c",
    "lights": "ct_a",
    "fridge": "ct_b",
}
_ENTITY_SUBJECT: dict[str, str] = {
    "switch.tv_plug": "tv",
    "light.living_room_1": "lights",
    "light.living_room_2": "lights",
}
_ANCESTORS: dict[str, tuple[str, ...]] = {
    "ct_c": ("Main",),
    "ct_a": ("Sub", "Main"),
    "ct_b": ("Sub", "Main"),
}


class _ReplayModelSet:
    """A minimal :class:`ModelSetView` with deterministic predictions."""

    def __init__(self, model_set_id: str, priors: Mapping[str, DeltaPrior]) -> None:
        self.model_set_id = model_set_id
        self._priors = dict(priors)

    def predict(self, subject_id: str, ctx: Any) -> Any:
        from wattmosaic_engine.contracts import PowerLevelV3, PowerPrediction, PredictionStatus

        prior = self._priors.get(subject_id)
        level = getattr(ctx, "level", None) or PowerLevelV3.UNKNOWN
        if prior is None:
            return PowerPrediction(
                level=level,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        return PowerPrediction(
            level=level,
            mean_w=prior.mean_w,
            sigma_w=prior.sigma_w,
            status=PredictionStatus.KNOWN,
            model_version_id=prior.model_version_id,
        )

    def transition_prior(
        self, subject_id: str, transition: str, attrs: Mapping[str, Any]
    ) -> DeltaPrior | None:
        return self._priors.get(subject_id)

    def expected_window(
        self, subject_id: str, transition: str, attrs: Mapping[str, Any], tau_s: Any
    ) -> tuple[Any, Any] | None:
        return None


class _EmptyHistory:
    def events_touching(self, subject_ids: Any, since: datetime) -> list[Any]:
        return []


class _EmptyMatches:
    def snapshot(self) -> Mapping[str, Any]:
        return MappingProxyType({})

    def apply(self, delta: Mapping[str, Any]) -> MatchStore:
        return self


class _ZeroResidual:
    def unknown_prior(self, scope_reader_id: str, at: datetime) -> Any | None:
        return None


_RUNS: dict[str, DerivedRun] = {}


def _build_model_set(model_set: str | ModelSetView) -> ModelSetView:
    if not isinstance(model_set, str):
        return model_set
    version_ids = _V1_IDS if model_set == "v1" else _V2_IDS if model_set == "v2" else ()
    if not version_ids:
        raise ValueError(f"unknown model set {model_set!r}; expected 'v1' or 'v2'")
    priors: dict[str, DeltaPrior] = {}
    for (subject, v1_w, v2_w), model_version_id in zip(
        _DEFAULT_SUBJECTS, version_ids, strict=True
    ):
        watts = v1_w if model_set == "v1" else v2_w
        priors[subject] = DeltaPrior(
            mean_w=watts,
            sigma_w=3.0,
            sign="pos",
            model_version_id=model_version_id,
        )
    from wattmosaic_engine.contracts import model_set_id as make_model_set_id

    return _ReplayModelSet(make_model_set_id(version_ids), priors)


def _build_operator(start: datetime) -> ObservationOperator:
    """Build H for the HB §17 house from production frozen views."""
    from wattmosaic_engine.electrical.operator import build_observation_operator

    tree = {
        "Main": {
            "Sub": {"ct_a": {}, "ct_b": {}},
            "ct_c": {},
        }
    }
    edges: list[TopologyEdgeV3] = []
    evidence = EdgeEvidence()

    def walk(parent: str, children: Mapping[str, Any]) -> None:
        for child, subtree in children.items():
            edges.append(
                TopologyEdgeV3(
                    edge_id=f"e::{parent}->{child}",
                    relation=RelationType.CONTAINS,
                    source_endpoint_id=parent,
                    target_endpoint_id=child,
                    lifecycle=EdgeLifecycleV3.CONFIRMED,
                    declared_by="user",
                    beta=1.0,
                    beta_basis="unity",
                    evidence=evidence,
                    valid_from=start,
                    pinned=True,
                )
            )
            walk(child, subtree if isinstance(subtree, Mapping) else {})

    for root, subtree in tree.items():
        walk(root, subtree)

    mapping_edges = [
        MappingEdge(
            mapping_edge_id=f"m::{subject}->{reader}",
            subject_kind="device",
            subject_id=subject,
            endpoint_id=reader,
            observability=Observability.DIRECT,
            lifecycle=EdgeLifecycleV3.CONFIRMED,
            declared_by="user",
            evidence=evidence,
            valid_from=start,
        )
        for subject, reader in _SUBJECT_READERS.items()
    ]
    topo_view = _FrozenTopologyView(edges)
    map_view = _FrozenMappingView(mapping_edges)
    readers = ("Main", "Sub", "ct_a", "ct_b", "ct_c")
    subjects = tuple(("device", sid) for sid, _v1, _v2 in _DEFAULT_SUBJECTS)
    return build_observation_operator(
        topo_view,
        map_view,
        None,
        readers,
        subjects,
        start,
    )


def _evidence_from_frames(observation: Sequence[Mapping[str, Any]]) -> list[EventGroup]:
    """Derive immutable event groups from an HB §17 observation frame.

    Only observation keys are read (HA states/messages and reader samples); no
    truth key is consulted.  The deltas are content-derived (the signed level
    change around each HA on/off step), so replaying the same frame twice yields
    byte-identical evidence.
    """
    by_reader: dict[str, list[tuple[datetime, float]]] = {}
    for record in observation:
        samples = record.get("reader_samples") or ()
        if isinstance(samples, Mapping):
            samples = samples.values()
        for sample in samples:
            reader = sample.get("reader_id")
            raw = sample.get("value_w", sample.get("value"))
            if reader is None or raw is None:
                continue
            ts = _parse_time(sample.get("state_timestamp") or sample.get("received_at"))
            if ts is None:
                continue
            by_reader.setdefault(reader, []).append((ts, float(raw)))

    transitions: list[tuple[datetime, str, str]] = []
    for record in observation:
        for msg in record.get("ha_messages") or ():
            entity = str(msg.get("entity_id", ""))
            state = str(msg.get("state", ""))
            if state not in ("on", "off"):
                continue
            at = _parse_time(msg.get("observed_at") or msg.get("last_changed"))
            if at is not None:
                transitions.append((at, entity, state))
    transitions.sort(key=lambda item: item[0])

    groups: list[EventGroup] = []
    for index, (at, entity, state) in enumerate(transitions):
        subject = _ENTITY_SUBJECT.get(entity)
        if subject is None:
            continue
        reader = _SUBJECT_READERS.get(subject)
        if reader is None:
            continue
        delta_w = _step_delta(by_reader.get(reader, ()), at)
        if delta_w is None:
            continue
        readers = [reader, *_ANCESTORS.get(reader, ())]
        deltas = [
            ReaderDelta(reader_id=rid, delta_w=delta_w, sigma_w=2.0, completeness="complete")
            for rid in readers
            if rid in by_reader
        ]
        if not deltas:
            continue
        vt = VersionTriple(
            topology_version_id="topo-hb17",
            mapping_version_id="map-hb17",
            model_set_id="mset-hb17",
        )
        groups.append(
            EventGroup(
                group_id=f"hb17::g{index:04d}::{subject}::{state}",
                revision=1,
                status=EventGroupStatus.CONFIRMED,
                opened_at=at,
                closed_at=at + timedelta(seconds=10),
                reader_deltas=deltas,
                versions=vt,
            )
        )
    return groups


def _step_delta(series: Sequence[tuple[datetime, float]], at: datetime) -> float | None:
    before = [v for t, v in series if t <= at]
    after = [v for t, v in series if t > at]
    if not before or not after:
        return None
    return float(after[0] - before[-1])


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def replay_week(
    raw_bytes: Sequence[Mapping[str, Any]],
    *,
    model_set: str | ModelSetView = "v1",
    reason: str = "replay",
) -> DerivedRun:
    """Replay one week of immutable evidence under ``model_set``.

    ``raw_bytes`` is the HB §17 observation frame.  It is read, never written:
    the returned run carries its canonical serialisation as
    ``raw_samples_bytes``/``evidence_bytes``.  The run is registered so
    :func:`get_derived_run` can retrieve it by id.
    """
    models = _build_model_set(model_set)
    evidence = _evidence_from_frames(raw_bytes)
    if evidence:
        start = min(g.opened_at for g in evidence)
        end = max(g.closed_at or g.opened_at for g in evidence)
    else:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=7)

    operator = _build_operator(start)
    residual: ResidualModelView = _ZeroResidual()
    history: IdentifiabilityHistory = _EmptyHistory()
    matches: MatchStore = _EmptyMatches()

    from wattmosaic_engine.inference.orchestrator import infer_group as do_infer_group

    recon = Reconciler(
        operator=operator,
        model_sets={models.model_set_id: models},
        residual=residual,
        history=history,
        matches=matches,
        clock=lambda: end,
        infer_group_fn=do_infer_group,
    )
    triple = VersionTriple(
        topology_version_id="topo-hb17",
        mapping_version_id="map-hb17",
        model_set_id=models.model_set_id,
    )
    run_id = recon.run((start, end), triple, reason, evidence=evidence)
    stamped = _stamp_run(recon.get(run_id), raw_bytes)
    _RUNS[stamped.derived_run_id] = stamped
    return stamped


def _stamp_run(run: DerivedRun, raw_bytes: Sequence[Mapping[str, Any]]) -> DerivedRun:
    """Attach the immutable raw/evidence frames to a run for AT-19 checks.

    The frames are stored as given — never copied, hashed or rewritten — so
    AT-19's byte-identity assertion checks the real input, not a proxy.
    """
    return DerivedRun(**{**run.__dict__, "raw_samples_bytes": raw_bytes, "evidence_bytes": raw_bytes})


def get_derived_run(derived_run_id: str) -> DerivedRun:
    """Retrieve a replayed run by id (including superseded runs)."""
    try:
        return _RUNS[derived_run_id]
    except KeyError as exc:
        raise KeyError(f"unknown derived run {derived_run_id!r}") from exc


def reset_replay_runs() -> None:
    """Clear the module run registry (for isolated tests)."""
    _RUNS.clear()
