# Domain Requirements Inventory

Owner: Domain & Test Inventory (Subagent B).
Scope: `engine/wattmosaic_engine/`, `custom_components/wattmosaic/`, `tools/simulator/`, `tests/`.
Companion document: [`test_scenario_matrix.md`](test_scenario_matrix.md)
(per-scenario COVERED / PARTIAL / MISSING status).
Contract under inspection: `CONTRACT_VERSION = "1.5.0"`, schema `LATEST_VERSION = 8`.

Method: every claim below cites the exact contract name, module path, or test that
carries it. A concept is **PRESENT** only when a first-class named field/enum
exists; it is **PARTIAL** when the information is reachable but conflated,
encoded in a free-form `dict`, or derivable only at read time; it is **ABSENT**
when nothing in the contract can express it.

---

## 1. Concept inventory

| # | Required concept | Exact representation today | Status | Conflation / defect |
| --- | --- | --- | --- | --- |
| 1a | **HA Entity** (software object) | `Measurement.entity_id`, `DeviceState.entity_id`, `DeviceEvent.entity_id`; `DeviceConfiguration.state_entity_ids` / `mode_entity_ids` / `controllable_entity_ids`; `DeviceTopology.metadata["link_state_entity_ids"]` (`device_links.py:73`) | PARTIAL | Entities are only ever *lists on a device*. There is no entity object with a role, so "many entities, one appliance" is a convention, not a contract. No `HaEntityRole`; cardinality is unconstrained and unvalidated. |
| 1b | **Physical Device** (appliance) | `DeviceTopology.device_id`, `DeviceConfiguration.device_id`, `InferenceResult.device_id` | PRESENT | Device identity exists and is stable. But nothing states *which* entity is the load evidence, so 1a↔1b cannot be verified mechanically. |
| 1c | Entity↔device cardinality, no double count | none | ABSENT | Nothing forbids two power-carrying entities mapping to one device, or one entity mapping to two devices. `tests/unit/test_device_links.py:144` only proves a *power* entity is not a *state* entity. |
| 2 | **CT Observation** (independent node) | `Measurement` (entity_id, ct_id, value, value_w, quality, polarization, sequence, 3 timestamps) + `CTChannel` (ct_id, entity_id, ct_type, phase, parent_ct_id, polarity, device_id, paired_ct_id, alias_of_ct_id, measurement_multiplier, panel_id, topology_status) | PARTIAL | A reading is independent (good), but `CTChannel` pre-assigns `device_id`, `parent_ct_id`, `ct_type`, `panel_id`. So the observation node is not kept neutral: position/hierarchy is a *field on the observation* rather than a separate inference. `MeasurementNode` (`electrical/nesting.py:146`) is the neutral node, but it is a local dataclass, never persisted or exposed. |
| 3 | **Circuit** (dedicated / shared / partial / unknown) | `CTType` (`MAIN`, `BRANCH`, `DEDICATED_DEVICE`, `UNKNOWN`) + `MonitoringLevel` (`DEDICATED`, `SHARED`, `HOME_TOTAL_ONLY`, `UNMONITORED`, `UNKNOWN`) + `DeviceTopology.branch_ct_ids` | PARTIAL | `CTType` classifies **the sensor**, `MonitoringLevel` classifies **a device's visibility**. Neither classifies **the circuit's electrical scope**, and there is no `PARTIALLY_UNDERSTOOD`/`PARTIAL` state at all. "Shared by two devices we know" and "shared by two we know plus one we do not" are the same value (`SHARED`). `test_device_taxonomy.py:241` documents that a sole-occupant branch is `SHARED`, not dedicated. |
| 4 | **Electrical Topology** (parent/child graph) | `CTChannel.parent_ct_id`, `TopologyGraph` (`electrical/topology.py`), `ancestors`/`descendants`, `effective_feeds`, `Topology.version`, `TopologyStatus`; read-time view `panel_tree.py` (`TREE_VERSION = 2`) | PRESENT | Graph is real, cycle-checked (`test_electrical.py:130`, `:256`), and never invented by discovery. The *view* is read-time only; no persisted edge table. |
| 5a | Relation: **device→CT association** | `CTChannel.device_id`, `DeviceTopology.dedicated_ct_ids`, `DeviceConfiguration.monitored_by_ct_ids` | PARTIAL | Three separate storages for one relation, with no precedence rule in the schema. Declared association and inferred association are not distinguished (only `configured_by` on the *device*, not on the edge). |
| 5b | Relation: **CT measurement overlap** | `CTChannel.alias_of_ct_id` + `NestingCandidate(kind="duplicate")` | PARTIAL | Only the *duplicate* case is representable. Generic overlap (A is a subset of B without being an alias) has no field; it is recomputed per run and thrown away. |
| 5c | Relation: **circuit membership** | `DeviceTopology.branch_ct_ids` | PARTIAL | Membership is a list of CT ids, so a device cannot be a member of a *circuit* that has no CT yet, and a circuit cannot exist without a device or a CT naming it. |
| 5d | Relation: **electrical parent/child** | `CTChannel.parent_ct_id` | PRESENT | Correctly a wiring fact, confirmed-only (`nesting.py` docstring), never derived from names. |
| 5e | The four relations must not auto-imply one another | enforced procedurally only | PARTIAL | `electrical/nesting.py` explicitly refuses to turn duplicate into nesting and nesting into duplicate (`test_two_monitors_on_one_wire_are_a_duplicate_not_a_nesting`, `test_a_nested_feed_is_never_reported_as_a_duplicate`). But because 5a–5d live in four unrelated places with no typed edge, a future writer can set `parent_ct_id` and thereby imply 5c. Nothing in the schema prevents it. |
| 6a | **Residual power** (first class, signed) | `Reconciliation.unknown_residual_w` (+ `unmonitored_w`, `meter_disagreement_w`, `meter_spread_w`, `meter_spread_exceeds_residual`) | PRESENT | Signed, never clamped, invariant `unmonitored + meter_disagreement == unknown_residual` held exactly (`tests/unit/test_reconciliation_split.py:165-497`). |
| 6b | Residual may stay unresolved forever | implied only | PARTIAL | Nothing records "this residual has been unexplained for 30 days". `unmonitored_w` is reused for "we do not know" and "we know it is not metered"; the code says so in the field docstring. No age, no `resolved` flag. |
| 7 | **Inference record** (versioned) | `InferenceResult` (timestamp, power_w, source, confidence, model_id, model_version, operating_stage, supporting_ct_channels, explanation, provenance) | PARTIAL | Per-row history exists (`inference_results` table, `inference_history()`). **But** there is no algorithm version, no previous/new result pair, no confidence delta, no structured evidence references, and no reason code. `provenance` is a free `dict[str, Any]`. |
| 7b | Versioned *topology/nesting* inference | `NestingCandidate` (`child_ct_id`, `parent_ct_id`, `kind`, `evidence: dict`, `requires_confirmation`) | PARTIAL | Closest existing analogue: has evidence + candidate status. Unversioned (no algorithm version), not persisted, and no previous result. |
| 8 | **Confidence dimensions** (independent) | `InferenceResult.confidence: float`, `ConfidenceTier` (0–4), `tier_reasons` | ABSENT | One scalar. `disaggregation/confidence.py` **blends** six components with fixed weights (`WEIGHTS`) then multiplies penalties, i.e. exactly the "single misleading blended score" the requirements forbid. The six components are internal and discarded except inside `provenance["confidence"]`. |
| 9 | **Measurement basis** | `Source` (`DIRECT_MEASURED`, `INFERRED`, `MODEL_ESTIMATED`, `CONFIGURED_PRIOR`, `UNKNOWN`) | ABSENT | `Source` answers *"how was the value obtained"*, not *"what kind of instrument produced the watts"*. It cannot express: physical CT vs Refoss-reported total vs factor-adjusted (`measurement_multiplier != 1.0`) vs estimated appliance total. `CTChannel.measurement_multiplier` exists but is invisible in the result and never recorded per reading. |
| 10 | Refoss ×2 factor | `CTChannel.measurement_multiplier: float = 1.0`, applied via `TopologyGraph.scale_w` / `multiplier_for`; validated >0 finite; wizard bounds 0.01–100 (`flow_logic`) | PARTIAL | It is a per-circuit user declaration with no provenance, no validity interval, and no record of whether the *monitor* or the *user* applied it. There is no `factor`/`x2` domain concept anywhere (`grep factor tools/simulator` finds only `default_factory`). |

