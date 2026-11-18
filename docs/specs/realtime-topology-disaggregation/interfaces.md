# Interfaces — contract 3.0, runtime handoffs, HTTP, storage

This file is **normative for shapes**. Field names, nullability, units and enum members here are what K00–K08 implement. Semantics are in `design.md`. A builder who needs a field that isn't listed asks the orchestrator. Do not add parallel fields (HB §16: no compatibility layers).

Notation: `T | None` = nullable. A missing value is `None`, **never 0** (HB §18). All powers are W, signed (`+` = consumption seen by the reader, `−` = export/reverse). All times are UTC `datetime`. `*_s` = seconds, `*_ms` = milliseconds.

---

## 1. Conventions (K00, `contracts/foundation.py`)

```python
class Model(BaseModel):            # unchanged base: extra="forbid", use_enum_values=True

TopologyVersionId = str            # "topo-<n>"
MappingVersionId  = str            # "map-<n>"
ModelVersionId    = str            # "mdl-<subject_id>-<n>"
ModelSetId        = str            # "mset-<sha256[:12]>" of sorted active ModelVersionIds
SnapshotId        = str            # "snap-<ulid>"

class VersionTriple(Model):
    topology_version_id: TopologyVersionId
    mapping_version_id: MappingVersionId
    model_set_id: ModelSetId

class EvidenceRef(Model):          # typed, resolvable by D4's resolver
    kind: Literal["sample","event_evidence","event_group","ha_event","edge","mapping_edge",
                  "model_version","configuration_event","observation"]
    id: str

class EstimateStatus(str, Enum):   MEASURED, ESTIMATED, ALLOCATED, UNKNOWN          # HB §8.5
class IdentifiabilityStatus(str, Enum):
    IDENTIFIABLE, PARTIALLY_IDENTIFIABLE, GROUP_IDENTIFIABLE, NON_IDENTIFIABLE, INSUFFICIENT_EVIDENCE  # HB §8.4
class InferenceMethod(str, Enum):  DIRECT_READING, JOINT_DELTA, JOINT_ABSOLUTE, MODEL_PRIOR, DECLARED, NONE

class Bounds(Model):
    lower_w: float | None
    upper_w: float | None
    basis: Literal["non_negativity","group_sum","reader_ceiling","posterior","declared_ceiling"] | None
    # validator: lower<=upper; zero width allowed only when the owning estimate is IDENTIFIABLE or MEASURED
```

**Null-policy test (K00):** a reflective test walks every exported model. No field whose name ends in `_w`, `_kwh`, `_wh` or `_a`, or that is named `confidence`, may have a numeric default other than `None`.

---

## 2. Canonical entities (K01, `contracts/entities.py`) — HB §1

```python
class EndpointKind(str, Enum):  CT, SMART_PLUG, APPLIANCE_MONITOR, PANEL_MAIN, VIRTUAL
class EndpointScope(str, Enum): CIRCUIT, PANEL_FEED, SINGLE_SUBJECT, UNKNOWN          # DEC-05
class ScopeStatus(str, Enum):   CANDIDATE, CONFIRMED, REJECTED

class MeasurementEndpoint(Model):
    endpoint_id: str                 # == legacy ct_id for CTs (preserved)
    kind: EndpointKind
    entity_id: str | None            # HA power entity
    energy_entity_id: str | None
    display_name: str | None
    scope: EndpointScope
    scope_status: ScopeStatus
    scope_subject_id: str | None     # set iff scope == SINGLE_SUBJECT
    panel_id: str | None
    enabled: bool
    created_at: datetime

class EndpointConfigRevision(Model):  # append-only, HB §1.1 D "configuration history"
    endpoint_id: str
    revision: int
    valid_from: datetime
    valid_until: datetime | None
    phase: Literal["l1","l2","both","unknown"]
    leg: Literal["l1","l2"] | None
    voltage_reference_v: float | None
    polarity: Literal["normal","inverted"]
    multiplier: float                # applied once at normalization (P1)
    calibration_version: str
    measurement_type: "MeasurementType"       # from K02
    measurement_window_ms: int | None
    expected_interval_s: float | None
    resolution_w: float | None
    declared_by: Literal["user","discovery","migration"]

class PhysicalDevice(Model):
    device_id: str                   # legacy ids preserved verbatim (DEC-04); new: "pd_<ulid>"
    name: str
    device_type: str | None
    load_shape: "LoadShape" | None   # existing enum
    sign_class: Literal["consumer","producer","bidirectional"] = "consumer"
    ha_device_id: str | None
    area_id: str | None              # display grouping only (DEC-27)
    origin: Literal["user","ha_device","unknown_profile_promotion","migration"]
    created_at: datetime
    retired_at: datetime | None

class LoadComponent(Model):
    component_id: str                # "lc_<ulid>"
    device_id: str
    name: str
    load_shape: "LoadShape" | None
    sign_class: Literal["consumer","producer","bidirectional"] = "consumer"

class HaEntity(Model):
    entity_id: str
    domain: str
    ha_device_id: str | None
    first_seen: datetime
    last_seen: datetime

class HaEntityLink(Model):           # many-to-many, DEC-08
    entity_id: str
    target_kind: Literal["device","component"]
    target_id: str
    role: "HaEntityRole"             # existing enum (state / mode / control_level / power_measurement ...)
    valid_from: datetime
    valid_until: datetime | None
    declared_by: Literal["user","ha_device_link","discovery","migration"]
```

