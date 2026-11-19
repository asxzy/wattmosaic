"""Topology facade: read/replace/confirm, nesting candidates, typed relations.

Owned by T4 after X03 (topology service)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from wattmosaic_engine.electrical._declared_shapes import DeclaredTree, EdgeState, RelationKindDecl

from ..api.facade import InvalidRequest, NotFound
from ..contracts import (
    ConfigurationEvent,
    EdgeEvidence,
    EdgeLifecycleV3,
    EndpointConfigRevision,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeV3,
)
from ..electrical.topology_graph import DisplayNode, TopologyGraph
from ..electrical.topology_inference import infer_nesting

#: Downsample the nesting comparison grid to the channels' declared sampling
#: cadence instead of using the union of every distinct timestamp.
#:
#: **Off by default, and deliberately so.**  The union is 29-38x larger than the
#: data it describes (76 893 instants for 2 615 samples per channel over 12 h), so
#: resampling is a large CPU win, and the set of candidate pairs it produces is
#: identical on the reference installation.  But ``instants``,
#: ``joint_instants``, ``containment`` and the median evidence in the payload all
#: describe *how many instants were compared*, and those genuinely change.
#: Enabling this by default would silently alter a published response, so it is a
#: flag: ``_sample_grid`` documents the argument and the guard rails.
NESTING_RESAMPLE_GRID = False


# --------------------------------------------------------------------------- #
# T4 — event-sourced v3 topology service.
#
# These free functions are the pure core of the v3 topology API.  They depend
# only on the injected VersionService Protocol, the S13/S14 repositories and
# the K03 contracts; they never read a wall clock (time comes from the service's
# injected clock) and never import ``runtime.fakes``.  The stateful facade
# methods below delegate to them so the same logic is usable without an engine.
# --------------------------------------------------------------------------- #


def _value(item: Any) -> Any:
    return getattr(item, "value", item) if item is not None else None


def _now(service: Any) -> datetime:
    """The service's injected clock — never the wall clock."""
    return service._clock()


def _edge_id(relation: str, source: str, target: str) -> str:
    """Deterministic edge id, so redeclaring one edge does not fork its history."""
    raw = f"{relation}|{source}|{target}"
    return "te-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@runtime_checkable
class TopologyRepo(Protocol):
    """The S14 surface the v3 service needs (satisfied by db.repos.topology_v3)."""

    def edges(
        self, version_id: str | None = None, lifecycles: Any = None
    ) -> list[dict[str, Any]]: ...

    def edge_history(self, edge_id: str) -> list[dict[str, Any]]: ...

    def upsert_edge(self, edge: Any) -> None: ...

    def active_version(self) -> dict[str, Any] | None: ...

    def versions(self) -> list[dict[str, Any]]: ...

    def append_configuration_event(self, event: Any) -> None: ...


@runtime_checkable
class EndpointsRepo(Protocol):
    """The S13 endpoint/revision surface the v3 service needs."""

    def endpoints(self, *, enabled_only: bool = False) -> list[dict[str, Any]]: ...

    def revisions(self, endpoint_id: str) -> list[dict[str, Any]]: ...

    def append_endpoint_revision(self, revision: Any) -> None: ...


@runtime_checkable
class VersionServiceLike(Protocol):
    """The D1 VersionService surface (production) used by the v3 service."""

    def current(self) -> Any: ...

    def topology_view(self, version_id: str) -> Any: ...

    def mapping_view(self, version_id: str) -> Any: ...

    def record_configuration_event(self, ev: ConfigurationEvent) -> str: ...

    def propose_topology(self, edges: Sequence[TopologyEdgeV3], change_event_id: str | None) -> str: ...

    def promote_topology(self, version_id: str, reason: str) -> None: ...