Summary: **PRESENT 4**, **PARTIAL 11**, **ABSENT 3** of the 18 sub-concepts above.

---

## 2. Persistence

| Concept | Table / column | Notes |
| --- | --- | --- |
| CT observation | `ct_samples` (id, ct_id, entity_id, value, unit, value_w, quality, polarization, sequence, state_timestamp, received_at, physical_timestamp) | All three timestamps persisted (`test_storage_e2e.py:198`). |
| CT channel / topology | `ct_channels`, `topology_versions`, `devices` | `parent_ct_id`, `alias_of_ct_id`, `measurement_multiplier`, `panel_id` all persisted (`test_contracts.py:183`). |
| Circuit (as scope) | — | No table. Circuits exist only as `ct_channels` rows. |
| Relations (typed edges) | — | No table. Association lives in `devices` + `ct_channels`; overlap in `alias_of_ct_id`; membership in `devices`; hierarchy in `ct_channels.parent_ct_id`. |
| Inference result | `inference_results` (id, device_id, timestamp, power_w, energy_kwh, source, confidence, model_id, model_version, operating_stage, stage_status, sample_count, quality_status, measurement_age_seconds, supporting_ct_channels_json, explanation_json, provenance_json) | **No** algorithm_version, previous_result, confidence_delta, measurement_basis, reason_code. |
| Inference history | same table, query by device+time (`database.py:567`) | Retention-pruned (`test_contracts.py:433`); older rows deleted, so a versioned audit cannot survive. |
| Reconciliation / residual | `reconciliation_results` | Split columns added in migration 7 (`test_reconciliation_persistence.py:307`). Sign preserved. |
| Model versions | `device_model_versions`, `device_models`, `operating_stages`, `device_flags` | `ModelStatus` lifecycle + `ROLLED_BACK` (`test_learning.py:700`). |
| HA entity | `device_events`, `device_state_samples`, `device_configurations` | Entity id is a string column; no role, no cardinality constraint. |
| Algorithm version | — | Only `HealthResponse.engine_version` = package version (`contracts.py:908`). Not attached to any result. |