---

## 3. Measurements and events (K02, `contracts/measurement.py`) — HB §2, §6

```python
class MeasurementType(str, Enum):
    INSTANTANEOUS, WINDOW_AVERAGE, DEVICE_REPORTED, CALCULATED, ACCUMULATED_ENERGY
class QualityFlag(str, Enum):
    UNIT_INVALID, IMPLAUSIBLE_RANGE, REPEATED, STALE_ON_ARRIVAL, CLOCK_SKEW_FUTURE, PRIMED, OUT_OF_ORDER

class ReaderMeasurement(Model):      # HB §2.1; produced only by P1
    reader_id: str                   # endpoint_id
    observed_at: datetime            # HA last_updated (physical time unknown)
    received_at: datetime
    active_power_w: float | None     # polarity+multiplier already applied
    current_rms_a: float | None
    voltage_rms_v: float | None
    reactive_power_var: float | None
    power_factor: float | None
    energy_wh: float | None
    phase: Literal["l1","l2","both","unknown"]
    leg: Literal["l1","l2"] | None
    voltage_reference_v: float | None
    measurement_type: MeasurementType
    measurement_window_ms: int | None
    calibration_version: str
    measurement_basis: "MeasurementBasis"     # existing enum, now persisted
    quality_flags: list[QualityFlag]
    sequence: int

class HAEvent(Model):                # no device binding (binding is a matching output)
    event_id: str                    # content hash
    entity_id: str
    observed_at: datetime
    received_at: datetime
    from_state: str | None
    to_state: str | None
    attributes_delta: dict[str, Any]
    context_id: str | None
    context_parent_id: str | None
    context_user_id: str | None
    linked: bool                     # entity has any HaEntityLink at observed_at

class StepClass(str, Enum): SUSTAINED, TRANSIENT, PERIODIC, DRIFT, DATA_GAP
class MeasurementEvent(Model):       # one detected reader change (P3)
    event_id: str                    # stable: hash(reader_id, onset_lo, detector_version)
    reader_id: str
    delta_w: float                   # signed, HB §2.3
    sigma_w: float
    z_score: float
    step_class: StepClass
    onset_lo: datetime
    onset_hi: datetime               # timestamp uncertainty = onset_hi - onset_lo
    pre_level_w: float | None
    post_level_w: float | None
    quality_score: float             # 0..1, documented formula in P3
    detected_at: datetime

class EventEvidence(Model):          # immutable (P4), HB §6.5
    evidence_id: str
    reader_id: str
    measurement_event_id: str
    window_start: datetime
    window_end: datetime
    samples: list[tuple[datetime, float | None]]   # retained per DEC-30
    baseline_w: float | None
    sigma_w: float | None
    delta_w: float
    quality_flags: list[QualityFlag]
    topology_version_id: str
    finalized: bool                  # False = PROVISIONAL window still open

class SubEventKind(str, Enum): HA_STATE, HA_ATTRIBUTE, READER_STEP, PLUG_STEP
class SubEvent(Model):
    sub_event_id: str
    kind: SubEventKind
    ref: EvidenceRef                 # ha_event or event_evidence
    observed_at: datetime
    timestamp_uncertainty_s: float

class EventGroupStatus(str, Enum):  DETECTED, MATCHING, PROVISIONAL, CONFIRMED, REVISED, UNRESOLVED  # HB §12.3
class ReaderDelta(Model):
    reader_id: str
    delta_w: float | None            # None = reader had no usable data in the window
    sigma_w: float | None
    completeness: Literal["complete","pre_missing","post_pending","missing"]
    evidence_ref: EvidenceRef | None

class EventGroup(Model):
    group_id: str
    revision: int                    # REVISED creates revision+1; prior revisions immutable
    status: EventGroupStatus
    opened_at: datetime
    closed_at: datetime | None
    sub_events: list[SubEvent]
    reader_deltas: list[ReaderDelta]
    grouping_reasons: list[str]      # "context_id", "context_parent", "temporal_overlap", "reader_comovement"
    versions: "VersionTriple"
```

