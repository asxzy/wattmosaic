# Gap Analysis A — Canonical Domain Model, Contracts, Storage & Versioning

Scope: HB §0.1, §1, §2.1, §12.1, §15, §16, §18, §4.4/§14/rule 20 (independent version lifecycles).
Repo: `<repo>` @ `27373aa` (branch `spec/realtime-topology-disaggregation`). Read-only analysis; no repo file modified.

Baseline facts:
- `CONTRACT_VERSION = "2.4.0"` (`engine/wattmosaic_engine/contracts.py:32`), mirrored at `custom_components/wattmosaic/const.py:31`.
- SQLite schema `LATEST_VERSION = 11` (`storage/migrations.py:802`, `max()` over `MIGRATIONS`); `PAYLOAD_ENCODING_VERSION = 12` is a *marker only*, not a migration (`storage/migrations.py:800`).
- 29 exported JSON schemas (`tools/dev/export_schemas.py:26-56`), CI gate `python tools/dev/export_schemas.py && git diff --exit-code data/schemas` (`.github/workflows/ci.yml:28-29`).

---

## 1. Current-state map

### 1.1 The four entity types (HB §1.1, §1.2, rule 3)

| HB concept | Where it lives today | What it does | Verdict |
|---|---|---|---|
| **Physical Device** | `DeviceTopology` (`contracts.py:1621-1635`) + `DeviceConfiguration` (`contracts.py:1798-1934`) keyed by the same `device_id`; DB tables `devices` (`migrations.py:293`) and `device_configurations` (`migrations.py:563`) | An "engine device" is **minted from a CT**: discovery creates `device_id = f"dev_{ct_id}"` (`ingestion/discovery.py:391`) or `dev_{base}` for a CT pair (`discovery.py:367`). Identity therefore = measurement endpoint identity. Name/type/axes live in `DeviceConfiguration`; placement in `DeviceTopology`. | **CONFLICTS** — a device is not independent of its reader. Rule 3 and HB §14 / acceptance test 15 ("Smart Plug relocation changes mappings without relearning device power") are unachievable while `device_id` is derived from `ct_id`; models (`device_model_versions` keyed by `device_id`) orphan when the reader changes. |
| **Load Component** | Absent as an entity. Only implicit as `CompositeModel.submodels` keyed by a free-text `component:` tag in `LearningSample.notes` (`models/composite.py:12-20`). `DeviceTopology.parent_device_id` (`contracts.py:1633`) is unused hierarchy. | Composite power model sums named sub-loads. | **ABSENT** |
| **HA Entity** | `HaEntityBinding` (`contracts.py:1029-1052`, table `ha_entity_bindings` `migrations.py:704`, PK `(entity_id, device_id)` so many-to-many is *storable*); **plus** `DeviceConfiguration.state_entity_ids / mode_entity_ids / controllable_entity_ids` (`contracts.py:1811-1816`); **plus** untyped `DeviceTopology.metadata` keys `state_entity_ids`, `link_state_entity_ids`, `config_load_entity_ids`, `state_overrides` (`device_links.py:68-84`); **plus** `ControllerConfiguration.mode_entity_id/load_entity_ids` (`contracts.py:1764`). Role vocabulary `HaEntityRole` (`contracts.py:445`). | Four overlapping representations of "which entity belongs to which device". `entity_links()` (`device_links.py:169`) builds the runtime entity→device map from metadata only; `ambiguous_entities()` (`device_links.py:201`) refuses one entity → many devices. HA-device link resolution happens in the integration (`custom_components/wattmosaic/state_links.py:346 resolve_links`) and is pushed back as metadata. | **PARTIAL** — typed binding exists but is not the source of truth; runtime explicitly forbids one HA entity → multiple devices/components (HB §1.2 "One HA Entity → multiple physical load changes"). No entity identity/history of its own. |
| **Measurement Endpoint** | `CTChannel` (`contracts.py:1538-1618`, table `ct_channels` `migrations.py:273`). Smart plugs are *deliberately not* endpoints: discovery excludes a bare `sensor.*_power` (`ingestion/discovery.py:22-25`); a plug can only be modelled by hand as a `CTChannel` with `measurement_basis=monitor_reported`. `Circuit` (`contracts.py:1055`) is a separate optional scope object. | Reader identity = `ct_id`, with config inline (phase, polarity, multiplier, parent, pair, alias, panel). `CTChannel.device_id: str|None` (`contracts.py:1551`) is a single device FK on the reader. Config history only for multipliers (`ct_measurement_factors`, `migrations.py:691`); everything else is **overwritten** by `save_topology` (`storage/database.py:311-361`: `DELETE FROM ct_channels; DELETE FROM devices` then re-insert). | **PARTIAL / CONFLICTS** — no endpoint kind (CT vs smart plug vs appliance monitor), no "what it measures" scope declaration, no configuration history (HB §1.1-D "independent identity and configuration history"), single-FK `device_id` (HB §1.2 "do not force into a single Device-to-Reader FK"). |
| Device↔Reader relationship | `DeviceTopology.branch_ct_ids: list[str]` (`contracts.py:1630`), `DeviceConfiguration.monitored_by_ct_ids` (`contracts.py:1823`), `TopologyRelation(kind=DEVICE_CT_ASSOCIATION)` (`contracts.py:1131`, table `topology_relations`), `CTChannel.device_id`. | Lists are many-to-many in shape, but three stores disagree and none is versioned independently of topology. | **PARTIAL** |

### 1.2 Measurement model (HB §2.1, §2.3, §18 null semantics)