---

## 3. Simulator capability

| Capability | Evidence | Gap for requirements |
| --- | --- | --- |
| 2 panels, main + nested sub panel | `tools/simulator/topology.py`, `test_simulator.py:28` | — |
| L1/L2 phases, 240 V double-pole pairs | `SimDeviceType.DOUBLE_POLE`, `paired_ct_id` | — |
| 36 CT channels / 18 per panel (39 simulated devices) | measured: `build_real_home_topology()` → 36 channels, 18 main + 18 sub, 39 devices; `docs/testing.md §2.1` | — |
| Dedicated + shared branch devices | `ct_main_*`, `ct_sub_*`, `*_ct1/_ct2` | — |
| Device classes | EV charger, water heater, A/C, central HVAC, space/patio heater, underfloor loop, refrigerator, freezer, dryer, washer, dishwasher, humidifier, ventilation unit, standby loads, 2 unknown loads (`topology.py:411-536`) | All classes exist but only `DEDICATED_DEVICES`/`SHARED_DEVICES` strata are asserted; no per-class scenario test. No pump/motor profile. |
| No multiplier / ×2 factor | `grep -n factor tools/simulator/*.py` → only `default_factory` | **Absent.** The simulator cannot emit a Refoss-published whole-load value, so Group B cannot be exercised end-to-end. |
| No factor history | — | Absent. No way to produce "factor changed at t". |
| No inrush / startup transient | no `startup` model in `behavior.py`; `ExpectedPower.startup_w` unused by the simulator | Absent. Only a unit-level model test (`test_models.py:231`). |
| Standby / always-on loads | `SimDeviceType.STANDBY`, `hot_water_tank`, `monitor`, `printer` | Present. |
| Dynamic / variable-speed loads | `VARIABLE_SPEED`, `HVAC`, `REFRIGERATOR` | Present. |
| Unknown / unmetered loads | `unknown_load_l1`, `unknown_load_l2`, `include_unknown_loads` | Present. |
| Quality levels, drift, inversion, outages, state lag, per-channel intervals | `SimulatorConfig.quality`, `drifting_channels`, `inverted_channels`, `outage_windows`, `state_lag_s`, `channel_intervals` | Present and exercised. |
| Ground truth independent of measurements | `scenario.py` record schema; `test_simulator.py:81` | Present. |