---

## 4. Topology, mapping edges, residual (K03 contracts + K08 runtime) — HB §3, §4, §14

```python
class RelationType(str, Enum):   CONTAINS, SHARED_LOAD, PAIRED_LEG, CORRELATED, SAME_CONDUCTOR
class EdgeLifecycle(str, Enum):  CANDIDATE, PROVISIONAL, CONFIRMED, CONFLICTED, SUSPECTED_CHANGE, REJECTED, RETIRED

class TopologyEdge(Model):           # reader <-> reader only
    edge_id: str
    relation: RelationType
    source_endpoint_id: str          # CONTAINS: parent
    target_endpoint_id: str          # CONTAINS: child
    lifecycle: EdgeLifecycle
    declared_by: Literal["user","inference","migration"]
    beta: float | None               # §3.3, only when physics-justified (T2)
    beta_basis: Literal["unity","recorded_factor","estimated"] | None
    evidence: "EdgeEvidence"         # counters: supporting, conflicting, parent_only(neutral), independent_causes
    valid_from: datetime
    valid_until: datetime | None
    pinned: bool                     # user pin/reject wins over inference

class TopologyVersion(Model):
    version_id: TopologyVersionId
    number: int
    parent_version_id: TopologyVersionId | None
    edge_ids: list[str]
    status: Literal["candidate","active","superseded","rolled_back"]
    change_event_id: str | None
    created_at: datetime
    digest: str                      # existing digest_edge_set

class Observability(str, Enum):  DIRECT, EXPECTED, INFERRED, PARTIAL, UNKNOWN, NOT_OBSERVABLE, CONFLICTED  # HB §9.7
class MappingEdge(Model):            # subject <-> reader, independent of topology (rule 20)
    mapping_edge_id: str
    subject_kind: Literal["device","component","unknown_profile"]
    subject_id: str
    endpoint_id: str                 # the *most specific* reader; ancestors derived via topology
    observability: Observability
    lifecycle: EdgeLifecycle
    declared_by: Literal["user","auto_placement","inference","migration"]
    evidence: "EdgeEvidence"
    valid_from: datetime
    valid_until: datetime | None

class MappingVersion(Model):  version_id, number, parent_version_id, mapping_edge_ids, status, change_event_id, created_at, digest

class ConfigurationEvent(Model):     # reuse ConfigurationChangeKind/ChangeDetection/ChangeStatus enums
    event_id: str
    kind: "ConfigurationChangeKind"
    detection: "ChangeDetection"     # DECLARED | INFERRED
    status: "ChangeStatus"
    affected_endpoint_ids: list[str]
    affected_edge_ids: list[str]
    affected_mapping_edge_ids: list[str]
    candidate_topology_version_id: str | None
    candidate_mapping_version_id: str | None
    evidence_refs: list[EvidenceRef]
    created_at: datetime
    resolved_at: datetime | None

class ReaderResidualPoint(Model):    # HB §9.4 E (per reader), DEF-05
    reader_id: str
    at: datetime
    residual_w: float | None         # signed, never clamped
    baseline_w: float | None
    dynamic_w: float | None
    noise_sigma_w: float | None
    topology_version_id: str
    basis: "MeasurementBasis"

class UnknownLoadProfile(Model):     # HB §9.4 D; lifecycle never CONFIRMED
    profile_id: str
    scope_endpoint_id: str           # deepest reader that sees it
    lifecycle: Literal["observing","characterised","candidate","promoted","retired"]
    signature: dict[str, float]      # delta_w_median, delta_w_mad, duration_s_median, period_s|None
    event_count: int
    independent_event_count: int
    first_seen: datetime
    last_seen: datetime
```