| HB field | Current | Verdict |
|---|---|---|
| `reader_id` | `Measurement.ct_id` (`contracts.py:1494`) | MATCHES (rename) |
| `timestamp` / `received_at` | `state_timestamp`, `physical_timestamp`, `received_at` (`contracts.py:1497-1500`) | MATCHES (richer) |
| `active_power_w` | `value` + `unit ∈ {W,kW,kWh,Wh}` + `value_w` (`contracts.py:1495-1497`) — a single numeric slot conflates power and energy | PARTIAL |
| `current_rms_a`, `voltage_rms_v`, `reactive_power_var`, `power_factor`, `energy_wh` | none; `CTChannel.nominal_voltage` is config, not a reading | ABSENT |
| `phase`, `leg`, `voltage_reference` | only on the channel (`CTChannel.phase` `Phase{l1,l2,both,unknown}`), not on the reading; no `leg` / `voltage_reference` | PARTIAL |
| `measurement_type` (instantaneous / window-avg / device-reported / calculated / accumulated) | `MeasurementBasis` (`contracts.py:350`) answers "which instrument", not "what kind of value" | ABSENT |
| `measurement_window_ms` | none (`CTChannel.expected_interval_s` is config) | ABSENT |
| `calibration_version` | `multiplier_applied` per reading (`contracts.py:1510`) + `ct_measurement_factors` history | PARTIAL |
| `quality_flags` | single `quality: QualityStatus` + `repeated` bool | PARTIAL |
| Persistence | `ct_samples` (`migrations.py:317-331`) stores only `value_w NOT NULL, raw_value, unit, timestamps, quality, repeated, sequence`; `insert_ct_samples` (`database.py:541-565`) **drops `measurement_basis` and `multiplier_applied`** | CONFLICTS (basis lost on persist) |
| Null semantics | `InferenceResult.power_w: float` required (`contracts.py:2072`); unknown results write `power_w=0.0` + `provenance.power_w_is_placeholder` (`disaggregation/engine.py:2660,2679`); `Reconciliation.main_power_w: float = 0.0` and 10 other `= 0.0` fields (`contracts.py:2152-2200`); `row_to_inference_result` / `row_to_reconciliation` coerce NULL→0 via `_safe_float(default=0.0)` (`main.py:213-218, 401-465`) | **CONFLICTS** with HB §18 "absent measurement must not become zero by default" and acceptance test 17 |
| Signed values | Sign preserved (`contracts.py:13-14`, `Measurement.effective_power_w`), `ResidualKind` derived | MATCHES |

### 1.3 Inference output (HB §8.5, §12.1, §18 provenance)

| HB concept | Current | Verdict |
|---|---|---|
| `InferenceSnapshot` (snapshot_id, observed_at, computed_at, topology/model/mapping_version, reader_measurements[], device_estimates[], group_estimates[], residual_estimates[], system_quality, stale_sources[], unresolved_events[]) | `SnapshotResponse{timestamp, reconciliation, results[], quality}` (`contracts.py:3113-3117`), assembled by `EngineCore.snapshot()` (`main.py:1554-1563`) **by re-reading `inference_results` rows**, not from the live cycle. | **PARTIAL/CONFLICTS** — no id, no versions, no reader measurements, no group/residual arrays, no stale sources, no unresolved events. |
| Estimate status MEASURED/ESTIMATED/ALLOCATED/UNKNOWN | `PowerState` (`contracts.py:76-101`), derived by `power_state_for` (`contracts.py:104-146`), set in `disaggregation/engine.py:1818` | MATCHES vocabulary, but **MEASURED is unreachable** in practice: `Source.DIRECT_MEASURED` has no producer after ADR-0034, and `Source` wins over basis. Also **`power_state` is not persisted** — `inference_results` has no column, so `/snapshot` (DB round trip) always returns `unknown`. |
| Bounds, group, identifiability | `InferenceResult.min_w/max_w/group_members` (2.2.0), `MappingCellStatus.NON_IDENTIFIABLE` | PARTIAL — group range is stapled onto each member's device result (no first-class `DeviceGroupEstimate`); **not persisted** (lost in `row_to_inference_result`). |
| Provenance (which readings, events, model, topology version, mapping version, last-updated) | `InferenceResult.supporting_ct_channels`, `sources_used/unused`, `model_id/model_version`, free-form `provenance: dict`; `InferenceRecord` (`contracts.py:1292-1406`, table `inference_records`) has `evidence_refs`, `topology_version:int`, `algorithm_version`, `contract_version`, revision chain. No `mapping_version` anywhere on a result. | PARTIAL — the audit record is the right shape (reuse), but snapshot/results lack topology+mapping version; evidence refs are not typed. |
| Event inference record (HB §12.2, immutable evidence, versioned interpretation) | `EventEvidenceRecord` contract (`contracts.py:2791`) **has no table**; actual evidence = `device_events` + `isolation_verdicts` (L2) | PARTIAL (pipeline/matching areas own semantics; storage gap noted below) |

### 1.4 Versioning (HB §4.4, §14, §16.3, rule 20)

| Lifecycle | Current | Verdict |
|---|---|---|
| Topology version | `topology_versions(version INTEGER AUTOINCREMENT, payload_json snapshot)` written on **every** `save_topology` (`database.py:347-357`); `Topology.version:int` (`contracts.py:1639`). Contract `TopologyVersion{version_id:str, version_number, parent_version_id, edge_ids, status, change_event_id, rollback_of}` (`contracts.py:2333`) and `TopologyVersionRecord` / diff (`electrical/topology.py:832-1100`) are **computed only, never persisted** (no table). `TopologyEdge` (`contracts.py:2245`) has no table either. | PARTIAL — a snapshot exists, but there is no parent chain, no change-event link, no rollback, and version ids are typed inconsistently: `int` in `InferenceRecord.topology_version` (`:1315`), `Reconciliation.topology_version` (`:2151`); `str` in `TopologyVersion.version_id`, `DeviceReaderMappingCell.topology_version` (`:2648`), `ConfigurationChangeEvent.topology_version` (`:2769`), `UnknownResidualEvent`/`ResidualBaselineChange.topology_version` (`:2995, :3070`). |
| Mapping version | No independent store. `DeviceTopology.branch_ct_ids` lives **inside** the topology snapshot, so every mapping edit bumps the topology version and vice versa. Mapping matrix is a read-time projection with `reader_configuration_version=f"topology-{topology.version}"` (`api/facade.py:247`). `DeviceConfigurationSet.version` is a third counter in `schema_metadata.device_configuration_version` (`database.py:461-464`) that nothing increments meaningfully. | **CONFLICTS** with rule 20 / HB §14 "Mapping and power model versions must remain independent" and HB §4.4 `H(t)=H(T(t),M(t))`. |
| Device power model version | `device_model_versions(device_id, model_type, version)` + `device_models.active_version`, `ModelStatus` lifecycle incl. `rolled_back`/`frozen` (`contracts.py:173-179`, `database.py:1292-1393`, `models/serde.py:98-157`) | MATCHES in shape (independent table); **but** keyed by a CT-derived `device_id`, so independence is only nominal. No system-wide "model set version" for a snapshot. |
| Configuration change events | `ConfigurationChangeEvent` contract (`contracts.py:2713`), produced by `electrical/topology.py:1235-1490`, held in memory by `disaggregation/audit.py:402` — **no table** | PARTIAL (runtime-only) |
| Schema / contract versions | `PRAGMA user_version` (forward-only, `migrations.py:809-826`), `schema_metadata.contract_version` seeded `'1.0.0'` and never updated (`migrations.py:542`), `InferenceRecord.contract_version` | PARTIAL — no rollback procedure, no pre-migration backup (only `Database.reset()` makes a backup, `database.py:182`). |

