# 18 — Completion gap analysis (Phase A, read-only)

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Phase A of the completion sprint.** This document records the *exact* state of
the repository against the 130 original requirements of
[`01-requirements.md`](01-requirements.md). It was produced by direct, read-only
inspection of `engine/**`, `addon/**`, `tests/**`, `tools/**`, the specification
set and Git history. **No Phase B implementation was started.** This analysis is
**non-exhaustive by design**: it resolves the 13 reported gaps and inspects the
code independently, but it is a point-in-time map, not a proof of absence. Where
a fact could not be determined from the repository it is recorded below as
**unresolved**, never guessed.

---

## 1. Exact Git state snapshot

All commands were run on branch `main` at workspace root. Recorded 2026-11-17.

| Item | Value |
| --- | --- |
| `git rev-parse HEAD` | `e54ce5d447185cc819346c50d92232de58ddb10a` |
| `git branch --show-current` | `main` |
| `git status --porcelain=v1` | empty (clean tree before this document set) |
| `git log --oneline -20` | see below |
| `git stash list` | `stash@{0}: On main: wave2-wip-events-models-joint-orchestrator`; `stash@{1}: WIP on main: 74b686d feat(topology): resolve every edge to one relation kind with an explicit lifecycle` |
| `git remote -v` | `origin https://github.com/asxzy/wattmosaic.git` (fetch + push) |
| `git log main..origin/main --oneline` | empty |
| `git rev-parse origin/main` | `e54ce5d447185cc819346c50d92232de58ddb10a` |
| `git rev-list --left-right --count HEAD...origin/main` | `0 0` — local `main` is **not behind** origin |
| PR #6 | merged in `7882db9` — "Merge pull request #6 from asxzy/feat/retention-horizon-app-options"; feature commit `5928718` exposes L2/L4 retention horizons as App options |
| HEAD commit | `e54ce5d Release 0.2.1-dev.37 [wm-auto-version]` |
| Version | `pyproject.toml:7` = `0.2.1-dev.37`; `addon/config.yaml:25` = `0.2.1-dev.37` |
| Latest reachable tag | `v0.2.1-dev.36` (`git describe --tags --always` = `v0.2.1-dev.36-19-ge54ce5d`) — the `dev.37` release commit is **not yet tagged**; recorded as a fact, not an error |
| Spec-set status gate | the measured 55-file count holds (`find … \| … grep -c 'SPEC_STATUS: …'` = 55) before this document set; the two new files in this change raise it to 57 |

Last 20 commits (`git log --oneline -20`):

```
e54ce5d Release 0.2.1-dev.37 [wm-auto-version]
7882db9 Merge pull request #6 from asxzy/feat/retention-horizon-app-options
5928718 feat(addon): expose the L2/L4 retention horizons as app options
f838dbf docs: update progress and traceability to actual implementation status
4cd3545 fix(simulation): correct the state-lag test so a genuine lag is withheld
8b70933 feat(models): add the load-shape axis, the device power profile and version reversal
8ee3973 feat(events): name every event kind, group automations and derive the window on the live path
036ffbc feat(electrical): enforce the containment evidence bar and once-only contribution
4bac434 feat(topology): version the topology with a deterministic diff
74b686d feat(topology): resolve every edge to one relation kind with an explicit lifecycle
14f7b20 feat(residual): pin the signed residual, its decomposition and the unresolved state
31f01b2 feat(storage): add the four-tier registry, per-tier retention and payload encoding
15c9a3f docs(spec): clarify the two cadence defaults are independent by design
415ef9e feat(engine): make the leg pair atomic and publish the reader declaration
76af12d feat(sync): publish the resolved cadence with its source and gate the 2s assumption
cd71d4e feat(engine): enforce the measurement unit whitelist and phase declaration
f5889b4 feat(engine): add the machine-readable four-state power label
bfb9938 feat(contracts): add the circuit-aware topology and evidence contracts
ee85417 docs(spec): add the circuit-aware load disaggregation specification set
612e123 Release 0.2.1-dev.36 [wm-auto-version]
```

**Interpretation.** The 11 `VERIFIED` tasks in `tasks/progress.md` correspond to
commits `bfb9938`, `f5889b4`, `cd71d4e`, `415ef9e`, `76af12d`, `14f7b20`,
`31f01b2`, `74b686d`, `036ffbc`, `4bac434`, `8ee3973`, `8b70933`. The reported
completions are checked against the code below rather than taken on trust.

### 1.1 Verification run during Phase A (read-only)