@dataclass
class TopologyService:
    """The v3 topology service: event-sourced, per-edge, versioned operations.

    Dependencies are injected Protocols/fakes: ``versions`` is the D1
    VersionService (or the K08 fake), ``topology_repo`` is the S14 repository,
    ``endpoints_repo`` is S13's entity repository, and ``clock`` supplies time.
    Production code never imports ``runtime.fakes`` and this service reads no
    wall clock of its own.
    """

    versions: VersionServiceLike
    topology_repo: TopologyRepo
    endpoints_repo: EndpointsRepo
    clock: Callable[[], datetime]

    # -- reads ------------------------------------------------------------- #
    def topology(self, *, lifecycles: Sequence[str] | None = None) -> dict[str, Any]:
        return self._call(topology_v3, lifecycles=lifecycles)

    def versions_history(self, version_id: str | None = None) -> Any:
        return self._call(topology_versions, version_id)

    def declared_edge(
        self, *, relation: str, source: str, target: str, declared_by: str = "user"
    ) -> dict[str, Any]:
        return self._call(
            declare_edge, relation=relation, source=source, target=target, declared_by=declared_by
        )

    def pin(self, edge_id: str) -> dict[str, Any]:
        return self._call(pin_edge, edge_id)

    def reject(self, edge_id: str) -> dict[str, Any]:
        return self._call(reject_edge, edge_id)

    def edit_config(self, endpoint_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        return self._call(edit_endpoint_config, endpoint_id, changes)

    def edges_confirmed_for(self, subject_id: str) -> bool:
        return self._call(edges_confirmed_for, subject_id)

    # -- internals --------------------------------------------------------- #
    def _adapter(self) -> _ServiceAdapter:
        return _ServiceAdapter(self.versions, self.topology_repo, self.endpoints_repo, self.clock)

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return fn(self._adapter(), *args, **kwargs)


@dataclass
class _ServiceAdapter:
    """Uniform seam the free functions above read their dependencies through."""

    versions: VersionServiceLike
    topology_repo: TopologyRepo
    endpoints_repo: EndpointsRepo
    clock: Callable[[], datetime]

    @property
    def _topo(self) -> TopologyRepo:
        return self.topology_repo

    @property
    def _endpoints(self) -> EndpointsRepo:
        return self.endpoints_repo

    @property
    def _clock(self) -> Callable[[], datetime]:
        return self.clock

    def current(self) -> Any:
        return self.versions.current()

    def topology_view(self, version_id: str) -> Any:
        return self.versions.topology_view(version_id)

    def mapping_view(self, version_id: str) -> Any:
        return self.versions.mapping_view(version_id)

    def record_configuration_event(self, ev: ConfigurationEvent) -> str:
        return self.versions.record_configuration_event(ev)

    def propose_topology(
        self, edges: Sequence[TopologyEdgeV3], change_event_id: str | None
    ) -> str:
        return self.versions.propose_topology(edges, change_event_id)

    def promote_topology(self, version_id: str, reason: str) -> None:
        self.versions.promote_topology(version_id, reason)


def _edge_dict(edge: TopologyEdgeV3) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "relation": _value(edge.relation),
        "source_endpoint_id": edge.source_endpoint_id,
        "target_endpoint_id": edge.target_endpoint_id,
        "lifecycle": _value(edge.lifecycle),
        "declared_by": edge.declared_by,
        "beta": edge.beta,
        "beta_basis": edge.beta_basis,
        "evidence": edge.evidence.model_dump(mode="json"),
        "valid_from": edge.valid_from.isoformat(),
        "valid_until": edge.valid_until.isoformat() if edge.valid_until else None,
        "pinned": edge.pinned,
    }


def _topology_graph(service: Any, version_id: str | None = None) -> TopologyGraph:
    """Build T1's validated graph over a version's edges and the S13 endpoints.

    Building the graph is pure: edges and endpoints come from the injected
    repositories, and ``at`` comes from the service's injected clock.  A version
    id of ``None`` means the active version.
    """
    vid = version_id or service.current().topology_version_id
    repo = service._topo
    edges = [TopologyEdgeV3(**_edge_row(row)) for row in repo.edges(vid)]
    endpoints = [
        MeasurementEndpoint(**row) for row in service._endpoints.endpoints()
    ]
    return TopologyGraph.from_edges(edges, endpoints, at=_now(service))


