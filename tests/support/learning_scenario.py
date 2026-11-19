"""Standby learning through the real L4 -> L5/L6 -> registry -> I5 path (AT-03).

Test support only (Z3b moved the scenario stand-in ``models.learn`` out of the
engine).  Nothing here fabricates a :class:`DeviceEstimate`: every published
estimate comes from :func:`~wattmosaic_engine.inference.orchestrator.infer_steady`
over observed reader samples, with levels from the subjects' *active* models in
a real :class:`~wattmosaic_engine.models.registry.Registry` on a migrated DB.

Two standby paths:

* **declared single-subject endpoint** (``declared``: ``reader -> subject``,
  DEC-05).  From the instant the subject's linked HA entity reports a state, each
  observed frame is solved by ``infer_steady``; the MEASURED reading becomes an
  ABSOLUTE_ANCHOR observation through L4's
  :func:`~wattmosaic_engine.learning.observations.observation_for_estimate`,
  labelled with the HA-reported state (the transition the L4 builder cannot see)
  and keyed to that HA report as its one evidence event.  The L2 FIXED fitter
  learns the level and the version is *declared* through the registry's
  ``propose``/``activate`` lifecycle: one HA report is one independent event, so
  the L6 worker's >= 5-event promotion gate (DEC-32) can never promote it.
* **shared circuit** (``shared``: device-attributed observations).  They are
  stored in S18 (``learning_observations``), submitted to the real
  :class:`~wattmosaic_engine.learning.worker.LearningWorker`, fitted by L5's
  incremental FIXED learner and promoted by the evidence-frozen L5 gate.  The
  subject's last confirmed transition names the level it sits at; its model's
  level for that transition is the established identity (DEC-22) the steady
  solve publishes, ESTIMATED, at that level.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    DeviceEstimate,
    EvidenceRef,
    LearningObservation,
    PowerLevelV3,
    VersionTriple,
    observation_id,
)
from wattmosaic_engine.inference.orchestrator import infer_steady
from wattmosaic_engine.learning.observations import observation_for_estimate
from wattmosaic_engine.learning.worker import CycleReport, InlineExecutor, LearningWorker
from wattmosaic_engine.models.fixed import FixedPowerModel
from wattmosaic_engine.models.registry import DatabaseModelRepository, Registry
from wattmosaic_engine.residual.reader_residual import UnknownPrior
from wattmosaic_engine.runtime.interfaces import HeldReading, PredictionContext
from wattmosaic_engine.storage.database import Database

from tests.support.fakes import FakeMapping, FakeResidual, FakeTopology, fake_operator

__all__ = ["StandbyLearningRun", "learn_standby"]

_TOPOLOGY_VERSION = "topo-at03"
_MAPPING_VERSION = "map-at03"


@dataclass
class StandbyLearningRun:
    """What :func:`learn_standby` produced (all real engine objects)."""

    estimates: list[DeviceEstimate]
    registry: Registry
    worker_report: CycleReport
    anchors: list[LearningObservation]
    db: Database


class _Identities:
    """An ``EstablishedStructure`` holding identity constraints only."""

    def __init__(self, identities: Mapping[tuple[str, str], float]) -> None:
        self._identities = dict(identities)

    def groups(self) -> Mapping[str, tuple[str, ...]]:
        return {}

    def group_sums(self) -> Mapping[str, float]:
        return {}

    def identities(self) -> Mapping[tuple[str, str], float]:
        return self._identities


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tree(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The declared reader tree, from each sample's observable ``parent``."""
    parents: dict[str, str | None] = {}
    for record in records:
        for reader, sample in (record.get("reader_samples") or {}).items():
            if isinstance(sample, Mapping):
                parent = sample.get("parent")
                parents[str(reader)] = str(parent) if parent else None
    nodes: dict[str, dict[str, Any]] = {reader: {} for reader in parents}
    roots: dict[str, Any] = {}
    for reader, parent in sorted(parents.items()):
        if parent is not None and parent in nodes:
            nodes[parent][reader] = nodes[reader]
        else:
            roots[reader] = nodes[reader]
    return roots


def _held(record: Mapping[str, Any], at: datetime) -> dict[str, HeldReading]:
    held: dict[str, HeldReading] = {}
    for reader, sample in (record.get("reader_samples") or {}).items():
        value = sample.get("value_w", sample.get("value")) if isinstance(sample, Mapping) else None
        if value is None:
            continue  # absent stays absent (HB §18)
        held[str(reader)] = HeldReading(
            value=float(value), held_since=at, last_confirmed_at=at, state="held"
        )
    return held


def _level_of(observation: LearningObservation) -> PowerLevelV3 | None:
    for raw in ((observation.attributes or {}).get("level"), observation.transition):
        text = str(getattr(raw, "value", raw) or "").strip().lower()
        for member in PowerLevelV3:
            if member.value == text:
                return member
    return None