| Command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/unit tests/integration -o addopts="" -q` (cache redirected outside the tree) | 0 | **2678 passed, 15 skipped** — matches the recorded baseline in `tasks/progress.md:185` |
| `.venv/bin/python -m pytest tests/unit/test_residual_power.py tests/unit/test_inference_records.py tests/unit/test_topology_persistence.py tests/unit/test_panel.py -o addopts="" -q` | 0 | 151 passed, 11 warnings |
| `find docs/specs/load-disaggregation -name '*.md' … grep -c 'SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION'` | 0 | 55 (pre-change) |

No code test was modified, weakened or deleted. The working tree stayed clean
throughout inspection.

---

## 2. Requirement-by-requirement status (all 130 IDs)

Legend: **IMPLEMENTED** = code path and named automated vehicle exist at HEAD;
**PARTIAL** = some code exists but a named capability is absent; **NOT
IMPLEMENTED** = no production path or no evidence found; **REVERTED** = work was
attempted and rolled back. A status is an inspection finding, **not** an
orchestrator verification move; the `14-…` matrix's `Verification Result` column
is unchanged by this document.

### 2.1 Foundation — 6

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-FOUNDATION-001` | IMPLEMENTED | 55 spec files carry the token on their first status line; `verify.py:57` `check_spec_status`; HEAD `bfb9938` |
| `REQ-FOUNDATION-002` | IMPLEMENTED | `contracts.py:776` `extra="forbid"` with neutral defaults; `test_contracts.py` 1.5.0 payload tests; schemas regenerated at `bfb9938` |
| `REQ-FOUNDATION-003` | IMPLEMENTED | `CONTRACT_VERSION = "2.1.0"` (`contracts.py:32`); `tools/dev/export_schemas.py`; `verify.py` gate `contract_schema_sync` |
| `REQ-FOUNDATION-004` | IMPLEMENTED | `pyproject.toml:13` dependencies unchanged; one SQLite file; `test_app_startup.py`; HEAD `f5889b4` |
| `REQ-FOUNDATION-005` | IMPLEMENTED | `PowerState` label and `power_state_for` derivation; `InferenceResult.power_state` published; HEAD `f5889b4` |
| `REQ-FOUNDATION-006` | PARTIAL | The plan names one owner per file per wave (`15-…` §4.5), but the named vehicle is a manual review and is `NOT RUN` in `14-…` §7.1; no static gate exists |

### 2.2 Measurement — 10

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-MEASURE-001` | IMPLEMENTED | unit whitelist in `contracts.py`/`ingestion/normalize.py`; HEAD `cd71d4e` |
| `REQ-MEASURE-002` | IMPLEMENTED | no V/A/PF/VAR/VA/Hz field in `contracts.py`; HEAD `cd71d4e` |
| `REQ-MEASURE-003` | IMPLEMENTED | `Phase` declared on `CTChannel`; `electrical/factors.py`; HEAD `cd71d4e` |
| `REQ-MEASURE-004` | IMPLEMENTED | polarity applied once in `Measurement.effective_power_w()` and correlation; HEAD `cd71d4e` |
| `REQ-MEASURE-005` | IMPLEMENTED | `_exclude_partial_legs` in `disaggregation/engine.py` (890–950 area); HEAD `415ef9e` |
| `REQ-MEASURE-006` | IMPLEMENTED | signed reading preserved through reconciliation; HEAD `415ef9e` |
| `REQ-MEASURE-007` | IMPLEMENTED | `resolution_w`/`reliability`/`expected_interval_s` on `CTChannel`; `publish_reader_declarations`; HEAD `415ef9e` |
| `REQ-MEASURE-008` | IMPLEMENTED | `synchronization/aligner.py`; three timestamps preserved; HEAD `76af12d` |
| `REQ-MEASURE-009` | IMPLEMENTED | `repeated` flag and event-intake suppression; `ingestion/ha_source.py`; HEAD `76af12d` |
| `REQ-MEASURE-010` | IMPLEMENTED | `tools/dev/check_cadence.py`; resolved cadence with source; HEAD `76af12d` |

### 2.3 Measurement topology — 11

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-TOPO-001` | IMPLEMENTED | single `RelationKind` on every edge; `contracts.py:254-265`; HEAD `74b686d` |
| `REQ-TOPO-002` | IMPLEMENTED | `TopologyRelation.lifecycle` with eight states; HEAD `74b686d` |
| `REQ-TOPO-003` | IMPLEMENTED | parent/child asymmetry in `electrical/matrix.py`; HEAD `036ffbc` |
| `REQ-TOPO-004` | IMPLEMENTED | `electrical/hierarchy.py` evidence bar; HEAD `036ffbc` |
| `REQ-TOPO-005` | IMPLEMENTED | violation tolerance recorded, not raised; HEAD `036ffbc` |
| `REQ-TOPO-006` | IMPLEMENTED | residual evidence is corroborating only; HEAD `036ffbc` |
| `REQ-TOPO-007` | IMPLEMENTED | nested children and alias chains contribute once; HEAD `036ffbc` |
| `REQ-TOPO-008` | IMPLEMENTED | `TopologyVersionRecord`, `EdgeView`, deterministic diff (`electrical/topology.py:751-828`); HEAD `4bac434` |
| `REQ-TOPO-009` | PARTIAL | version retention exists, but automatic change detection/reconciliation and a runtime `ConfigurationChangeEvent` are absent; `ConfigurationChangeEvent` is contracts-only (`contracts.py:2590`); `TASK-TOPOLOGY-003` `IN_REVIEW` |
| `REQ-TOPO-010` | NOT IMPLEMENTED | no evidence of an incomplete-topology publication path; `TASK-TOPOLOGY-004` `NOT_STARTED` |
| `REQ-TOPO-011` | NOT IMPLEMENTED | `/circuits` route exists (`server.py:775`) and `Circuit` contract exists, but classification confidence publication is unverified and `TASK-TOPOLOGY-004` is `NOT_STARTED` |