**Runtime (K08, `runtime/interfaces.py`, frozen dataclasses, numpy allowed):**

```python
@dataclass(frozen=True)
class EntryMeta:
    coefficient: float
    observability: Observability
    edge_lifecycle: EdgeLifecycle | None   # weakest edge on the path; None for DIRECT
    edge_ids: tuple[str, ...]

@dataclass(frozen=True)
class RowMeta:
    reader_id: str
    kind: Literal["reader","residualised_parent"]
    sigma_w: float | None
    sigma_inflation: float                 # >=1, covariance-conservative weighting (I3)
    excluded_reason: str | None

@dataclass(frozen=True)
class ObservationOperator:                 # T2 produces; I1/I3/I4/M1 consume
    H: np.ndarray                          # R x N (subjects); unknown columns are separate
    rows: tuple[RowMeta, ...]
    subjects: tuple[tuple[str, str], ...]  # (subject_kind, subject_id)
    entries: Mapping[tuple[int, int], EntryMeta]
    unknown_scopes: tuple[tuple[str, tuple[int, ...]], ...]  # (scope_reader_id, row indices it enters)
    soft_alternatives: tuple["AlternativeEdge", ...]          # PROVISIONAL edges not applied
    topology_version_id: str
    mapping_version_id: str
    at: datetime

def build_observation_operator(topology: "TopologyView", mapping: "MappingView",
                               factors: "FactorRegistry", reader_ids: Sequence[str],
                               subjects: Sequence[tuple[str, str]], at: datetime) -> ObservationOperator

@dataclass(frozen=True)
class UnknownPrior:                        # T3 produces; I3 consumes
    scope_reader_id: str
    mu_w: float
    sigma_w: float
    lambda_u: float
    step_rate_per_h: float | None

class ResidualModelView(Protocol):
    def unknown_prior(self, scope_reader_id: str, at: datetime) -> UnknownPrior | None: ...
```

---

## 5. Power models (K06 contracts + L1 registry) — HB §11, §13

