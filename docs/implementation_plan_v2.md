# WattMosaic — Event-Driven Disaggregation & Topology Discovery: Implementation Plan

**Status:** approved plan (Phase 2 gate). Supersedes nothing; extends the existing system.
**Baseline:** `0.2.1-dev.24`, contract `1.5.0`, schema `LATEST_VERSION = 8`, **2177 tests collected / 2168 pass / 9 skipped** (the 9 are live-capture gates, unrunnable without `data/wattmosaic.db`).
**Inputs:** `docs/assessment_gap_analysis.md`, `docs/domain_requirements_inventory.md`, `docs/test_scenario_matrix.md`.
**Date:** 2026-11-13.

---

## 0. Governing decision — extend, do not redesign

The assessment found a production-shaped engine (one asyncio process, one SQLite writer,
supervised loops, read-only HA client, additive versioned contracts, forward-only migrations).
The prompt's rule 1 ("existing architecture takes precedence") and rule 19 ("no new
databases/frameworks/services") therefore bind hard.

**No new runtime dependencies. No new database. No new service. No message bus.**
Everything ships as:

1. **additive** contract fields with safe defaults (1.5.0 readers keep validating),
2. **migration 9** (additive tables/columns, zero non-null backfill),
3. **new modules** inside existing packages,
4. **new tests** in existing suites,
5. **simulator extensions** inside `tools/simulator/`.

### The 1.5.0 compatibility rule (non-negotiable)

Every contract change is *additive and optional*. Existing 2177 tests must stay green after
every wave. A subagent that breaks an existing test has not finished.

---

## 1. What already satisfies the requirement (do not rebuild)

| Requirement | Reused component | Evidence |
| --- | --- | --- |
| HA discovery / entity filtering | `ingestion/{discovery,candidate_entities,ha_source}.py` | assessment §2 |
| Irregular CT sampling, original timestamps | `CTChannel`, `Measurement.state_timestamp` | `contracts.py:277-390` |
| Double-pole 240 V pairing | `electrical/direct.py`, `main_power.py` | `test_electrical.py` |
| Alias/duplicate CT handling | `canonical_channel_id`, `alias_of_ct_id` | `topology.py:641` |
| No-aliasing / no double count | `electrical/nesting.py`, `matrix.py` | `test_nesting.py` |
| Signed residual, never redistributed | `engine.py:2177`, `contracts.py:827` | assessment §7 |
| Ambiguity → unknown | `models/base.py`, `disaggregation/engine.py` | `test_disaggregation.py` |
| Confidence tiers 0–4 | `ConfidenceTier` | `contracts.py:226` |
| Synthetic simulator + ground truth separation | `tools/simulator/*` | `test_simulator.py` |
| Learning sample/rejection ledger | `learning/sample_filter.py` | `test_learning.py` |

**Reuse rule:** new modules import existing contracts and helpers. They must not
re-implement window statistics, alignment, alias resolution or quality scoring.

---

## 2. Gap → work package mapping

| # | Gap (assessment id) | Work package | Wave |
| --- | --- | --- | --- |
| 1 | No `Circuit` concept, no dedicated/shared/partial/unknown (§16) | **WP6** | 2 |
| 2 | No measurement-basis distinction (physical vs reported vs factored vs estimated) (§9) | **WP3** | 2 |
| 3 | Factor is a bare float: no provenance, no validity interval, no history (§9) | **WP3** | 2 |
| 4 | No `ISOLATED`/`CONTAMINATED`/`INSUFFICIENT_DATA`; offset/window not enforced (§11) | **WP4** | 2 |
| 5 | Dead concurrency guard; multi-entity→one-device dedup unenforced (§11) | **WP4** | 2 |
| 6 | Device→CT association scattered over 3 fields, no evidence, no validity (§14) | **WP5** | 2 |
| 7 | No residual baseline/dynamic split, no residual history (§17-18) | **WP7** | 2 |
| 8 | Parent/child proposed only for feeds; overlap vs association vs parenthood not typed (§19) | **WP8** | 2 |
| 9 | Single blended confidence score; no independent dimensions (§20) | **WP1** | 1 |
| 10 | Inference rows are not versions; retention deletes audit trail (§21) | **WP1** | 1 |
| 11 | `_device_history` queries `since=utcnow()` → always empty (latent bug) | **WP9** | 3 |
| 12 | Simulator has no factor/×2 concept, no class strata (§22) | **WP2** | 1 |