### 2.4 Residuals — 8

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-RESIDUAL-001` | IMPLEMENTED | `_signed_residual` is the single subtraction; HEAD `14f7b20` |
| `REQ-RESIDUAL-002` | PARTIAL | contract/model two-way split works (`test_residual_power.py:587`), **but** the production `ResidualScope` at `audit.py:1808-1813` supplies no `window_w`, so the estimator is always `no_window_current_only` and `dynamic_w == 0.0` (`test_audit_wiring.py:1031-1064`); no noise term (see G-01) |
| `REQ-RESIDUAL-003` | IMPLEMENTED | `GET /api/v1/residuals` with `hours`/`limit` (`server.py:638-646`, route at `:778`); facade impl `main.py:2086`; HEAD `14f7b20` |
| `REQ-RESIDUAL-004` | PARTIAL | `ResidualTrend.as_dict()` publishes pattern fields; trend needs the populated window to be informative (see G-02) |
| `REQ-RESIDUAL-005` | NOT IMPLEMENTED | no residual drift event producer found; `TASK-RESIDUAL-002` `NOT_STARTED` |
| `REQ-RESIDUAL-006` | NOT IMPLEMENTED | no unknown-residual event reaching the learning ledger found; `TASK-RESIDUAL-002` `NOT_STARTED` |
| `REQ-RESIDUAL-007` | IMPLEMENTED | residual-never-redistributed safety invariant in `tests/integration/test_safety_invariants.py`; guard present in engine |
| `REQ-RESIDUAL-008` | IMPLEMENTED | `resolved=False` with `unresolved_since`, never `0 W`; HEAD `14f7b20` |

### 2.5 Disaggregation — 7

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-DISAGG-001` | REVERTED | source ledger exists only in `stash@{0}`/`stash@{1}`; `engine.infer` (`engine.py:517-524`) accepts no residual-event source and has no `sources_used`; `TASK-DISAGG-001` `FAILED` |
| `REQ-DISAGG-002` | REVERTED | joint simultaneous handling reverted with the source ledger; no `min_w`/`max_w` in HEAD engine |
| `REQ-DISAGG-003` | NOT IMPLEMENTED | `analyse_identifiability` exists in `electrical/solver.py`, but it is not published by the inference result at HEAD; `TASK-DISAGG-001` `FAILED` |
| `REQ-DISAGG-004` | REVERTED | group min/max range exists only in `stash@{1}:engine.py:1456-1542`, not at HEAD |
| `REQ-DISAGG-005` | NOT IMPLEMENTED | fabrication guard motivation (`250 W` bug) recorded in `progress.md:47`; no completed task; `TASK-DISAGG-002` `NOT_STARTED` |
| `REQ-DISAGG-006` | IMPLEMENTED | single-counting via `nested_children` in `electrical/matrix.py`; existing disaggregation tests pass |
| `REQ-DISAGG-007` | IMPLEMENTED | both reconciliation identities checked in `tests/unit/test_reconciliation_split.py`; contract at `contracts.py:1714-1718` |

**No DISAGG requirement is marked `PASS`** in the original matrix, and none is
upgraded here.

### 2.6 Device and load models — 8

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-POWER-001` | PARTIAL | `LoadShape` enum with six classes including `ADJUSTABLE`/`DELAYED`/`ALWAYS_ON` (`contracts.py:182-233`) and `InferenceResult.load_shape` (`:2039`); the *behavior* for delayed and always-on is not implemented (see G-03/G-04); HEAD `8b70933` |
| `REQ-POWER-002` | IMPLEMENTED | `ExpectedPower` levels and `DevicePowerProfile` (`models/device_profile.py:41-171`); HEAD `8b70933` |
| `REQ-POWER-003` | NOT IMPLEMENTED | measured-step attribution via `disaggregation/attachment.py` (new) was never landed; `TASK-POWER-002` `NOT_STARTED` (existing step detection is upstream evidence, not the published device attribution) |
| `REQ-POWER-004` | IMPLEMENTED | standby preserved and published; HEAD `8b70933` |
| `REQ-POWER-005` | NOT IMPLEMENTED | `HaConnectivity` contract exists but no no-HA publication path; `TASK-POWER-002` `NOT_STARTED` |
| `REQ-POWER-006` | NOT IMPLEMENTED | `HaEntityRole` contract exists; no verified API field separation; `TASK-POWER-002` `NOT_STARTED` |
| `REQ-POWER-007` | IMPLEMENTED | configured-priors row budget guard in `tests/unit/test_device_attachment.py`; existing behavior |
| `REQ-POWER-008` | IMPLEMENTED | `DeviceModelVersion`/`ModelStatus`, `Database.rollback_model` (`database.py:1223`); HEAD `8b70933` |

### 2.7 Storage and lifecycle — 12

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-STORAGE-001` | IMPLEMENTED | `storage/tiers.py`; HEAD `31f01b2` |
| `REQ-STORAGE-002` | IMPLEMENTED | per-tier retention in `config/settings.py`, `addon/run.sh`, `.env.example`; PR #6 added L2/L4 options; HEAD `31f01b2` |
| `REQ-STORAGE-003` | NOT IMPLEMENTED | `InferenceRecord` append-only contract exists, but no verified immutability path; `TASK-STORAGE-002` `NOT_STARTED` |
| `REQ-STORAGE-004` | NOT IMPLEMENTED | writer loop batches through `main.py`, but the named transaction-count test does not exist; `TASK-STORAGE-002` `NOT_STARTED` |
| `REQ-STORAGE-005` | PARTIAL | `AUDIT_TABLES` exemption in `storage/database.py`/`migrations.py` exists and prune reports zeroed counts, but the named test vehicle is `NOT RUN`; `TASK-STORAGE-002` `NOT_STARTED` |
| `REQ-STORAGE-006` | NOT IMPLEMENTED | indexes exist in migrations, query-plan assertion does not; `TASK-STORAGE-002` `NOT_STARTED` |
| `REQ-STORAGE-007` | PARTIAL | `InferenceRecord` carries the four version fields in contract/migration, but no verified publication path; `TASK-STORAGE-002` `NOT_STARTED` |
| `REQ-STORAGE-008` | IMPLEMENTED | `storage/payload.py` additive codec; HEAD `31f01b2` |
| `REQ-STORAGE-009` | IMPLEMENTED | satisfied by doing nothing (no partitioning); manual review only |
| `REQ-STORAGE-010` | NOT IMPLEMENTED | startup integrity/recompute behavior exists in `main.py`, but the named E2E restart test is unverified; `TASK-STORAGE-003` `NOT_STARTED` |
| `REQ-STORAGE-011` | PARTIAL | `migrations.py` is forward-only/idempotent; no full prior-version migration matrix test; `TASK-STORAGE-003` `NOT_STARTED` |
| `REQ-STORAGE-012` | PARTIAL | `tools/audit/replay_capture.py` and `tests/simulation/test_replay_capture.py` exist and the replay tests pass, but no T## scenario-based identical-window replay gate; `TASK-STORAGE-003` `NOT_STARTED` |