---

## 4. Proposed minimal contract additions

Specification only — nothing below is implemented. Naming follows the existing
`contracts.py` style (`Model` base, `str, Enum`, `Field(default_factory=...)`,
docstring per field, additive/optional so 1.5.0 readers keep validating).

### 4.1 Enums

```python
class CircuitClass(str, Enum):
    """Electrical scope of a circuit, not the type of the sensor on it."""
    DEDICATED = "dedicated"          # one device, and only that device
    SHARED = "shared"                # >=2 known devices
    PARTIALLY_UNDERSTOOD = "partially_understood"  # >=2 known + load we cannot name
    UNKNOWN = "unknown"

class MeasurementBasis(str, Enum):
    """What produced the watt value, independent of how much we trust it."""
    CT_MEASURED = "ct_measured"                    # physical CT conductor reading
    MONITOR_REPORTED = "monitor_reported"          # Refoss publishes the whole load
    FACTOR_ADJUSTED = "factor_adjusted"            # measurement_multiplier != 1.0
    ESTIMATED_APPLIANCE_TOTAL = "estimated_appliance_total"  # model/prior total
    UNKNOWN = "unknown"

class ConfidenceDimension(str, Enum):
    POWER = "power"
    CT_ASSOCIATION = "ct_association"
    TOPOLOGY = "topology"
    CIRCUIT_CLASSIFICATION = "circuit_classification"
    RESIDUAL = "residual"

class ResidualKind(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEAR_ZERO = "near_zero"

class RelationKind(str, Enum):
    """Four relations that must never imply one another."""
    DEVICE_CT_ASSOCIATION = "device_ct_association"
    CT_MEASUREMENT_OVERLAP = "ct_measurement_overlap"
    CIRCUIT_MEMBERSHIP = "circuit_membership"
    ELECTRICAL_PARENT_CHILD = "electrical_parent_child"

class HaEntityRole(str, Enum):
    LOAD_STATE = "load_state"
    MODE_ONLY = "mode_only"
    POWER_MEASUREMENT = "power_measurement"
    ENERGY_COUNTER = "energy_counter"
    AVAILABILITY = "availability"
    UNKNOWN = "unknown"
```

### 4.2 Models