```python
class ModelBasis(str, Enum):   DELTA, ABSOLUTE_ANCHOR, LEGACY_ABSOLUTE
class ModelClass(str, Enum):   FIXED, ADJUSTABLE, DYNAMIC, RESPONSE_KERNEL, COMPONENT_SUM
class PowerLevel: existing enum (STANDBY, IDLE, ACTIVE, TRANSIENT, UNKNOWN …)

class LevelStat(Model):  median_w: float | None; mad_w: float | None; n: int
class FixedSpec(Model):        kind: Literal["fixed"]; levels: dict[PowerLevel, LevelStat]; transition_delta: dict[str, LevelStat]
class AdjustableSpec(Model):   kind: Literal["adjustable"]; control_variable: str; knots_x: list[float]; knots_w: list[float]; monotone: bool; x_coverage: tuple[float, float]; standby: LevelStat
class DynamicSpec(Model):      kind: Literal["dynamic"]; states: dict[str, LevelStat]; ha_state_to_states: dict[str, list[str]]; transition_counts: dict[str, dict[str, int]]; duration_s: dict[str, LevelStat]
class KernelPhase(Model):      phase: Literal["startup","active","steady","shutdown","standby"]; delay_s: LevelStat; amplitude_w: LevelStat; duration_s: LevelStat
class ResponseKernelSpec(Model): kind: Literal["response_kernel"]; by_transition: dict[str, list[KernelPhase]]
class ComponentSumSpec(Model): kind: Literal["component_sum"]; component_ids: list[str]
PowerModelSpec = Annotated[Union[FixedSpec, AdjustableSpec, DynamicSpec, ResponseKernelSpec, ComponentSumSpec], Field(discriminator="kind")]

class PowerModelVersion(Model):
    version_id: ModelVersionId
    subject_kind: Literal["device","component"]
    subject_id: str
    number: int
    model_class: ModelClass
    basis: ModelBasis
    spec: PowerModelSpec
    schema_version: int
    evidence_refs: list[EvidenceRef]       # frozen evidence set used to fit/validate
    topology_version_at_training: str | None
    trainer_version: str
    parent_version_id: str | None
    created_at: datetime

class PowerModelEvent(Model):  version_id; event: Literal["candidate","activated","superseded","rejected","rolled_back","frozen","unfrozen"]; reason: str; at: datetime

class PredictionStatus(str, Enum): KNOWN, UNKNOWN
class PowerPrediction(Model):  level: PowerLevel; mean_w: float | None; sigma_w: float | None; status: PredictionStatus; model_version_id: str | None
class DeltaPrior(Model):       mean_w: float; sigma_w: float; sign: Literal["pos","neg","any"]; model_version_id: str

class LearningObservation(Model):     # L4; deterministic id
    observation_id: str               # hash(group_id, group_revision, subject_id, endpoint_id, topology_version_id)
    subject_kind: Literal["device","component"]
    subject_id: str
    endpoint_id: str
    event_group_ref: EvidenceRef
    transition: str
    attributes: dict[str, Any]
    delta_w: float | None
    level_w: float | None             # only for ABSOLUTE_ANCHOR (single-subject endpoint)
    status: Literal["provisional","confirmed","rejected"]
    rejection_reason: str | None
    identifiability: IdentifiabilityStatus
    created_at: datetime
```

**Registry (L1, `models/registry.py`):**

```python
class ModelSetView(Protocol):
    model_set_id: ModelSetId
    def predict(self, subject_id: str, ctx: "PredictionContext") -> PowerPrediction: ...
    def transition_prior(self, subject_id: str, transition: str, attrs: Mapping[str, Any]) -> DeltaPrior | None: ...
    def expected_window(self, subject_id: str, transition: str, attrs: Mapping[str, Any],
                        tau_s: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None: ...   # (mean_w, sigma_w)
```

---

## 6. Inference (K04 contracts) — HB §7, §8, §12

```python
class MatchState(str, Enum): UNMATCHED, PROPOSED, PROVISIONAL, CONFIRMED, AMBIGUOUS, CONFLICTED, REJECTED, EXPIRED  # HB §7.6
class CandidateSource(str, Enum): HA_STATE, HA_ATTRIBUTE, LEARNED_PROFILE, PRIOR_MAPPING, PLUG, UNKNOWN_PROFILE, TOPOLOGY_VISIBLE, DELAYED_RESPONSE, DEVICE_GROUP, UNKNOWN_SCOPE

class CandidateHypothesis(Model):
    candidate_id: str
    subject_kind: Literal["device","component","group","unknown_profile","unknown_scope"]
    subject_id: str
    sources: list[CandidateSource]
    transition: str | None
    evidence_terms: dict[str, float]   # A..H log-likelihood terms (I2), documented keys
    filtered: bool
    filter_reason: str | None

class DeviceEstimate(Model):
    subject_kind: Literal["device","component"]
    subject_id: str
    point_w: float | None
    bounds: Bounds
    status: EstimateStatus
    identifiability: IdentifiabilityStatus
    group_id: str | None
    level: PowerLevel | None
    method: InferenceMethod
    allocation_rule: str | None        # required iff status == ALLOCATED
    confidence: "ConfidenceVector"     # 7 components, HB §9.6
    source_reader_ids: list[str]
    evidence_refs: list[EvidenceRef]
    versions: VersionTriple
    model_version_id: str | None
    updated_at: datetime
    provisional: bool

class DeviceGroupEstimate(Model):
    group_id: str                      # stable across cycles (I4)
    member_ids: list[str]
    point_w: float | None              # identified sum
    bounds: Bounds                     # bounds of the sum
    identifiability: IdentifiabilityStatus   # GROUP_IDENTIFIABLE when point_w set
    source_reader_ids: list[str]
    evidence_refs: list[EvidenceRef]
    versions: VersionTriple
    updated_at: datetime

class UnknownEstimate(Model):  scope_reader_id: str; delta_w: float | None; point_w: float | None; sigma_w: float | None; profile_id: str | None

class JointInferenceResult(Model):
    result_id: str
    event_group_id: str | None         # None for steady-state pass
    event_group_revision: int | None
    status: EventGroupStatus
    device_estimates: list[DeviceEstimate]
    group_estimates: list[DeviceGroupEstimate]
    unknown_estimates: list[UnknownEstimate]
    candidates: list[CandidateHypothesis]
    matches: dict[str, MatchState]     # candidate_id -> state
    residual_w_by_reader: dict[str, float | None]
    versions: VersionTriple
    computed_at: datetime

class EventInferenceRecord(Model):     # HB §12.2; interpretation versioned, evidence immutable
    record_id: str
    event_group_id: str
    interpretation_revision: int
    previous_record_id: str | None
    result: JointInferenceResult
    reason: Literal["initial","late_data","window_closed","model_update","topology_update","replay"]
    created_at: datetime

class InferenceSnapshot(Model):        # HB §12.1
    snapshot_id: SnapshotId
    observed_at: datetime
    computed_at: datetime
    versions: VersionTriple
    reader_measurements: list[ReaderMeasurement]
    device_estimates: list[DeviceEstimate]
    group_estimates: list[DeviceGroupEstimate]
    residual_estimates: list[ReaderResidualPoint]
    reconciliation: "ReconciliationV3"   # main = Σ identified + Σ groups + unknown + residual; all float|None
    system_quality: "DataQualityReport"
    stale_sources: list[str]
    unresolved_events: list[str]         # event group ids
```