### 2.8 Event and inference — 9

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-EVENT-001` | IMPLEMENTED | named event kinds; HEAD `8ee3973` |
| `REQ-EVENT-002` | IMPLEMENTED | automation groups withheld once; HEAD `8ee3973` |
| `REQ-EVENT-003` | IMPLEMENTED | `lag_exceeded` withheld; HEAD `8ee3973` (note: delayed *device models* are separate — G-04) |
| `REQ-EVENT-004` | IMPLEMENTED | cadence-derived window on the audit path with published source; HEAD `8ee3973` |
| `REQ-EVENT-005` | NOT IMPLEMENTED | per-reader effect vector not published; `TASK-EVENT-002` `NOT_STARTED` |
| `REQ-EVENT-006` | NOT IMPLEMENTED | late classification path unverified; `TASK-EVENT-002` `NOT_STARTED` |
| `REQ-EVENT-007` | IMPLEMENTED | `event_id` primary key + `INSERT OR IGNORE` in `migrations.py:336`/`database.py:533`; duplicate suppression works |
| `REQ-EVENT-008` | IMPLEMENTED | `INSUFFICIENT_DATA` isolation status; no result from incomplete group |
| `REQ-EVENT-009` | IMPLEMENTED | `occurred_at` never mutated in `EventIsolation` |

### 2.9 Learning — 7

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-LEARNING-001` | NOT IMPLEMENTED | pipeline stage exists, isolated-event sample creation unverified; `TASK-LEARNING-001` `NOT_STARTED` |
| `REQ-LEARNING-002` | NOT IMPLEMENTED | `SampleRejection` reasons in contract; ledger publication unverified |
| `REQ-LEARNING-003` | IMPLEMENTED | `learning/promotion.py`, `test_learning.py::test_candidate_promotes_only_when_it_beats_active_model`; existing behavior |
| `REQ-LEARNING-004` | NOT IMPLEMENTED | no unknown-residual event entering the pipeline; `TASK-LEARNING-002` `NOT_STARTED` |
| `REQ-LEARNING-005` | IMPLEMENTED | `electrical/factors.py` interval resolution; `test_factors.py` passes |
| `REQ-LEARNING-006` | PARTIAL | `EffectiveConfig` is a resolution point but has no per-device learning-threshold override; `TASK-LEARNING-001` `NOT_STARTED` |
| `REQ-LEARNING-007` | NOT IMPLEMENTED | `measurement_disagrees_with_model` flag is appended but no test asserts precedence; `TASK-LEARNING-002` `NOT_STARTED` |

### 2.10 Confidence and identifiability — 7

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-CONFIDENCE-001` | IMPLEMENTED | `ConfidenceVector` has no aggregate/`__float__`; contract shape verified |
| `REQ-CONFIDENCE-002` | IMPLEMENTED | `confidence_is_calibrated = false` in contract; no probability rendering in panel |
| `REQ-CONFIDENCE-003` | IMPLEMENTED | five existing dimension names; `None` distinct from `0.0` |
| `REQ-CONFIDENCE-004` | IMPLEMENTED | `ConfidenceTier`/`TIER_RANK`, `disaggregation/tiers.py:126`; existing electrical tests pass |
| `REQ-CONFIDENCE-005` | NOT IMPLEMENTED | topology/residual dimensions are still declared-but-unpopulated (`OD-34` open); `TASK-CONFIDENCE-002` `NOT_STARTED` |
| `REQ-CONFIDENCE-006` | PARTIAL | association evidence carries stats/supporting/conflicting, but published measurement quality/freshness/residual uncertainty is unverified |
| `REQ-CONFIDENCE-007` | PARTIAL | edge confidence cap on candidates exists in association logic; per-edge dimension publication unverified |

### 2.11 Mapping matrix — 7

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-MAPPING-001` | NOT IMPLEMENTED | no `disaggregation/mapping.py`; no derivation path (G-07) |
| `REQ-MAPPING-002` | NOT IMPLEMENTED | no cardinality model at runtime |
| `REQ-MAPPING-003` | NOT IMPLEMENTED | `DeviceReaderMappingCell` contract at `contracts.py:2501` is unused; no `/mapping` route |
| `REQ-MAPPING-004` | NOT IMPLEMENTED | no unknown-load entry in a matrix |
| `REQ-MAPPING-005` | NOT IMPLEMENTED | no matrix refresh path |
| `REQ-MAPPING-006` | NOT IMPLEMENTED | no runtime complexity/confidence separation for cells |
| `REQ-MAPPING-007` | NOT IMPLEMENTED | no coverage block |