### 1.5 Storage & performance (HB §15)

| HB requirement | Current | Verdict |
|---|---|---|
| High-resolution short-term buffers | `ct_samples` L1 raw, pruned at raw horizon (`storage/tiers.py:61-68, 201-228`) | MATCHES |
| Immutable event evidence sufficient to replay after raw expiry | `device_events` is classified L2 but **pruned on the raw horizon** (`tiers.py:216-219`, documented conflict); `EventEvidenceRecord` has no table; `isolation_verdicts`, `inference_records` unpruned | **CONFLICTS** |
| Aggregated long-term measurements | `power_aggregates`, `energy_aggregates` (L3) | MATCHES |
| Versioned models and topology | see §1.4 | PARTIAL |
| Sparse matrix persistence, materialised only on change | Matrix not persisted at all; recomputed per `GET /mapping` (`api/facade.py:179-249`, `api/server.py:809-833`) | ABSENT (not wrong per se, but there is no history of cells, so "mapping version" cannot be replayed) |
| No full snapshot per second; batch writes | `inference_results` row per device per cycle (L1, pruned); writes batched via `persist_queue` | PARTIAL |
| Restart without hidden in-memory state | `latest_results` dict, `_pending_transitions`, config-change events, unknown-load profiles, topology version records and mapping state are in-memory only (`main.py:1199-1290`, `disaggregation/audit.py:402`) | CONFLICTS |
| Tier registry | `storage/tiers.py:59-100` + test `test_every_table_is_classified` forces every new table into a tier | MATCHES (reuse; also a trap) |

### 1.6 Typed contracts list (HB §18)