**Runtime (K08):**

```python
@dataclass(frozen=True)
class JointSolution:                     # I3 -> I4/I5
    x: np.ndarray                        # subject deltas or levels, len N
    cov: np.ndarray | None               # N x N posterior covariance (None if singular)
    u: np.ndarray                        # unknown columns
    active_rows: tuple[int, ...]
    row_residual_w: np.ndarray
    objective: float
    converged: bool
    mode: Literal["delta","absolute"]

def infer_group(group: EventGroup, operator: ObservationOperator, models: ModelSetView,
                residual: ResidualModelView, history: "IdentifiabilityHistory",
                matches: "MatchStore", versions: VersionTriple, clock: Callable[[], datetime]) -> JointInferenceResult
def infer_steady(readings: Mapping[str, "HeldReading"], operator: ObservationOperator, models: ModelSetView,
                 residual: ResidualModelView, established: "EstablishedStructure",
                 versions: VersionTriple, clock: Callable[[], datetime]) -> JointInferenceResult
```

Both are **pure** given their inputs (`MatchStore` and `IdentifiabilityHistory` are passed-in snapshots). That makes replay possible (L7, AT-19).

---

## 7. Version service (D1, `versioning/service.py`) — rule 20, HB §14

```python
class VersionService(Protocol):
    def current(self) -> VersionTriple: ...
    def as_of(self, at: datetime) -> VersionTriple: ...
    def topology_view(self, version_id: str) -> "TopologyView": ...
    def mapping_view(self, version_id: str) -> "MappingView": ...
    def record_configuration_event(self, ev: ConfigurationEvent) -> str: ...
    def mark_suspected(self, change_event_id: str, edge_ids: Sequence[str], mapping_edge_ids: Sequence[str]) -> None: ...
    def propose_topology(self, edges: Sequence[TopologyEdge], change_event_id: str | None) -> TopologyVersionId: ...
    def promote_topology(self, version_id: str, reason: str) -> None: ...
    def propose_mapping(self, edges: Sequence[MappingEdge], change_event_id: str | None) -> MappingVersionId: ...
    def promote_mapping(self, version_id: str, reason: str) -> None: ...
    def rollback(self, kind: Literal["topology","mapping"], to_version_id: str) -> str: ...   # appends, never rewrites
```

The model-set id is derived by L1's registry, so the triple is assembled here without coupling the lifecycles.

---

## 8. Matrix (K05 contracts, M1/M2 runtime) — HB §9, §10