```python
class Circuit(Model):
    circuit_id: str
    name: str | None = None
    panel_id: str | None = None
    #: Scope, with its own evidence and its own status.  ``PARTIALLY_UNDERSTOOD``
    #: exists because "shared by two known devices" and "shared by two known
    #: devices plus an unmetered one" must not read the same.
    classification: CircuitClass = CircuitClass.UNKNOWN
    classification_status: TopologyStatus = TopologyStatus.CANDIDATE
    parent_circuit_id: str | None = None
    ct_ids: list[str] = Field(default_factory=list)
    device_ids: list[str] = Field(default_factory=list)
    measurement_basis: MeasurementBasis = MeasurementBasis.UNKNOWN
    metadata: dict[str, Any] = Field(default_factory=dict)

class TopologyRelation(Model):
    """One typed edge.  Storing four kinds in one model is what keeps them
    separate: nothing can set ``parent_ct_id`` and thereby assert membership."""
    relation_id: str
    kind: RelationKind
    subject_id: str
    object_id: str
    status: TopologyStatus = TopologyStatus.CANDIDATE
    #: Per-relation confidence.  Deliberately *not* a device power score.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = True
    declared_by: Literal["user", "inference", "discovery", "default"] = "user"
    created_at: datetime = Field(default_factory=utcnow)

class ConfidenceVector(Model):
    """Independent dimensions.  Deliberately has no aggregate field and no
    ``__float__``; a consumer that wants one number must say which."""
    power: float | None = Field(default=None, ge=0.0, le=1.0)
    ct_association: float | None = Field(default=None, ge=0.0, le=1.0)
    topology: float | None = Field(default=None, ge=0.0, le=1.0)
    circuit_classification: float | None = Field(default=None, ge=0.0, le=1.0)
    residual: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str = "unavailable"   # e.g. "blend_v1"; names the derivation, not a score

class ResidualPower(Model):
    """Residual as a first-class value.  Never clamped, may stay unresolved."""
    residual_w: float = 0.0                    # signed
    kind: ResidualKind = ResidualKind.NEAR_ZERO
    resolved: bool = False                     # may be False forever
    unresolved_since: datetime | None = None
    explained_by_device_w: dict[str, float] = Field(default_factory=dict)
    quality_status: QualityStatus = QualityStatus.GOOD
    basis: MeasurementBasis = MeasurementBasis.UNKNOWN

class MeasurementFactor(Model):
    """Validity interval for a per-circuit scaling factor.

    A bare float on ``CTChannel`` cannot answer "what factor was in force when
    this reading was taken", which is what historical reconstruction needs."""
    ct_id: str
    multiplier: float = Field(gt=0.0)
    effective_from: datetime
    effective_to: datetime | None = None
    applied_by: Literal["monitor", "user", "unknown"] = "unknown"
    declared_by: Literal["user", "import", "default"] = "user"
    note: str | None = None

class HaEntityBinding(Model):
    entity_id: str
    device_id: str
    role: HaEntityRole = HaEntityRole.UNKNOWN
    is_load_evidence: bool = False
    #: At most one binding per device may be True: the anti-double-count rule.
    contributes_power: bool = False
    attributes_key: str | None = None

class InferenceRecord(Model):
    """Versioned inference record.  Append-only; a revision never overwrites."""
    record_id: str
    device_id: str
    timestamp: datetime
    #: Algorithm + inputs, so a stored result can be explained later.
    algorithm_version: str
    contract_version: str = CONTRACT_VERSION
    topology_version: int = 1
    revision: int = 1
    previous_record_id: str | None = None
    previous_power_w: float | None = None
    power_w: float
    source: Source = Source.UNKNOWN
    measurement_basis: MeasurementBasis = MeasurementBasis.UNKNOWN
    confidence: ConfidenceVector = Field(default_factory=ConfidenceVector)
    previous_confidence: ConfidenceVector | None = None
    #: ``{dimension: delta}`` for the dimensions that moved.
    confidence_delta: dict[str, float] = Field(default_factory=dict)
    reason: str = ""                              # short machine code
    explanation: list[str] = Field(default_factory=list)
    #: Stable references (sample ids / event ids / relation ids), not free text.
    evidence_refs: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
```

### 4.3 Additive fields on existing models

