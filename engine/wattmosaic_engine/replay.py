"""Versioned replay surface for historical reconciliation (L7, AT-19).

``replay_week(raw_bytes, model_set=...)`` replays immutable observation evidence
through the pure I7 inference entry points under an explicit model-set version
and returns a :class:`DerivedRun`.  Raw samples and evidence frames are carried
through unchanged (the replay never writes them), and each run is retrievable
by its ``derived_run_id`` via :func:`get_derived_run`.

A replay always runs against a real database: the caller passes an explicit
:class:`~wattmosaic_engine.contracts.VersionTriple` and a ``db_path``.  The
observation operator, the subject↔reader mapping, the HA entity → subject links
and the model set are all rebuilt from that triple's *stored*
topology/mapping/entity-link/model versions in the given database -- never
relabelled afterwards.  There is no built-in house and no built-in model set:
a scenario is whatever the database declares.

Production code here depends only on the K08 runtime Protocols and production
value objects (``versioning.service`` frozen views, the production observation
operator).  It never imports ``runtime.fakes``, never writes a database and
never reads the wall clock: the clock is derived from the evidence timestamps.
It never imports a Home Assistant or HA-statistics client (DEC-35).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wattmosaic_engine.contracts import (
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    MappingEdge,
    ReaderDelta,
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
from wattmosaic_engine.versioning.service import (
    _edge_contract,
    _FrozenMappingView,
    _FrozenTopologyView,
    _map_edge_contract,
)

__all__: list[str] = ["replay_week", "get_derived_run", "reset_replay_runs"]



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


# --------------------------------------------------------------------------- #
# Topology / mapping / model views from a stored database (V2b).
# --------------------------------------------------------------------------- #


def _open_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open a capture database read-only: a replay never writes to its input."""
    path = Path(db_path)
    if not path.exists():
        raise ValueError(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _edge_ids(conn: sqlite3.Connection, table: str, column: str, version_id: str) -> list[str]:
    row = conn.execute(
        f"SELECT {column} FROM {table} WHERE version_id=?", (version_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown stored version {version_id!r} in {table}")
    raw = row[0] or "[]"
    return list(json.loads(raw) if isinstance(raw, str) else raw)


def _views_from_db(
    conn: sqlite3.Connection, triple: VersionTriple
) -> tuple[Any, Any, tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Rebuild the stored topology/mapping views for ``triple``.

    The ids come from the version rows themselves, and the edge rows come from
    the same S14/S15 tables the live engine reads, so a replay of a database
    with its own topology uses *that* topology.
    """
    topo_ids = _edge_ids(conn, "topology_versions_v3", "edge_ids_json", triple.topology_version_id)
    topo_edges: list[TopologyEdgeV3] = []
    if topo_ids:
        placeholders = ",".join("?" for _ in topo_ids)
        rows = conn.execute(
            f"SELECT * FROM topology_edges WHERE edge_id IN ({placeholders}) ORDER BY edge_id",  # noqa: S608 - bound ids
            tuple(topo_ids),
        ).fetchall()
        topo_edges = [_edge_contract(dict(row)) for row in rows]

    map_ids = _edge_ids(conn, "mapping_versions", "mapping_edge_ids_json", triple.mapping_version_id)
    map_edges: list[MappingEdge] = []
    if map_ids:
        placeholders = ",".join("?" for _ in map_ids)
        rows = conn.execute(
            f"SELECT * FROM mapping_edges WHERE mapping_edge_id IN ({placeholders}) ORDER BY mapping_edge_id",  # noqa: S608 - bound ids
            tuple(map_ids),
        ).fetchall()
        map_edges = [_map_edge_contract(dict(row)) for row in rows]

    topo_view = _FrozenTopologyView(topo_edges)
    map_view = _FrozenMappingView(map_edges)
    readers = tuple(
        sorted(
            {
                *(e.source_endpoint_id for e in topo_edges),
                *(e.target_endpoint_id for e in topo_edges),
                *(e.endpoint_id for e in map_edges),
            }
        )
    )
    subjects = tuple(sorted({("device", e.subject_id) for e in map_edges}))
    return topo_view, map_view, readers, subjects


def _model_set_from_db(conn: sqlite3.Connection, triple: VersionTriple) -> ModelSetView:
    """Build the model set named by ``triple`` from the stored L1 versions.

    ``triple.model_set_id`` is content-derived (``mset-<hash>`` of the active
    version ids), so the set is located in the append-only event log: the log is
    walked instant by instant and, at each instant, every subject's active
    version is derived with L1's own rule (:func:`derive_active_version_id`).
    The first instant (latest first) whose active ids hash to the requested id
    names the versions replayed.  An id that names no stored set yields an
    empty model set -- never another set relabelled with that id.  Fixed
    models contribute their stored transition delta as a signed prior; other
    specs contribute no prior (never a fabricated 0 W, HB §18).
    """
    from wattmosaic_engine.contracts import FixedSpec
    from wattmosaic_engine.contracts import model_set_id as make_model_set_id
    from wattmosaic_engine.models.registry import derive_active_version_id
    from wattmosaic_engine.models.spec import SPEC_SCHEMA_VERSION, decode_spec

    try:
        rows = conn.execute(
            "SELECT version_id, subject_id, spec_json, schema_version "
            "FROM power_model_versions_v3"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    try:
        events = conn.execute(
            "SELECT version_id, event, at, id FROM power_model_events ORDER BY at, id"
        ).fetchall()
    except sqlite3.Error:
        events = []

    subject_of = {str(row["version_id"]): str(row["subject_id"]) for row in rows}
    logged = [
        {"version_id": str(e["version_id"]), "event": str(e["event"]), "at": str(e["at"]),
         "id": int(e["id"] or 0)}
        for e in events
        if str(e["version_id"]) in subject_of
    ]

    def active_at(instant: str) -> list[str]:
        per_subject: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for event in logged:
            if event["at"] > instant:
                continue
            vid = event["version_id"]
            per_subject.setdefault(subject_of[vid], {}).setdefault(vid, []).append(event)
        active = (derive_active_version_id(by_version) for by_version in per_subject.values())
        return [vid for vid in active if vid is not None]

    chosen: list[str] = []
    for instant in sorted({event["at"] for event in logged}, reverse=True):
        active = active_at(instant)
        if active and make_model_set_id(active) == triple.model_set_id:
            chosen = active
            break

    priors: dict[str, DeltaPrior] = {}
    for row in rows:
        version_id = str(row["version_id"])
        if version_id not in chosen:
            continue
        try:
            schema = int(row["schema_version"] or SPEC_SCHEMA_VERSION)
            spec = decode_spec(row["spec_json"], schema)
        except (TypeError, ValueError, KeyError):
            continue
        if not isinstance(spec, FixedSpec):
            continue
        mean = next(
            (stat.median_w for stat in spec.transition_delta.values() if stat.median_w is not None),
            None,
        )
        sigma = next(
            (stat.mad_w for stat in spec.transition_delta.values() if stat.mad_w is not None),
            None,
        )
        if mean is None:
            continue
        priors[str(row["subject_id"])] = DeltaPrior(
            mean_w=float(mean),
            sigma_w=float(sigma) if sigma is not None else 3.0,
            sign="pos" if mean >= 0 else "neg",
            model_version_id=version_id,
        )
    return _ReplayModelSet(triple.model_set_id, priors)


def _entity_subjects_from_db(
    conn: sqlite3.Connection, subjects: Sequence[tuple[str, str]]
) -> dict[str, str]:
    """HA entity id -> subject id, from the stored ``ha_entity_links`` (DEC-08).

    Only links to a subject the replayed mapping version knows are kept; an
    entity linked to several of them is ambiguous and dropped (a replay never
    guesses which subject an HA transition belongs to).
    """
    known = {subject_id for kind, subject_id in subjects if kind == "device"}
    try:
        rows = conn.execute(
            "SELECT entity_id, target_id FROM ha_entity_links "
            "WHERE target_kind='device' ORDER BY entity_id, target_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    targets: dict[str, set[str]] = {}
    for row in rows:
        target = str(row["target_id"])
        if target in known:
            targets.setdefault(str(row["entity_id"]), set()).add(target)
    return {entity: next(iter(ids)) for entity, ids in targets.items() if len(ids) == 1}


def _build_operator(
    start: datetime,
    topo_view: Any,
    map_view: Any,
    readers: Sequence[str],
    subjects: Sequence[tuple[str, str]],
) -> ObservationOperator:
    """Build H for one frozen (topology, mapping) view pair."""
    from wattmosaic_engine.electrical.operator import build_observation_operator

    return build_observation_operator(topo_view, map_view, None, tuple(readers), tuple(subjects), start)


def _evidence_from_frames(
    observation: Sequence[Mapping[str, Any]],
    triple: VersionTriple,
    topo_view: Any,
    map_view: Any,
    entity_subjects: Mapping[str, str],
) -> list[EventGroup]:
    """Derive immutable event groups from an observation frame.

    Only observation keys are read (HA states/messages and reader samples); no
    truth key is consulted.  The deltas are content-derived (the signed level
    change around each HA on/off step), so replaying the same frame twice yields
    byte-identical evidence.  ``entity_subjects`` (the stored HA entity links)
    names the subject of each HA transition; ``triple`` and the two views decide
    which reader a subject is mapped on and which ancestors observe it, so a
    database with a different topology produces different evidence.
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
            by_reader.setdefault(str(reader), []).append((ts, float(raw)))

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
        subject = entity_subjects.get(entity)
        if subject is None:
            continue
        reader = map_view.most_specific_reader(("device", subject))
        if reader is None:
            continue
        delta_w = _step_delta(by_reader.get(reader, ()), at)
        if delta_w is None:
            continue
        ancestors = tuple(topo_view.ancestors(reader)) if hasattr(topo_view, "ancestors") else ()
        readers = [reader, *ancestors]
        deltas = [
            ReaderDelta(reader_id=rid, delta_w=delta_w, sigma_w=2.0, completeness="complete")
            for rid in readers
            if rid in by_reader
        ]
        if not deltas:
            continue
        groups.append(
            EventGroup(
                group_id=f"replay::g{index:04d}::{subject}::{state}",
                revision=1,
                status=EventGroupStatus.CONFIRMED,
                opened_at=at,
                closed_at=at + timedelta(seconds=10),
                reader_deltas=deltas,
                versions=triple,
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
    triple: VersionTriple,
    db_path: str | Path,
    model_set: ModelSetView | None = None,
    reason: str = "replay",
) -> DerivedRun:
    """Replay one week of immutable evidence under an explicit version triple.

    ``raw_bytes`` is an observation frame (HA messages + reader samples).  It is
    read, never written: the returned run carries it as
    ``raw_samples_bytes``/``evidence_bytes``.  The run is registered so
    :func:`get_derived_run` can retrieve it by id.

    The operator, mapping, HA entity links and model set are rebuilt from
    ``triple``'s stored versions in ``db_path`` (opened read-only).  An explicit
    ``model_set`` (any :class:`ModelSetView`) replaces the stored model set; the
    run is then stamped with that view's ``model_set_id``.
    """
    if triple is None or db_path is None:
        raise ValueError("replay_week needs an explicit triple and db_path")

    conn = _open_ro(db_path)
    try:
        topo_view, map_view, readers, subjects = _views_from_db(conn, triple)
        entity_subjects = _entity_subjects_from_db(conn, subjects)
        models = model_set if model_set is not None else _model_set_from_db(conn, triple)
    finally:
        conn.close()
    effective_triple = (
        triple
        if model_set is None
        else triple.model_copy(update={"model_set_id": model_set.model_set_id})
    )

    evidence = _evidence_from_frames(
        raw_bytes, effective_triple, topo_view, map_view, entity_subjects
    )
    if evidence:
        start = min(g.opened_at for g in evidence)
        end = max(g.closed_at or g.opened_at for g in evidence)
    else:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=7)

    operator = _build_operator(start, topo_view, map_view, readers, subjects)
    residual: ResidualModelView = _ZeroResidual()
    history: IdentifiabilityHistory = _EmptyHistory()
    matches: MatchStore = _EmptyMatches()

    from wattmosaic_engine.inference.orchestrator import infer_group as do_infer_group

    recon = Reconciler(
        operator=operator,
        model_sets={effective_triple.model_set_id: models},
        residual=residual,
        history=history,
        matches=matches,
        clock=lambda: end,
        infer_group_fn=do_infer_group,
    )
    run_id = recon.run((start, end), effective_triple, reason, evidence=evidence)
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