---

## 3. Work packages

Each WP is TDD: **fixtures and failing tests first**, then minimal implementation, then
refactor. A WP is `VERIFIED` only when the orchestrator re-runs the suite.

### WP1 — Contracts, persistence, versioned inference, confidence vector (critical path)
**Owner:** Subagent "Contracts & Storage". **Scope:** `engine/wattmosaic_engine/contracts.py`,
`storage/migrations.py`, `storage/database.py`, `tests/unit/test_contracts.py`,
`tests/unit/test_storage_config.py`, new `tests/unit/test_inference_records.py`,
`data/schemas/**`.

Deliver:
- `CircuitClass`, `MeasurementBasis`, `ConfidenceDimension`, `ResidualKind`, `RelationKind`,
  `HaEntityRole`, `IsolationStatus`, `CircuitClassStatus` enums.
- Models: `Circuit`, `TopologyRelation`, `ConfidenceVector` (no aggregate, no arithmetic),
  `ResidualPower`, `MeasurementFactor`, `HaEntityBinding`, `InferenceRecord`, `EventIsolation`.
- Additive fields on `CTChannel`, `Measurement`, `InferenceResult`, `Reconciliation`,
  `DeviceConfiguration`, `Topology`.
- **Migration 9**: `circuits`, `topology_relations`, `inference_records`,
  `ct_measurement_factors`, `ha_entity_bindings`, `residual_power`; indexes on
  `(device_id, timestamp)` and `(device_id, revision)`.
- `prune()` must **exempt** `inference_records`, `ct_measurement_factors`,
  `topology_relations`, `ha_entity_bindings` from raw retention (they are audit state, not raw
  samples). `inference_results`/`reconciliation_results` keep existing pruning.
- Validation rules 1–7 from `docs/domain_requirements_inventory.md` §4.4.
- `CONTRACT_VERSION` → `1.6.0`; regenerate `data/schemas/` with the existing generator.
- Tests: every new enum round-trips; `ConfidenceVector` has no `__float__`; `InferenceRecord`
  rejects `previous_record_id` pointing forward; `MeasurementFactor` rejects overlapping
  intervals for one `ct_id`; migration 8→9 is additive and idempotent; retention exempts audit
  tables.

**Acceptance:** all 2177 existing tests pass; schema-sync gate passes; new tests fail first.

### WP2 — Simulator fidelity & reusable fixtures (independent, parallel with WP1)
**Owner:** Subagent "Test Engineering". **Scope:** `tools/simulator/**`,
`tests/simulation/**`, `tests/conftest.py`, new `tests/fixtures/labhouse/**`,
`docs/testing.md`.

Deliver:
- Simulator support for **Refoss factor channels**: a 240 V device whose L1 leg is physically
  metered and whose reported power is (or is not) multiplied by ×2, with `factor` recorded as
  ground truth separate from observation.
- Named scenario strata so every matrix row in `docs/test_scenario_matrix.md` can be built from
  a documented builder: `dedicated`, `shared`, `standby`, `unknown`, `dynamic`, `factor2`,
  `noisy_legs`, `unbalanced`.
- A reusable `LabHouse` fixture builder with **strict ground-truth separation** (algorithms get
  observations only; a test may assert against truth).
- The **mandatory Hot Tub regression fixture** (§9 of the brief) as a permanent dataset:
  HA power 0→500 W, CT1 1000→1250 W, CT2 900→1150 W, factor ×2, plus variants factor=1,
  factor change, missing factor, multiple factor CTs, independent CTs, noisy/unbalanced legs,
  inconsistent HA/CT evidence.
- Tests: ground truth never leaks into engine input (assert by key inspection); all builders
  produce deterministic output for a fixed seed.

**Acceptance:** existing `tests/simulation/**` pass unchanged; new builders deterministic.

### WP3 — Refoss factor semantics & measurement basis
**Owner:** Subagent "Factor & Measurement Basis". **Depends on:** WP1.
**Scope:** new `engine/wattmosaic_engine/electrical/factors.py`,
`tests/unit/test_factors.py`, minimal wiring in `electrical/topology.py`.