| Model | New field | Why |
| --- | --- | --- |
| `CTChannel` | `measurement_basis: MeasurementBasis = MeasurementBasis.UNKNOWN` | says whether the channel reports a conductor reading or a monitor-computed total |
| `CTChannel` | `circuit_id: str \| None = None` | lets a circuit exist without a device naming it |
| `Measurement` | `measurement_basis: MeasurementBasis = MeasurementBasis.UNKNOWN` | basis must travel with the reading, not only with the channel |
| `Measurement` | `multiplier_applied: float = 1.0` | what scaling this reading actually received |
| `InferenceResult` | `measurement_basis: MeasurementBasis = MeasurementBasis.UNKNOWN` | closes concept 9 without breaking 1.5.0 readers |
| `InferenceResult` | `confidence_dimensions: ConfidenceVector \| None = None` | per-dimension confidence |
| `InferenceResult` | `algorithm_version: str \| None = None` | makes a stored result reproducible |
| `InferenceResult` | `record_id: str \| None = None` | link to `InferenceRecord` / previous result |
| `Reconciliation` | `residual: ResidualPower \| None = None` | first-class residual with resolution state |
| `DeviceConfiguration` | `entity_bindings: list[HaEntityBinding] = []` | typed entity roles + at-most-one power contributor |
| `Topology` | `circuits: list[Circuit] = []`, `relations: list[TopologyRelation] = []` | circuit objects and typed edges |
| `Topology` | `algorithm_version: str \| None = None` | topology inference provenance |

### 4.4 Validation rules to carry the distinctions

1. `DeviceConfiguration.entity_bindings`: at most one `contributes_power=True`;
   `role=POWER_MEASUREMENT` and `role=LOAD_STATE` on one device is allowed but
   only the former contributes watts. (Closes 1a/1b/1c.)
2. `TopologyRelation`: `subject_id != object_id`; a
   `ELECTRICAL_PARENT_CHILD` edge must reference two `ct_ids` and must not
   create a cycle; a `CIRCUIT_MEMBERSHIP` edge must reference a `circuit_id`.
   Cross-kind implication is simply not expressible. (Closes 5a–5e.)
3. `Circuit.classification == PARTIALLY_UNDERSTOOD` requires
   `len(device_ids) >= 2` plus an explicit note that unattributed load remains.
   (Closes 3.)
4. `ResidualPower.kind` is derived, never stored from user input; a negative
   `residual_w` must yield `NEGATIVE`, and `resolved=False` must never be
   rendered as `0 W`. (Closes 6a/6b.)
5. `MeasurementFactor` intervals for one `ct_id` must not overlap; a reading is
   resolved against the interval covering its `state_timestamp`. (Closes the
   factor-over-time and historical-reconstruction gaps.)
6. `InferenceRecord`: `previous_record_id` must reference an earlier record for
   the same `device_id`; `revision` strictly increases; `evidence_refs` must be
   non-empty whenever `source` is not `UNKNOWN`. (Closes 7.)
7. `ConfidenceVector` has no aggregated value and no arithmetic operators; a
   producer that has only one score sets `power` alone and leaves the rest
   `None`. (Closes 8.)

### 4.5 Persistence sketch (specification only)

```
circuits(id, name, panel_id, classification, classification_status, parent_circuit_id, metadata_json)
topology_relations(relation_id PK, kind, subject_id, object_id, status, confidence, declared_by, evidence_json, created_at)
inference_records(record_id PK, device_id, timestamp, algorithm_version, contract_version,
                  topology_version, revision, previous_record_id, previous_power_w, power_w,
                  source, measurement_basis, confidence_json, previous_confidence_json,
                  confidence_delta_json, reason, explanation_json, evidence_refs_json, evidence_json)
  + index (device_id, timestamp), index (device_id, revision)
ct_measurement_factors(id, ct_id, multiplier, effective_from, effective_to, applied_by, declared_by)
ha_entity_bindings(entity_id, device_id, role, is_load_evidence, contributes_power)
residual_power(reconciliation_id, residual_w, kind, resolved, unresolved_since, explained_by_json)
```
Migration 9 should be additive with zero defaults, following the pattern proven
by migration 7 (`test_reconciliation_persistence.py:307`). `inference_results`
stays as-is; `inference_records` supersedes it for audit and is not pruned by the
same retention rule as raw samples.

---

## 5. What this inventory does *not* claim

- It does not claim the current design is wrong. `Source`, `ConfidenceTier` and
  `MonitoringLevel` are each internally consistent and well documented.
- It does not propose changing any existing field's meaning; every addition above
  is optional with a neutral default.
- It does not cover the UI/panel (`addon/`), only the domain contract and engine.
