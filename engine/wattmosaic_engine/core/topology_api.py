"""Topology facade: read/replace/confirm, nesting candidates, typed relations.

Owned by T4 after X03 (topology service)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..api.facade import InvalidRequest, NotFound
from ..contracts import (
    ConfigurationEvent,
    EdgeEvidence,
    EndpointConfigRevision,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from ..devices.service import endpoints_from_repo
from ..electrical.topology_graph import DisplayNode, TopologyGraph

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


def _edge_dict(edge: TopologyEdgeV3, *, in_version: bool = True) -> dict[str, Any]:
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
        # True for an edge referenced by the active version's edge_ids_json;
        # False for a stored inference suggestion surfaced alongside the
        # version.  A suggestion is never promoted by this read (T4/HB §4.3).
        "in_version": in_version,
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
    # Raw S13 rows carry the storage-only ``metadata_json`` column, which the
    # contract forbids; the shared builder drops it and loads the revisions.
    endpoints = endpoints_from_repo(service._endpoints)
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
        lifecycle=TopologyEdgeState.STRUCTURAL,
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
    STRUCTURAL.  T1's override rule makes a pinned/rejected user edge survive a
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
        updated = edge.model_copy(update={"pinned": True, "lifecycle": TopologyEdgeState.STRUCTURAL})
    else:
        # Reject moves the edge to REJECTED and closes its validity interval.
        if current.get("valid_until") is None:
            current["valid_until"] = at
        updated = edge.model_copy(
            update={"valid_until": at, "lifecycle": TopologyEdgeState.REJECTED}
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


# Lifecycles that are surfaced as inference suggestions when they are stored
# but not yet part of the active version.  CANDIDATE is deliberately excluded:
# one event never promotes an edge past CANDIDATE (HB §4.2/§4.3, DEC-10), so a
# single-support pair is raw ledger noise until the caller opts in with
# ``?lifecycles=candidate``.
_SUGGESTION_LIFECYCLES = frozenset(
    {
        TopologyEdgeState.PROVISIONAL.value,
        TopologyEdgeState.STRUCTURAL.value,
        TopologyEdgeState.CONFLICTED.value,
    }
)
_SUMMARY_LIFECYCLES = (
    TopologyEdgeState.CANDIDATE.value,
    TopologyEdgeState.PROVISIONAL.value,
    TopologyEdgeState.STRUCTURAL.value,
    TopologyEdgeState.CONFLICTED.value,
)


def _evidence_counts(edge: TopologyEdgeV3) -> dict[str, int]:
    """support/conflict/independent-cause counts from a stored edge's evidence.

    ``upsert_edge`` serialises :class:`EdgeEvidence` into the row's
    ``evidence_json``; ``_edge_row`` decodes it back onto the contract model, so
    reading it here is pure and needs no extra storage query.
    """
    evidence = edge.evidence
    return {
        "support": int(evidence.support),
        "conflict": int(evidence.conflict),
        "independent_causes": int(evidence.independent_causes),
    }


def _discovery_summary(stored_inferred: Sequence[TopologyEdgeV3]) -> dict[str, Any]:
    """Counts by lifecycle and the top candidate/provisional pairs by support.

    The thresholds are reported from the engine's own normative constants so a
    caller can see what is still needed to move a pair to PROVISIONAL or
    STRUCTURAL.  Nothing here writes or promotes an edge.
    """
    from ..electrical.topology_inference import (
        MIN_INDEPENDENT_CAUSES,
        MIN_SUPPORT_PROVISIONAL,
        MIN_SUPPORT_STRUCTURAL,
    )

    by_lifecycle = {name: 0 for name in _SUMMARY_LIFECYCLES}
    pairs: list[dict[str, Any]] = []
    for edge in stored_inferred:
        lifecycle = _value(edge.lifecycle)
        if lifecycle in by_lifecycle:
            by_lifecycle[lifecycle] += 1
        if lifecycle not in (
            TopologyEdgeState.CANDIDATE.value,
            TopologyEdgeState.PROVISIONAL.value,
        ):
            continue
        counts = _evidence_counts(edge)
        support = counts["support"]
        independent = counts["independent_causes"]
        pairs.append(
            {
                "edge_id": edge.edge_id,
                "source_endpoint_id": edge.source_endpoint_id,
                "target_endpoint_id": edge.target_endpoint_id,
                "lifecycle": lifecycle,
                "declared_by": edge.declared_by,
                **counts,
                "needed": {
                    "provisional": {
                        "support": max(0, MIN_SUPPORT_PROVISIONAL - support),
                        "independent_causes": max(0, MIN_INDEPENDENT_CAUSES - independent),
                    },
                    "structural": {
                        "support": max(0, MIN_SUPPORT_STRUCTURAL - support),
                        "independent_causes": max(0, MIN_INDEPENDENT_CAUSES - independent),
                    },
                },
            }
        )
    pairs.sort(key=lambda pair: (-pair["support"], pair["edge_id"]))
    return {
        "counts": by_lifecycle,
        "top_pairs": pairs[:5],
        "thresholds": {
            "MIN_SUPPORT_PROVISIONAL": MIN_SUPPORT_PROVISIONAL,
            "MIN_INDEPENDENT_CAUSES": MIN_INDEPENDENT_CAUSES,
            "MIN_SUPPORT_STRUCTURAL": MIN_SUPPORT_STRUCTURAL,
        },
    }


def topology_v3(service: Any, *, lifecycles: Sequence[str] | None = None) -> dict[str, Any]:
    """The active version, its edges, inference suggestions and a discovery summary.

    The active version's edges are returned unchanged (each with
    ``in_version: true``).  Stored inferred edges that are *not* referenced by
    the active version and whose lifecycle is PROVISIONAL, STRUCTURAL or
    CONFLICTED are appended as soft suggestions with ``in_version: false`` —
    this read never promotes them into a version.  A single-event CANDIDATE pair
    is hidden unless the caller passes ``lifecycles`` containing ``candidate``
    (HB §4.3, DEC-10).  ``discovery`` summarises all stored inferred pairs so
    the panel can show that discovery is working before any suggestion exists.
    """
    vid = service.current().topology_version_id
    repo = service._topo
    active = repo.active_version() or {}
    version_edges = [TopologyEdgeV3(**_edge_row(row)) for row in repo.edges(vid)]
    version_ids = {e.edge_id for e in version_edges}
    wanted = {_value(item) for item in lifecycles} if lifecycles else None

    # Version edges keep their existing lifecycle filter semantics.
    shown_version = version_edges
    if wanted is not None:
        shown_version = [e for e in shown_version if _value(e.lifecycle) in wanted]
    output_edges = [_edge_dict(e, in_version=True) for e in shown_version]

    # Stored inferred edges outside the active version.  All of the repo read;
    # only PROVISIONAL+ pairs are suggestions by default, CANDIDATE only on
    # explicit opt-in.  A lifecycle filter, when given, is applied to them too.
    stored_inferred = [
        TopologyEdgeV3(**_edge_row(row))
        for row in repo.edges()
        if row.get("declared_by") == "inference" and row.get("edge_id") not in version_ids
    ]
    eligible = set(_SUGGESTION_LIFECYCLES)
    if wanted is not None and "candidate" in wanted:
        eligible = eligible | {TopologyEdgeState.CANDIDATE.value}
    for edge in stored_inferred:
        lifecycle = _value(edge.lifecycle)
        if lifecycle not in eligible:
            continue
        if wanted is not None and lifecycle not in wanted:
            continue
        output_edges.append(_edge_dict(edge, in_version=False))

    graph = _topology_graph(service, vid)
    return {
        "version_id": vid,
        "version": active.get("number"),
        "status": active.get("status"),
        "edges": output_edges,
        "primary_display_tree": [_display_node_dict(n) for n in graph.primary_display_tree()],
        "discovery": _discovery_summary(stored_inferred),
    }


def with_edge_health(result: dict[str, Any], book: Any) -> dict[str, Any]:
    """Add ``health`` (M3 ``as_dict`` minus parent/child) to CONTAINS edge rows.

    Every STRUCTURAL CONTAINS row carries ``health``: an edge with no decisive
    verdict yet (its child has not stepped since it was confirmed -- live, a
    hot tub is off 97 % of the time, and a busy main often reads a child's step
    as neutral) shows an empty window with status ``unknown`` instead of no
    field at all.  Other lifecycles carry it only when the book has an entry.
    """
    from ..electrical.topology_health import EdgeHealth

    entries = getattr(book, "entries", None) or {}
    for row in result.get("edges", ()):
        if row.get("relation") != RelationType.CONTAINS.value:
            continue
        parent, child = row.get("source_endpoint_id"), row.get("target_endpoint_id")
        entry = entries.get((parent, child))
        if entry is None:
            if row.get("lifecycle") not in (TopologyEdgeState.STRUCTURAL.value, "confirmed") or not parent or not child:
                continue
            entry = EdgeHealth(parent=str(parent), child=str(child))
        health = entry.as_dict()
        health.pop("parent", None)
        health.pop("child", None)
        row["health"] = health
    return result


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


class TopologyApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

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
        """The active version, its edges (all lifecycles) and the primary tree.

        CONTAINS rows with an edge-health record (topology linking v2, M3) gain
        ``health``: the rolling verdict window, its timestamps and status.
        """
        result = self._v3_topology_service().topology(lifecycles=lifecycles)
        ledger = getattr(self, "topology_inference", None)
        book = getattr(ledger, "health", None)
        return with_edge_health(result, book)

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

    def _topology_health(self) -> dict[str, Any]:
        """Topology presence for ``/health``; no measurement endpoint is not healthy.

        ``configured`` means at least one v3 :class:`MeasurementEndpoint` exists:
        a fresh install with none answers every probe and infers nothing, so it
        must not report ``ok``.  ADR-0043 removes topology confirmation: the
        payload reports ``state`` (``structural`` / ``candidate``), the count of
        ``structural_edges`` and each edge's ``state``, ``strength`` and
        ``conflict_ratio`` so a pinned edge's disagreement stays visible.
        """
        repos = self.db.repos
        try:
            endpoints = repos.entities.endpoints()
        except Exception:  # noqa: BLE001 - health must never fail
            endpoints = []
        if not endpoints:
            return {
                "configured": False,
                "reason": self.topology_bootstrap_error or "no measurement endpoints configured",
            }
        version_id: str | None = None
        rows: list[dict[str, Any]] = []
        try:
            active = repos.topology_v3.active_version()
            if active is not None:
                version_id = str(active.get("version_id") or "") or None
                rows = repos.topology_v3.edges(version_id) if version_id else []
        except Exception:  # noqa: BLE001 - health must never fail
            rows = []
        edges: list[dict[str, Any]] = []
        structural_edges = 0
        for edge in rows:
            lifecycle = str(_value(edge.get("lifecycle")))
            evidence = edge.get("evidence")
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence or "{}")
                except (TypeError, ValueError):
                    evidence = {}
            evidence = evidence if isinstance(evidence, dict) else {}
            sw = float(evidence.get("support_w") or 0.0)
            cw = float(evidence.get("conflict_w") or 0.0)
            decisive = sw + cw
            ratio = cw / decisive if decisive > 0 else 0.0
            if not decisive:
                support, conflict = int(evidence.get("support") or 0), int(evidence.get("conflict") or 0)
                decisive = support + conflict
                ratio = conflict / decisive if decisive else 0.0
                sw = float(support)
            is_structural = (
                lifecycle == TopologyEdgeState.STRUCTURAL.value or bool(edge.get("pinned"))
            )
            structural_edges += int(is_structural)
            edges.append(
                {
                    "edge_id": edge.get("edge_id"),
                    "source_endpoint_id": edge.get("source_endpoint_id"),
                    "target_endpoint_id": edge.get("target_endpoint_id"),
                    "state": lifecycle,
                    "strength": sw,
                    "conflict_ratio": ratio,
                    "pinned": bool(edge.get("pinned")),
                }
            )
        try:
            devices = len(repos.entities.devices())
        except Exception:  # noqa: BLE001
            devices = 0
        return {
            "configured": True,
            "state": "structural" if structural_edges else "candidate",
            "structural_edges": structural_edges,
            "version": version_id,
            "endpoints": len(endpoints),
            "edges": len(edges),
            "devices": devices,
            "edge_states": edges,
        }