Deliver:
- `FactorRegistry`: resolve the factor in force at an arbitrary timestamp from
  `MeasurementFactor` intervals; never apply today's factor to a historical sample.
- `classify_basis()`: return `CT_MEASURED` / `MONITOR_REPORTED` / `FACTOR_ADJUSTED` /
  `ESTIMATED_APPLIANCE_TOTAL` / `UNKNOWN` from explicit evidence, **never** from `phase` or
  `ct_type`.
- Explicit "factor ownership" resolution: which CT the factor belongs to, whether the reported
  power already includes it (`applied_by="monitor"`), and refusal to double-apply.
- When semantics are unknown: `MeasurementBasis.UNKNOWN` + lowered confidence, not a guess.
- `scale_w` delegates to the registry while preserving its "apply once on the way out"
  invariant.
- Tests: the full Hot Tub matrix; factor change mid-history reconstructs the correct basis per
  sample; missing factor → UNKNOWN; incorrect metadata → UNKNOWN + reason; monitor-applied
  factor is not multiplied again; two factor CTs on one appliance are not summed as
  independent loads.

**Acceptance:** Hot Tub fixture matches expected interpretation exactly; no double-application.

### WP4 — Event isolation & physical-device event identity
**Owner:** Subagent "Event Processing". **Depends on:** WP1.
**Scope:** new `engine/wattmosaic_engine/disaggregation/isolation.py`,
`tests/unit/test_event_isolation.py`, `learning/sample_filter.py` (guard repair),
`learning/thresholds.py` (per-device config).

Deliver:
- `EventIsolator` producing `EventIsolation` with status `ISOLATED` / `CONTAMINATED` /
  `INSUFFICIENT_DATA`, configurable `analysis_offset_s` and `isolation_window_s` per device
  class, and recording: original timestamp (never mutated), receive timestamp, window bounds,
  competing events, and the in-window power-analysis check.
- Physical-device identity: multiple HA entities of one device collapse to **one** device event
  (uses existing `DeviceConfiguration.entity_bindings` from WP1); never N independent events.
- Repair the dead concurrency guard: `SampleEvidence` must be constructed on the production
  path so `SampleRejection.CONCURRENT_EVENTS` can actually fire.
- Contaminated events are retained but excluded from clean V1 attribution evidence.
- Tests: all Group G rows in the matrix, including out-of-order arrival, differing
  timestamp/receive time, overlapping groups, offset config change, missing CT samples.

**Acceptance:** Group G fully covered; no timing-dependent flake (inject a clock).

### WP5 — Device→CT association discovery
**Owner:** Subagent "Association Discovery". **Depends on:** WP1, WP4 (clean events), WP3 (basis).
**Scope:** new `engine/wattmosaic_engine/disaggregation/association.py`,
`tests/unit/test_association.py`.

Deliver:
- Evidence accumulation from isolated ON/OFF events: repeated response, magnitude, lag,
  stability, basis compatibility, conflicting responses.
- Output `TopologyRelation(kind=DEVICE_CT_ASSOCIATION)` records with status, per-relation
  confidence, supporting **and** conflicting evidence, algorithm version, validity interval.
- Many-to-many: one device→many CTs, one CT→many devices. Never 1:1.
- Explicit refusal: **equal deltas do not prove association**; **association does not imply
  parenthood**; correlation alone is insufficient.
- When two devices always switch together → `AMBIGUOUS`, no independent estimates invented.
- Tests: Group C + Group I rows; the Hot Tub fixture must yield "CT1 and CT2 both respond",
  not "two independent loads".

**Acceptance:** no false association on the "independent CTs with similar responses" fixture.

### WP6 — Circuit classification (dedicated / shared / partial / unknown)
**Owner:** Subagent "Circuit Discovery". **Depends on:** WP1, WP5.
**Scope:** new `engine/wattmosaic_engine/disaggregation/circuits.py`,
`tests/unit/test_circuits.py`.

Deliver:
- `Circuit` candidates with `classification` + separate `classification_confidence` +
  `classification_status` (candidate/confirmed) and evidence.
- `DEDICATED` requires: repeated isolated events, stable response, device power explains CT
  behaviour, no independent contributions, long observation, no residual contradiction.