```python
class MappingStatus(str, Enum): UNKNOWN, LEARNING, PROVISIONAL, CONFIRMED, SUSPECTED_CHANGE, CONFLICTED, RETIRED
class RowKind(str, Enum):  DEVICE, COMPONENT, GROUP, UNKNOWN_PROFILE, READER_RESIDUAL, SUMMARY
class CellVisual(str, Enum): DIRECTLY_MEASURED, CONFIRMED_INFERRED, PROVISIONAL, GROUP_ONLY, UNKNOWN, NOT_OBSERVABLE, CONFLICTED, RETIRED  # HB §19.3

class MappingCell(Model):
    row_kind: RowKind; row_id: str; load_component_id: str | None; reader_id: str
    mapping:   CellMapping      # observability, relation_type: RelationType|Literal["DIRECT_ENDPOINT"]|None, mapping_status, mapping_confidence: float|None, valid_from, valid_until
    power:     CellPower        # measurement_status: EstimateStatus, estimated_power_w|None, bounds: Bounds, standby_power_w|None, operating_state: PowerLevel|None, expected_profile_ref: str|None, uncertainty_w|None
    identifiability: IdentifiabilityStatus; group_id: str | None
    event_association: CellEvents   # matched, confirmed, provisional (independent event counts), avg_response_delay_ms|None, response_signature: dict|None, last_matched_at|None
    confidence: ConfidenceVector    # measurement_quality, event_association, model_fit, topology_consistency, identifiability, sample_coverage, freshness
    provenance: CellProvenance      # sources_used, evidence_refs, topology_version_id, model_version_id|None, mapping_version_id
    timestamps: CellTimestamps      # first_observed_at, last_observed_at, last_validated_at, updated_at
    visual: CellVisual              # derived, deterministic (M1)
    derived: bool                   # True for SUMMARY rows

class ColumnNode(Model):  node_id; level: Literal["L0","L1","L2","L3","L4"]; label; kind: Literal["domain","panel","subpanel","circuit","reader","unassigned"]; reader_id: str | None; children: list["ColumnNode"]; annotations: list["ColumnAnnotation"]   # secondary edges, never extra leaves
class RowNode(Model):     node_id; kind: RowKind | Literal["room","folder"]; label; row_id: str | None; children: list["RowNode"]
class MatrixPayload(Model):
    mapping_version_id: str; versions: VersionTriple; generated_at: datetime
    columns: list[ColumnNode]; rows: list[RowNode]; cells: list[MappingCell]   # sparse
    reader_totals: dict[str, "ReaderColumnSummary"]   # measured_w, known_contrib_w, residual_w, unknown_w (never summed across readers)
    physical_total: "PhysicalTotal"                    # each load once
```

```python
class MatrixUpdater(Protocol):                         # M2
    def apply(self, result: JointInferenceResult, operator: ObservationOperator) -> "MatrixDelta": ...
    def reconcile(self, reason: str) -> "MatrixDelta": ...   # deterministic, audited
```

---

## 9. HTTP API v3 (all under `/api/v1`, `X-API-Token` when configured)

| Method + path | Owner | Returns |
|---|---|---|
| `GET /snapshot` | D4 | `InferenceSnapshot` (latest persisted) |
| `GET /devices`, `GET /devices/{id}` | D4 | `DeviceEstimate` + `PhysicalDevice` + components |
| `GET /devices/{id}/records` | I6 | `EventInferenceRecord[]` (paged) |
| `GET /groups`, `GET /groups/{id}` | D4 | `DeviceGroupEstimate` |
| `GET /events?status=&since=` / `GET /events/{group_id}` | I6 | `EventGroup` + record revisions |
| `GET /endpoints`, `PUT /endpoints/{id}`, `POST /endpoints/{id}/scope` | D2 | endpoints; scope confirm/reject (DEC-05/06) |
| `GET /topology` | T4 | active `TopologyVersion` + `TopologyEdge[]` + derived primary tree |
| `POST /topology/edges` | T4 | declare a user edge → `ConfigurationEvent` + candidate version |
| `POST /topology/edges/{id}/pin`, `/reject` | T4 | per-edge user authority (DEC-10) |
| `GET /topology/versions`, `GET /topology/versions/{id}` | T4 | history |
| `GET /configuration-events` | T7 | `ConfigurationEvent[]` |
| `GET /matrix?at=&filter...` | M3 | `MatrixPayload` (ETag = mapping_version_id) |
| `GET /matrix/cells/{row_id}/{reader_id}` | M3 | cell + history + event groups + contradictions |
| `GET /matrix/readers/{id}` / `GET /matrix/rows/{id}` | M3 | column/row drill-down (HB §19.4) |
| `GET /residuals?reader_id=` | T3 | `ReaderResidualPoint[]` |
| `GET /unknown-profiles` | T6 | `UnknownLoadProfile[]` |
| `GET /models/{subject_id}` | L1 | `PowerModelVersion[]` + events |
| `GET /provenance/{evidence_kind}/{id}` | D4 | resolved evidence (HB §18 questions) |