def learn_standby(
    observation: Iterable[Mapping[str, Any]],
    *,
    declared: Mapping[str, str],
    links: Mapping[str, str],
    shared: Sequence[LearningObservation] = (),
) -> StandbyLearningRun:
    """Learn standby levels and publish the subjects' steady estimates.

    ``declared`` maps a confirmed single-subject endpoint (reader) to its subject;
    ``links`` maps an HA entity id to the subject it is linked to.  ``shared``
    are device-attributed observations for subjects without such an endpoint.
    The estimates are published for the frame of the first linked HA report.
    """
    records = [r for r in observation if isinstance(r, Mapping)]
    tree = _tree(records)
    readers = tuple(sorted({str(r) for rec in records for r in rec.get("reader_samples") or {}}))
    endpoint_of = {subject: reader for reader, subject in declared.items()}
    for obs in shared:
        endpoint_of.setdefault(obs.subject_id, obs.endpoint_id)
    subjects = tuple(("device", subject) for subject in sorted(endpoint_of))
    declared_pairs = {(reader, subject) for reader, subject in declared.items()}
    residual = FakeResidual(
        UnknownPrior(scope_reader_id=readers[0], mu_w=0.0, sigma_w=50.0, lambda_u=0.25)
    )

    # The frame of the first linked HA state report, and that report's state.
    report: tuple[int, str, str, str] | None = None  # (index, subject, state, ref id)
    for index, record in enumerate(records):
        for message in record.get("ha_messages") or ():
            subject = links.get(str(message.get("entity_id")))
            if subject is not None and subject in endpoint_of and message.get("state"):
                context = message.get("context") or {}
                ref = str(context.get("id") or message.get("entity_id"))
                report = (index, subject, str(message["state"]).strip().lower(), ref)
                break
        if report is not None:
            break
    if report is None:
        raise ValueError("no linked HA state report in the observation")
    report_index, report_subject, report_state, report_ref = report
    origin = _as_utc(records[report_index]["timestamp"])

    db = Database(":memory:")
    now = [origin]
    registry = Registry(DatabaseModelRepository(db), clock=lambda: now[0])

    def solve(record: Mapping[str, Any], established: Any) -> list[DeviceEstimate]:
        at = _as_utc(record["timestamp"])
        now[0] = at
        topology = FakeTopology.from_tree(tree, at=at)
        mapping = FakeMapping.from_pairs(
            {("device", subject): endpoint_of[subject] for _kind, subject in subjects}
        )
        operator = fake_operator(
            topology, mapping, readers, subjects, at=at,
            topology_version_id=_TOPOLOGY_VERSION, mapping_version_id=_MAPPING_VERSION,
        )
        versions = VersionTriple(
            topology_version_id=_TOPOLOGY_VERSION,
            mapping_version_id=_MAPPING_VERSION,
            model_set_id=str(registry.model_set_id),
        )
        result = infer_steady(
            _held(record, at), operator, registry, residual, established, versions,
            lambda: at, declared_single_subject=declared_pairs,
        )
        return [_with_result(result, e) for e in result.device_estimates]

    # -- Path 1: anchors on the declared endpoint, from the HA report onward. --
    reader = endpoint_of[report_subject]
    anchors: list[LearningObservation] = []
    for record in records[report_index:]:
        if any(
            links.get(str(m.get("entity_id"))) == report_subject
            and str(m.get("state", "")).strip().lower() != report_state
            for m in record.get("ha_messages") or ()
        ):
            break  # the reported state ended
        for result, estimate in solve(record, _Identities({})):
            if estimate.subject_id != report_subject:
                continue
            anchor = observation_for_estimate(
                result, estimate, reader, result.versions,
                clock=lambda at=result.computed_at: at,
                single_subject_endpoints=declared,
            )
            if anchor is None or anchor.level_w is None:
                continue  # not MEASURED from the declared endpoint (DEC-05)
            anchors.append(
                anchor.model_copy(
                    update={
                        "observation_id": observation_id(
                            report_ref, record.get("sequence"), report_subject, reader,
                            _TOPOLOGY_VERSION,
                        ),
                        "event_group_ref": EvidenceRef(kind="ha_event", id=report_ref),
                        "transition": report_state,
                    }
                )
            )
    if anchors:
        now[0] = origin
        model = FixedPowerModel().fit(anchors)
        version = model.build_version(
            subject_id=report_subject, number=1, trainer_version="l2-anchor",
            evidence_refs=[EvidenceRef(kind="observation", id=a.observation_id) for a in anchors],
            created_at=origin,
        )
        registry.propose(version, reason="declared single-subject endpoint level")
        registry.activate(version.version_id, reason="declared")

    # -- Path 2: shared-circuit evidence through S18 + the L6 worker. ---------- #
    for obs in shared:
        db.repos.learning.put_observation(obs)
    worker = LearningWorker(
        db.repos.learning, registry=registry, executor=InlineExecutor(), clock=lambda: now[0]
    )
    worker.submit_observations(shared)
    worker_report = worker.drain()

    identities: dict[tuple[str, str], float] = {}
    last: dict[str, LearningObservation] = {}
    for obs in sorted(shared, key=lambda o: (o.created_at, o.observation_id)):
        if obs.status == "confirmed":
            last[obs.subject_id] = obs
    for subject, obs in last.items():
        level = _level_of(obs)
        if level is None or registry.active_version(subject) is None:
            continue
        prediction = registry.predict(subject, PredictionContext(at=origin, level=level))
        if prediction.mean_w is not None:
            identities[("device", subject)] = float(prediction.mean_w)

    estimates = [e for _r, e in solve(records[report_index], _Identities(identities))]
    return StandbyLearningRun(
        estimates=estimates,
        registry=registry,
        worker_report=worker_report,
        anchors=anchors,
        db=db,
    )


def _with_result(result: Any, estimate: DeviceEstimate) -> tuple[Any, DeviceEstimate]:
    return result, estimate
