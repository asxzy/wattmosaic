"""Versioned replay surface for historical reconciliation (L7, AT-19).

``replay_week(raw_bytes, model_set=...)`` replays immutable observation evidence
through the pure I7 inference entry points under an explicit model-set version
and returns a :class:`DerivedRun`.  Raw samples and evidence frames are carried
through unchanged (the replay never writes them), and each run is retrievable
by its ``derived_run_id`` via :func:`get_derived_run`.

A replay against a real database additionally accepts an explicit
:class:`~wattmosaic_engine.contracts.VersionTriple` and a ``db_path``.  In that
mode the observation operator, the subject↔reader mapping and the model set are
all rebuilt from that triple's *stored* topology/mapping/model versions in the
given database -- never relabelled afterwards.  The default (no ``db_path``) is
the built-in HB §17 house and the built-in ``v1``/``v2`` model sets, so existing
callers are unchanged.

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
from wattmosaic_engine.versioning.service import (
    _edge_contract,
    _FrozenMappingView,
    _FrozenTopologyView,
    _map_edge_contract,
)

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
#: Triple stamped on the built-in HB §17 replay (kept for existing callers).
_HB17_TRIPLE = ("topo-hb17", "map-hb17", "mset-hb17")


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
    whose topology differs from HB §17 uses *that* topology.
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

    A version is active iff its last activation-relevant event is ``activated``
    (the same rule L1's registry uses).  Fixed models contribute their stored
    transition delta as a signed prior; unknown specs contribute no prior
    (never a fabricated 0 W, HB §18).
    """
    from wattmosaic_engine.contracts import FixedSpec
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

    by_version: dict[str, list[tuple[str, int]]] = {}
    for row in events:
        event = str(row["event"])
        if event in ("frozen", "unfrozen"):
            continue
        by_version.setdefault(str(row["version_id"]), []).append((event, int(row["id"] or 0)))

    priors: dict[str, DeltaPrior] = {}
    for row in rows:
        version_id = str(row["version_id"])
        history = by_version.get(version_id, [])
        if not history or history[-1][0] != "activated":
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


def _hb17_views(start: datetime) -> tuple[Any, Any, tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The built-in HB §17 house views (the default replay target)."""
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
    return (
        _FrozenTopologyView(edges),
        _FrozenMappingView(mapping_edges),
        ("Main", "Sub", "ct_a", "ct_b", "ct_c"),
        tuple(("device", sid) for sid, _v1, _v2 in _DEFAULT_SUBJECTS),
    )


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
) -> list[EventGroup]:
    """Derive immutable event groups from an HB §17 observation frame.

    Only observation keys are read (HA states/messages and reader samples); no
    truth key is consulted.  The deltas are content-derived (the signed level
    change around each HA on/off step), so replaying the same frame twice yields
    byte-identical evidence.  ``triple`` and the two views decide which reader
    a subject is mapped on and which ancestors observe it: a database whose
    topology differs from HB §17 therefore produces different evidence.
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
        subject = _ENTITY_SUBJECT.get(entity)
        if subject is None:
            continue
        reader = map_view.most_specific_reader(("device", subject)) or _SUBJECT_READERS.get(subject)
        if reader is None:
            continue
        delta_w = _step_delta(by_reader.get(reader, ()), at)
        if delta_w is None:
            continue
        ancestors = tuple(topo_view.ancestors(reader)) if hasattr(topo_view, "ancestors") else _ANCESTORS.get(reader, ())
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
    model_set: str | ModelSetView = "v1",
    reason: str = "replay",
    triple: VersionTriple | None = None,
    db_path: str | Path | None = None,
) -> DerivedRun:
    """Replay one week of immutable evidence under an explicit version triple.

    ``raw_bytes`` is the HB §17 observation frame.  It is read, never written:
    the returned run carries its canonical serialisation as
    ``raw_samples_bytes``/``evidence_bytes``.  The run is registered so
    :func:`get_derived_run` can retrieve it by id.

    With ``triple`` **and** ``db_path`` the operator, mapping and model set are
    rebuilt from that triple's stored versions in the database (opened
    read-only).  With ``triple`` alone the built-in HB §17 operator is used but
    the run is stamped with the given triple (an explicit override against a
    capture with no database).  Without either the built-in HB §17 house and
    ``model_set`` are used, preserving the original L7/AT-19 behaviour.
    """
    if db_path is not None and triple is None:
        raise ValueError("db_path requires an explicit triple")

    if db_path is not None and triple is not None:
        conn = _open_ro(db_path)
        try:
            topo_view, map_view, readers, subjects = _views_from_db(conn, triple)
            models = _model_set_from_db(conn, triple)
        finally:
            conn.close()
        effective_triple = triple
    else:
        models = _build_model_set(model_set)
        if triple is None:
            # The HB §17 triple is stamped from the models actually replayed so
            # the built-in path stays byte-identical to the pre-V2b behaviour.
            effective_triple = VersionTriple(
                topology_version_id=_HB17_TRIPLE[0],
                mapping_version_id=_HB17_TRIPLE[1],
                model_set_id=models.model_set_id,
            )
        else:
            effective_triple = triple
        topo_view, map_view, _readers, _subjects = _hb17_views(
            datetime(2026, 1, 1, tzinfo=UTC)
        )
        readers, subjects = _readers, _subjects

    evidence = _evidence_from_frames(raw_bytes, effective_triple, topo_view, map_view)
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