- `SHARED` requires ≥2 independently evidenced devices.
- `PARTIALLY_UNDERSTOOD` = ≥2 known devices **plus** unattributable load.
- Never name-derived. A circuit stays `CANDIDATE` until evidence threshold met.
- Tests: Group D + Group E rows, including "appears dedicated but has a second load" →
  SHARED/partial; "insufficient evidence" → `UNKNOWN`.

**Acceptance:** dedicated candidates only on sufficient evidence; no name-based shortcut.

### WP7 — Residual as first-class, decomposed object
**Owner:** Subagent "Residual Analysis". **Depends on:** WP1, WP3, WP6.
**Scope:** new `engine/wattmosaic_engine/models/residual_power.py` (or extend
`models/residual.py`), `tests/unit/test_residual_power.py`.

Deliver:
- Signed residual with `kind` derived (`POSITIVE`/`NEGATIVE`/`NEAR_ZERO`), `resolved` flag,
  `unresolved_since`, `explained_by_device_w`, basis, quality.
- Baseline vs dynamic decomposition (robust median baseline + dynamic component).
- Duplicate/overlapping CTs must not be subtracted twice — use the existing canonical-channel
  and nesting logic, never re-derive.
- Refuse to compute residual across incompatible bases (physical vs factor-adjusted).
- Never clamp negatives; `resolved=False` never renders as 0 W.
- Tests: Group F rows including "new device explains part of residual", "cannot disaggregate",
  "model conflict", "duplicate CT not subtracted twice".

**Acceptance:** all Group F rows green; no clamping path exists.

### WP8 — CT hierarchy discovery (conservative parent/child)
**Owner:** Subagent "Topology Discovery". **Depends on:** WP1, WP5, WP6, WP7.
**Scope:** new `engine/wattmosaic_engine/electrical/hierarchy.py`,
`tests/unit/test_hierarchy.py`.

Deliver:
- Candidate `ELECTRICAL_PARENT_CHILD` relations from: multiple independent device response
  signatures, repeated coverage patterns, long-term power relations, measurement scope, factor
  configuration, circuit membership, consistency across conditions.
- Hard guards: two CTs responding to the same device is **not** proof; larger reading is **not**
  proof of upstream; shared signature is **not** proof of direct relationship.
- Cycle prevention and impossible-structure rejection (validated tree, not just a graph).
- `TopologyRelation` kinds stay separate: overlap ≠ shared-device ≠ membership ≠ parenthood.
- Incomplete paths and unknown relationships are valid outputs.
- Tests: Group I topology rows; synthetic hierarchy recovery where evidence suffices; cycle
  injection rejected; two CTs on one device stays `MEASUREMENT_OVERLAP` only.

**Acceptance:** known synthetic hierarchy recovered; zero spurious parenthood on the
"similar response, independent circuit" fixture.

### WP9 — Integration wiring, audit API, device-history defect
**Owner:** Subagent "Integration". **Depends on:** WP1–WP8.
**Scope:** `engine/wattmosaic_engine/main.py`, `integration.py`, `api/*`,
`disaggregation/engine.py` (wiring only), `config/settings.py`, `tests/integration/*`,
`tests/end_to_end/*`.

Deliver:
- Wire isolation, factor registry, association, circuits, residual, hierarchy into one
  incremental analysis pass + one periodic reanalysis pass, using the existing scheduler.
- Feature flag for anything that would change existing inference output; new artifacts always
  written.
- Fix `_device_history` (`since=utcnow()` → explicit lookback) + regression test.
- Read-only API for: circuits, relations, inference records, residual components, isolation
  status. Additive endpoints only.
- Tests: end-to-end synthetic household through the real HTTP API; deterministic replay.

**Acceptance:** existing API contract tests pass; new E2E proves the chain
`HA event → isolation → association → circuit → residual → topology → versioned record`.

### WP10 — QA, regression, docs, final report
**Owner:** Subagent "QA & Review". **Depends on:** WP9.
**Scope:** `tests/**` (additions only), `docs/**`, `PROJECT_STATUS.md`, `RELEASE_NOTES.md`.

Deliver: full-suite run with metrics, coverage report, replay determinism check, false
positive/negative review on synthetic strata, operational documentation, known limitations,
final validation report. Must report real numbers and real failures.

