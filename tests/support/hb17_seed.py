"""Test-only seeding of the HB §17 reference house into a real D1 database.

Z3b moved this out of the engine (it was ``VersionService.from_hb17`` and its
``_seed_*`` helpers).  Everything here goes through the engine's own public
declaration paths -- nothing is fabricated around them:

* the topology CONTAINS edges are the observation's *declared* reader parents
  (the ``parent`` metadata a user enters per reader), proposed and promoted
  through :meth:`VersionService.propose_topology` / ``promote_topology``;
* the subject -> endpoint mapping is the user's up-front declaration, proposed
  and promoted through :meth:`VersionService.propose_mapping` /
  ``promote_mapping``.

The simulator's ``truth`` frame is never read.  ``EngineHarness`` takes the
seed as a callable (:func:`hb17_seed`), so the engine imports no scenario data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    MappingEdge,
    Observability,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.versioning.service import VersionService

__all__ = [
    "HB17_DEFAULT_DEVICES",
    "HB17_MODEL_SET_ID",
    "HB17_ORIGIN",
    "declared_parents",
    "declared_seed",
    "hb17_seed",
    "seed_hb17_versions",
    "start_hb17_harness",
    "stored_reading_at",
]

#: The fixed instant the declarations are made at (the old ``from_hb17`` clock).
HB17_ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)
HB17_MODEL_SET_ID = "mset-hb17"

#: The default declared mapping: the TV and the lights on Circuit A (pre-move).
#: The refrigerator is *not* declared -- HB §17 says it has no HA entity, so
#: Circuit B's scope is its unknown-load scope (step 7).
HB17_DEFAULT_DEVICES: Mapping[str, str] = {"tv": "ct_a", "lights": "ct_a"}


def declared_parents(observation: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``child -> parent`` from the readers' declared ``parent`` metadata."""
    parents: dict[str, str] = {}
    for record in observation:
        if not isinstance(record, Mapping):
            continue
        samples = record.get("reader_samples") or record.get("measurements") or {}
        if not isinstance(samples, Mapping):
            continue
        for reader, sample in samples.items():
            if not isinstance(sample, Mapping):
                continue
            parent = sample.get("parent")
            if isinstance(parent, str) and parent:
                parents[str(sample.get("reader_id", reader))] = parent
    return parents


def seed_hb17_versions(
    db: Any,
    observation: Iterable[Mapping[str, Any]],
    *,
    devices: Mapping[str, str] | None = None,
    parents: Mapping[str, str] | None = None,
    model_set_id: str = HB17_MODEL_SET_ID,
    matrix_store: Any | None = None,
    clock: Callable[[], datetime] | None = None,
) -> VersionService:
    """A D1 service over ``db`` with the HB §17 declarations promoted.

    ``devices`` replaces :data:`HB17_DEFAULT_DEVICES` (e.g. the TV on its plug);
    ``parents`` adds ``child -> parent`` declarations the readers' metadata
    does not carry (e.g. which circuit a smart plug is plugged into).
    ``db`` must be migrated (``Database(":memory:")`` is).  The caller keeps
    ``db`` alive for the service's lifetime.
    """
    clock = clock or (lambda: HB17_ORIGIN)
    svc = VersionService(
        topology_repo=db.repos.topology_v3,
        mapping_repo=db.repos.mapping,
        clock=clock,
        model_set_id=model_set_id,
        matrix_store=matrix_store,
    )
    at = clock()
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
            evidence=EdgeEvidence(support=8, independent_causes=2),
            valid_from=at,
            pinned=True,
        )
        for child, parent in sorted({**declared_parents(list(observation)), **(parents or {})}.items())
    ]
    if topology:
        svc.promote_topology(svc.propose_topology(topology, None), "declared reader parents")
    declared = dict(devices) if devices is not None else dict(HB17_DEFAULT_DEVICES)
    mapping = [
        MappingEdge(
            mapping_edge_id=f"me::{subject}->{endpoint}",
            subject_kind="device",
            subject_id=subject,
            endpoint_id=endpoint,
            observability=Observability.DIRECT,
            lifecycle=EdgeLifecycleV3.CONFIRMED,
            declared_by="migration",
            evidence=EdgeEvidence(support=8, independent_causes=2),
            valid_from=at,
        )
        for subject, endpoint in sorted(declared.items())
    ]
    if mapping:
        svc.promote_mapping(svc.propose_mapping(mapping, None), "declared subject mapping")
    return svc


def declared_seed(
    observation: Iterable[Mapping[str, Any]],
    *,
    devices: Mapping[str, str] | None = None,
    parents: Mapping[str, str] | None = None,
    model_set_id: str = HB17_MODEL_SET_ID,
) -> Callable[[Any], VersionService]:
    """The ``seed`` callable :meth:`EngineHarness.start` takes: ``seed(core)``.

    Declares the observation's reader parents and the ``devices`` mapping
    (default :data:`HB17_DEFAULT_DEVICES`).  The service shares the runtime's
    live matrix store, so a declared configuration event marks the cells the
    runtime actually publishes.
    """
    records = list(observation)

    def seed(core: Any) -> VersionService:
        updater = getattr(core, "matrix_updater", None)
        return seed_hb17_versions(
            core.db,
            records,
            devices=devices,
            parents=parents,
            model_set_id=model_set_id,
            matrix_store=getattr(updater, "store", None),
        )

    return seed


#: The HB §17 house's seed is the generic declared seed with its default mapping.
hb17_seed = declared_seed


def start_hb17_harness(
    hb17: Any,
    *,
    devices: Mapping[str, str] | None = None,
    parents: Mapping[str, str] | None = None,
    configure: Callable[[Any], None] | None = None,
    run: bool = True,
) -> Any:
    """Build an ``EngineHarness`` for the acceptance ``hb17`` fixture.

    With ``run=True`` (the old ``EngineHarness.from_hb17``) the whole stream is
    replayed and the harness settled; otherwise the caller drives it.
    """
    from wattmosaic_engine.realtime import EngineHarness

    house = hb17.get("house") if isinstance(hb17, Mapping) else hb17
    observation = list(hb17.get("observation", [])) if isinstance(hb17, Mapping) else []
    if not observation and house is not None:
        observation = house.observation_view()
    harness = EngineHarness.start(
        observation,
        start_time=getattr(house, "start_time", None),
        seed=hb17_seed(observation, devices=devices, parents=parents),
        configure=configure,
    )
    if run:
        harness.run(observation)
        harness.settle()
    return harness


def stored_reading_at(db: Any, reader_id: str, *, at: datetime) -> float | None:
    """``reader_id``'s reading in the last snapshot the runtime *stored* by ``at``.

    Reads the append-only S-snapshot table (``snapshot_as_of``), never an
    in-memory view; an absent reading stays ``None`` (HB §18).
    """
    row = db.repos.inference.snapshot_as_of(at)
    if row is None:
        return None
    for measurement in row["payload"].get("reader_measurements") or ():
        if measurement.get("reader_id") == reader_id:
            return measurement.get("active_power_w")
    return None
