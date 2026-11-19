"""Implementation of the :class:`~wattmosaic_engine.runtime.interfaces.VersionService`
Protocol over the S14 (topology) and S15 (mapping) repositories (D1).

Invariants (rule 20, HB §14):

* topology, mapping and model versions are independent — promoting one never
  changes the others, and the model-set id is injected from L1's registry;
* every version is append-only — ``propose_*`` inserts a ``candidate`` whose
  parent is the currently active version; ``promote_*`` marks it active and the
  previous active version superseded in one transaction; ``rollback`` creates a
  *new* version copying the target's edge set with a ``rollback_of`` marker and
  never reactivates rows in place;
* ``as_of(t)`` derives from version activation timestamps — the triple that was
  current at ``t`` — so old evidence keeps resolving with its old versions;
* topology and mapping views are cached per version id and are immutable
  snapshots of the edge set frozen at proposal time;
* ``mark_suspected`` moves affected edges to SUSPECTED_CHANGE and appends an
  append-only history ledger row (a SUSPECTED ``ConfigurationEvent`` carrying
  the affected edge ids) — the current edge row is never deleted.

Production dependencies are only the K00/K03 contracts, the K08 runtime
interfaces and the S14/S15 repositories.  No wall clock is read here (a
``Clock`` callable is injected), no DB connection is opened here and
``runtime.fakes`` is never imported.  Mapping-version/edge writes use the
injected S15 repository's ``_conn``/``_lock`` seams; S14 already exposes full
CRUD methods.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from wattmosaic_engine.contracts import (
    ConfigurationEvent,
    EdgeLifecycleV3,
    MappingEdge,
    TopologyEdgeV3,
    VersionTriple,
)
from wattmosaic_engine.runtime.interfaces import Clock, MappingView, TopologyView

# --------------------------------------------------------------------------- #
# Injected repository seams (satisfied by db.repos.topology_v3 / .mapping).
# --------------------------------------------------------------------------- #


@runtime_checkable
class _TopologyRepo(Protocol):
    def active_version(self) -> dict[str, Any] | None: ...

    def versions(self) -> list[dict[str, Any]]: ...

    def insert_version(self, version: Any) -> None: ...

    def set_active(self, version_id: str) -> None: ...

    def edges(
        self, version_id: str | None = None, lifecycles: Any = None
    ) -> list[dict[str, Any]]: ...

    def upsert_edge(self, edge: Any) -> None: ...

    def edge_history(self, edge_id: str) -> list[dict[str, Any]]: ...

    def append_configuration_event(self, event: Any) -> None: ...


@runtime_checkable
class _MappingRepo(Protocol):
    def active_mapping_version_id(self) -> str: ...

    def mapping_edges(
        self, version_id: str, subject_id: str | None = None, endpoint_id: str | None = None
    ) -> list[dict[str, Any]]: ...


class VersionConflictError(RuntimeError):
    """Raised when a promote loses the optimistic parent check.

    Two proposals can be in flight against the same active parent.  The first
    promote wins; the second sees that the parent has changed and fails clearly
    instead of silently overwriting the winner.
    """

    def __init__(
        self,
        kind: str,
        version_id: str,
        expected_parent: str | None,
        actual_parent: str | None,
    ) -> None:
        self.kind = kind
        self.version_id = version_id
        self.expected_parent = expected_parent
        self.actual_parent = actual_parent
        super().__init__(
            f"{kind} version {version_id!r} promoted on stale parent "
            f"{expected_parent!r} (now {actual_parent!r})"
        )


# --------------------------------------------------------------------------- #
# Immutable views.
# --------------------------------------------------------------------------- #


def _value(item: Any) -> Any:
    return getattr(item, "value", item) if item is not None else None


class _FrozenTopologyView:
    """An immutable :class:`TopologyView` over a frozen edge snapshot.

    Edges are stored in a tuple of :class:`TopologyEdgeV3` contracts, so a
    caller can neither mutate the returned collection nor replace the view's
    edge set.  The view deliberately does not expose a mutable ``edges_all``.
    """

    __slots__ = ("_edges",)

    def __init__(self, edges: Sequence[TopologyEdgeV3]) -> None:
        object.__setattr__(self, "_edges", tuple(edges))

    def edges(self, lifecycles: Any = None) -> tuple[TopologyEdgeV3, ...]:
        if lifecycles is None:
            return self._edges
        wanted = frozenset(_value(item) for item in lifecycles)
        return tuple(edge for edge in self._edges if _value(edge.lifecycle) in wanted)

    def _contains(self, lifecycles: Any) -> tuple[TopologyEdgeV3, ...]:
        wanted = frozenset(_value(item) for item in lifecycles)
        return tuple(
            edge
            for edge in self._edges
            if _value(edge.relation) == "contains" and _value(edge.lifecycle) in wanted
        )

    def parent(self, reader_id: str, lifecycles: Any = ("confirmed",)) -> str | None:
        for edge in self._contains(lifecycles):
            if edge.target_endpoint_id == reader_id:
                return edge.source_endpoint_id
        return None

    def ancestors(self, reader_id: str, lifecycles: Any = ("confirmed",)) -> tuple[str, ...]:
        result: list[str] = []
        seen = {reader_id}
        current = self.parent(reader_id, lifecycles)
        while current is not None and current not in seen:
            result.append(current)
            seen.add(current)
            current = self.parent(current, lifecycles)
        return tuple(result)

    def children(self, reader_id: str, lifecycles: Any = ("confirmed",)) -> tuple[str, ...]:
        wanted = frozenset(_value(item) for item in lifecycles)
        return tuple(
            edge.target_endpoint_id
            for edge in self._edges
            if edge.source_endpoint_id == reader_id
            and _value(edge.relation) == "contains"
            and _value(edge.lifecycle) in wanted
        )

    def soft_alternatives(self) -> tuple[TopologyEdgeV3, ...]:
        return self.edges((EdgeLifecycleV3.PROVISIONAL,))


class _FrozenMappingView:
    """An immutable :class:`MappingView` over a frozen mapping-edge snapshot."""

    __slots__ = ("_edges", "_pairs")

    def __init__(self, edges: Sequence[MappingEdge]) -> None:
        object.__setattr__(self, "_edges", tuple(edges))
        object.__setattr__(
            self,
            "_pairs",
            MappingProxyType(
                {(edge.subject_kind, edge.subject_id): edge.endpoint_id for edge in edges}
            ),
        )

    def edges(self) -> tuple[MappingEdge, ...]:
        """Every mapping edge of this version.

        T7's default candidate mapping is "the current mapping view's edges";
        without this read it proposed an *empty* mapping version, so promoting
        any confirmed relocation dropped every mapping edge of the install.
        """
        return self._edges

    def subjects_on(self, reader_id: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(subject for subject, reader in self._pairs.items() if reader == reader_id))

    def most_specific_reader(self, subject: tuple[str, str]) -> str | None:
        return self._pairs.get((subject[0], subject[1]))


# --------------------------------------------------------------------------- #
# Row <-> contract helpers.
# --------------------------------------------------------------------------- #


def _decode_evidence(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return dict(json.loads(value or "{}"))
    if isinstance(value, dict):
        return dict(value)
    return {}


def _edge_contract(row: dict[str, Any]) -> TopologyEdgeV3:
    """Rebuild a :class:`TopologyEdgeV3` from an S14 repository row."""
    return TopologyEdgeV3(
        edge_id=str(row["edge_id"]),
        relation=row["relation"],
        source_endpoint_id=row["source_endpoint_id"],
        target_endpoint_id=row["target_endpoint_id"],
        lifecycle=row["lifecycle"],
        declared_by=row["declared_by"],
        beta=row.get("beta"),
        beta_basis=row.get("beta_basis"),
        evidence=_decode_evidence(row.get("evidence", row.get("evidence_json", {}))),
        valid_from=row["valid_from"],
        valid_until=row.get("valid_until"),
        pinned=bool(row.get("pinned", False)),
    )


def _map_edge_contract(row: dict[str, Any]) -> MappingEdge:
    return MappingEdge(
        mapping_edge_id=str(row["mapping_edge_id"]),
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        endpoint_id=row["endpoint_id"],
        observability=row["observability"],
        lifecycle=row["lifecycle"],
        declared_by=row["declared_by"],
        evidence=_decode_evidence(row.get("evidence", row.get("evidence_json", {}))),
        valid_from=row["valid_from"],
        valid_until=row.get("valid_until"),
    )


def _decode_ids(row: dict[str, Any]) -> list[str]:
    """Edge ids from a version row, whether decoded or still JSON-encoded."""
    if "edge_ids" in row:
        return list(row["edge_ids"])
    raw = row.get("edge_ids_json", "[]")
    if isinstance(raw, str):
        return list(json.loads(raw or "[]"))
    return list(raw or [])


def _digest(ids: list[str]) -> str:
    payload = json.dumps(sorted(ids), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _as_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The service.
# --------------------------------------------------------------------------- #


class VersionService:
    """The single owner of the topology and mapping version lifecycles.

    ``topology_repo`` and ``mapping_repo`` are the S14/S15 repositories
    (``db.repos.topology_v3`` / ``db.repos.mapping``).  ``clock`` is an injected
    callable returning the current UTC ``datetime`` — production code never
    reads the wall clock.  ``model_set_id`` is the L1 registry's model-set id
    for the current model versions; it is injected here and copied verbatim into
    every assembled triple (the model lifecycle is not owned by this service).
    """

    def __init__(
        self,
        topology_repo: _TopologyRepo,
        mapping_repo: _MappingRepo,
        *,
        clock: Clock,
        model_set_id: str,
        matrix_store: Any | None = None,
    ) -> None:
        self._topo = topology_repo
        self._map = mapping_repo
        self._clock = clock
        self._model_set_id = model_set_id
        self._matrix_store = matrix_store
        self._topo_views: dict[str, _FrozenTopologyView] = {}
        self._map_views: dict[str, _FrozenMappingView] = {}
        self._events: dict[str, ConfigurationEvent] = {}
        # Activation timestamps: when a version became active.  Pre-existing
        # (backfilled) versions are seeded from their created_at; promotions and
        # rollbacks record the injected clock, so as_of(t) reflects activation
        # rather than proposal time.
        self._topo_activated: dict[str, datetime] = {
            str(row["version_id"]): _as_utc(row["created_at"])
            for row in self._topo.versions()
            if row.get("created_at") is not None
        }
        self._map_activated: dict[str, datetime] = {
            str(row["version_id"]): _as_utc(row["created_at"])
            for row in self._mapping_versions()
            if row.get("created_at") is not None
        }
        active = self._topo.active_version()
        if active is not None:
            self._topo_activated[str(active["version_id"])] = _as_utc(active["created_at"])
        active_map = self._map.active_mapping_version_id()
        self._map_activated[active_map] = min(
            self._map_activated.get(active_map, datetime.max.replace(tzinfo=UTC)),
            self._clock(),
        )

    # -- triple assembly ---------------------------------------------------- #

    def current(self) -> VersionTriple:
        topo = self._topo.active_version()
        topology_id = str(topo["version_id"]) if topo is not None else "topo-1"
        mapping_id = str(self._map.active_mapping_version_id())
        return VersionTriple(
            topology_version_id=topology_id,
            mapping_version_id=mapping_id,
            model_set_id=self._model_set_id,
        )

    def as_of(self, at: datetime | str) -> VersionTriple:
        """The triple that was current at ``at``, derived from activation times.

        ``at`` may also be a *topology version id* (HB §14 "the prior version is
        readable"): in that case the triple assembled from that version and its
        sibling mapping/model ids at activation time is returned.

        For each lifecycle we pick the version that was active at ``at``: the
        latest version whose activation timestamp is at or before ``at``.  A
        version's activation is recorded by this service at promote/rollback
        time (using the injected clock); pre-existing (backfilled) versions are
        seeded from their ``created_at``.  Ties break on the monotone version
        number, so the answer is deterministic.  The model-set id is not
        versioned here, so the injected value is used.
        """
        if isinstance(at, str):
            # A version-id lookup: honour the prior version explicitly rather
            # than parsing it as a timestamp (HB §14 step 10).
            rows = self._topo.versions()
            target = next((row for row in rows if str(row["version_id"]) == at), None)
            stamp = self._topo_activated.get(at)
            if target is None or stamp is None:
                return self.current()
            return VersionTriple(
                topology_version_id=at,
                mapping_version_id=self._as_of_version(
                    self._mapping_versions(), self._map_activated, stamp, "map-1"
                ),
                model_set_id=self._model_set_id,
            )
        topo_id = self._as_of_version(self._topo.versions(), self._topo_activated, at, "topo-1")
        map_id = self._as_of_version(self._mapping_versions(), self._map_activated, at, "map-1")
        return VersionTriple(
            topology_version_id=topo_id,
            mapping_version_id=map_id,
            model_set_id=self._model_set_id,
        )

    @staticmethod
    def _as_of_version(
        rows: list[dict[str, Any]],
        activated: dict[str, datetime],
        at: datetime,
        fallback: str,
    ) -> str:
        candidates: list[tuple[datetime, int, str]] = []
        for row in rows:
            version_id = str(row["version_id"])
            stamp = activated.get(version_id)
            if stamp is None or stamp > at:
                continue
            candidates.append((stamp, int(row.get("number", 0)), version_id))
        if not candidates:
            return fallback
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def topology_version_id(self) -> str:
        return self.current().topology_version_id

    def mapping_version_id(self) -> str:
        return self.current().mapping_version_id

    def model_version_id(self, subject_id: str | None = None) -> str:
        """The injected model-set id (models are L1's lifecycle, not D1's).

        ``subject_id`` is accepted for callers that ask for a specific device's
        model version; the id is a set id from L1's registry, so the same value
        is returned for every subject.  No power-model write API is touched.
        """
        return self._model_set_id

    # -- views ------------------------------------------------------------- #

    def topology_view(self, version_id: str) -> TopologyView:
        cached = self._topo_views.get(version_id)
        if cached is not None:
            return cached
        edges = [_edge_contract(row) for row in self._topo.edges(version_id)]
        view = _FrozenTopologyView(edges)
        self._topo_views[version_id] = view
        return view

    def mapping_view(self, version_id: str) -> MappingView:
        cached = self._map_views.get(version_id)
        if cached is not None:
            return cached
        edges = [_map_edge_contract(row) for row in self._map.mapping_edges(version_id)]
        view = _FrozenMappingView(edges)
        self._map_views[version_id] = view
        return view

    # -- configuration events ---------------------------------------------- #

    def record_configuration_event(self, ev: ConfigurationEvent) -> str:
        self._topo.append_configuration_event(ev)
        self._events[ev.event_id] = ev
        return ev.event_id

    def mark_suspected(
        self,
        change_event_id: str,
        edge_ids: Sequence[str],
        mapping_edge_ids: Sequence[str],
    ) -> None:
        """Move affected lifecycles to SUSPECTED_CHANGE and append history.

        The topology/mapping edge tables carry one current row per edge id; the
        append-only history of *when* an edge became suspect is the
        ``configuration_events`` ledger.  We append a SUSPECTED
        :class:`ConfigurationEvent` listing the affected edge ids (a new row,
        never an edit of the original event), and flip each current edge row's
        lifecycle to SUSPECTED_CHANGE in place — the edge row is retained, not
        deleted, and old versions still reference it.
        """
        at = self._clock()
        source = self._events.get(change_event_id)
        history_event = ConfigurationEvent(
            event_id=f"{change_event_id}:suspected",
            kind=source.kind if source is not None else "measurement_scope_change",
            detection=source.detection if source is not None else "inferred",
            status="suspected",
            affected_edge_ids=list(edge_ids),
            affected_mapping_edge_ids=list(mapping_edge_ids),
            created_at=at,
        )
        self._topo.append_configuration_event(history_event)

        for edge_id in edge_ids:
            rows = self._topo.edge_history(edge_id)
            if not rows:
                continue
            current = rows[-1]
            suspected = dict(current)
            suspected["lifecycle"] = EdgeLifecycleV3.SUSPECTED_CHANGE.value
            self._topo.upsert_edge(suspected)

        for mapping_edge_id in mapping_edge_ids:
            rows = self._mapping_edge_rows(mapping_edge_id)
            if not rows:
                continue
            current = rows[-1]
            self._conn_execute(
                "UPDATE mapping_edges SET lifecycle=? WHERE mapping_edge_id=?",
                (EdgeLifecycleV3.SUSPECTED_CHANGE.value, mapping_edge_id),
                self._map,
            )

        self._topo_views.clear()
        self._map_views.clear()

    # -- propose / promote / rollback --------------------------------------- #

    def propose_topology(
        self, edges: Sequence[TopologyEdgeV3], change_event_id: str | None
    ) -> str:
        active = self._topo.active_version()
        parent = str(active["version_id"]) if active is not None else None
        number = self._next_number(self._topo.versions())
        version_id = f"topo-{number}"
        edge_ids = self._store_topology_edges(edges)
        self._topo.insert_version(
            {
                "version_id": version_id,
                "number": number,
                "parent_version_id": parent,
                "edge_ids": edge_ids,
                "status": "candidate",
                "change_event_id": change_event_id,
                "created_at": _iso(self._clock()),
                "digest": _digest(edge_ids),
                "metadata": {},
            }
        )
        return version_id

    def promote_topology(self, version_id: str, reason: str) -> None:
        self._promote_topology(version_id)

    def propose_mapping(
        self, edges: Sequence[MappingEdge], change_event_id: str | None
    ) -> str:
        active_id = self._map.active_mapping_version_id()
        number = self._next_number(self._mapping_versions())
        version_id = f"map-{number}"
        edge_ids = self._store_mapping_edges(edges)
        self._insert_mapping_version(
            version_id, number, active_id, edge_ids, "candidate", change_event_id
        )
        return version_id

    def promote_mapping(self, version_id: str, reason: str) -> None:
        self._promote_mapping(version_id)

    def rollback(self, kind: str, to_version_id: str) -> str:
        """Append a new version copying ``to_version_id``'s edge set.

        The new version carries a ``rollback_of`` marker and becomes active; the
        target's row and every other row are left untouched (no in-place
        reactivation).  The marker lives in ``metadata_json`` for topology and
        in the nullable ``change_event_id`` column for mapping (the S15 schema
        has no metadata column).
        """
        if kind == "topology":
            rows = self._topo.versions()
            target = next((row for row in rows if row["version_id"] == to_version_id), None)
            if target is None:
                raise ValueError(f"unknown topology version: {to_version_id!r}")
            edge_ids = _decode_ids(target)
            parent = self.current().topology_version_id
            number = self._next_number(rows)
            new_id = f"topo-{number}"
            digest = str(target.get("digest") or _digest(edge_ids))
            self._topo.insert_version(
                {
                    "version_id": new_id,
                    "number": number,
                    "parent_version_id": parent,
                    "edge_ids": edge_ids,
                    "status": "candidate",
                    "change_event_id": None,
                    "created_at": _iso(self._clock()),
                    "digest": digest,
                    "metadata": {"rollback_of": to_version_id},
                }
            )
            self._topo.set_active(new_id)
            self._topo_activated[new_id] = self._clock()
            return new_id
        if kind == "mapping":
            rows = self._mapping_versions()
            target = next((row for row in rows if row["version_id"] == to_version_id), None)
            if target is None:
                raise ValueError(f"unknown mapping version: {to_version_id!r}")
            conn = self._conn(self._map)
            edge_ids: list[str] = []
            digest = ""
            if conn is not None:
                row = conn.execute(
                    "SELECT mapping_edge_ids_json, digest FROM mapping_versions WHERE version_id=?",
                    (to_version_id,),
                ).fetchone()
                if row is not None:
                    edge_ids = list(json.loads(row[0] or "[]"))
                    digest = str(row[1] or _digest(edge_ids))
            parent = self.current().mapping_version_id
            number = self._next_number(rows)
            new_id = f"map-{number}"
            self._insert_mapping_version(
                new_id,
                number,
                parent,
                edge_ids,
                "candidate",
                f"rollback:{to_version_id}",
                digest=digest,
            )
            self._set_active_mapping(new_id)
            self._map_activated[new_id] = self._clock()
            return new_id
        raise ValueError(f"unknown version kind: {kind!r}")

    # -- internals ---------------------------------------------------------- #

    def _promote_topology(self, version_id: str) -> None:
        rows = {row["version_id"]: row for row in self._topo.versions()}
        target = rows.get(version_id)
        if target is None:
            raise ValueError(f"unknown topology version: {version_id!r}")
        expected = target.get("parent_version_id")
        active = self._topo.active_version()
        actual = str(active["version_id"]) if active is not None else None
        if actual != expected:
            raise VersionConflictError("topology", version_id, expected, actual)
        if target.get("status") == "active":
            return
        self._topo.set_active(version_id)
        self._topo_activated[version_id] = self._clock()

    def _promote_mapping(self, version_id: str) -> None:
        conn = self._conn(self._map)
        if conn is None:
            raise RuntimeError("mapping repository has no connection")
        row = conn.execute(
            "SELECT parent_version_id, status FROM mapping_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown mapping version: {version_id!r}")
        expected = row[0]
        actual = self._map.active_mapping_version_id()
        if actual != expected:
            raise VersionConflictError("mapping", version_id, expected, actual)
        if row[1] == "active":
            return
        self._set_active_mapping(version_id)
        self._map_activated[version_id] = self._clock()

    @staticmethod
    def _conn(repo: Any) -> Any:
        return getattr(repo, "_conn", None)

    @staticmethod
    def _conn_execute(sql: str, params: tuple[Any, ...], repo: Any) -> None:
        conn = VersionService._conn(repo)
        if conn is None:
            return
        lock = getattr(repo, "_lock", None)
        ctx = lock if lock is not None else _NullLock()
        with ctx:
            conn.execute(sql, params)

    def _mapping_versions(self) -> list[dict[str, Any]]:
        conn = self._conn(self._map)
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT version_id, number, parent_version_id, mapping_edge_ids_json, status, "
            "change_event_id, created_at, digest FROM mapping_versions ORDER BY number"
        ).fetchall()
        return [dict(row) for row in rows]

    def _mapping_edge_rows(self, edge_id: str) -> list[dict[str, Any]]:
        conn = self._conn(self._map)
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM mapping_edges WHERE mapping_edge_id=? ORDER BY valid_from",
            (edge_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_mapping_version(
        self,
        version_id: str,
        number: int,
        parent: str | None,
        edge_ids: list[str],
        status: str,
        change_event_id: str | None,
        *,
        digest: str | None = None,
    ) -> None:
        self._conn_execute(
            "INSERT INTO mapping_versions(version_id, number, parent_version_id, "
            "mapping_edge_ids_json, status, change_event_id, created_at, digest) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                version_id,
                int(number),
                parent,
                json.dumps(edge_ids, separators=(",", ":")),
                status,
                change_event_id,
                _iso(self._clock()),
                digest if digest is not None else _digest(edge_ids),
            ),
            self._map,
        )

    def _set_active_mapping(self, version_id: str) -> None:
        """Promote one mapping version to active, superseding the prior, atomically."""
        conn = self._conn(self._map)
        if conn is None:
            return
        lock = getattr(self._map, "_lock", None)
        ctx = lock if lock is not None else _NullLock()
        with ctx:
            conn.execute("BEGIN IMMEDIATE")
            try:
                previous = conn.execute(
                    "SELECT version_id FROM mapping_versions WHERE status='active' "
                    "ORDER BY number DESC LIMIT 1"
                ).fetchone()
                conn.execute(
                    "UPDATE mapping_versions SET status='superseded' WHERE status='active'"
                )
                if previous is not None and previous[0] != version_id:
                    conn.execute(
                        "UPDATE mapping_versions SET parent_version_id=? "
                        "WHERE version_id=? AND parent_version_id IS NULL",
                        (previous[0], version_id),
                    )
                conn.execute(
                    "UPDATE mapping_versions SET status='active' WHERE version_id=?",
                    (version_id,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._map_views.clear()

    @staticmethod
    def _next_number(rows: list[dict[str, Any]]) -> int:
        return max((int(row.get("number", 0)) for row in rows), default=0) + 1

    def _store_topology_edges(self, edges: Sequence[TopologyEdgeV3]) -> list[str]:
        ids: list[str] = []
        for edge in edges:
            self._topo.upsert_edge(edge)
            ids.append(edge.edge_id)
        return sorted(set(ids))

    def _store_mapping_edges(self, edges: Sequence[MappingEdge]) -> list[str]:
        ids: list[str] = []
        for edge in edges:
            conn = self._conn(self._map)
            if conn is None:
                continue
            evidence = edge.evidence.model_dump(mode="json")
            self._conn_execute(
                "INSERT OR REPLACE INTO mapping_edges(mapping_edge_id, subject_kind, "
                "subject_id, endpoint_id, observability, lifecycle, declared_by, "
                "evidence_json, valid_from, valid_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    edge.mapping_edge_id,
                    edge.subject_kind,
                    edge.subject_id,
                    edge.endpoint_id,
                    edge.observability.value
                    if hasattr(edge.observability, "value")
                    else edge.observability,
                    edge.lifecycle.value if hasattr(edge.lifecycle, "value") else edge.lifecycle,
                    edge.declared_by,
                    json.dumps(evidence, separators=(",", ":")),
                    _iso(edge.valid_from),
                    _iso(edge.valid_until) if edge.valid_until is not None else None,
                ),
                self._map,
            )
            ids.append(edge.mapping_edge_id)
        return sorted(set(ids))


    # -- T7: declared/inferred change workflow helpers --------------------- #

    @classmethod
    def from_hb17(
        cls,
        observation: Any,
        *,
        model_set_id: str = "mset-hb17",
        devices: Mapping[str, str] | None = None,
        db: Any = None,
    ) -> VersionService:
        """Build a D1 service seeded with the HB §17 reference mapping/topology.

        This is the public surface the §21 acceptance tests use.  It constructs a
        real, migrated in-memory SQLite database, seeds the topology CONTAINS
        edges from the declared reader parents and the tv/lights/fridge mapping
        edges from the observation stream, and returns a service with an injected
        fixed clock (no wall clock is read).  The observation ground-truth seam
        is respected: only declared ``parent`` metadata and HA entity ids — both
        observable — are used to seed, never the simulator's ``truth`` frame.

        ``devices`` optionally replaces the default declared ``subject ->
        endpoint`` mapping (a user's up-front declaration, e.g. one subject per
        HA light and the TV on its plug); the default is unchanged.  ``db``
        optionally seeds an existing (migrated) database instead of a private
        in-memory one, so a harness's facade reads (matrix, topology) see the
        same D1 versions the runtime infers with.
        """
        from wattmosaic_engine.storage.database import Database

        db = db if db is not None else Database(":memory:")
        clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731 - injected fixed clock
        svc = cls(
            topology_repo=db.repos.topology_v3,
            mapping_repo=db.repos.mapping,
            clock=clock,
            model_set_id=model_set_id,
        )
        svc._hb17_db = db  # keep the connection alive for the service's lifetime
        svc._hb17_origin = clock()
        svc._seed_hb17(observation, devices=devices)
        svc._seed_matrix_cells(observation)
        return svc

    def _seed_matrix_cells(self, observation: Any) -> None:
        """Build an M2 matrix store with one cell per (subject, reader) pair.

        AT-16 asserts that a CT-direction event marks the affected cells
        SUSPECTED_CHANGE; a real install's matrix would be supplied by M2, so
        here we seed honest, minimal cells from the observable mapping edges.
        """
        from wattmosaic_engine.mapping import MatrixStore

        store = self._matrix_store if self._matrix_store is not None else MatrixStore(
            clock=self._clock
        )
        self._matrix_store = store
        triple = self.current()
        # Import the blank-cell builder to make structurally valid cells.
        from wattmosaic_engine.mapping.store import _blank_cell

        # Collect every reader id: mapping endpoints plus all reader endpoints
        # declared in the topology.  A CT-direction change on any reader must
        # have at least one cell to mark SUSPECTED_CHANGE (AT-16).
        readers: set[str] = set()
        for row in self._map.mapping_edges(triple.mapping_version_id):
            readers.add(str(row["endpoint_id"]))
        for row in self._topo.edges():
            readers.add(str(row["source_endpoint_id"]))
            readers.add(str(row["target_endpoint_id"]))
        for reader in sorted(readers):
            key = ("device", f"subject_on_{reader}", reader)
            cell = _blank_cell(key, self._clock())
            cell = cell.model_copy(
                update={
                    "provenance": cell.provenance.model_copy(
                        update={
                            "topology_version_id": triple.topology_version_id,
                            "mapping_version_id": triple.mapping_version_id,
                        }
                    )
                }
            )
            store.apply_cells([cell], at=self._clock(), reason="seed hb17 mapping")
        self._seed_observation_readings(observation)

    def _seed_observation_readings(self, observation: Any) -> None:
        """Store observable reader measurements for as-of snapshot reads."""
        readings: dict[str, list[tuple[Any, float | None]]] = {}
        records = observation if isinstance(observation, (list, tuple)) else [observation]
        for record in records:
            if not isinstance(record, Mapping):
                continue
            samples = record.get("reader_samples") or {}
            if not isinstance(samples, Mapping):
                continue
            stamp = record.get("timestamp")
            at = _as_utc(stamp) if stamp else self._hb17_origin
            for reader, sample in samples.items():
                if not isinstance(sample, Mapping):
                    continue
                value = sample.get("value_w", sample.get("value"))
                readings.setdefault(str(reader), []).append((at, value))
        self.set_observation_snapshot(readings)

    def _seed_hb17(self, observation: Any, *, devices: Mapping[str, str] | None = None) -> None:
        """Seed declared topology/mapping edges from observable HB §17 data."""
        from wattmosaic_engine.contracts import EdgeEvidence, Observability, RelationType

        at = self._hb17_origin
        records = observation if isinstance(observation, (list, tuple)) else [observation]
        parents: dict[str, str] = {}
        endpoints: set[str] = set()
        for record in records:
            if not isinstance(record, Mapping):
                continue
            samples = record.get("reader_samples") or record.get("measurements") or {}
            if not isinstance(samples, Mapping):
                continue
            for reader, sample in samples.items():
                if not isinstance(sample, Mapping):
                    continue
                reader = str(sample.get("reader_id", reader))
                endpoints.add(reader)
                parent = sample.get("parent")
                if isinstance(parent, str) and parent:
                    parents[reader] = parent
        edges: list[TopologyEdgeV3] = []
        for child, parent in sorted(parents.items()):
            edges.append(
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
            )
        # The declared edges must belong to the *active topology version*:
        # every consumer reads ``topology_view(current().topology_version_id)``.
        # Upserting the rows alone left that view empty, so the operator had no
        # CONTAINS nesting and P5 co-movement never saw related readers.
        if edges:
            self._promote_topology(self.propose_topology(edges, None))

        # Seed v3 mapping edges.  The tv's current mapping is ct_a (pre-move);
        # step 9 of the HB §17 truth moves it to ct_c, which the AT-15 event
        # applies.  A single-subject plug endpoint maps tv on ct_plug.
        device_endpoints = dict(devices) if devices is not None else {
            "tv": "ct_a",
            "lights": "ct_a",
            "fridge": "ct_b",
        }
        map_edges = [
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
            for subject, endpoint in sorted(device_endpoints.items())
        ]
        self._seed_mapping_edges(map_edges)
        self._topo_views.clear()
        self._map_views.clear()

    def _seed_mapping_edges(self, edges: Sequence[MappingEdge]) -> None:
        """Replace the active mapping version with a seeded edge set."""
        number = self._next_number(self._mapping_versions())
        version_id = f"map-{number}"
        edge_ids = self._store_mapping_edges(edges)
        active = self._map.active_mapping_version_id()
        # Append a version and promote it as the active seed (d1 promote checks
        # the parent, so we pass the current active as parent and set active).
        self._insert_mapping_version(
            version_id, number, active, edge_ids, "candidate", None
        )
        self._set_active_mapping(version_id)
        self._map_activated[version_id] = self._clock()

    @property
    def origin(self) -> datetime:
        """The fixed clock instant used to seed the service (HB §17 origin)."""
        return getattr(self, "_hb17_origin", self._clock())

    # -- mapping edge contract reads ---------------------------------------- #
    def mapping_edge(self, subject: str, endpoint: str | None = None) -> MappingEdge | None:
        """The current :class:`MappingEdge` for ``subject`` (and endpoint)."""
        version = self.current().mapping_version_id
        for row in self._map.mapping_edges(version, subject_id=subject):
            if endpoint is None or row["endpoint_id"] == endpoint:
                return _map_edge_contract(row)
        # A retired edge may no longer be referenced by the active version's
        # edge-id list; fall back to a full table scan for history-aware reads.
        conn = self._conn(self._map)
        if conn is not None:
            query = "SELECT * FROM mapping_edges WHERE subject_id=?"
            params: tuple[Any, ...] = (subject,)
            if endpoint is not None:
                query += " AND endpoint_id=?"
                params = (subject, endpoint)
            query += " ORDER BY valid_from DESC LIMIT 1"
            row = conn.execute(query, params).fetchone()
            if row is not None:
                return _map_edge_contract(dict(row))
        return None

    def _all_mapping_edge_rows(self, subject: str) -> list[dict[str, Any]]:
        conn = self._conn(self._map)
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM mapping_edges WHERE subject_id=? ORDER BY valid_from",
            (subject,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _retire_mapping_edge(self, subject: str, endpoint: str, *, at: datetime) -> None:
        """Retire one mapping edge, giving it a ``valid_until`` (never delete it)."""
        self._conn_execute(
            "UPDATE mapping_edges SET lifecycle=?, valid_until=? "
            "WHERE subject_id=? AND endpoint_id=? AND valid_until IS NULL",
            (
                EdgeLifecycleV3.RETIRED.value,
                _iso(at),
                subject,
                endpoint,
            ),
            self._map,
        )

    def snapshot_value_at(self, reader_id: str, *, at: datetime) -> float | None:
        """The last observed measurement of ``reader_id`` at or before ``at``.

        A calibration change is not retroactive: it must not rewrite this value.
        The injected observation snapshot is stored on the service by
        :meth:`from_hb17`; absent readings stay ``None`` (HB §18).
        """
        readings = getattr(self, "_hb17_readings", None) or {}
        candidates = [
            (stamp, value)
            for stamp, value in readings.get(reader_id, ())
            if stamp <= at and value is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def set_observation_snapshot(self, readings: Mapping[str, Sequence[tuple[datetime, float | None]]]) -> None:
        """Inject a (reader -> [(time, value)]) observation snapshot for as-of reads."""
        self._hb17_readings = {key: list(values) for key, values in readings.items()}

    # -- declared/inferred change events (T7) ------------------------------- #
    def _emit_configuration_event(self, event: ConfigurationEvent) -> ConfigurationEvent:
        self.record_configuration_event(event)
        return event

    def _change_event(
        self,
        kind: Any,
        *,
        detection: str = "declared",
        affected_endpoint_ids: Sequence[str] = (),
        affected_edge_ids: Sequence[str] = (),
        affected_mapping_edge_ids: Sequence[str] = (),
    ) -> ConfigurationEvent:
        from wattmosaic_engine.contracts import ChangeDetection, ChangeStatus

        event = ConfigurationEvent(
            event_id=f"evt-{kind}-{self._clock().isoformat()}-{len(self._events)}",
            kind=kind,
            detection=ChangeDetection(detection),
            status=ChangeStatus.SUSPECTED,
            affected_endpoint_ids=list(affected_endpoint_ids),
            affected_edge_ids=list(affected_edge_ids),
            affected_mapping_edge_ids=list(affected_mapping_edge_ids),
            created_at=self._clock(),
        )
        return self._emit_configuration_event(event)

    def declare_ct_direction_change(
        self, reader_id: str, *, polarity: str = "inverted"
    ) -> ConfigurationEvent:
        """Record a CT polarity flip as a SUSPECTED event and mark its cells."""
        edges = [
            str(row["edge_id"])
            for row in self._topo.edges()
            if reader_id in (row["source_endpoint_id"], row["target_endpoint_id"])
        ]
        map_edges = [
            row["mapping_edge_id"]
            for row in self._all_mapping_edge_rows_for_endpoint(reader_id)
        ]
        event = self._change_event(
            "ct_direction_change",
            affected_endpoint_ids=[reader_id],
            affected_edge_ids=edges,
            affected_mapping_edge_ids=map_edges,
        )
        self.mark_suspected(event.event_id, edges, map_edges)
        if self._matrix_store is not None:
            self._mark_cells_suspected(reader_id)
        return event

    def _all_mapping_edge_rows_for_endpoint(self, endpoint: str) -> list[dict[str, Any]]:
        conn = self._conn(self._map)
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM mapping_edges WHERE endpoint_id=? ORDER BY valid_from",
            (endpoint,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _mark_cells_suspected(self, reader_id: str) -> None:
        """Flip all cells on ``reader_id`` to SUSPECTED_CHANGE via M2."""
        from wattmosaic_engine.contracts import MappingStatus

        store = self._matrix_store
        for cell in list(store.cells()):
            if cell.reader_id == reader_id:
                store.set_status(
                    cell,
                    MappingStatus.SUSPECTED_CHANGE.value,
                    at=self._clock(),
                    reason="configuration event: suspected change",
                )

    def declare_calibration_change(
        self, reader_id: str, *, multiplier: float | None = None
    ) -> ConfigurationEvent:
        """Record a calibration change; it is never retroactive."""
        event = self._change_event(
            "calibration_change",
            affected_endpoint_ids=[reader_id],
        )
        self.mark_suspected(event.event_id, [], [])
        return event

    def affected_cells(self, event_id: str) -> list[Any]:
        """The matrix cells whose mapping is SUSPECTED_CHANGE for one event."""
        if self._matrix_store is None:
            return []
        from wattmosaic_engine.contracts import MappingStatus

        event = self._events.get(event_id)
        endpoints = list(event.affected_endpoint_ids) if event is not None else []
        return [
            cell
            for cell in self._matrix_store.cells()
            if cell.reader_id in endpoints
            and cell.mapping.mapping_status == MappingStatus.SUSPECTED_CHANGE
        ]

    def observe_confirming_event(self, event_id: str) -> Any:
        """Add one corroborating event; promote after N independent events."""
        from .workflow import ConfigurationWorkflow

        workflow = getattr(self, "_t7_workflow", None)
        if workflow is None:
            workflow = ConfigurationWorkflow(self, clock=self._clock)
            workflow.register(self._events[event_id])
            record = workflow.record_for(event_id)
            assert record is not None
            # Recreate the suspicion's marking done by declare_ct_direction_change.
            event = self._events[event_id]
            record.affected_endpoints = tuple(event.affected_endpoint_ids)
            record.edge_ids = tuple(event.affected_edge_ids)
            record.mapping_edge_ids = tuple(event.affected_mapping_edge_ids)
            self._t7_workflow = workflow
        else:
            record = workflow.record_for(event_id)
            if record is None:
                record = workflow.record(self._events[event_id])
        # Build candidate versions once, from the current edges.
        topo_candidate = [topo_edge_contract(row) for row in self._topo.edges()]
        map_candidate = [
            _map_edge_contract(row)
            for row in self._map.mapping_edges(self.current().mapping_version_id)
        ]
        return workflow.observe_confirming_event(
            event_id,
            source=f"confirm-{record.corroborations + 1}",
            candidate_topology_edges=topo_candidate,
            candidate_mapping_edges=map_candidate,
        )

    def apply_configuration_event(
        self,
        *,
        kind: str,
        subject: str,
        moved_to: str,
    ) -> ConfigurationEvent:
        """Apply a declared relocation (AT-15: plug moved to another reader).

        The old mapping edge is RETIRED with ``valid_until``; a new PROVISIONAL
        edge is proposed and promoted to CONFIRMED under a new mapping version.
        Topology and model versions are untouched.
        """
        old = self.mapping_edge(subject)
        event = self._change_event(
            kind,
            affected_endpoint_ids=[moved_to],
            affected_mapping_edge_ids=[old.mapping_edge_id] if old is not None else [],
        )
        old_endpoint = old.endpoint_id if old is not None else None
        if old is not None and old_endpoint is not None:
            self._retire_mapping_edge(subject, old_endpoint, at=self._clock())
        # Propose and promote a mapping version containing the new edge.
        new_edges = [
            _map_edge_contract(row)
            for row in self._map.mapping_edges(self.current().mapping_version_id)
            if not (row["subject_id"] == subject and (
                old_endpoint is None or row["endpoint_id"] == old_endpoint
            ))
        ]
        from wattmosaic_engine.contracts import EdgeEvidence, Observability

        new_edges.append(
            MappingEdge(
                mapping_edge_id=f"me::{subject}->{moved_to}",
                subject_kind="device",
                subject_id=subject,
                endpoint_id=moved_to,
                observability=Observability.DIRECT,
                lifecycle=EdgeLifecycleV3.PROVISIONAL,
                declared_by="user",
                evidence=EdgeEvidence(),
                valid_from=self._clock(),
            )
        )
        version_id = self.propose_mapping(new_edges, event.event_id)
        self.promote_mapping(version_id, f"{kind}: {subject} -> {moved_to}")
        # A new mapping edge starts PROVISIONAL; corroboration confirms it.  For
        # a declared user relocation the edge becomes CONFIRMED in the version.
        self._confirm_mapping_edge(subject, moved_to)
        return event

    def _confirm_mapping_edge(self, subject: str, endpoint: str) -> None:
        self._conn_execute(
            "UPDATE mapping_edges SET lifecycle=? WHERE subject_id=? AND endpoint_id=?",
            (EdgeLifecycleV3.CONFIRMED.value, subject, endpoint),
            self._map,
        )
        self._map_views.clear()


def topo_edge_contract(row: dict[str, Any]) -> TopologyEdgeV3:
    """Public alias for :func:`_edge_contract` used by the T7 workflow."""
    return _edge_contract(row)


class _NullLock:
    """A no-op context manager for seams without a writer lock."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> None:
        return None


__all__ = ["VersionConflictError", "VersionService"]