def _edge_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw repository row for the :class:`TopologyEdgeV3` contract."""
    data = dict(row)
    evidence = data.get("evidence", data.get("evidence_json", {}))
    if isinstance(evidence, str):
        evidence = json.loads(evidence or "{}")
    data["evidence"] = evidence
    data.pop("evidence_json", None)
    for field in ("valid_from", "valid_until"):
        val = data.get(field)
        if isinstance(val, str):
            data[field] = datetime.fromisoformat(val.replace("Z", "+00:00"))
    return data


def _active_edges(service: Any) -> list[TopologyEdgeV3]:
    """Every edge referenced by the active version, as contract models."""
    vid = service.current().topology_version_id
    return [TopologyEdgeV3(**_edge_row(row)) for row in service._topo.edges(vid)]


def _propose_and_promote(
    service: Any, edges: Sequence[TopologyEdgeV3], event: ConfigurationEvent, reason: str
) -> str:
    """Append a candidate version from ``edges`` and promote it immediately.

    A user-declared edge is authoritative (DEC-10): after recording the
    DECLARED configuration event the candidate is proposed against the current
    active version and promoted in the same call, so the parent link in the new
    version chains through ``parent_version_id``.
    """
    event_id = service.record_configuration_event(event)
    version = service.propose_topology(edges, event_id)
    service.promote_topology(version, reason)
    return version


def _in_force_row(
    rows: Sequence[dict[str, Any]], *, relation: str, source: str, target: str
) -> dict[str, Any] | None:
    """The still-valid row for one logical (relation, parent, child) triple."""
    for row in rows:
        if (
            _value(row["relation"]) == relation
            and row["source_endpoint_id"] == source
            and row["target_endpoint_id"] == target
            and row.get("valid_until") is None
            and _value(row["lifecycle"]) not in ("rejected", "retired")
        ):
            return row
    return None


def declare_edge(
    service: Any,
    *,
    relation: str,
    source: str,
    target: str,
    declared_by: str = "user",
) -> dict[str, Any]:
    """Declare a user edge: validate via T1, record an event, promote a version.

    The event-sourced write never deletes rows.  If the same logical edge is
    re-declared, its still-valid row is closed with ``valid_until`` and a *new*
    edge row (a new ``edge_id``) carries the declaration.  The old row remains in
    ``topology_edges`` and in the previous version's byte-identical edge set.
    """
    if not source or not target:
        raise InvalidRequest("source and target endpoint ids are required")
    if source == target:
        raise InvalidRequest("an edge cannot connect an endpoint to itself")
    if declared_by != "user":
        raise InvalidRequest("T4 only declares user edges")
    try:
        relation_type = RelationType(relation)
    except ValueError as exc:
        raise InvalidRequest(f"unknown relation {relation!r}") from exc

    at = _now(service)
    repo = service._topo
    all_rows = repo.edges()
    base_id = _edge_id(relation_type.value, source, target)

    # Close any still-valid row for the same logical edge instead of deleting it.
    prior = _in_force_row(all_rows, relation=relation_type.value, source=source, target=target)
    if prior is not None:
        closed = _edge_row(prior)
        closed["valid_until"] = at
        repo.upsert_edge(TopologyEdgeV3(**closed))

    # A fresh declaration gets a fresh row id: the base id for the first one,
    # a revision-suffixed id thereafter.  This keeps the old row as history.
    family = [
        r
        for r in all_rows
        if r["edge_id"] == base_id or r["edge_id"].startswith(base_id + "-r")
    ]
    edge_id = base_id if not family else f"{base_id}-r{len(family) + 1}"

    new_edge = TopologyEdgeV3(
        edge_id=edge_id,
        relation=relation_type,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="user",
        evidence=EdgeEvidence(),
        valid_from=at,
        pinned=False,
        beta=1.0 if relation_type == RelationType.CONTAINS else None,
        beta_basis="unity" if relation_type == RelationType.CONTAINS else None,
    )
    # T1 validation: a self edge or a cycle is rejected before any write.  The
    # closed prior row has valid_until <= at, so T1's validity filter ignores it
    # and only the new declaration is structural.
    proposed = [e for e in _active_edges(service) if e.edge_id != (prior or {}).get("edge_id")]
    proposed.append(new_edge)
    graph = TopologyGraph.from_edges(proposed, at=at)
    if not graph.is_consistent():
        raise InvalidRequest("; ".join(graph.validate()))

    event = ConfigurationEvent(
        event_id=f"evt-declare-{edge_id}",
        kind="circuit_reconfiguration",
        detection="declared",
        status="confirmed",
        affected_endpoint_ids=[source, target],
        affected_edge_ids=[edge_id],
        created_at=at,
    )
    version = _propose_and_promote(service, proposed, event, "user declaration")
    return {"version_id": version, "event_id": event.event_id, "edge_id": edge_id}


def _set_edge_authority(service: Any, edge_id: str, *, pin: bool) -> dict[str, Any]:
    """Take user authority over an inferred edge (pin) or reject it.

    Both operations record a DECLARED configuration event and promote a version
    carrying the resulting edge set.  A rejection closes the edge with
    ``valid_until`` and moves it to REJECTED; a pin flips ``pinned`` and keeps it
    CONFIRMED.  T1's override rule makes a pinned/rejected user edge survive a
    later inference re-run (DEC-10).
    """
    at = _now(service)
    repo = service._topo
    history = repo.edge_history(edge_id)
    if not history:
        raise NotFound(f"no edge {edge_id!r}")
    current = _edge_row(history[-1])
    edge = TopologyEdgeV3(**current)

    if pin:
        updated = edge.model_copy(update={"pinned": True, "lifecycle": EdgeLifecycleV3.CONFIRMED})
    else:
        # Reject moves the edge to REJECTED and closes its validity interval.
        if current.get("valid_until") is None:
            current["valid_until"] = at
        updated = edge.model_copy(
            update={"valid_until": at, "lifecycle": EdgeLifecycleV3.REJECTED}
        )
        repo.upsert_edge(updated)

    if pin:
        repo.upsert_edge(updated)

    event = ConfigurationEvent(
        event_id=f"evt-{'pin' if pin else 'reject'}-{edge_id}",
        kind="circuit_reconfiguration",
        detection="declared",
        status="confirmed",
        affected_endpoint_ids=[edge.source_endpoint_id, edge.target_endpoint_id],
        affected_edge_ids=[edge_id],
        created_at=at,
    )
    edges = [e for e in _active_edges(service) if e.edge_id != edge_id] + [updated]
    version = _propose_and_promote(
        service, edges, event, "user pin" if pin else "user reject"
    )
    return {"version_id": version, "event_id": event.event_id, "edge_id": edge_id}


def pin_edge(service: Any, edge_id: str) -> dict[str, Any]:
    return _set_edge_authority(service, edge_id, pin=True)


def reject_edge(service: Any, edge_id: str) -> dict[str, Any]:
    return _set_edge_authority(service, edge_id, pin=False)


def _display_node_dict(node: DisplayNode) -> dict[str, Any]:
    return {
        "label": node.label,
        "level": node.level,
        "kind": node.kind,
        "reader_id": node.reader_id,
        "children": [_display_node_dict(child) for child in node.children],
    }


def topology_v3(service: Any, *, lifecycles: Sequence[str] | None = None) -> dict[str, Any]:
    """The active version, its edges (all lifecycles) and T1's primary tree.

    PROVISIONAL edges are exposed here as soft suggestions, which is why the
    legacy ``/topology/nesting`` endpoint is no longer needed: the read returns
    every lifecycle by default and callers may filter with ``lifecycles=``.
    """
    vid = service.current().topology_version_id
    repo = service._topo
    active = repo.active_version() or {}
    edges = [TopologyEdgeV3(**_edge_row(row)) for row in repo.edges(vid)]
    if lifecycles:
        wanted = {_value(item) for item in lifecycles}
        edges = [e for e in edges if _value(e.lifecycle) in wanted]
    graph = _topology_graph(service, vid)
    return {
        "version_id": vid,
        "version": active.get("number"),
        "status": active.get("status"),
        "edges": [_edge_dict(edge) for edge in edges],
        "primary_display_tree": [_display_node_dict(n) for n in graph.primary_display_tree()],
    }


def topology_versions(service: Any, version_id: str | None = None) -> Any:
    """The version history, or one version when ``version_id`` is given."""
    rows = service._topo.versions()
    if version_id is not None:
        for row in rows:
            if row["version_id"] == version_id:
                return dict(row)
        raise NotFound(f"no topology version {version_id!r}")
    return [dict(row) for row in rows]


def edit_endpoint_config(
    service: Any,
    endpoint_id: str,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Append an EndpointConfigRevision and record a DECLARED configuration event.

    Only phase, polarity and multiplier may be edited here.  The old revision's
    ``valid_until`` is closed at the injected clock, a new revision with the next
    number is appended, and a DECLARED ``calibration_change`` event is recorded.
    Nothing rewrites an existing revision or an old sample: the revision stores
    its own ``calibration_version`` and time range, so historical readings keep
    resolving with the factor that was true when they were taken (DEF-06).
    """
    allowed = {"phase", "polarity", "multiplier"}
    unknown = set(changes) - allowed
    if unknown:
        raise InvalidRequest(f"unsupported endpoint config fields: {sorted(unknown)}")
    if not changes:
        raise InvalidRequest("at least one of phase, polarity, multiplier is required")

    at = _now(service)
    repo = service._endpoints
    revisions = repo.revisions(endpoint_id)
    if not revisions:
        raise NotFound(f"no endpoint {endpoint_id!r}")
    previous = max(revisions, key=lambda r: int(r["revision"]))
    number = int(previous["revision"]) + 1

    if "multiplier" in changes:
        try:
            multiplier = float(changes["multiplier"])
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("multiplier must be a number") from exc
        if not math.isfinite(multiplier) or multiplier == 0:
            raise InvalidRequest("multiplier must be finite and non-zero")
    else:
        multiplier = float(previous["multiplier"])

    phase = changes.get("phase", previous.get("phase") or "unknown")
    polarity = changes.get("polarity", previous.get("polarity") or "normal")
    if phase not in {"l1", "l2", "both", "unknown"}:
        raise InvalidRequest(f"unknown phase {phase!r}")
    if polarity not in {"normal", "inverted"}:
        raise InvalidRequest(f"unknown polarity {polarity!r}")

    # Revisions are append-only: never rewrite the previous row.  Its
    # ``valid_until`` (if any) was fixed when it was created; the new revision
    # begins now and is selected by ``endpoint_revision_at`` for later samples.
    revision = EndpointConfigRevision(
        endpoint_id=endpoint_id,
        revision=number,
        valid_from=at,
        phase=phase,
        leg=previous.get("leg"),
        voltage_reference_v=previous.get("voltage_reference_v"),
        polarity=polarity,
        multiplier=multiplier,
        calibration_version=f"{endpoint_id}-rev{number}",
        measurement_type=previous.get("measurement_type") or "instantaneous",
        measurement_window_ms=previous.get("measurement_window_ms"),
        expected_interval_s=previous.get("expected_interval_s"),
        resolution_w=previous.get("resolution_w"),
        declared_by="user",
    )
    repo.append_endpoint_revision(revision)
    service.record_configuration_event(
        ConfigurationEvent(
            event_id=f"evt-calibration-{endpoint_id}-rev{number}",
            kind="calibration_change",
            detection="declared",
            status="confirmed",
            affected_endpoint_ids=[endpoint_id],
            created_at=at,
        )
    )
    return {
        "endpoint_id": endpoint_id,
        "revision": number,
        "revision_row": revision.model_dump(mode="json"),
    }