### 2.12 Dynamic topology and change — 7

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-CHANGE-001` | NOT IMPLEMENTED | no runtime `ConfigurationChangeEvent` (contracts-only at `contracts.py:2590`); `TASK-CHANGE-001` `NOT_STARTED` |
| `REQ-CHANGE-002` | NOT IMPLEMENTED | no suspected-change hypothesis path |
| `REQ-CHANGE-003` | PARTIAL | `topology_version`/`model_version` exist on `InferenceRecord`; `reader_configuration_version`/`device_mapping_version` do not |
| `REQ-CHANGE-004` | PARTIAL | topology versions are immutable and byte-identical at HEAD (`4bac434`), but the read-as-of query is unverified |
| `REQ-CHANGE-005` | NOT IMPLEMENTED | no uncertainty-preserved change path |
| `REQ-CHANGE-006` | PARTIAL | `Database.rollback_model` exists for models; topology config rollback is not a versioned change |
| `REQ-CHANGE-007` | NOT IMPLEMENTED | no reconciliation of inference history after a change |

### 2.13 User interface — 13

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-UI-001` | PARTIAL | `/api/v1/topology` exposes the channel list; panel inventory fields unverified; `TASK-UI-001` `NOT_STARTED` |
| `REQ-UI-002` | PARTIAL | quality vocabulary in `STRINGS` (`app.js:264-292`); three-timestamp/provenance rendering unverified |
| `REQ-UI-003` | PARTIAL | topology view exists (`#/topology`), but per-edge status/confidence drawing unverified |
| `REQ-UI-004` | PARTIAL | panel shows `unknown_residual_w` scalar (`app.js:826-995`); no baseline/dynamic/noise decomposition view |
| `REQ-UI-005` | NOT IMPLEMENTED | no per-device model-version detail view (G-09) |
| `REQ-UI-006` | NOT IMPLEMENTED | no mapping view (no `/mapping` backend) |
| `REQ-UI-007` | PARTIAL | `STRINGS` has measured/estimated labels; allocated/unknown per-device labeling unverified |
| `REQ-UI-008` | PARTIAL | panel reads `individually_identifiable` groups (`app.js:3284-3410`), but no min/max range publication (no backend `min_w`/`max_w`) |
| `REQ-UI-009` | NOT IMPLEMENTED | no event evidence view |
| `REQ-UI-010` | NOT IMPLEMENTED | no version history or suspected-change view |
| `REQ-UI-011` | PARTIAL | topology confirm route exists and is exercised (`tests/integration/test_confirm_topology_e2e.py`); corrections/withdrawals unverified |
| `REQ-UI-012` | NOT IMPLEMENTED | no storage/processing health view for tiers |
| `REQ-UI-013` | PARTIAL | view-state vocabulary exists in panel; per-view empty/error/stale behavior unverified |