| HB contract | Current | Verdict |
|---|---|---|
| Reader measurement | `Measurement` | PARTIAL (§1.2) |
| Measurement event | none (ResidualDecompositionRecord / UnknownResidualEvent are residual-specific) | ABSENT |
| HA event | `DeviceEvent` (`contracts.py:1974`) — requires `device_id`, so an HA event for an unbound entity cannot exist | CONFLICTS (event bound to device at ingestion) |
| Event group | none | ABSENT |
| Topology edge | `TopologyEdge` + `TopologyRelation` (two shapes, `RelationKind` lacks HB's CONTAINS/SHARED_LOAD/PAIRED_LEG/CORRELATED) | PARTIAL |
| Residual estimate | `ResidualPower`, `ResidualDecompositionRecord` | PARTIAL |
| Unknown Load Profile | `UnknownLoadProfile` (`contracts.py:2494`) | MATCHES shape, no table |
| Physical Device / Load Component | `DeviceTopology`+`DeviceConfiguration` / none | CONFLICTS / ABSENT |
| Device Power Model | `DeviceModelVersion` | MATCHES |
| Mapping Cell | `DeviceReaderMappingCell` (`contracts.py:2578`) — flat, no `load_component_id`, no `mapping_version`, no `valid_from/valid_until`, no event-association block; `MappingObservability{direct,derived,indirect,unknown}` ≠ HB's 7 states; `MappingCellStatus` mixes freshness (`valid/stale/invalid`) with epistemic state and lacks `SUSPECTED_CHANGE`/`RETIRED` | PARTIAL (mapping area owns semantics) |
| Joint Inference Result / Device Group Estimate | `InferenceResult` per device; group only as fields on members | PARTIAL / ABSENT |
| Inference Snapshot | `SnapshotResponse` | PARTIAL |
| Model version / Topology version | `DeviceModelVersion` / `TopologyVersion` | PARTIAL (no mapping version type) |

---

## 2. Conflicts needing breaking change

Each row: legacy concept → violated rule → replacement → blast radius.

**C1. Device identity derived from a CT (`dev_{ct_id}`).**
Violates rule 3, HB §1.1-A, §14, acceptance test 15.
Replace with an opaque `physical_device_id` (e.g. `pd_<uuid/ulid>`) minted once; the reader link moves to a separate mapping table. On migration, existing `dev_*` ids **are kept verbatim as the physical-device id** (they are opaque strings everywhere) so that `device_model_versions`, HA unique ids (`custom_components/wattmosaic/sensor.py:291` `f"{entry_id}_{device_id}_{key}"`) and `inference_records` keep joining; only *new* devices get new-style ids. Discovery stops creating a device per CT (it creates endpoints; devices come from HA entities/user/unknown-load promotion).
Blast radius: `ingestion/discovery.py` (device minting), `devices` table, `device_configurations`, HA `sensor.py`/`binary_sensor.py`/`entity_migration.py`, addon UI device lists (`addon/ui/app.js`, ~40 `device_id/branch_ct_ids` refs), ~53 files referencing `branch_ct_ids`.

**C2. `Topology` as a monolith that carries devices + device→CT mapping (`Topology.devices`, `DeviceTopology.branch_ct_ids`, `CTChannel.device_id`).**
Violates rule 20, HB §4.4 (`H(T,M)`), §1.2 (no single FK), §14.
Replace with three aggregates:
`MeasurementEndpoint` (+ `EndpointConfigRevision`), `TopologyVersion` (edges between endpoints only), `MappingVersion` (device/component ↔ endpoint edges with observability + validity interval). Remove `CTChannel.device_id`; demote `branch_ct_ids` to a derived read-only view during transition then delete.
Blast radius: tables `ct_channels`, `devices`, `topology_versions`, `topology_relations` (DEVICE_CT_ASSOCIATION rows move to mapping); API `GET/PUT /topology`, `/topology/tree`, `/topology/confirm`, `/mapping`, `/devices`; schemas `topology.json`, `ct_channel.json`, `device_topology.json`; HA `flow_logic.build_topology_update` (`flow_logic.py:827`), `plan_topology_push` (`:1090`), `async_replay_topology` (`:1301`), `state_links.apply_state_link_metadata` (`state_links.py:497`); every solver/attribution consumer of `branch_ct_ids` (other areas).

**C3. Destructive `save_topology` (DELETE + re-insert, no history of endpoint config).**
Violates HB §1.1-D ("configuration history"), §14.10 ("preserve prior versions"), §16.4.
Replace with append-only `endpoint_config_revisions(endpoint_id, revision, valid_from, valid_until, phase, leg, polarity, calibration_version, scope, ...)`; `ct_channels` becomes a "current" view or is dropped.
Blast radius: `storage/database.py:311-434`, `ct_channels` FK self-reference trap (§5), `/topology` PUT semantics.

**C4. HA entity → device stored four ways; one entity may not map to multiple devices/components.**
Violates HB §1.1-C, §1.2, rule 3.
Replace with a single `ha_entities(entity_id, domain, ha_device_id, first_seen, last_seen)` + `entity_links(entity_id, target_kind∈{device,component}, target_id, role, is_load_evidence, contributes_power, valid_from, valid_until, declared_by)`; delete metadata keys `state_entity_ids/link_state_entity_ids/config_load_entity_ids` and `DeviceConfiguration.state_entity_ids/mode_entity_ids` (role carries mode). `ambiguous_entities` refusal becomes a joint-inference question, not an ingestion refusal.
Blast radius: `device_links.py` (whole module), `entity_evidence.py` (classification stays, storage changes), `ha_entity_bindings` table, `main.py` entity routing, HA `state_links.py`, `flow_logic.normalise_device_configuration` (`:1861`), `device_configuration.json` schema, `ServiceCommand set_device_configuration` payload.

**C5. `Measurement` / `ct_samples` lack HB §2.1 fields and drop basis on persist.**
Violates HB §2.1, §2.2, "do not fabricate".
Replace with `ReaderMeasurement` (all §2.1 fields, nullable, `measurement_type` enum, `quality_flags: list`); `ct_samples` → `reader_samples` with nullable `active_power_w` (energy-only readers allowed), `current_rms_a`, `voltage_rms_v`, `reactive_power_var`, `power_factor`, `energy_wh`, `phase`, `leg`, `measurement_type`, `measurement_window_ms`, `calibration_version`, `quality_flags_json`, `measurement_basis`.
Blast radius: `ingestion/ha_source.py:1146`, `insert_ct_samples`/`ct_samples_between`/`latest_ct_sample(s)` (`database.py:541-627`), aggregation, `measurement.json` schema; pipeline area owns producers.

**C6. Zero-as-absent: `InferenceResult.power_w: float` required; `Reconciliation.*_w = 0.0`; `_safe_float(default=0.0)` on read.**
Violates HB §18 null semantics, acceptance test 17.
Replace: `estimated_power_w: float | None`, `status` mandatory, reconciliation fields `float | None`; DB columns nullable (`inference_results.power_w NOT NULL` must be rebuilt — SQLite cannot drop NOT NULL with ALTER, requires table copy); readers return `None`. Legacy rows with `provenance.power_w_is_placeholder=true` are migrated to NULL (permitted: §16.6 "mark legacy records whose semantics are unknown"), or flagged `legacy_semantics_unknown`.
Blast radius: `inference_results`, `reconciliation_results` tables; `main.py:213-465`; HA `entity_values.py`/`sensor.py` (must render `unknown` not 0); addon UI; `inference_result.json`, `reconciliation.json`, `snapshot_response.json`.

**C7. `SnapshotResponse` is not an `InferenceSnapshot` and is rebuilt from a lossy DB round-trip.**
Violates HB §12.1, §18, §15 (restart without hidden state).
Replace with `InferenceSnapshot` contract (fields per HB §12.1) produced by the inference cycle, carrying `topology_version_id`, `mapping_version_id`, `model_set_version_id`; persisted sparsely (on change) in `inference_snapshots`; `/snapshot` serves the latest persisted one. Persist `power_state`, `min/max`, group membership, `measurement_basis`, `sources_used` — today dropped by `row_to_inference_result` (`main.py:401-423`).
Blast radius: `main.py:1554`, `/snapshot` route, HA `coordinator.py`, `entity_values.py`, addon UI, `snapshot_response.json`.

**C8. Version ids typed inconsistently and mapping version derived from topology.**
Violates rule 20, HB §12.1, §18.
Replace with one `VersionRef` convention: opaque string ids `topo_v<N>`, `map_v<N>`, `mdl_<device>_<type>_v<N>` (or ULIDs) plus `*_number:int`; every result/record/cell/event carries the triple. Delete `reader_configuration_version=f"topology-{n}"` (`api/facade.py:247`).
Blast radius: `InferenceRecord`, `Reconciliation`, `DeviceReaderMappingCell`, `ConfigurationChangeEvent`, `UnknownResidualEvent`, `ResidualBaselineChange`, `inference_records.topology_version INTEGER`, `reconciliation_results.topology_version INTEGER`, `Database.inference_records_for_topology_version` (`database.py:833`), HA `CONF_TOPOLOGY_VERSION` (`const.py:47`, an int returned by `/topology/confirm`).

**C9. Duplicate vocabularies kept for "additive-only" reasons.**
`TopologyStatus` vs `EdgeLifecycle` on one relation with a cross-validator (`contracts.py:1180-1214`); `TopologyRelation` vs `TopologyEdge`; `Source` vs `MeasurementBasis` vs `PowerState` vs `ConfidenceTier` (four axes for "what kind of value"); `MappingCellStatus` with 4 legacy + 5 new members; `Source.DIRECT_MEASURED` retained only so old rows validate (ADR-0034 §5).
Violates HB §16 ("do not accumulate compatibility layers indefinitely").
Replace: one `EstimateStatus{MEASURED,ESTIMATED,ALLOCATED,UNKNOWN}` + `MeasurementSource`/`InferenceSource` (HB §8.5 lists both as separate fields); one edge lifecycle; one relation-edge contract. Map legacy values by data migration rather than retained enum members.
Blast radius: practically every engine module and 66 test files importing contracts; schemas; UI badges.

**C10. Smart plug / MEASURED path removed by ADR-0034.**
HB §8.5 requires `MEASURED`; acceptance test 8 requires Smart Plug measurements as stronger anchors; HB §1.1-D lists "Smart Plug Energy Monitor" and "Dedicated appliance energy monitor" as endpoints.
ADR-0034 removed a *role on a CT* (`dedicated_device`, which asserted that a circuit equals one appliance). The handbook's version is different: an **endpoint whose declared scope is one physical device** (`EndpointScope.SINGLE_DEVICE`, typically a plug or appliance monitor), linked by a mapping edge with `observability=DIRECT`. That yields `MEASURED` without bringing back the "a circuit CT is a device" claim. This needs a new ADR that amends ADR-0034 decision 2 ("Level-1 direct measurement is deleted") and keeps decisions 1, 3 and 4.
Blast radius: `ingestion/discovery.py:22-25` (plugs are excluded today), `HaEntityRole.POWER_MEASUREMENT`/`contributes_power` (become endpoint declarations), `Source`/`PowerState`, `disaggregation/confidence.py:45,86-88` (DIRECT floors still present), HA flow (minimal-ha-setup has no place to declare a plug).

**C11. Evidence retention: `device_events` pruned at raw horizon; `EventEvidenceRecord`, `ConfigurationChangeEvent`, `UnknownLoadProfile`, `TopologyVersion`, `TopologyEdge` have no tables.**
Violates HB §15 ("persisted event evidence sufficient to replay inference after raw samples expire"), §12.2, §14.1, rule 24.
Replace: new L2 tables `event_evidence`, `configuration_events`; L4 `topology_versions_v2`/`topology_edges`, `mapping_versions`/`mapping_edges`, `unknown_load_profiles`; `device_events` either re-tiered to evidence retention or superseded by `event_evidence`.
Blast radius: `storage/tiers.py` (must classify each new table, `test_every_table_is_classified`), `prune`, `/status` storage health.

**C12. `DeviceEvent.device_id` required.**
Violates HB §1.1-C (HA entity is evidence, not a device) and §12.2 (candidate devices are an *output* of matching).
Replace with `HaEvent{entity_id, ...}` with no device binding; binding is a matching result. Blast radius: `device_events.device_id NOT NULL`, `main.py` bridge (`unlinked_events_ignored` counter), `device_event.json`.

---

## 3. Reusable assets to keep

- **Pydantic base** `Model(extra="forbid", use_enum_values=True)` (`contracts.py:820-821`) and the `_document()` enum-doc helper (`:310`). Keep. `extra="forbid"` is what makes contract drift loud.
- **`InferenceRecord` append-only revision chain** (`contracts.py:1292-1406`, `inference_records` table, `resolve_evidence_ref` `database.py:1099`). Reuse as the "versioned interpretation" for HB §12.2/§22 rule 22. Extend with the version triple.
- **`ConfidenceVector`** (`contracts.py:832`): per-dimension, never blended. Matches the HB §9.6 confidence block in spirit. Rename the dimensions to HB's seven.
- **`PowerState` + `power_state_for` totality** (`contracts.py:76-146`). This is the HB §8.5 status. Promote it to the canonical `EstimateStatus`.
- **`TopologyEdge`, `TopologyVersion`, `TopologyVersionRecord`/`TopologyDiff`** (`contracts.py:2245-2378`, `electrical/topology.py:720-1100`): validity intervals, parent version, rollback-as-append, and diffing are already written. They only need persistence, and endpoints in place of devices.
- **`ConfigurationChangeEvent` + `ChangeStatus`/`ChangeDetection`/`ConfigurationChangeKind`** (`contracts.py:748-818, 2713`). These are exactly HB §14 step 1. Persist them.
- **`ct_measurement_factors`** time-bounded multiplier history (`migrations.py:691`, `electrical/factors.py`). This is the template for endpoint config revisions and `calibration_version`.
- **`HaEntityRole` and `entity_evidence.classify_entity`** (`entity_evidence.py:40-190`): the mode-vs-load domain rules (ADR-0012) carry over unchanged to the new entity-link table.
- **`DeviceModelVersion` / `ModelStatus` / `models/serde.py`**: an independent model lifecycle with rollback and freeze. Keep. Re-key to physical device (+ component).
- **Storage machinery**: `Migration(statements, apply)` with idempotent `add_column_if_missing` (`migrations.py:17-48`), per-migration `BEGIN IMMEDIATE` atomicity, `payload.py` `Z1:` compression with dual-read decoder, the `tiers.py` registry + prune declaration, and `Database.reset()` backup logic (reuse it for pre-migration backup).
- **`_retire_dedicated_rows`** (`migrations.py:166-258`). This is the precedent pattern for a JSON-column data migration that must land before an enum narrows.
- **Contract mirror + major-version gate** (`api_client._check_contract`, `api_client.py:190-210`). A major bump makes the HA integration refuse a mismatched engine. That is the right mechanism for a breaking change.
- **`DeviceReaderMappingCell` bounds validators** (`contracts.py:2672-2710`: estimate within [min,max], and no numbers when observability is UNKNOWN). Keep these validators on the new cell.

---

## 4. Proposed work packages

File ownership is listed explicitly. Shared hot files (`contracts.py`, `main.py`, `storage/migrations.py`, `storage/database.py`, `api/server.py`, `api/facade.py`, `custom_components/wattmosaic/const.py`, `data/schemas/*`, `tools/dev/export_schemas.py`) are each given **one owner per wave**. Other packages contribute through a submodule that the owner re-exports.

Suggested split so packages can be file-disjoint: make `contracts.py` a package facade. Keep `contracts.py` as the re-export point, because ~66 test files and every module import `from wattmosaic_engine.contracts import X`. New models go in `engine/wattmosaic_engine/domain/{entities,measurement,versions,inference,evidence}.py`, which `contracts.py` re-exports.

### WP-A0: Decision record and migration plan (docs only). Size S
- **Objective:** ADR-0035 "Canonical domain model (HB §1/§16)". It covers C1-C12, the id-preservation rule, the version-ref convention, the null policy, and an amendment of ADR-0034 for single-device-scope endpoints (C10). Also a legacy-field disposition table (keep/rename/derive/delete) and the rollback procedure (§16.8).
- **Owns:** `docs/decisions/README.md` (append), `docs/specs/realtime-topology-disaggregation/decisions.md`, `docs/interface_contracts.md` (the contract-version line is pinned by `tests/unit/test_contracts.py:84-99`).
- **Deps:** product-owner answers in §6. Gates every other WP.
- **Tests:** none (doc). The version-line test updates in WP-A1.

### WP-A1: Canonical domain contracts v3.0.0. Size L
- **Objective:** typed models for PhysicalDevice, LoadComponent, HaEntity, HaEntityLink, MeasurementEndpoint(+EndpointScope, EndpointKind{ct, smart_plug, appliance_monitor, panel_main, virtual}), EndpointConfigRevision, ReaderMeasurement (HB §2.1, nullable, `MeasurementType`, `quality_flags`), MappingEdge, TopologyVersionRef/MappingVersionRef/ModelSetVersionRef, `EstimateStatus` (from PowerState) plus separate `measurement_source`/`inference_source`, `ProvenanceRef` (typed evidence refs: `sample:`, `event:`, `edge:`, `model:`), `InferenceSnapshot`, `DeviceGroupEstimate`, and `HaEvent` (no device FK). Remove the legacy duplicates in C9. Bump `CONTRACT_VERSION` to `3.0.0`, regenerate schemas, update the HA mirror.
- **Owns:** `engine/wattmosaic_engine/contracts.py` (exclusive this wave), new `engine/wattmosaic_engine/domain/*.py`, `tools/dev/export_schemas.py`, `data/schemas/*` (generated), `custom_components/wattmosaic/const.py:31` (one line), `tests/unit/test_contracts.py`, new `tests/unit/test_domain_*.py`.
- **Interfaces needed from others:** event/EventGroup/measurement-event field lists (pipeline area); edge type vocabulary CONTAINS/SHARED_LOAD/PAIRED_LEG/CORRELATED (topology area); `DeviceReaderCell` nested blocks (mapping area); group estimate fields (joint-inference area); power-model kinds (models area). Proposal: WP-A1 defines **empty-but-typed slots** and the version/provenance/null conventions, and each area fills its own `domain/<area>.py`.
- **Tests:** round-trip every model through JSON. Null preservation: a missing field stays `None` through validate→dump→validate. Many-to-many cardinalities (HB §1.2, each bullet a case). No model has a numeric default of `0.0` for a measurement/estimate field (a reflective test over `model_fields`). `extra="forbid"`. Schema export is deterministic.

### WP-A2: Storage schema v12+ (canonical tables and data migration). Size L
- **Objective:** a new migration (number: see trap T3) that creates `physical_devices`, `load_components`, `ha_entities`, `ha_entity_links`, `measurement_endpoints`, `endpoint_config_revisions`, `topology_versions_v2`, `topology_edges`, `mapping_versions`, `mapping_edges`, `configuration_events`, `event_evidence`, `unknown_load_profiles`, `inference_snapshots` and `reader_samples` (or `ALTER ct_samples` with nullable §2.1 columns). Rebuild `inference_results`/`reconciliation_results` with nullable power. Backfill: `ct_channels`→endpoints+revision 1. `devices`+`device_configurations`→physical_devices, **keeping `device_id` verbatim**. `branch_ct_ids`/`monitored_by_ct_ids`/`DEVICE_CT_ASSOCIATION` relations→mapping_edges (with `declared_by` preserved, and `legacy_semantics_unknown=1` where provenance is unknown). Metadata entity keys + `ha_entity_bindings`→ha_entity_links. `topology_versions` rows→`topology_versions_v2` (number preserved, `legacy=1`). Placeholder-zero inference rows→NULL power plus a flag. Add a pre-migration file backup (reuse `reset()` steps 1-2) and a documented rollback (restore the backup; no down-migrations).
- **Owns:** `engine/wattmosaic_engine/storage/migrations.py`, `storage/tiers.py`, `storage/payload.py` (if needed), new `storage/repositories/*.py` (per-aggregate DAO). `storage/database.py`: this WP owns the constructor/migration/backup section and adds new repository delegation. Old methods are removed in WP-A3.
- **Deps:** WP-A1.
- **Tests:** migrate a fixture DB at schema 11 (build it from `tests/fixtures` or by running migrations 1-11 plus seed rows that include a legacy `dedicated` row and a `power_w_is_placeholder` row). Then assert: zero data loss in counts; ids preserved; `test_every_table_is_classified` passes; idempotence (running twice is a no-op); interrupted-migration rollback (raise inside `apply` and verify `user_version` is unchanged); backup file created; `LATEST_VERSION` bumped.

### WP-A3: Repositories and version lifecycle service. Size M
- **Objective:** an append-only API for the three independent lifecycles: `promote_topology_version(edges, change_event_id)`, `promote_mapping_version(edges, change_event_id)`, `activate_model_version(...)` (exists today), `record_configuration_event`, and `current_version_triple()`. Validity-interval queries `as_of(t)` for topology/mapping (HB §4.4 `H(t)`). Remove destructive `save_topology` (C3). Implement the HB §14 workflow at the data level: record event → mark affected mapping edges `SUSPECTED_CHANGE` → candidate version → promote → old version retained. Matching/topology areas own the inference that decides; this WP owns the state transitions and persistence.
- **Owns:** new `engine/wattmosaic_engine/versioning.py` (or `domain/lifecycle.py`), `storage/database.py` topology/config sections (`:279-540`, `:833-1180`), `models/serde.py` (re-key to physical device/component).
- **Deps:** WP-A2. **Interface to topology area:** it calls `promote_topology_version`. **To mapping area:** it calls `promote_mapping_version` and reads `mapping_edges` as of t.
- **Tests:** a Smart Plug relocation scenario (HB §17 steps 9-10, acceptance test 15). The topology version bumps, the mapping version bumps, the model version is unchanged, the old edges are readable as of the old t, the old evidence still resolves with the old version ids. A reader calibration change (test 16) creates a revision and suspects the edges. Rollback creates a new version (`rollback_of`) and never rewrites rows.

### WP-A4: Snapshot, provenance and null-semantics output path. Size M
- **Objective:** replace `EngineCore.snapshot()` (`main.py:1554`) with an `InferenceSnapshot` built from the live cycle and persisted only on change (HB §15). Persist all estimate fields (status, bounds, group, basis, sources). Remove `_safe_float(default=0.0)` coercions on read (`main.py:213`, `401-465`). Add a typed provenance resolver that answers the 9 HB §18 questions for any estimate (extend `Database.resolve_evidence_ref`).
- **Owns:** `main.py` snapshot/row-conversion section (`:200-470`, `:1540-1565`); `main.py` is shared with the pipeline area, so coordinate ownership per wave. Also the snapshot route in `api/server.py` and `api/facade.py` protocol methods `snapshot`/`provenance`.
- **Deps:** WP-A1, WP-A2. The joint-inference area supplies `device_estimates/group_estimates/residual_estimates`; the pipeline supplies `reader_measurements/stale_sources/unresolved_events`.
- **Tests:** acceptance 17 (missing reader → `None`/UNKNOWN, never 0, end to end through DB and API). After a restart the snapshot equals the pre-restart one (no hidden state). Every estimate in the snapshot carries the version triple and resolvable evidence refs. A group estimate never yields individual values.

### WP-A5: Endpoint ingestion identity (smart plugs and appliance monitors as endpoints). Size M
- **Objective:** discovery proposes `MeasurementEndpoint`s for multi-channel monitors **and** single-load power entities (plugs) with an `EndpointScope` (candidate, never auto-confirmed). Stop minting `dev_{ct_id}` devices; devices come from HA devices that own load-evidence entities, from user declaration, or from unknown-load promotion. `MEASURED` is produced only when an endpoint with confirmed `SINGLE_DEVICE` scope has a `DIRECT` mapping edge (C10).
- **Owns:** `engine/wattmosaic_engine/ingestion/discovery.py`, `device_links.py` (rewrite to read `ha_entity_links`), `entity_evidence.py`.
- **Deps:** WP-A1, WP-A3, and the ADR (WP-A0). Pipeline area for the `ha_source.py` producers of `ReaderMeasurement`.
- **Tests:** a plug is discovered as an endpoint, not as a CT channel nor a device. A plug estimate is MEASURED only after its scope is confirmed. One HA entity linked to two components is accepted. A legacy `dev_*` id survives.

### WP-A6: HA integration and API payload migration. Size M
- **Objective:** move the HA integration to contract 3.x. The CT step writes endpoints and an optional declared CONTAINS parent (as a *user-declared topology edge*, not a channel field). Device links write `ha_entity_links`. Handle the unknown/None power state. Keep unique-id stability. Replace `CONF_TOPOLOGY_VERSION:int` with the version-id string.
- **Owns:** `custom_components/wattmosaic/{api_client.py, flow_logic.py, state_links.py, config_flow.py, coordinator.py, entity_values.py, sensor.py, binary_sensor.py, entity_migration.py, strings.json, translations/*}`. Co-ordinate with the minimal-ha-setup spec (§6 Q5); the two must merge into one plan.
- **Deps:** WP-A1, plus the endpoint/topology API routes from the topology/mapping areas.
- **Tests:** contract major mismatch is refused. A legacy config entry re-adds cleanly. `None` power renders as HA `unknown`, not 0. Unique ids are unchanged for migrated devices.

### WP-A7: Storage-performance and retention policy. Size S
- **Objective:** re-tier `device_events` versus the new `event_evidence` (C11). Retention for raw event windows "only when meaningful" (HB §15). Sparse materialisation rule for mapping/snapshot writes. Storage-health surface covers the new tables.
- **Owns:** `storage/tiers.py` (after WP-A2 lands), `Database.prune`, `diagnostics/` storage health.
- **Deps:** WP-A2; the pipeline area defines what counts as "meaningful" evidence.
- **Tests:** prune never deletes L2 evidence or versions. A replay using only `event_evidence` plus aggregates (no raw samples) reproduces an inference (acceptance test 19, jointly with the learning area). Write counts per cycle stay bounded (no snapshot row when nothing changed).

Dependency order: A0 → A1 → A2 → {A3, A4, A5} → A6, A7. A1 is the Stage-A gate in HB §20.

---

## 5. Traps / non-negotiable constraints

- **T1. Pydantic validation on load, mostly with no fallback.** `load_topology` builds `CTChannel`/`DeviceTopology` from rows with no try/except (`database.py:363-434`). One invalid enum value makes every startup raise. `load_device_configurations` *silently drops* an invalid row (`database.py:509-512`), which loses user declarations. `row_to_inference_result` passes stored enum strings straight through (`main.py:401`). **Rule:** every enum narrowing or field removal needs a data migration that lands *before* the code narrows, in the same release (the precedent is ADR-0034 decision 6, `_retire_dedicated_rows`). `extra="forbid"` means any stored JSON payload (`device_configurations.payload_json`, `topology_versions.payload_json`) with a removed field also fails validation. Those payloads must be rewritten by the migration or read through a tolerant legacy reader.
- **T2. Runtime-only state.** Topology version records/diffs (`electrical/topology.py:832`), configuration change events (`disaggregation/audit.py:402`), unknown-load profiles, mapping matrix (`api/facade.py:179`), `latest_results`, `_pending_transitions`, and `_pending_backend_events` (`main.py:1240-1285`) all live in memory. Anything the HB requires to be versioned or replayable cannot stay there.
- **T3. Schema numbering.** `LATEST_VERSION` is 11. `PAYLOAD_ENCODING_VERSION = 12` is an unmigrated marker, and its comment calls itself "a thirteenth migration" (`migrations.py:791-800`). A new migration numbered 12 will be read as the payload-encoding version by any doc or diagnostic that cites "schema 12". Decide explicitly: either number the new migration **13** and leave 12 reserved, or retire the marker in the same change. Migrations are forward-only (`apply_migrations` skips `version <= start`), so a reused number is silently skipped (see the comment on migration 8, `migrations.py:603-613`).
- **T4. SQLite limits.** `ALTER TABLE` cannot drop `NOT NULL` or a column on older SQLite versions. Making `inference_results.power_w`/`ct_samples.value_w` nullable requires a create-copy-rename inside the migration transaction. `PRAGMA foreign_keys=ON` (`database.py:105`) together with `ct_channels.parent_ct_id REFERENCES ct_channels` (`migrations.py:279`) is why `_insertion_order` exists (`database.py:279-309`). Table rebuilds must order inserts or disable FKs temporarily. Each migration runs in `BEGIN IMMEDIATE`. `PRAGMA user_version` is set inside it, so a crash rolls back cleanly, but only if no statement auto-commits (avoid `VACUUM` inside a migration).
- **T5. No pre-migration backup today.** `Database.__init__` runs `apply_migrations` immediately (`database.py:93`). Only `reset()` backs up. HB §16.8 requires a rollback procedure, so add a backup before any destructive migration.
- **T6. CONTRACT_VERSION mirror and gates.** `custom_components/wattmosaic/const.py:31` must equal `contracts.py:32`. `api_client._check_contract` (`api_client.py:190-210`) **refuses** on a major mismatch but silently tolerates a minor one, and tolerates a missing header. A 3.0.0 bump therefore hard-breaks old integrations, which is intended, so ship the engine and integration together. Pins: `tests/unit/test_contracts.py:76` (`== "2.4.0"`), `tests/unit/test_reconciliation_split.py:546`, `docs/interface_contracts.md` "Contract version:" line (`test_contracts.py:84-99`), and `tests/integration/test_ha_integration.py` (mirror check).
- **T7. CI schema-drift gate.** `ci.yml:28-29` regenerates `data/schemas` and fails on any diff. Every contract edit must commit regenerated schemas. Never put a changing value (e.g. a version default) into a schema; `HealthResponse.engine_version` uses `default_factory` for exactly this reason (`contracts.py:3094-3102`). New exported models must be added to `export_schemas.MODELS`.
- **T8. Tier registry completeness test.** Every new table must be added to `storage/tiers.py:TIER_TABLES`, or `test_every_table_is_classified` fails.
- **T9. ID stability is user-visible.** HA unique ids embed the engine `device_id` (`sensor.py:291`). The add-on UI and saved HA `device_links` (`{engine_device_id: ha_device_id}`) key on it too. Renaming device ids orphans entities and history; preserve them (C1).
- **T10. Additive-only habits in the old spec.** Many contracts say "frozen field" or "never retype" (e.g. `DeviceReaderMappingCell.device_id` must stay `str`, hence `is_unknown_load`, `contracts.py:2650-2660`). HB §16 lifts that rule. Implementers must not keep adding parallel fields; the old progress tracker (`docs/specs/load-disaggregation/tasks/progress.md`) is partly stale and must not be used as evidence.
- **T11. ADR-0034 is accepted policy.** Reintroducing a measured path without a superseding ADR would contradict an accepted decision and AC-6 of `retire-dedicated-ct-concept` ("no write can reintroduce the role"). The new concept must not be named or shaped like `dedicated_device`.
- **T12. Discovery never auto-confirms, and confirmation is a user act** (`TopologyRelation` validator `contracts.py:1180-1214`, ADR-0024). HB §4.3 agrees that a single event must not confirm. The minimal-ha-setup D7 "silent auto-confirm" sits in tension with both.
- **T13. Energy publishing.** HA energy sensors are `total_increasing` and pass through the engine total (ADR-0020). A `None` energy must not reset an HA counter, so the restore-last-state path matters.

---

## 6. Open questions for the product owner

1. **Breaking-change scope and cadence.** Should there be one 3.0.0 cutover (engine, integration and UI in the same release, users re-add the HA integration as in minimal-ha-setup D2), or a staged path with a temporary dual-read API?
2. **Device identity.** Should existing `dev_<ct>` ids be kept verbatim as Physical Device ids (recommended: preserves HA entities, models and history), or should devices be re-minted? Should discovery stop creating one device per CT, so that devices only come from HA devices, user declarations or promoted unknown loads? That changes what a fresh install shows.
3. **MEASURED / Smart Plug anchors versus ADR-0034.** Should ADR-0034 be amended to allow a *single-device-scope endpoint* (plug or appliance monitor) to produce MEASURED, while the CT role stays removed? Must the scope be user-confirmed, or may it be auto-confirmed for a single-outlet plug whose HA device is the appliance?
4. **Smart-plug ingestion.** Should all HA power entities that are not CT-monitor channels be ingested as candidate endpoints (more data, more storage), or only ones the user or app selects?
5. **Interaction with minimal-ha-setup** (draft, untracked at `<repo>/docs/specs/minimal-ha-setup/`). Its D7 auto-confirms topology on setup, and D3 writes an engine-proposed parent as if it were a declaration. Under HB §4.2-4.4 the parent should be stored as a *candidate* CONTAINS edge (`declared_by=inference`) unless the user picks it. Questions: should minimal-ha-setup be rebased onto the new endpoint/topology API (it currently PUTs the legacy `Topology` with `CTChannel` fields, `interfaces.md` §2.4) and sequenced after WP-A1? Should the setup flow gain an optional "this power sensor measures one appliance" step for plugs, which minimal-ha-setup does not provide?
6. **Legacy history semantics.** Should old `inference_results` rows with placeholder `power_w=0.0` be rewritten to NULL (HB §16.6-7 favours flagging), or left untouched and only marked `legacy_semantics_unknown`? Should old `source=direct_measured` rows be shown as MEASURED, or as legacy/unknown?
7. **Load Components.** Are they user-declared only (e.g. HVAC compressor, fan, aux heat), or may the engine propose them from composite-model evidence? Are they required in v1, or a typed placeholder?
8. **Retention budget.** How long should immutable event evidence be kept (forever, N months, or size-capped)? `device_events` is currently the ~1.5 GB/day growth driver (`tiers.py:211-215`), so HB §15 "replay after raw expiry" has a real storage cost.
9. **Version-id format.** Opaque ULIDs or human-readable `topo_v12`? This is visible in the UI and in HA attributes.
10. **Rollback promise.** Is "restore the pre-migration backup file" an acceptable rollback procedure for HB §16.8, or are down-migrations required?