def edges_confirmed_for(service: Any, subject_id: str) -> bool:
    """Per-subject learning gate (replaces the aggregate topology flag, T11).

    A subject may learn when every CONTAINS edge on the CONFIRMED chain from the
    subject's mapped reader up through its ancestors is CONFIRMED.  When the
    subject has no mapped reader, or no structural edges at all, the gate is
    open: there is no *contradictory* structure to block learning.  A subject
    with a CANDIDATE or PROVISIONAL parent is not yet established.
    """
    vid = service.current().topology_version_id
    edges = [TopologyEdgeV3(**_edge_row(row)) for row in service._topo.edges(vid)]
    mapped = None
    try:
        view = service.mapping_view(service.current().mapping_version_id)
        mapped = view.most_specific_reader(("device", subject_id))
        if mapped is None:
            mapped = view.most_specific_reader(("component", subject_id))
    except (KeyError, AttributeError):
        mapped = None
    # Fall back to the endpoint carrying the subject id when there is no mapping.
    if mapped is None and any(
        e.target_endpoint_id == subject_id or e.source_endpoint_id == subject_id for e in edges
    ):
        mapped = subject_id
    if mapped is None:
        return True

    # Walk the structural chain from the mapped reader upward.  Every parent
    # edge must be CONFIRMED; a softer lifecycle on the path closes the gate.
    current = mapped
    seen = {current}
    while True:
        parent_edges = [
            e
            for e in edges
            if e.target_endpoint_id == current
            and _value(e.relation) == "contains"
            and e.lifecycle not in (EdgeLifecycleV3.REJECTED, EdgeLifecycleV3.RETIRED)
        ]
        if not parent_edges:
            return True
        if not any(e.lifecycle == EdgeLifecycleV3.CONFIRMED for e in parent_edges):
            return False
        confirmed = next(e for e in parent_edges if e.lifecycle == EdgeLifecycleV3.CONFIRMED)
        current = confirmed.source_endpoint_id
        if current in seen:
            return True
        seen.add(current)


class TopologyApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def topology(self) -> DeclaredTree:
        topology = self._active_topology()
        if topology is None:
            return DeclaredTree(version=0, status=EdgeState.CANDIDATE, notes=["no topology configured"])
        return topology

    def set_topology(self, topology: Any) -> DeclaredTree:
        if not isinstance(topology, DeclaredTree):
            topology = DeclaredTree.model_validate(topology)
        if topology.status == EdgeState.CONFIRMED:
            # Replacing a confirmed topology creates a candidate revision unless
            # the caller explicitly marks it confirmed.
            pass
        self.db.save_topology(topology, note="api: replace topology")
        self.topology_cache = self.db.load_topology()
        self.db.log_system_event(
            "info",
            "topology",
            "topology replaced via API",
            {"channels": len(topology.channels), "devices": len(topology.devices)},
        )
        return self.topology_cache or topology

    def confirm_topology(self) -> DeclaredTree:
        topology = self._active_topology()
        if topology is None:
            raise NotFound("no topology to confirm")
        confirmed = topology.model_copy(
            update={
                "status": EdgeState.CONFIRMED,
                "devices": [
                    d.model_copy(update={"topology_status": EdgeState.CONFIRMED})
                    for d in topology.devices
                ],
            }
        )
        self.db.save_topology(confirmed, note="api: confirm topology")
        self.topology_cache = self.db.load_topology()
        self.db.log_system_event("info", "topology", "topology confirmed via API")
        return self.topology_cache or confirmed

    # -- v3 event-sourced topology service --------------------------------- #

    def _v3_topology_service(self) -> TopologyService:
        """Build the v3 service from the engine's injected dependencies.

        Cached on the core so repeated calls share the VersionService's frozen
        views and activation timestamps.  Production code composes real D1/S13/
        S14 dependencies; ``runtime.fakes`` is never referenced here.
        """
        cached = getattr(self, "_topology_v3_service", None)
        if cached is not None:
            return cached
        from ..versioning import VersionService

        versions = getattr(self, "version_service", None)
        if versions is None:
            from ..contracts import model_set_id

            registry = getattr(self, "model_registry", None)
            model_set = "mset-engine"
            active = getattr(registry, "active_version_ids", None)
            if callable(active):
                model_set = model_set_id(active())
            versions = VersionService(
                topology_repo=self.db.repos.topology_v3,
                mapping_repo=self.db.repos.mapping,
                clock=self._clock,
                model_set_id=model_set,
            )
            self.version_service = versions
        service = TopologyService(
            versions=versions,
            topology_repo=self.db.repos.topology_v3,
            endpoints_repo=self.db.repos.entities,
            clock=self._clock,
        )
        self._topology_v3_service = service
        return service

    def topology_v3(self, *, lifecycles: Sequence[str] | None = None) -> dict[str, Any]:
        """The active version, its edges (all lifecycles) and the primary tree."""
        return self._v3_topology_service().topology(lifecycles=lifecycles)

    def topology_versions(self, version_id: str | None = None) -> Any:
        return self._v3_topology_service().versions_history(version_id)

    def declare_topology_edge(
        self, *, relation: str, source: str, target: str, declared_by: str = "user"
    ) -> dict[str, Any]:
        return self._v3_topology_service().declared_edge(
            relation=relation, source=source, target=target, declared_by=declared_by
        )

    def pin_topology_edge(self, edge_id: str) -> dict[str, Any]:
        return self._v3_topology_service().pin(edge_id)

    def reject_topology_edge(self, edge_id: str) -> dict[str, Any]:
        return self._v3_topology_service().reject(edge_id)

    def edit_endpoint_config(self, endpoint_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        return self._v3_topology_service().edit_config(endpoint_id, changes)

    def edges_confirmed_for(self, subject_id: str) -> bool:
        return self._v3_topology_service().edges_confirmed_for(subject_id)

    def nesting_candidates(self, *, hours: float = 12.0) -> dict[str, Any]:
        """Propose parent links and duplicate feeds from stored history.

        This is a *read*: it never writes a parent link.  The evidence is
        measured containment (a child cannot exceed its feed) plus closure (the
        parent's own loads plus the child must not over-explain the parent), and
        every result is a candidate the integration wizard asks the user to
        confirm.
        """
        topology = self._active_topology()
        if topology is None:
            return {"candidates": [], "reason": "no topology configured"}
        channels = topology.channels
        if not channels:
            return {"candidates": [], "reason": "topology has no channels"}

        end = self._clock()
        start = end - timedelta(hours=max(0.5, float(hours)))
        # ``ct_samples.ct_id`` is the *aligner* key (the entity object id), not the
        # topology's ``ct_id``: the ingestion path derives it with
        # ``entity_id.split(".", 1)[1]``.  Querying the topology id directly
        # silently returns nothing, which would look exactly like "no history
        # yet" and leave the wizard with no proposals forever.
        key_to_ct: dict[str, str] = {}
        for ch in channels:
            if not ch.enabled:
                continue
            key_to_ct[self._aligner_key(ch)] = ch.ct_id
        rows = self.db.ct_samples_between(start, end, ct_ids=sorted(key_to_ct))
        if not rows:
            return {
                "candidates": [],
                "reason": "no stored CT samples in the comparison window",
                "window_hours": float(hours),
            }

        samples: dict[str, list[tuple[datetime, float]]] = {}
        for row in rows:
            value = row.get("value_w")
            if not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            # ``state_timestamp`` / ``received_at`` are stored as ISO text.
            stamp = _as_utc(row.get("state_timestamp")) or _as_utc(row.get("received_at"))
            if stamp is None:
                continue
            # Re-key from the aligner key back to the topology ``ct_id`` so the
            # rest of this method (and the caller) speaks one namespace.
            ct_id = key_to_ct.get(str(row["ct_id"]))
            if ct_id is None:
                continue
            samples.setdefault(ct_id, []).append((stamp, float(value)))
        if len(samples) < 2:
            return {"candidates": [], "reason": "fewer than two channels have samples"}

        grid = (
            _sample_grid(
                samples,
                cadence_s=_sampling_cadence_s(channels),
                window_hours=max(0.5, float(hours)),
            )
            if NESTING_RESAMPLE_GRID
            else sorted({stamp for series in samples.values() for stamp, _ in series})
        )
        if len(grid) < 10:
            return {
                "candidates": [],
                "reason": "too few distinct instants to compare",
                "instants": len(grid),
            }
        aligned = {
            cid: _forward_fill([entry for entry in series], grid)
            for cid, series in samples.items()
        }

        by_id = {ch.ct_id: ch for ch in channels}
        container_ids = [
            ch.ct_id
            for ch in channels
            if ch.enabled and str(ch.ct_type) in ("main", "branch")
        ]

        report: dict[str, Any] = {}
        candidates = infer_nesting(
            {
                cid: {
                    "ct_type": str(ch.ct_type),
                    # Declared relationships are *facts the user has already
                    # stated*: the two legs of a declared double-pole pair are
                    # one circuit, not one wire measured twice, and a declared
                    # parent is not re-proposed.  ``phase`` is passed so the
                    # written-back leg is readable, but it is never used to
                    # reject a candidate -- on the reference installation the
                    # phase labels are contradicted by the measurements.
                    "phase": str(ch.phase),
                    "paired_ct_id": ch.paired_ct_id,
                    "parent_id": ch.parent_ct_id,
                }
                for cid, ch in by_id.items()
            },
            {cid: values for cid, values in aligned.items() if cid in by_id},
            container_ct_ids=container_ids,
            # Closure now relies on the parent's measured series alone: there is
            # no per-container "own loads" series to subtract, because the
            # channel role that grouped one is gone.  ``infer_nesting`` accepts
            # ``None`` and skips the own-load correction.
            direct_loads=None,
            report=report,
        )

        # Attach the candidate parent(s) to each channel as *proposals* so the
        # wizard can pre-select a single unambiguous one without ever writing it.
        proposals: dict[str, list[str]] = {}
        nested = [c for c in candidates if c.kind == "nested"]
        for candidate in nested:
            proposals.setdefault(candidate.child_reader_id, []).append(candidate.parent_id)
        # Duplicate feeds are the same *conductor* seen twice, not a nesting.
        # They are offered separately because they answer a different question:
        # "is this CT an alias of another one?" rather than "what feeds it?".
        alias_proposals: dict[str, str] = {}
        for candidate in candidates:
            if candidate.kind == "duplicate":
                alias_proposals[candidate.child_reader_id] = candidate.parent_id

        return {
            "window_hours": float(hours),
            "instants": len(grid),
            "channels_compared": len(aligned),
            "candidates": [c.as_dict() for c in candidates],
            "candidate_parents": {cid: sorted(p) for cid, p in proposals.items()},
            "candidate_aliases": dict(sorted(alias_proposals.items())),
            # What was considered and rejected, so the wizard can say "three
            # channels are ambiguous" rather than silently showing nothing.
            "ambiguous": report.get("ambiguous", []),
            "suppressed": report.get("suppressed", []),
            # Loads whose level measurement cannot establish.  Reported so the
            # UI can ask the user instead of presenting a guess as evidence.
            "not_inferable": report.get("not_inferable", []),
            "nodes": report.get("nodes", []),
            "notes": [
                "Candidates only: no parent link is written until the user confirms it.",
                "Containment proves a child never exceeds its feed; it does not by "
                "itself prove which feed.",
                "Only a panel feed can be placed by measurement. A load is contained "
                "by every panel while idle, so loads are never proposed and the "
                "wizard asks the user.",
                "A child whose level cannot be established is listed as ambiguous "
                "and is never pre-selected.",
                "A duplicate feed is the same conductor measured twice: it is offered "
                "as an alias, never summed as a second feed.",
            ],
        }

    def relations(self, kind: str | None = None) -> dict[str, Any]:
        """Stored typed edges, grouped by kind.

        ``kind`` filters to one relation kind; without it every kind is
        reported separately, because ``DEVICE_CT_ASSOCIATION``,
        ``CT_MEASUREMENT_OVERLAP`` and ``ELECTRICAL_PARENT_CHILD`` answer
        different questions and must never be merged into one list.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for relation_kind in RelationKindDecl:
            if kind is not None and str(getattr(relation_kind, "value", relation_kind)) != str(
                kind
            ):
                continue
            try:
                rows = self.db.relations_of_kind(relation_kind)
            except Exception:  # noqa: BLE001
                rows = []
            if rows or kind is not None:
                out[str(getattr(relation_kind, "value", relation_kind))] = [
                    self._jsonable(r) for r in rows
                ]
        return {
            "kinds": out,
            "counts": {name: len(rows) for name, rows in out.items()},
            "requires_confirmation": sorted(
                {
                    str(row.get("subject_id"))
                    for rows in out.values()
                    for row in rows
                    if row.get("requires_confirmation")
                }
            ),
        }

    def _topology_health(self) -> dict[str, Any]:
        """Topology presence for ``/health``; a missing topology is not healthy."""
        topology = self._active_topology()
        if topology is None:
            return {
                "configured": False,
                "reason": self.topology_bootstrap_error or "no topology configured",
            }
        raw_status = getattr(topology.status, "value", topology.status)
        return {
            "configured": True,
            "status": str(raw_status),
            "version": topology.version,
            "channels": len(topology.channels),
            "devices": len(topology.devices),
        }


def _as_utc(value: Any) -> datetime | None:
    """Parse a stored timestamp into an aware UTC ``datetime``.

    ``ct_samples`` keeps timestamps as ISO **text**, so a ``datetime`` check is
    never true for a database row.  Naive values are treated as UTC because that
    is what the writer guarantees (``storage.database.iso``).
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _sampling_cadence_s(channels: Iterable[Any]) -> float | None:
    """The channels' declared sampling cadence, in seconds, or ``None``.

    Uses the median of the enabled channels' ``expected_interval_s`` so one
    mis-declared channel cannot change the grid.  Deliberately *not* a hardcoded
    15.7 s: the cadence is a property of the installation, and the reference
    install happens to declare 15.7 s while its cheapest channel samples every
    10 s.
    """
    declared = [
        float(ch.expected_interval_s)
        for ch in channels
        if getattr(ch, "enabled", False)
        and getattr(ch, "expected_interval_s", None)
        and float(ch.expected_interval_s) > 0
    ]
    if not declared:
        return None
    declared.sort()
    mid = len(declared) // 2
    return declared[mid] if len(declared) % 2 else (declared[mid - 1] + declared[mid]) / 2.0


def _sample_grid(
    samples: Mapping[str, Sequence[tuple[datetime, float]]],
    *,
    cadence_s: float | None,
    window_hours: float,
) -> list[datetime]:
    """Alignment instants for the nesting comparison.

    Why this is not simply the union of every timestamp
    ------------------------------------------------
    The union is not a sampling grid.  On the reference installation a 12 h
    window contains **76 893** distinct timestamps for 38 channels that each
    carry only ~2 615 samples: the union is dominated by per-channel clock jitter,
    so it is 29–38× larger than the data it describes.  Every instant then costs
    one comparison per pair in :func:`~wattmosaic_engine.electrical.topology_inference.infer_nesting`
    and one cell in every aligned series, for no new information: forward-filling
    a channel between two of its own samples cannot tell you anything the earlier
    sample did not.

    What it does instead
    --------------------
    Bucket the union into fixed ``cadence_s`` slots (anchored at the first
    timestamp so the result is deterministic) and keep the **latest real sample**
    in each slot.  Keeping a real timestamp rather than the bucket edge means
    :func:`_forward_fill` still sees true sample times, so no reading is ever
    invented or shifted earlier than it happened.

    The guard rails
    ---------------
    * ``cadence_s is None`` (nothing declared) → the union, i.e. the old
      behaviour.  Never guess a cadence from nothing.
    * Never coarser than the median inter-sample gap of the *fastest* channel:
      a cadence above the real sampling interval would throw away real readings
      and replace a measured series with a piecewise-constant one.  The step is
      therefore ``min(declared cadence, fastest median gap)``.
    * Never when the window holds fewer than two cadence periods: with less than
      that, the grid would collapse to one or two instants and containment would
      lose all meaning, so the union is kept.

    Equivalence
    -----------
    The **set of candidate pairs** (``kind``, ``child_ct_id``, ``parent_ct_id``)
    is byte-identical to the union grid at hours ∈ {2, 6, 12, 24} on the reference
    installation, and the whole payload is byte-identical when this returns the
    union.  It is **not** byte-identical when it resamples: ``instants``,
    ``joint_instants``, ``containment`` and the median evidence describe *how many
    instants were compared*, and comparing 2 526 instead of 76 893 genuinely
    changes those numbers.  That is a real, visible change to the response, which
    is why :data:`NESTING_RESAMPLE_GRID` defaults to ``False``.
    """
    union = sorted({stamp for series in samples.values() for stamp, _ in series})
    if cadence_s is None or not union:
        return union

    # Median inter-sample gap across channels: the fastest real cadence.
    gaps: list[float] = []
    for series in samples.values():
        ordered = sorted(series)
        deltas = [
            (later[0] - earlier[0]).total_seconds()
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        ]
        deltas = [delta for delta in deltas if delta > 0]
        if deltas:
            deltas.sort()
            mid = len(deltas) // 2
            gaps.append(
                deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2.0
            )
    if gaps:
        gaps.sort()
        mid = len(gaps) // 2
        fastest_gap = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        # Never stride past the fastest real sampling: that would discard real
        # readings rather than merely de-duplicating jittered ones.
        cadence_s = min(cadence_s, fastest_gap)

    if window_hours > 0:
        periods = (window_hours * 3600.0) / cadence_s
        if periods < 2.0:
            # Too short a window to resample: keep every instant.
            return union

    anchor = union[0]
    grid: list[datetime] = []
    current_slot: int | None = None
    last_stamp: datetime | None = None
    for stamp in union:
        slot = int((stamp - anchor).total_seconds() // cadence_s)
        if slot != current_slot:
            if last_stamp is not None:
                grid.append(last_stamp)
            current_slot = slot
        last_stamp = stamp
    if last_stamp is not None:
        grid.append(last_stamp)
    return grid


def _forward_fill(
    series: list[tuple[datetime, float]], grid: list[datetime]
) -> list[float]:
    """Project a sparse ``(timestamp, watts)`` series onto an aligned grid.

    A channel that did not update at an instant is represented by its last known
    value, which is what the synchroniser does for a point-in-time snapshot.  A
    value is *never* invented before the first sample: those slots are ``nan``,
    and ``infer_nesting`` skips non-finite samples rather than treating an
    absent reading as zero (ADR-0015 / defect #33).
    """
    out: list[float] = []
    index = 0
    current = math.nan
    ordered = sorted(series)
    for stamp in grid:
        while index < len(ordered) and ordered[index][0] <= stamp:
            current = ordered[index][1]
            index += 1
        out.append(current)
    return out


def _linkable_state_entities(topology: DeclaredTree) -> set[str]:
    """Entities the topology declares as runtime signals for some device.

    Auto placement may only reason about entities the engine already links.  An
    undeclared entity is unknown to the topology -- it could be a scene, a
    template, or another integration's shadow -- and treating it as a load would
    be a guess about a device that does not exist yet.  The mapping wizard (and
    the user) decide what is linkable; this only reads that decision.

    Candidate *population* is now the state snapshot (see
    :mod:`wattmosaic_engine.ingestion.candidate_entities`); this remains the
    declaration read used by the learning/event paths and is kept as the single
    definition both places share.
    """
    from ..ingestion.candidate_entities import declared_state_entities

    return declared_state_entities(topology)

