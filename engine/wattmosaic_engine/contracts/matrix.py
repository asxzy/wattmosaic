"""Matrix contracts (K05): structured cells, trees and drill-downs.

Implements ``interfaces.md`` §8 and the HB §9/§10/§19 vocabulary for the
topology-aware measurement matrix.  A cell is a structured, evidence-backed
object (HB §9.6); the observability and mapping-status vocabularies are the
HB §9.7 seven-state sets; the column tree is L0–L4 (``ColumnNode``); and the row
tree carries the special row kinds (``RowKind``) of DEC-27.  Drill-down payloads
(``CellDetail``, ``ReaderDetail``, ``RowDetail``) serve HB §19.4.

Shared vocabularies are reused: ``Observability`` and ``RelationType`` from
:mod:`.topology` (``RelationType`` widened with ``"direct_endpoint"`` and
``None``), ``PowerLevelV3`` from :mod:`.power_models` for
``CellPower.operating_state``, and ``Bounds`` / ``EvidenceRef`` /
``IdentifiabilityStatus`` / ``EstimateStatus`` / ``VersionTriple`` /
``ConfidenceVectorV3`` from :mod:`.foundation`.

Null policy (HB §18): every watt / confidence field that can be absent is
``None``; a non-identifiable member never receives a number.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .foundation import (
    Bounds,
    ConfidenceVectorV3,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    Model,
    VersionTriple,
)
from .power_models import PowerLevelV3
from .topology import Observability, RelationType

__all__: list[str] = [
    # Enumerations (HB §9.7 / §8)
    "MappingStatus",
    "RowKind",
    "CellVisual",
    # Structured cell (HB §9.6)
    "CellMapping",
    "CellPower",
    "CellEvents",
    "CellProvenance",
    "CellTimestamps",
    "MappingCell",
    # Trees
    "ColumnAnnotation",
    "ColumnNode",
    "RowNode",
    # Aggregates (§8)
    "ReaderColumnSummary",
    "PhysicalTotal",
    "MatrixPayload",
    # Drill-down (HB §19.4)
    "CellDetail",
    "ReaderDetail",
    "RowDetail",
]

# --------------------------------------------------------------------------- #
# Enumerations (HB §9.7 / interfaces §8).
# --------------------------------------------------------------------------- #


class MappingStatus(str, Enum):
    """The epistemic state of one device↔reader mapping (HB §9.7).

    Deliberately contains **no** identifiability value (``analysis/E-…`` C1):
    identifiability is a separate axis, carried by ``MappingCell.identifiability``
    as an :class:`~wattmosaic_engine.contracts.foundation.IdentifiabilityStatus`.
    """

    UNKNOWN = "unknown"
    LEARNING = "learning"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    SUSPECTED_CHANGE = "suspected_change"
    CONFLICTED = "conflicted"
    RETIRED = "retired"


class RowKind(str, Enum):
    """The row families of the matrix (DEC-27)."""

    DEVICE = "device"
    COMPONENT = "component"
    GROUP = "group"
    UNKNOWN_PROFILE = "unknown_profile"
    READER_RESIDUAL = "reader_residual"
    SUMMARY = "summary"


class CellVisual(str, Enum):
    """The eight deterministic visual categories (HB §19.3 / design §8)."""

    DIRECTLY_MEASURED = "directly_measured"
    CONFIRMED_INFERRED = "confirmed_inferred"
    PROVISIONAL = "provisional"
    GROUP_ONLY = "group_only"
    UNKNOWN = "unknown"
    NOT_OBSERVABLE = "not_observable"
    CONFLICTED = "conflicted"
    RETIRED = "retired"


# Observable levels that may carry an estimated power; UNKNOWN and
# NOT_OBSERVABLE must not (validated on MappingCell).  Compared against the
# stored string values (``use_enum_values=True``).
_NO_ESTIMATE_LEVELS = frozenset(
    {Observability.UNKNOWN.value, Observability.NOT_OBSERVABLE.value}
)

# --------------------------------------------------------------------------- #
# Structured cell blocks (HB §9.6).
# --------------------------------------------------------------------------- #


class CellMapping(Model):
    """The mapping block of a cell: how the reader observes the load."""

    observability: Observability
    relation_type: RelationType | Literal["direct_endpoint"] | None
    mapping_status: MappingStatus
    mapping_confidence: float | None = None
    valid_from: datetime
    valid_until: datetime | None = None


class CellPower(Model):
    """The power block of a cell: contribution, bounds and operating state.

    ``estimated_power_w`` is ``None`` when the mapping is not observable or the
    cell is group-only; it is never defaulted to ``0`` (HB §18).
    """

    measurement_status: EstimateStatus
    estimated_power_w: float | None = None
    bounds: Bounds | None = None
    standby_power_w: float | None = None
    operating_state: PowerLevelV3 | None = None
    expected_profile_ref: str | None = None
    uncertainty_w: float | None = None


class CellEvents(Model):
    """Independent event-association counts (HB §10.3: 100 samples ≠ 100 events)."""

    matched_event_count: int = 0
    confirmed_match_count: int = 0
    provisional_match_count: int = 0
    avg_response_delay_ms: float | None = None
    response_signature: dict[str, Any] | None = None
    last_matched_at: datetime | None = None


class CellProvenance(Model):
    """Typed, resolvable provenance for a cell (HB §9.6 / §18)."""

    sources_used: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    topology_version_id: str
    model_version_id: str | None = None
    mapping_version_id: str


class CellTimestamps(Model):
    """The observation/validation/refresh times of a cell."""

    first_observed_at: datetime
    last_observed_at: datetime
    last_validated_at: datetime | None = None
    updated_at: datetime


class MappingCell(Model):
    """One device/component × reader cell of the matrix (HB §9.6)."""

    row_kind: RowKind
    row_id: str
    load_component_id: str | None = None
    reader_id: str
    mapping: CellMapping
    power: CellPower
    identifiability: IdentifiabilityStatus
    group_id: str | None = None
    event_association: CellEvents
    confidence: ConfidenceVectorV3
    provenance: CellProvenance
    timestamps: CellTimestamps
    visual: CellVisual
    derived: bool = False

    # -- Validators --------------------------------------------------------- #

    @model_validator(mode="after")
    def _bounds_bracket_the_estimate(self) -> MappingCell:
        """Ported from legacy ``DeviceReaderMappingCell``: bounds must bracket."""
        estimate = self.power.estimated_power_w
        bounds = self.power.bounds
        if bounds is not None:
            low, high = bounds.lower_w, bounds.upper_w
            if low is not None and high is not None and low > high:
                raise ValueError(
                    f"bounds.lower_w ({low}) must not exceed bounds.upper_w ({high})"
                )
            if estimate is not None:
                if low is not None and estimate < low:
                    raise ValueError(
                        f"estimated_power_w ({estimate}) is below bounds.lower_w ({low})"
                    )
                if high is not None and estimate > high:
                    raise ValueError(
                        f"estimated_power_w ({estimate}) is above bounds.upper_w ({high})"
                    )
        return self

    @model_validator(mode="after")
    def _observability_gates_estimate(self) -> MappingCell:
        """UNKNOWN / NOT_OBSERVABLE observability carries no estimate."""
        if (
            str(self.mapping.observability) in _NO_ESTIMATE_LEVELS
            and self.power.estimated_power_w is not None
        ):
            raise ValueError(
                f"observability={self.mapping.observability!r} forbids an "
                "estimated_power_w; use None (HB §18)"
            )
        return self

    @model_validator(mode="after")
    def _group_only_has_no_estimate(self) -> MappingCell:
        """A GROUP_ONLY visual publishes no number on member cells."""
        if (
            str(self.visual) == CellVisual.GROUP_ONLY.value
            and self.power.estimated_power_w is not None
        ):
            raise ValueError(
                "a GROUP_ONLY cell must not carry estimated_power_w on a member; "
                "the group row carries the sum"
            )
        return self

    @model_validator(mode="after")
    def _summary_is_derived(self) -> MappingCell:
        """SUMMARY rows are always derived."""
        if str(self.row_kind) == RowKind.SUMMARY.value and not self.derived:
            raise ValueError("a SUMMARY row must have derived=True")
        return self


# --------------------------------------------------------------------------- #
# Column tree (L0–L4, design §8 / HB §19.1).
# --------------------------------------------------------------------------- #


class ColumnAnnotation(Model):
    """A secondary edge on a reader leaf — never an extra column (design §8)."""

    kind: Literal[
        "SHARED_LOAD",
        "PAIRED_LEG",
        "SAME_CONDUCTOR",
        "CORRELATED",
        "CONTAINS",
        "ALIAS",
    ]
    related_reader_id: str | None = None
    note: str | None = None


class ColumnNode(Model):
    """One node of the L0–L4 column tree.

    A ``reader`` node is a leaf: it has ``reader_id`` set and no children.  Any
    other kind is an internal node with no ``reader_id``.  Secondary relations
    live in ``annotations`` so they never duplicate a reader leaf.
    """

    node_id: str
    level: Literal["L0", "L1", "L2", "L3", "L4"]
    label: str
    kind: Literal["domain", "panel", "subpanel", "circuit", "reader", "unassigned"]
    reader_id: str | None = None
    children: list[ColumnNode] = Field(default_factory=list)
    annotations: list[ColumnAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reader_iff_leaf(self) -> ColumnNode:
        is_reader = self.kind == "reader"
        if is_reader:
            if not self.reader_id:
                raise ValueError("a reader ColumnNode must set reader_id")
            if self.children:
                raise ValueError("a reader ColumnNode must have no children")
            if self.level != "L4":
                raise ValueError("a reader ColumnNode must be at level L4")
        elif self.reader_id is not None:
            raise ValueError("only a reader ColumnNode may set reader_id")
        return self


class RowNode(Model):
    """One node of the row tree; ``room``/``folder`` are grouping nodes."""

    node_id: str
    kind: RowKind | Literal["room", "folder"]
    label: str
    row_id: str | None = None
    children: list[RowNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _grouping_rows_have_no_row_id(self) -> RowNode:
        if self.kind in ("room", "folder") and self.row_id is not None:
            raise ValueError("a room/folder RowNode is a grouping node with no row_id")
        return self


# --------------------------------------------------------------------------- #
# Aggregates (interfaces §8).
# --------------------------------------------------------------------------- #


class ReaderColumnSummary(Model):
    """Per-reader totals; never summed across readers (design §8)."""

    measured_w: float | None = None
    known_contrib_w: float | None = None
    residual_w: float | None = None
    unknown_w: float | None = None


class PhysicalTotal(Model):
    """The household physical total — each load counted once (design §8)."""

    measured_w: float | None = None
    identified_w: float | None = None
    group_w: float | None = None
    unknown_w: float | None = None
    residual_w: float | None = None
    total_w: float | None = None


class MatrixPayload(Model):
    """The full matrix view: version, trees, sparse cells and totals."""

    mapping_version_id: str
    versions: VersionTriple
    generated_at: datetime
    columns: list[ColumnNode]
    rows: list[RowNode]
    cells: list[MappingCell]
    reader_totals: dict[str, ReaderColumnSummary]
    physical_total: PhysicalTotal
    #: Readers excluded or silent right now, as the snapshot reports them
    #: (``reader`` or ``reader: reason``).  A stale column's totals are null
    #: with its reason here -- never a fabricated 0 W (HB §18, AT-17).
    stale_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reader_leaves_are_unique(self) -> MatrixPayload:
        """AT-13 at contract level: reader ids unique across all column leaves."""
        seen: set[str] = set()
        stack = list(self.columns)
        while stack:
            node = stack.pop()
            if node.kind == "reader":
                rid = str(node.reader_id)
                if rid in seen:
                    raise ValueError(f"duplicated reader leaf in columns: {rid!r}")
                seen.add(rid)
            else:
                stack.extend(node.children)
        return self


# --------------------------------------------------------------------------- #
# Drill-down payloads (HB §19.4).
# --------------------------------------------------------------------------- #


class CellDetail(Model):
    """Cell drill-down: history, event groups and evidence references."""

    cell: MappingCell
    mapping_history: list[MappingCell] = Field(default_factory=list)
    event_groups: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence_components: ConfidenceVectorV3 | None = None
    uncertainty_reasons: list[str] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)


class ReaderDetail(Model):
    """Reader (column) drill-down: scope, relations and totals."""

    reader_id: str
    scope: str | None = None
    topology_relations: list[dict[str, Any]] = Field(default_factory=list)
    measured_w: float | None = None
    known_contrib_w: float | None = None
    known_contributions: list[dict[str, Any]] = Field(default_factory=list)
    residual_w: float | None = None
    unknown_w: float | None = None


class RowDetail(Model):
    """Row (device) drill-down: operating state, profile and mapping history."""

    row_id: str
    kind: RowKind
    label: str
    operating_state: PowerLevelV3 | None = None
    power_profile_ref: str | None = None
    observed_readers: list[str] = Field(default_factory=list)
    mapping_history: list[dict[str, Any]] = Field(default_factory=list)
    cells: list[MappingCell] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("cell_mapping", CellMapping),
    ("cell_power", CellPower),
    ("cell_events", CellEvents),
    ("cell_provenance", CellProvenance),
    ("cell_timestamps", CellTimestamps),
    ("mapping_cell_v3", MappingCell),
    ("column_annotation", ColumnAnnotation),
    ("column_node", ColumnNode),
    ("row_node", RowNode),
    ("reader_column_summary", ReaderColumnSummary),
    ("physical_total", PhysicalTotal),
    ("matrix_payload", MatrixPayload),
    ("cell_detail", CellDetail),
    ("reader_detail", ReaderDetail),
    ("row_detail", RowDetail),
]