**Removed at Z1:** `/mapping`, `PUT /topology`, `POST /topology/confirm`, `/topology/nesting` (replaced by PROVISIONAL edges in `GET /topology`), `/confidence_tiers`, `/relations`.

**HA integration payload (H1)** reads `/snapshot`. Per device: `point_w → sensor state (None if not MEASURED, or ESTIMATED+IDENTIFIABLE)`; attributes `status, identifiability, lower_w, upper_w, group_id, topology_version_id, mapping_version_id, model_version_id, provisional`. One group power sensor per `DeviceGroupEstimate.group_id` (DEC-25).

---

## 10. Storage tables (S13–S19)

| Table | Mig | Tier | Key | Notes |
|---|---|---|---|---|
| `measurement_endpoints` | 13 | L4 | endpoint_id | backfilled from `ct_channels` (ids preserved) |
| `endpoint_config_revisions` | 13 | L4 | (endpoint_id, revision) | append-only; rev 1 from `ct_channels` + `ct_measurement_factors` |
| `physical_devices` | 13 | L4 | device_id | from `devices` + `device_configurations` (ids verbatim) |
| `load_components` | 13 | L4 | component_id | empty at migration |
| `ha_entities`, `ha_entity_links` | 13 | L4 | entity_id / (entity_id,target_kind,target_id,valid_from) | from `ha_entity_bindings` + metadata keys + `DeviceConfiguration.*_entity_ids` |
| `topology_edges`, `topology_versions_v3` | 14 | L4 | edge_id / version_id | pointer fields → edges `declared_by=migration`, lifecycle CONFIRMED if user-declared; `topology_relations` with lifecycle |
| `configuration_events` | 14 | L4 | event_id | |
| `mapping_edges`, `mapping_versions` | 15 | L4 | mapping_edge_id / version_id | from `branch_ct_ids`, `monitored_by_ct_ids`, `DEVICE_CT_ASSOCIATION` |
| `matrix_cells`, `matrix_cell_versions` | 15 | L4 | (row_kind,row_id,reader_id) / +version | sparse; history append-only |
| `ct_samples` (+cols) | 16 | L1 | existing | adds nullable §2.1 columns + `measurement_basis`, `calibration_version` |
| `event_evidence` | 16 | L2 | evidence_id | DEC-30 retention |
| `event_groups`, `event_group_members` | 16 | L2 | (group_id, revision) | immutable per revision |
| `event_inference_records` | 16 | L2 | record_id | revision chain |
| `inference_results` (rebuild) | 17 | L1 | existing | nullable `power_w`, + status, identifiability, bounds, group_id, versions, `legacy_semantics` |
| `inference_snapshots` | 17 | L2 | snapshot_id | written only on change |
| `device_group_estimates` | 17 | L1 | (group_id, at) | |
| `power_model_versions_v3`, `power_model_events` | 18 | L4 | version_id / (version_id, at) | old `device_model_versions` → `LEGACY_ABSOLUTE` read-only (DEC-31) |
| `learning_observations` | 18 | L4 | observation_id | deterministic ids |
| `reader_residual_series` | 19 | L3 | (reader_id, at) | old `residual_power` kept, flagged `scope_unknown` |
| `unknown_load_profiles` | 19 | L4 | profile_id | |