---

## 4. Dependency graph & waves

```
Wave 1  ┌── WP1 contracts/storage (critical path, serial)
        └── WP2 simulator/fixtures (parallel, independent)

Wave 2  (all depend on WP1)
        WP3 factor ──┐
        WP4 isolation ┼─> WP5 association ─> WP6 circuits ─> WP7 residual ─> WP8 hierarchy

Wave 3  WP9 integration wiring (single owner of every shared file)

Wave 4  WP10 QA / regression / docs / report
```

**Parallelism inside Wave 2:** WP3, WP4 start together. WP5 needs WP4. WP6 needs WP5.
WP7 needs WP3+WP6. WP8 needs WP5–WP7. So the chain is real; only WP3 and WP4 are truly
concurrent, then WP5+WP6 can overlap partially, then WP7, then WP8.

## 5. File-ownership map (no two agents write the same file in one wave)

| Wave | Agent | Owns (write) |
| --- | --- | --- |
| 1 | Contracts | `contracts.py`, `storage/*`, `data/schemas/*`, `tests/unit/test_contracts.py`, `test_storage_config.py`, `test_inference_records.py` |
| 1 | Test Eng | `tools/simulator/*`, `tests/simulation/*`, `tests/conftest.py`, `tests/fixtures/*`, `docs/testing.md` |
| 2 | Factor | `electrical/factors.py`, `electrical/topology.py`, `tests/unit/test_factors.py` |
| 2 | Events | `disaggregation/isolation.py`, `learning/sample_filter.py`, `learning/thresholds.py`, `tests/unit/test_event_isolation.py` |
| 2 | Assoc | `disaggregation/association.py`, `tests/unit/test_association.py` |
| 2 | Circuits | `disaggregation/circuits.py`, `tests/unit/test_circuits.py` |
| 2 | Residual | `models/residual_power.py`, `tests/unit/test_residual_power.py` |
| 2 | Topology | `electrical/hierarchy.py`, `tests/unit/test_hierarchy.py` |
| 3 | Integration | `main.py`, `integration.py`, `api/*`, `disaggregation/engine.py`, `config/settings.py`, `tests/integration/*`, `tests/end_to_end/*` |
| 4 | QA | `tests/**` (additions), `docs/**`, `PROJECT_STATUS.md`, `RELEASE_NOTES.md` |

Any cross-file need is a message to the orchestrator, not an uncoordinated edit.

## 6. Interface contracts frozen before Wave 2

1. `EventIsolator.classify(...) -> EventIsolation` (WP4) — consumed by WP5.
2. `FactorRegistry.basis_for(ct_id, ts) -> (MeasurementBasis, multiplier, confidence_factor)` (WP3) — consumed by WP5/WP7.
3. `AssociationFinder.observe(event, samples) -> list[TopologyRelation]` (WP5) — consumed by WP6/WP8.
4. `CircuitClassifier.classify(relations, residual) -> list[Circuit]` (WP6) — consumed by WP7/WP8.
5. `ResidualAnalyzer.compute(scope) -> ResidualPower` (WP7) — consumed by WP8.
6. `HierarchyFinder.infer(relations, circuits) -> list[TopologyRelation]` (WP8) — consumed by WP9.

Signatures are fixed by WP1's contract models, so all six can be written against a stable
type surface.

## 7. Non-negotiable acceptance gates

1. `python -m pytest tests` — zero regressions; 2168 baseline must not shrink.
2. Every existing test passes after every wave (no "I'll fix it next wave").
3. No existing JSON schema breaks for a 1.5.0 reader.
4. No new runtime dependency, no new service, no new database.
5. `ruff check engine tools tests` and `mypy engine` clean.
6. Every new inference rule has a test that **fails first**.
7. No clamping of negative residual; no fabricated 0 W; no invented device.
8. No claim of completion without an orchestrator-run test log.

## 8. Explicit non-goals (V1 boundary)

- No automation-aware disaggregation: contaminated events are excluded, not decomposed.
- No claim of real-house per-device accuracy (unchanged; synthetic ground truth only).
- No ML/DL model; solvers stay interpretable.
- No automatic promotion of candidate topology to fact without user confirmation.
- No retroactive factor application, ever.