### 2.14 Deployment — 8

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-DEPLOY-001` | IMPLEMENTED | `Dockerfile`, GHCR release workflow, `scripts/validate_addon.sh --no-build` green at HEAD; PR #6 merged |
| `REQ-DEPLOY-002` | IMPLEMENTED | option table in `addon/run.sh` and `addon/config.yaml`; PR #6 added L2/L4 horizons; validation green |
| `REQ-DEPLOY-003` | IMPLEMENTED | startup migrations idempotent in `storage/migrations.py`; `test_migrations.py` passes |
| `REQ-DEPLOY-004` | PARTIAL | backup commands documented in `docs/deployment.md:329-344`; no verified E2E backup/restore assertion |
| `REQ-DEPLOY-005` | NOT IMPLEMENTED | deployment procedure exists at `docs/deployment.md` but the required version-stamped final deployment report (17) does not |
| `REQ-DEPLOY-006` | NOT IMPLEMENTED | no `scripts/smoke_test.sh`; watchdog probe is deliberately separate |
| `REQ-DEPLOY-007` | NOT IMPLEMENTED | rollback documented, no rehearsal record (17) |
| `REQ-DEPLOY-008` | PARTIAL | `/metrics` publishes uptime/status/HA/queue metrics (`main.py:2946-2953`); risk-to-metric mapping unverified |

### 2.15 Testing — 10

| ID | Status | Evidence |
| --- | --- | --- |
| `REQ-TEST-001` | PARTIAL | unit tests exist and pass (2678), but per-rule red-then-green records are not published |
| `REQ-TEST-002` | IMPLEMENTED | `tests/integration/test_engine_api_contract.py` and `tests/end_to_end/` pass at HEAD |
| `REQ-TEST-003` | PARTIAL | `tools/simulator/labhouse.py` has seeded, deterministic strata and `test_simulator_fixtures.py` passes; there is **no** `tools/simulator/scenarios.py` and no named T01–T15 builders |
| `REQ-TEST-004` | NOT IMPLEMENTED | no assertion that every §4 scenario id has a builder; `TASK-TEST-003` `NOT_STARTED` |
| `REQ-TEST-005` | PARTIAL | `tests/simulation/test_replay_capture.py` passes, but nine live-capture tests skip without local DB (`OD-36`) |
| `REQ-TEST-006` | IMPLEMENTED | full unit+integration suite green at HEAD (2678 passed); `mypy engine` baseline recorded in `docs/final_validation_report.md` |
| `REQ-TEST-007` | IMPLEMENTED | `tools/dev/negverify_nesting.py` exists; existing guards proven |
| `REQ-TEST-008` | PARTIAL | `test_migrations.py` passes; no every-prior-version migration matrix |
| `REQ-TEST-009` | PARTIAL | `test_health_probe_cost.py` and `test_nesting_perf.py` pass but assert existing budgets only; new-path budgets (mapping projection, topology diff, residual history) not asserted |
| `REQ-TEST-010` | IMPLEMENTED | `tests/simulation/test_accuracy.py` passes; `test_replay_capture.py::test_replay_reports_no_per_device_error_on_a_real_house`; limitation stated in docs |

### 2.16 Status totals (inspection view)

| Status | Count |
| --- | --- |
| IMPLEMENTED | 57 |
| PARTIAL | 30 |
| NOT IMPLEMENTED | 40 |
| REVERTED | 3 |
| **Total** | **130** |

(Counts are derived from the table above; they are an inspection assessment and
do not change the `14-…` matrix's orchestrator-gate `Verification Result`
column.)

---

## 3. The 13 reported gaps — resolved with code evidence

The 13 items below correspond to the inspection list in the sprint mandate;
compound items from the brief are split so each resolution is independently
checkable. Statuses: **CONFIRMED** = a real gap exists at HEAD; **PARTIAL** =
some capability exists but is incomplete; **NOT-A-GAP** = the reported gap is
not present.

| # | Reported gap | Resolution | Evidence (file:line) |
| --- | --- | --- | --- |
| G-01 | Residual noise term on the `ResidualPower` model | **CONFIRMED** | `contracts.py:840-872` has `baseline_w`/`dynamic_w` but no `noise_w`/`noise_floor_w`; `test_residual_power.py:974-989` (`test_the_noise_component_is_not_publishable_without_a_contract_change`) asserts their absence. A `ResidualDecompositionRecord.noise_w` exists at `contracts.py:2341` but has **no runtime producer** (contracts.py-only). Measured noise floor is published as `channel_noise_floor_w` (`main.py:1241`) but never carried into a residual. |
| G-02 | Residual baseline window population on the production audit/ingestion path | **CONFIRMED** | `audit.py:1808-1813` constructs `ResidualScope` with scope_id/ct_id/basis/contributions but **no `window_w`**; `test_audit_wiring.py:1031-1064` pins `no_window_current_only` and `dynamic_w == 0.0`. No `window_w` reference exists in `engine/aggregation/**` or `engine/ingestion/**`. |
| G-03 | Adjustable power models / non-linear response | **PARTIAL** | `LoadShape.ADJUSTABLE` label (`contracts.py:220`) and `InferenceResult.load_shape` (`:2039`); a measured dimming curve is implemented (`configuration.py:150-153,252-292,882-891`; `models/dimmable.py:31-88`, monotonic PAVA + linear interpolation) and a `measured_dimming_curve` note is published (`engine.py:1472`). But the product-facing "non-linear response" is limited to dimmable lights; generic adjustable load shapes have no response model. |
| G-04 | Delayed power models | **CONFIRMED** | `LoadShape.DELAYED` (`contracts.py:226`) is a label only. Its docstring points to `response_delay_s` (`contracts.py:208`) and `always_on` (`:210`), but the only occurrences of those names in the engine are those docstrings — no field, no model, no configuration path. `OD-46`/`OD-47` are still `OPEN` (`13-…:568-569`). This is a **design decision** (recorded as unresolved, not guessed). |
| G-05 | Joint inference (`sources_used`) | **CONFIRMED** | `disaggregation/engine.py:517-524` `infer(window, *, device_states, models, topology)` has no residual-event input and no source ledger. `sources_used`/`sources_unused` appear only in `stash@{0}:engine.py:431-644` and `stash@{1}:engine.py:570-741`, not in HEAD. `TASK-DISAGG-001` is `FAILED`. |
| G-06 | `min_w`/`max_w` group estimates | **CONFIRMED** | No `min_w`/`max_w` group estimate in HEAD engine or `InferenceResult`. The implementation exists only in `stash@{1}:engine.py:1456-1542`. Ambiguity diagnostics do exist at HEAD (`engine.py:569` `diagnostics["ambiguity"]`), so the group state is detected but not published as a range. |
| G-07 | Device↔Reader Mapping Matrix | **CONFIRMED** | No `engine/wattmosaic_engine/disaggregation/mapping.py`, no `/mapping` route (`server.py:759-785`), no runtime use of `DeviceReaderMappingCell` (`contracts.py:2501`). `TASK-MAPPING-001`/`002` `NOT_STARTED`. |
| G-08 | Topology self-healing (change detection/reconciliation) | **PARTIAL** | Versioned topology object + deterministic diff landed (`electrical/topology.py:751-828`, `4bac434`) and `REQ-TOPO-008` is `PASS`. But no automatic change detection, reconciliation of inference history, or runtime `ConfigurationChangeEvent` (`contracts.py:2590`, contracts-only). `TASK-TOPOLOGY-003` `IN_REVIEW`. |
| G-09 | `addon/ui/` contents and exposure of new backend data | **CONFIRMED** | `addon/ui/` has four files (`app.js` 3941 lines, `app.css` 966, `index.html` 83, `mark.svg` 6); five views (`app.js:740-755`: overview/devices/device/quality/diagnostics; topology reachable via hash). The UI renders the scalar `unknown_residual_w` (`app.js:826-995`) and group identifiability flags (`:3284-3410`), but no mapping matrix, version history, suspected changes, per-device model versions, or residual baseline/noise decomposition. The four new contract shapes (`UnknownLoadProfile`, `ResidualDecompositionRecord`, `DeviceReaderMappingCell`, `ConfigurationChangeEvent`) have no runtime producer, so there is no new backend data to expose yet. |
| G-10 | Unified end-to-end workflow | **CONFIRMED** | `scripts/` contains only `generate_icons.py`, `package_addon.sh`, `validate_addon.sh`; no single `run`/`workflow` entry point, no `smoke_test.sh` (`TASK-DEPLOY-002` `NOT_STARTED`). `tests/end_to_end/test_full_inference_chain.py` exercises a chain in-process but is not a deployable workflow. |
| G-11 | Performance / replay / migration-matrix / UI verification tests | **PARTIAL** | Test files exist and pass: `tests/unit/test_health_probe_cost.py`, `test_nesting_perf.py`, `test_migrations.py`, `tests/simulation/test_replay_capture.py`, `tests/unit/test_panel.py`. However there is no T01–T15 builder set (`tools/simulator/scenarios.py` absent; LabHouse uses `strata`), no every-prior-version migration matrix, `test_panel.py` tests only shell/asset/CSP serving (no data-rendering assertions), and the direct-panel check is `tools/dev/check_panel.py` static. |
| G-12 | `16-final-verification-report.md` | **CONFIRMED** | File absent. `README.md:63-65` names `16-final-verification-report.md` and `17-deployment-report.md` as forthcoming. `docs/verification_report.md` (2026-11-11) and `docs/final_validation_report.md` (2026-11-13) are earlier-phase reports for contracts 1.6.0 / schema v10, not the load-disaggregation final report (current contract 2.1.0). |
| G-13 | `17-deployment-report.md` | **CONFIRMED** | File absent (same `README.md:63-65` reference). `docs/deployment.md` documents the procedure but is not the version-stamped deployment/rollback report this requirement names. |

### 3.1 Additional gaps found by independent inspection

These were not in the 13 reported items but are material to completion:

1. **Contract-only models (cross-cutting).** `UnknownLoadProfile` (`contracts.py:2417`), `ResidualDecompositionRecord` (`:2303`), `DeviceReaderMappingCell` (`:2501`) and `ConfigurationChangeEvent` (`:2590`) were added in `bfb9938` but have **zero runtime references outside `contracts.py`**. They are frozen shapes without producers or consumers. Any completion task that uses them must first define the additive contract bump and the producer/consumer path.
2. **Two open decisions block model classes.** `OD-46` (`ALWAYS_ON` as flag vs member) and `OD-47` (`DELAYED` as member vs orthogonal parameter) are `OPEN` (`13-…:568-569`). Resolving them needs a product decision; this analysis does not guess. Recorded as unresolved in §6.
3. **Four version identifiers incomplete.** `REQ-CHANGE-003` needs `reader_configuration_version` and `device_mapping_version`; only `topology_version` and `model_version` exist on `InferenceRecord` (`contracts.py:893`, `:1650`, `:1254`).
4. **Spec-set status count will change.** Adding documents 18 and 19 with the status token raises `REQ-FOUNDATION-001`'s measured 55 to 57; the `spec_status` gate reads first status lines, so both new files carry the token. `README.md:63-65` still names 16/17 as forthcoming and is now stale for 18/19 (recorded, not silently rewritten outside scope).
5. **Release tag lag.** HEAD is `Release 0.2.1-dev.37` but the latest tag is `v0.2.1-dev.36`; the deployed smoke record should state which image tag was actually pulled.

---

## 4. Verification-state assessment

| Gate / vehicle | Recorded state | Where | Assessment |
| --- | --- | --- | --- |
| Unit + integration suite | 2678 passed, 15 skipped at HEAD `4cd3545`/`e54ce5d` | `tasks/progress.md:185`; Phase A re-run exit 0 | **Current** — re-run at HEAD during Phase A |
| Simulation suite | 122 passed, 9 errors (pre-existing `test_real_topology.py`), 0 failures | `tasks/progress.md:186` | **Known limitation** — nine live-capture skips/errors carry `OD-36` |
| Lint (`ruff check engine tools tests`) | clean at `4cd3545` | `tasks/progress.md:187` | Historically green; not re-run in Phase A |
| Type check (`mypy engine/wattmosaic_engine`) | 109 errors (baseline) | `tasks/progress.md:188` | **Baseline**, not zero; delta is the meaningful measure |
| Schema sync | no drift | `tasks/progress.md:189` | Green at implementation close |
| Secret scan | clean | `tasks/progress.md:190` | Green at implementation close |
| Addon validation | 71 passed, 0 failed, 3 skipped | `tasks/progress.md:191` | Green; PR #6 option table included |
| Full `tools/dev/verify.py` run | 12/12 required gates PASS at 2026-11-11 | `docs/verification_report.md` | **Stale** — predates contract 2.1.0 and the implementation commits |
| Final verification report (16) | **missing** | `README.md:63` | **MISSING** |
| Deployment report (17) | **missing** | `README.md:63-65` | **MISSING** |
| Deployed smoke test | **no record** | no `scripts/smoke_test.sh` | **MISSING** — G-10/G-13 |
| Performance budgets for new paths | **not asserted** | `test_health_probe_cost.py`, `test_nesting_perf.py` cover existing paths only | **MISSING** for mapping/topology-diff/residual-history |
| Replay under same algorithm version (scenario-based) | partial | `test_replay_capture.py` passes; T## gate absent | **PARTIAL** |
| Migration matrix across prior versions | **not asserted** | `test_migrations.py` passes current-path only | **MISSING** |
| UI data-rendering verification | **not asserted** | `test_panel.py` shell/asset tests only | **MISSING** |

---

## 5. Backward-compatibility constraints — must not break

1. **Contract version and additive rule.** `CONTRACT_VERSION = "2.1.0"` (`contracts.py:32`); `Model` uses `model_config = ConfigDict(extra="forbid", use_enum_values=True)` (`contracts.py:776`). Every new field must be optional with a neutral default so a pre-change payload still validates (`REQ-FOUNDATION-002`). Any bump requires regenerating `data/schemas/` (**30 JSON files** at HEAD) with `tools/dev/export_schemas.py` and a clean `git diff --exit-code data/schemas` (`REQ-FOUNDATION-003`).
2. **Public contract models and enums.** Existing `Topology`, `InferenceResult`/`InferenceBatch`, `Reconciliation`, `InferenceRecord`, `ResidualPower`, `ConfidenceVector`, `ConfidenceTier`, `PowerState`, `LoadShape` and the new-but-unused `DeviceReaderMappingCell`, `ConfigurationChangeEvent`, `ResidualDecompositionRecord`, `UnknownLoadProfile` names, field names and default values are frozen surfaces; do not retype, remove or make required.
3. **Engine entry point.** `DisaggregationEngine.infer(window, *, device_states, models, topology) -> InferenceBatch` (`engine.py:517-524`) and its `window`/`topology` coercion contract; new sources must be added additively (e.g. optional keyword with default), never by changing the positional signature. `disaggregate`-style callers and `facade.EngineFacade` protocol methods (`facade.py:94-148`) must keep their names and defaults.
4. **API surface.** Existing routes at `server.py:760-784` (`/health`, `/status`, `/snapshot`, `/devices`, `/topology` + PUT/confirm/nesting/tree, `/quality`, `/diagnostics`, `/circuits`, `/relations`, `/devices/{id}/records`, `/residuals`, `/isolation`, `/models/{id}`, `/commands`, `/discover/placements`, `/export/{id}`, `/metrics`, `/api/v1` contract header, 401/400 error shape) must not change path or response envelope. New read paths are additive.
5. **Storage signatures.** `Database.checkpoint()` (`:171`), `save_topology()` (`:223`), `latest_inference_results()` (`:596`), `rollback_model()` (`:1223`), `prune()` (`:1314`), `stats()` (`:1373`), and `apply_migrations`/`LATEST_VERSION` in `storage/migrations.py` keep names and defaults; migrations remain forward-only, additive and idempotent (schema version 11 at HEAD). `AUDIT_TABLES` retention exemption must be preserved.
6. **Addon option surface.** `addon/run.sh` option table ⇄ `addon/config.yaml` ⇄ `config/settings.py` ⇄ `.env.example` must stay consistent (`scripts/validate_addon.sh --no-build`, 71 passed); the two cadence defaults are independent by design (`15c9a3f`).
7. **Safety invariants.** Residual never redistributed onto devices; no fabricated per-device number; no `abs()`/`max(0,…)` on the signed residual; "missing is never zero"; measurement outranks a model. These are asserted by existing tests that must stay green.
8. **Spec-set status invariant.** Every file of the set carries `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION` on its first status line; the count rises with each added file (55 → 57 with documents 18/19).

---

## 6. Unresolved items (facts a person must decide)

| Item | Why unresolved | Impact |
| --- | --- | --- |
| `OD-46` / `OD-47` | Whether `ALWAYS_ON` and `DELAYED` are enum members, flags or orthogonal parameters is still `OPEN` in `13-…:568-569` | Blocks the `DELAYED`/`ALWAYS_ON` *behavior* of `TASK-POWER-003` (G-04). The enum labels already exist and must not break. |
| `OD-34` | Whether the `topology`/`residual` confidence dimensions stay unpopulated in V1 | `REQ-CONFIDENCE-005`; the traceability matrix assumes the requirement wins, but the decision is not recorded as resolved |
| Deployment image tag | HEAD is `dev.37` but the latest reachable tag is `v0.2.1-dev.36` | The deployment report must state the exact tag pulled; not inferable from the repository |
| Deployed smoke evidence | No deployed installation or smoke log is present in the repository | "Deployed smoke" verification is `MISSING`, not a pass |

---

## 7. Scope compliance statement

This Phase A inspected only: `docs/specs/load-disaggregation/**` (documents), and
read-only inspection of `engine/**`, `addon/**`, `tests/**`, `tools/**`, Git state.
No Phase B feature was implemented; no code, test, contract, schema, version,
push or original spec text was changed. The companion `19-completion-plan.md`
proposes tasks but starts none. The original spec is preserved; the only
documentation change is the addition of documents 18 and 19 and the explicit
extensions to `progress.md` and `14-requirement-traceability.md` required by the
mandate. This analysis is **non-exhaustive by design** and derived from direct
code inspection.

---

## Related documents

- [`01-requirements.md`](01-requirements.md) — the 130 normative requirements.
- [`14-requirement-traceability.md`](14-requirement-traceability.md) — the original task/requirement matrix (unchanged rows).
- [`15-implementation-plan.md`](15-implementation-plan.md) — the original 36-task plan.
- [`19-completion-plan.md`](19-completion-plan.md) — the dependency-ordered Phase B plan derived from this analysis.
- [`tasks/progress.md`](tasks/progress.md) — the authoritative task status tracker.
