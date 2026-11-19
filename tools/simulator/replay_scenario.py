"""A replayable scenario database for the v3 replay surface (L7 / AT-19).

:func:`wattmosaic_engine.replay.replay_week` replays only what a database
declares: its stored topology version (S14), mapping version (S15), HA entity
links (DEC-08) and the power-model versions of the L1 registry.  This module
writes such a database for a simulated house, through the engine's own
declaration paths -- :class:`~wattmosaic_engine.versioning.service.VersionService`
for topology/mapping, the entities repository for HA links and the L1
:class:`~wattmosaic_engine.models.registry.Registry` for model versions -- so a
replay test or the audit tool exercises the same stored rows a real install has.

The scenario itself (which subject sits on which reader, which HA entity drives
it, what each model set predicts) is simulator data and lives here, never in
the engine.  The topology is taken from the observation frames' own declared
``parent`` metadata (observable, never truth).

Two model sets are written: ``"v1"`` is activated first, ``"v2"`` supersedes it
one second later, so both are addressable by the content-derived ``model_set_id``
the registry reports as of each activation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "REPLAY_DEVICES",
    "REPLAY_ENTITY_LINKS",
    "REPLAY_MODEL_SETS",
    "build_replay_db",
    "declared_parents",
]

#: Declared ``subject -> reader`` mapping of the reference house's replay.
REPLAY_DEVICES: dict[str, str] = {
    "tv": "ct_c",
    "lights": "ct_a",
    "fridge": "ct_b",
}

#: Declared HA entity -> subject links (an observable declaration).
REPLAY_ENTITY_LINKS: dict[str, str] = {
    "switch.tv_plug": "tv",
    "light.living_room_1": "lights",
    "light.living_room_2": "lights",
}

#: Per model set, the ``on`` transition delta (W) of each subject.
REPLAY_MODEL_SETS: dict[str, dict[str, float]] = {
    "v1": {"tv": 100.0, "lights": 60.0, "fridge": 80.0},
    "v2": {"tv": 120.0, "lights": 45.0, "fridge": 60.0},
}

_ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def declared_parents(frames: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``child -> parent`` from the frames' declared reader ``parent`` metadata."""
    parents: dict[str, str] = {}
    for record in frames:
        samples = record.get("reader_samples") or {}
        if isinstance(samples, Mapping):
            samples = samples.values()
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            reader = sample.get("reader_id")
            parent = sample.get("parent")
            if isinstance(reader, str) and isinstance(parent, str) and parent:
                parents[reader] = parent
    return parents


def build_replay_db(
    path: str | Path,
    frames: Iterable[Mapping[str, Any]] = (),
    *,
    devices: Mapping[str, str] | None = None,
    entity_links: Mapping[str, str] | None = None,
    model_sets: Mapping[str, Mapping[str, float]] | None = None,
    parents: Mapping[str, str] | None = None,
    at: datetime = _ORIGIN,
) -> dict[str, Any]:
    """Write the scenario into a new database at ``path``.

    Returns ``{name: VersionTriple}`` -- one triple per model set, all sharing
    the declared topology and mapping versions.
    """
    from wattmosaic_engine.contracts import (
        EdgeEvidence,
        EdgeLifecycleV3,
        FixedSpec,
        HaEntityLink,
        LevelStat,
        MappingEdge,
        ModelBasis,
        ModelClass,
        Observability,
        PowerModelVersion,
        RelationType,
        TopologyEdgeV3,
        VersionTriple,
    )
    from wattmosaic_engine.contracts import model_set_id as make_model_set_id
    from wattmosaic_engine.models.registry import DatabaseModelRepository, Registry
    from wattmosaic_engine.storage.database import Database
    from wattmosaic_engine.versioning.service import VersionService

    devices = dict(REPLAY_DEVICES if devices is None else devices)
    entity_links = dict(REPLAY_ENTITY_LINKS if entity_links is None else entity_links)
    model_sets = dict(REPLAY_MODEL_SETS if model_sets is None else model_sets)
    parents = dict(declared_parents(frames) if parents is None else parents)

    now = [at]
    clock = lambda: now[0]  # noqa: E731 - injected, deterministic
    db = Database(Path(path))
    try:
        svc = VersionService(
            topology_repo=db.repos.topology_v3,
            mapping_repo=db.repos.mapping,
            clock=clock,
            model_set_id=make_model_set_id([]),
        )
        evidence = EdgeEvidence(support=8, independent_causes=2)
        topology = [
            TopologyEdgeV3(
                edge_id=f"te::declared::{parent}::{child}",
                relation=RelationType.CONTAINS,
                source_endpoint_id=parent,
                target_endpoint_id=child,
                lifecycle=EdgeLifecycleV3.CONFIRMED,
                declared_by="user",
                beta=1.0,
                beta_basis="unity",
                evidence=evidence,
                valid_from=at,
                pinned=True,
            )
            for child, parent in sorted(parents.items())
        ]
        if topology:
            svc.promote_topology(svc.propose_topology(topology, None), "declared topology")
        mapping = [
            MappingEdge(
                mapping_edge_id=f"me::{subject}->{reader}",
                subject_kind="device",
                subject_id=subject,
                endpoint_id=reader,
                observability=Observability.DIRECT,
                lifecycle=EdgeLifecycleV3.CONFIRMED,
                declared_by="user",
                evidence=evidence,
                valid_from=at,
            )
            for subject, reader in sorted(devices.items())
        ]
        svc.promote_mapping(svc.propose_mapping(mapping, None), "declared mapping")
        base = svc.current()

        for entity, subject in sorted(entity_links.items()):
            db.repos.entities.add_entity_link(
                HaEntityLink(
                    entity_id=entity,
                    target_kind="device",
                    target_id=subject,
                    role="load_state",
                    valid_from=at,
                    declared_by="user",
                )
            )

        registry = Registry(DatabaseModelRepository(db), clock=clock)
        triples: dict[str, Any] = {}
        for number, (name, watts) in enumerate(model_sets.items(), start=1):
            now[0] = at + timedelta(seconds=number)
            for subject, delta_w in sorted(watts.items()):
                version = registry.propose(
                    PowerModelVersion(
                        version_id=f"mdl-{subject}-{name}",
                        subject_kind="device",
                        subject_id=subject,
                        number=number,
                        model_class=ModelClass.FIXED,
                        basis=ModelBasis.DELTA,
                        spec=FixedSpec(
                            transition_delta={"on": LevelStat(median_w=delta_w, mad_w=3.0, n=8)}
                        ),
                        trainer_version="replay-scenario",
                        created_at=now[0],
                    )
                )
                registry.activate(version.version_id, reason=f"model set {name}")
            triples[name] = VersionTriple(
                topology_version_id=base.topology_version_id,
                mapping_version_id=base.mapping_version_id,
                model_set_id=str(registry.model_set_id_at(now[0])),
            )
        return triples
    finally:
        db.close()
