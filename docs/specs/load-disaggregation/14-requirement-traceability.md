# 14 — Requirement traceability matrix

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION** — the coverage proof for the 130
requirements in [`01-requirements.md`](01-requirements.md): every requirement
mapped to an architecture component, an implementation task, a proposed code
location, a test and a verification result.

**What this document is.** The proof that every requirement has an owner and a
verification method, and the stable task-ID list that
`15-implementation-plan.md` reuses.

**Post-implementation status (2026-11-17).** This matrix now records the
verified outcomes of the implementation phase. Requirements whose task landed
in a commit and whose named automated vehicle passed carry
`PASS (orchestrator gate, <commit-sha>)`; requirements in deferred or reverted
tasks, or whose only vehicle is a manual review, remain `NOT RUN`. See §1 and
§9 for the current state.

**Completion-sprint reconciliation (2026-11-17).** The 18 completion commits
(`b295fbb` … `497ef10`) are reconciled into `tasks/progress.md` §1.1. The
orchestrator gate record is `16-final-verification-report.md` (doc 16, §2–§3)
at HEAD `497ef10`: unit+integration 2777 passed/15 skipped, simulation 168
passed/9 pre-existing errors/0 failed, ruff clean, mypy 109 (delta 0), schema
clean, secret scan clean, addon validation 71/0/3, panel check clean, smoke
non-zero propagation verified. A subset of §7 rows now carries
`PASS (orchestrator gate, <commit>)` where a named vehicle from doc 16 §3
passed at that commit. Requirements in deferred/blocked areas — deployment
report 17, delayed-model behavior (`OD-46`/`OD-47`), and `OD-34` confidence
dimensions — stay `NOT RUN` with a note. The 130 original rows and their task
IDs are unchanged; §11 forward links are updated to reflect what landed.

**What this document is not.** Evidence of completion for the tasks that did
not land. Requirements in deferred, partial or reverted tasks have no commit
and their `Verification Result` stays `NOT RUN`.

**Completion-sprint extension (2026-11-17).** §11 adds 15 *proposed* completion
tasks from `19-completion-plan.md`, linking them to the requirement IDs they
would finish. The 130 original rows in §7 and their `Verification Result`
values are **unchanged** by Phase A. Every new link's verification is `NOT RUN`
(no orchestrator log); in particular **no DISAGG requirement is marked PASS**.

---

## 1. Scope and status

| Field | Value |
| --- | --- |
| Source of truth | [`01-requirements.md`](01-requirements.md) — 130 requirements, `REQ-FOUNDATION-001` … `REQ-TEST-010` |
| Architecture source | [`02-architecture.md`](02-architecture.md) — component map, new-versus-reused table, module boundaries |
| Decision source | [`13-open-decisions-and-assumptions.md`](13-open-decisions-and-assumptions.md) — `OD-01` … `OD-38`, `ASM-01` … `ASM-07` |
| Requirements traced | 130 of 130 — no requirement appears twice and none is absent |
| Tasks defined | 36 |
| Implementation state | post-implementation — 11 tasks `VERIFIED`, 1 partial (`TASK-TOPOLOGY-003`, `IN_REVIEW`), 1 reverted (`TASK-DISAGG-001`, `FAILED`), 22 `NOT_STARTED`, 1 continuous (`TASK-TEST-001`, `IN_PROGRESS`) |
| Verification state | 58 rows `PASS` (orchestrator gate, commit-sha), 72 rows `NOT RUN` (deferred, partial, reverted or manual-only) — see completion-sprint reconciliation below |

**Rule.** This matrix is a plan. A row is not a claim that anything exists.

**Rule.** The task IDs defined in §4 are stable. `15-implementation-plan.md`
elaborates the same IDs into full task files and must not renumber them.

---

## 2. How to read this matrix

Six columns, in this order:

| Column | What it names | What it does **not** name |
| --- | --- | --- |
| **Requirement** | The `REQ-` identifier and a short title, exactly as `01-requirements.md` states it | Any implementation |
| **Architecture component** | The subsystem that carries the requirement, anchored to the module path in `02-architecture.md` §3–§4 | A file that exists yet |
| **Task ID** | The implementation task that owns the requirement — the implementation owner | A person |
| **Code location** | The **proposed** path where the behaviour lands. `(new)` marks a module that does not exist yet; an unmarked path is an existing module named in `01-requirements.md` or `02-architecture.md` | An existing implementation of the new behaviour |
| **Test ID** | The **planned** verification vehicle: a `pytest` selector, a canonical `T##` scenario, a `tools/dev/verify.py` gate or a named manual review | A test that has run |
| **Verification Result** | The outcome of that vehicle in an orchestrator-run test log | A builder's self-report |

**Rule.** Every `Verification Result` reads `NOT RUN` until the orchestrator
runs the named vehicle. The column is populated by the orchestrator's own test
run during Phase 4/5; a builder's self-report is never sufficient
(`docs/agent_assignments.md:26-28`; `docs/implementation_plan_v2.md:333`).

**Rule.** No requirement may be moved to a task that does not cover its own
requirement area. The prefix `TASK-<AREA>-` maps to `REQ-<AREA>-` and to
nothing else.

**Rule.** Contract changes stay additive and optional. No row in this matrix
implies a new runtime dependency, database, service or message bus.

---

## 3. Requirement count by area

Reproduced from `01-requirements.md` §2. Every count below appears in the
matrix exactly once.

| Area | Requirements | Range | In matrix |
| --- | --- | --- | --- |
| Foundation | 6 | `REQ-FOUNDATION-001` … `006` | 6 of 6 |
| Measurement | 10 | `REQ-MEASURE-001` … `010` | 10 of 10 |
| Measurement topology | 11 | `REQ-TOPO-001` … `011` | 11 of 11 |
| Residual | 8 | `REQ-RESIDUAL-001` … `008` | 8 of 8 |
| Disaggregation | 7 | `REQ-DISAGG-001` … `007` | 7 of 7 |
| Device and load models | 8 | `REQ-POWER-001` … `008` | 8 of 8 |
| Storage and lifecycle | 12 | `REQ-STORAGE-001` … `012` | 12 of 12 |
| Event and inference | 9 | `REQ-EVENT-001` … `009` | 9 of 9 |
| Learning | 7 | `REQ-LEARNING-001` … `007` | 7 of 7 |
| Confidence and identifiability | 7 | `REQ-CONFIDENCE-001` … `007` | 7 of 7 |
| Mapping matrix | 7 | `REQ-MAPPING-001` … `007` | 7 of 7 |
| Dynamic topology and change | 7 | `REQ-CHANGE-001` … `007` | 7 of 7 |
| User interface | 13 | `REQ-UI-001` … `013` | 13 of 13 |
| Deployment | 8 | `REQ-DEPLOY-001` … `008` | 8 of 8 |
| Testing | 10 | `REQ-TEST-001` … `010` | 10 of 10 |
| **Total** | **130** | — | **130 of 130** |

**Rule.** The matrix row count equals the requirement count. A row added or
removed changes §3 with it.

---

## 4. Task ID scheme and the task list

### 4.1 The scheme

`TASK-<AREA>-<NNN>`. The area token is the requirement-area token, so a task's
prefix names the requirement area it owns.

| Prefix | Requirement area | Prefix | Requirement area |
| --- | --- | --- | --- |
| `TASK-FOUNDATION-*` | `REQ-FOUNDATION-*` | `TASK-CONFIDENCE-*` | `REQ-CONFIDENCE-*` |
| `TASK-MEASURE-*` | `REQ-MEASURE-*` | `TASK-MAPPING-*` | `REQ-MAPPING-*` |
| `TASK-TOPOLOGY-*` | `REQ-TOPO-*` | `TASK-CHANGE-*` | `REQ-CHANGE-*` |
| `TASK-RESIDUAL-*` | `REQ-RESIDUAL-*` | `TASK-UI-*` | `REQ-UI-*` |
| `TASK-DISAGG-*` | `REQ-DISAGG-*` | `TASK-DEPLOY-*` | `REQ-DEPLOY-*` |
| `TASK-POWER-*` | `REQ-POWER-*` | `TASK-TEST-*` | `REQ-TEST-*` |
| `TASK-STORAGE-*` | `REQ-STORAGE-*` | | |
| `TASK-EVENT-*` | `REQ-EVENT-*` | | |
| `TASK-LEARNING-*` | `REQ-LEARNING-*` | | |

**Rule.** The reserved prefixes `TASK-FOUNDATION`, `TASK-TOPOLOGY`,
`TASK-RESIDUAL`, `TASK-DISAGG`, `TASK-STORAGE`, `TASK-API`, `TASK-UI`,
`TASK-TEST` and `TASK-DEPLOY` are all used. `TASK-API` is not used: the API
read surfaces are owned by the task that owns the artefact they serve, so that
no file has two owners in one wave.

**Rule.** One task owns one file group. Where two tasks touch the same file,
the plan places them in different waves (§5).

**Rule.** Granularity is deliverable-sized, not requirement-sized. There is no
one-task-per-requirement split; the largest task carries five requirements and
the smallest carries two.

### 4.2 The task list — 36 tasks

Statuses below are post-implementation (2026-11-17); see `tasks/progress.md`
for the authoritative per-task status and commit evidence.

| Task ID | Title | Requirements covered | Proposed files/modules | Depends on | Status | Acceptance summary |
| --- | --- | --- | --- | --- | --- | --- |
| `TASK-FOUNDATION-001` | Specification status and the additive contract workflow | `REQ-FOUNDATION-001`, `REQ-FOUNDATION-002`, `REQ-FOUNDATION-003` | `docs/specs/load-disaggregation/README.md`; `engine/wattmosaic_engine/contracts.py`; `tools/dev/export_schemas.py`; `tools/dev/verify.py`; `docs/interface_contracts.md` | — | `NOT_STARTED` | Every spec file carries the approved status; a pre-change payload still validates; `CONTRACT_VERSION` is bumped, `data/schemas/` regenerates with no diff and `verify.py:contract_schema_sync` passes. |
| `TASK-FOUNDATION-002` | Runtime boundary, the four-state label and one owner per wave | `REQ-FOUNDATION-004`, `REQ-FOUNDATION-005`, `REQ-FOUNDATION-006` | `pyproject.toml`; `engine/wattmosaic_engine/main.py`; `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/engine.py`; `docs/specs/load-disaggregation/15-implementation-plan.md` (new — sibling agent) | `TASK-FOUNDATION-001` | `NOT_STARTED` | No new dependency, database, service or bus; every device result exposes one machine-readable power-state label; the plan names one owner per file per wave. |
| `TASK-MEASURE-001` | Measurable quantities, units, phase and polarity | `REQ-MEASURE-001`, `REQ-MEASURE-002`, `REQ-MEASURE-003`, `REQ-MEASURE-004` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/ingestion/normalize.py`; `engine/wattmosaic_engine/electrical/factors.py` | `TASK-FOUNDATION-002` | `NOT_STARTED` | Only W/kW/kWh/Wh are accepted; no V/A/PF/VAR/VA/Hz field exists; phase is declared and never inferred; polarity is applied once and never clamped. |
| `TASK-MEASURE-002` | Leg pairs, reverse flow and the reader declaration surface | `REQ-MEASURE-005`, `REQ-MEASURE-006`, `REQ-MEASURE-007` | `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/electrical/matrix.py`; `engine/wattmosaic_engine/ingestion/correlation.py` | `TASK-MEASURE-001` | `NOT_STARTED` | A skewed leg pair excludes both rows with a named reason; a negative reading keeps its sign; `resolution_w`, `reliability` and `expected_interval_s` are published. |
| `TASK-MEASURE-003` | Timestamps, repeat suppression and cadence derivation | `REQ-MEASURE-008`, `REQ-MEASURE-009`, `REQ-MEASURE-010` | `engine/wattmosaic_engine/synchronization/`; `engine/wattmosaic_engine/config/settings.py` | `TASK-MEASURE-001` | `NOT_STARTED` | The three timestamps are preserved and never conflated; a repeated value is not new evidence; windows derive from the observed cadence and a hard-coded 2 s interval is refused by a static gate. |
| `TASK-TOPOLOGY-001` | Typed edge contract and the lifecycle vocabulary | `REQ-TOPO-001`, `REQ-TOPO-002` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/association.py` | `TASK-FOUNDATION-002` | `NOT_STARTED` | Every edge resolves to exactly one relation kind; the eight lifecycle states exist; only `CONFIRMED` reads as established; a candidate always requires confirmation. |
| `TASK-TOPOLOGY-002` | Containment evidence bar, violations and residual awareness | `REQ-TOPO-003`, `REQ-TOPO-004`, `REQ-TOPO-005`, `REQ-TOPO-006`, `REQ-TOPO-007` | `engine/wattmosaic_engine/electrical/hierarchy.py`; `engine/wattmosaic_engine/electrical/matrix.py`; `engine/wattmosaic_engine/disaggregation/association.py` | `TASK-TOPOLOGY-001` | `NOT_STARTED` | A single coincident response yields overlap only; a violation within tolerance is recorded and never raised; residual evidence alone cannot promote an edge; nested children and alias chains contribute once. |
| `TASK-TOPOLOGY-003` | Versioned topology, change detection and reconciliation | `REQ-TOPO-008`, `REQ-TOPO-009` | `engine/wattmosaic_engine/electrical/topology.py`; `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/disaggregation/audit.py` | `TASK-TOPOLOGY-001` | `NOT_STARTED` | A topology version carries an identifier, time, status, parent and diff; a change is detected, stated and reconciled; every stored record keeps the version it was produced under. |
| `TASK-TOPOLOGY-004` | Incomplete topology and the circuit scope | `REQ-TOPO-010`, `REQ-TOPO-011` | `engine/wattmosaic_engine/disaggregation/circuits.py`; `engine/wattmosaic_engine/api/server.py` | `TASK-TOPOLOGY-002` | `NOT_STARTED` | An unplaced device still loads and publishes as unknown; `GET /circuits` returns identifier, class, status, reader set and device set; a `PARTIALLY_UNDERSTOOD` circuit states its unattributed load. |
| `TASK-RESIDUAL-001` | Signed residual, decomposition, history and the unresolved state | `REQ-RESIDUAL-001`, `REQ-RESIDUAL-002`, `REQ-RESIDUAL-003`, `REQ-RESIDUAL-004`, `REQ-RESIDUAL-008` | `engine/wattmosaic_engine/models/residual_power.py`; `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/api/server.py`; `engine/wattmosaic_engine/api/facade.py` | `TASK-FOUNDATION-002` | `NOT_STARTED` | The signed residual is the only subtraction; `baseline_w + dynamic_w == residual_w`; the trend publishes its pattern with a `None` confidence when there is no history; an unresolved residual is `None` with an age, never `0 W`. |
| `TASK-RESIDUAL-002` | Drift events, the unknown-residual hand-off and the non-redistribution guard | `REQ-RESIDUAL-005`, `REQ-RESIDUAL-006`, `REQ-RESIDUAL-007` | `engine/wattmosaic_engine/models/residual_power.py`; `engine/wattmosaic_engine/disaggregation/events.py`; `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `TASK-RESIDUAL-001`, `TASK-EVENT-001` | `NOT_STARTED` | A sustained baseline shift emits a named event and rewrites no stored point; an unexplained residual change reaches the learning ledger with a decision and a reason code; no device power changes because a residual was distributed. |
| `TASK-DISAGG-001` | Joint inference, identifiability and group estimates | `REQ-DISAGG-001`, `REQ-DISAGG-002`, `REQ-DISAGG-003`, `REQ-DISAGG-004` | `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/electrical/solver.py` | `TASK-MEASURE-003`, `TASK-RESIDUAL-001` | `NOT_STARTED` | The result names every source used and every source available but unused with a reason; rank, null space and condition number are published; an inseparable set publishes `min_w` and `max_w` and no per-device watt figure. |
| `TASK-DISAGG-002` | No fabrication, single counting and the balance identity | `REQ-DISAGG-005`, `REQ-DISAGG-006`, `REQ-DISAGG-007` | `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/electrical/matrix.py`; `engine/wattmosaic_engine/contracts.py` | `TASK-DISAGG-001` | `NOT_STARTED` | Every published value carries source, quality status and tier; a device with no evidence is `TIER_0_UNKNOWN` with no watt figure; a load visible to several readers is attributed once; both reconciliation identities hold for every written row. |
| `TASK-POWER-001` | Load-shape class and the model-version surface | `REQ-POWER-001`, `REQ-POWER-002`, `REQ-POWER-004`, `REQ-POWER-008` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/models/registry.py`; `engine/wattmosaic_engine/models/device_profile.py` (new) | `TASK-FOUNDATION-002` | `NOT_STARTED` | The six load-shape classes are a separate axis from `ModelType` and are never inferred from a name; a published power names its level and an inrush sample enters neither a steady-state fit nor the energy integral; an off device's standby draw is published and counted once; model versions are listed, promoted and rolled back without deletion. |
| `TASK-POWER-002` | Measured-step attribution, non-HA devices, entity separation and declared bounds | `REQ-POWER-003`, `REQ-POWER-005`, `REQ-POWER-006`, `REQ-POWER-007` | `engine/wattmosaic_engine/disaggregation/attachment.py` (new); `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/api/server.py` | `TASK-DISAGG-001` | `NOT_STARTED` | A transition is credited with the measured step from the same reader; a device with no HA entity is published with an honest confidence; the five identities are separate fields; a declared wattage is a non-relaxable upper bound on its row. |
| `TASK-STORAGE-001` | Tier registry, retention and payload encoding | `REQ-STORAGE-001`, `REQ-STORAGE-002`, `REQ-STORAGE-008`, `REQ-STORAGE-009` | `engine/wattmosaic_engine/storage/tiers.py` (new); `engine/wattmosaic_engine/config/settings.py`; `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/storage/migrations.py`; `addon/run.sh` | `TASK-FOUNDATION-002` | `NOT_STARTED` | Every table is classified L1–L4 in one registry and published; raw retention is validated against its stated range; a compressed payload reads through the same API and a legacy payload still loads; any partitioning decision carries the measurement that justified it. |
| `TASK-STORAGE-002` | Evidence immutability, batched writes, indexes and version identifiers | `REQ-STORAGE-003`, `REQ-STORAGE-004`, `REQ-STORAGE-005`, `REQ-STORAGE-006`, `REQ-STORAGE-007` | `engine/wattmosaic_engine/storage/migrations.py`; `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/main.py` | `TASK-STORAGE-001` | `NOT_STARTED` | L2 evidence is append-only with strictly increasing revisions; a flush is one transaction per table; no version deletes an earlier version's rows; the time and entity access paths use an index; every record names all four versions. |
| `TASK-STORAGE-003` | Recovery, migration safety and replay | `REQ-STORAGE-010`, `REQ-STORAGE-011`, `REQ-STORAGE-012` | `engine/wattmosaic_engine/storage/migrations.py`; `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/main.py`; `tools/audit/replay_capture.py` | `TASK-STORAGE-002` | `NOT_STARTED` | A restart preserves every record count and refuses a corrupt file; migrations are forward-only, additive and idempotent; a stored window replays identically under the same algorithm version. |
| `TASK-EVENT-001` | Event kinds, grouping, lag and window sizing | `REQ-EVENT-001`, `REQ-EVENT-002`, `REQ-EVENT-003`, `REQ-EVENT-004` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/ingestion/ha_source.py`; `engine/wattmosaic_engine/disaggregation/isolation.py`; `engine/wattmosaic_engine/disaggregation/configuration.py`; `engine/wattmosaic_engine/disaggregation/association.py` | `TASK-MEASURE-003` | `NOT_STARTED` | Every event names one of the three kinds; one automation produces one group; a response beyond `max_lag_s` is withheld as `lag_exceeded`; the resolved window and its source are published. |
| `TASK-EVENT-002` | Cross-reader vector, late data, duplicates and incomplete groups | `REQ-EVENT-005`, `REQ-EVENT-006`, `REQ-EVENT-007`, `REQ-EVENT-008`, `REQ-EVENT-009` | `engine/wattmosaic_engine/disaggregation/isolation.py`; `engine/wattmosaic_engine/disaggregation/events.py`; `engine/wattmosaic_engine/ingestion/ha_source.py`; `engine/wattmosaic_engine/synchronization/`; `engine/wattmosaic_engine/storage/database.py` | `TASK-EVENT-001` | `NOT_STARTED` | The event publishes its per-reader effect vector; a late event is classified and rewrites no published record; a re-delivered event adds no row; an incomplete group names its missing part; `occurred_at` is never mutated. |
| `TASK-LEARNING-001` | Sample intake, the rejection ledger and per-device thresholds | `REQ-LEARNING-001`, `REQ-LEARNING-002`, `REQ-LEARNING-006` | `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/disaggregation/configuration.py`; `engine/wattmosaic_engine/disaggregation/isolation.py` | `TASK-EVENT-002` | `NOT_STARTED` | Only isolated events produce samples; every rejection keeps an enumerated reason code; a per-device override changes that device's threshold and the resolved source is published. |
| `TASK-LEARNING-002` | Promotion, factor timing and measurement precedence | `REQ-LEARNING-003`, `REQ-LEARNING-004`, `REQ-LEARNING-005`, `REQ-LEARNING-007` | `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/electrical/factors.py`; `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/storage/database.py` | `TASK-LEARNING-001`, `TASK-RESIDUAL-002` | `NOT_STARTED` | A candidate that does not beat the active model by the margin is not activated and its decision publishes its metrics; a factor is resolved against the reading's timestamp; a measurement outranks a model, the conflict is flagged and the window is not used for training. |
| `TASK-CONFIDENCE-001` | Confidence dimensions, the calibration flag and tier monotonicity | `REQ-CONFIDENCE-001`, `REQ-CONFIDENCE-002`, `REQ-CONFIDENCE-003`, `REQ-CONFIDENCE-004` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/confidence.py`; `engine/wattmosaic_engine/disaggregation/tiers.py`; `addon/ui/app.js` | `TASK-FOUNDATION-002` | `NOT_STARTED` | No aggregate field, no `__float__` and no arithmetic on the vector; `confidence_is_calibrated` is false; only the five existing member names are used and an unassessed dimension is `null`; a tier never falls without a recorded reason. |
| `TASK-CONFIDENCE-002` | Live confidence dimensions and per-edge evidence | `REQ-CONFIDENCE-005`, `REQ-CONFIDENCE-006`, `REQ-CONFIDENCE-007` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/disaggregation/association.py`; `engine/wattmosaic_engine/disaggregation/confidence.py` | `TASK-CONFIDENCE-001` | `NOT_STARTED` | The `topology` and `residual` dimensions carry a value where derivable and `null` otherwise; measurement quality, evidence count, freshness and residual uncertainty are separately observable; an edge confidence is capped at 0.99 while a candidate and never becomes a device's `power` dimension. |
| `TASK-MAPPING-001` | The derived mapping projection | `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-006`, `REQ-MAPPING-007` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new); `engine/wattmosaic_engine/api/server.py` | `TASK-TOPOLOGY-002` | `NOT_STARTED` | The matrix is derived from relations, residual history, the model registry and the topology and is never written; 1:1, 1:N, N:1 and N:N are all expressible; every cell publishes its nine fields; reader count never multiplies into confidence; the response carries a coverage block. |
| `TASK-MAPPING-002` | Unknown-load entries and the evidence-triggered refresh | `REQ-MAPPING-004`, `REQ-MAPPING-005` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new); `engine/wattmosaic_engine/models/residual_power.py`; `engine/wattmosaic_engine/storage/database.py` | `TASK-MAPPING-001` | `NOT_STARTED` | An unexplained scope shows an unknown-load entry that names no device; a steady stream of identical samples produces no matrix write while an association withdrawal produces one. |
| `TASK-CHANGE-001` | Configuration events and the hypothesis lifecycle | `REQ-CHANGE-001`, `REQ-CHANGE-002`, `REQ-CHANGE-005` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/storage/migrations.py`; `engine/wattmosaic_engine/contracts.py` | `TASK-TOPOLOGY-001`, `TASK-STORAGE-002` | `NOT_STARTED` | Each of the eight change kinds emits a named event with before/after state, timestamp and resulting version; a suspected change leaves the live configuration untouched until validated; an unresolved change publishes the missing evidence and applies no silent default. |
| `TASK-CHANGE-002` | Four version identifiers, immutable history, rollback and reconciliation | `REQ-CHANGE-003`, `REQ-CHANGE-004`, `REQ-CHANGE-006`, `REQ-CHANGE-007` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/electrical/topology.py`; `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/api/server.py` | `TASK-TOPOLOGY-003`, `TASK-CHANGE-001` | `NOT_STARTED` | Every derived artefact names all four version identifiers with validity intervals; a previous version stays byte-identical and readable; a restore creates a new version whose parent is the version in force; a change response lists the affected device and record counts and rewrites no record. |
| `TASK-UI-001` | Panel vocabulary: inventory, quality, the four-state label and the view states | `REQ-UI-001`, `REQ-UI-002`, `REQ-UI-007`, `REQ-UI-013` | `addon/ui/app.js`; `tools/dev/check_panel.py` | `TASK-FOUNDATION-002` | `NOT_STARTED` | The reader inventory lists every declared field; every measurement shows value, unit, the three timestamps, quality and its four-state label; every view has loading, empty, error, stale, offline and degraded states; missing renders as missing and never as `0 W`. |
| `TASK-UI-002` | Panel views: topology, residual, model, mapping and identifiability | `REQ-UI-003`, `REQ-UI-004`, `REQ-UI-005`, `REQ-UI-006`, `REQ-UI-008` | `addon/ui/app.js`; `tools/dev/check_panel.py` | `TASK-UI-001` | `NOT_STARTED` | Each drawn edge carries its status and confidence and a candidate is visually distinct; the residual view shows the signed value, baseline, dynamic and age; the mapping view lists every cell; an ambiguous group shows minimum, maximum and its members instead of a watt figure. |
| `TASK-UI-003` | Panel evidence, version history, operator correction and storage health | `REQ-UI-009`, `REQ-UI-010`, `REQ-UI-011`, `REQ-UI-012` | `addon/ui/app.js`; `engine/wattmosaic_engine/api/server.py` | `TASK-UI-002` | `NOT_STARTED` | The event list shows verdict, window bounds, competitors and reason code; two versions can be compared and a suspected change is visibly not applied; a correction is stored with `configured_by="user"` and a relation can be withdrawn with a reason; the health view names the retention horizons, row counts and the last prune. |
| `TASK-DEPLOY-001` | Build pipeline, option surface, startup migrations and backup | `REQ-DEPLOY-001`, `REQ-DEPLOY-002`, `REQ-DEPLOY-003`, `REQ-DEPLOY-004` | `Dockerfile`; `addon/config.yaml`; `addon/run.sh`; `addon/healthcheck.py`; `engine/wattmosaic_engine/config/settings.py`; `engine/wattmosaic_engine/main.py`; `engine/wattmosaic_engine/storage/migrations.py`; `.env.example` | `TASK-STORAGE-001` | `NOT_STARTED` | The existing image and GHCR pipeline are reused and `scripts/validate_addon.sh --no-build` passes; every new setting appears in the option table, `settings.py` and `.env.example` and is validated at startup; pending migrations run at startup and `PRAGMA user_version` equals `LATEST_VERSION`; a backup taken while running is a consistent snapshot that restores cleanly. |
| `TASK-DEPLOY-002` | Deployment procedure, smoke tests, rollback and operational metrics | `REQ-DEPLOY-005`, `REQ-DEPLOY-006`, `REQ-DEPLOY-007`, `REQ-DEPLOY-008` | `scripts/smoke_test.sh` (new); `addon/healthcheck.py`; `engine/wattmosaic_engine/main.py`; `docs/deployment.md`; `docs/risks.md` | `TASK-DEPLOY-001` | `NOT_STARTED` | The procedure names a verification command per step; the smoke script exits non-zero on any of the six checks and is separate from the watchdog probe; a rollback restores the previous image and database copy and is verified by the same smoke tests; every operational risk names the metric that would reveal it. |
| `TASK-TEST-001` | Unit-rule coverage, regression and negative verification | `REQ-TEST-001`, `REQ-TEST-006`, `REQ-TEST-007` | `tests/unit/`; `tools/dev/negverify_nesting.py`; `tools/dev/verify.py` | `TASK-FOUNDATION-001` | `NOT_STARTED` | Every new rule has a test that failed against the pre-change tree; the suite result is not smaller than the baseline and the `mypy engine` error count does not increase; every new guard has a negative-verification record and repeated families have an automated harness. |
| `TASK-TEST-002` | API surface, replay and migration harnesses | `REQ-TEST-002`, `REQ-TEST-005`, `REQ-TEST-008` | `tests/integration/`; `tests/end_to_end/`; `tests/simulation/`; `engine/wattmosaic_engine/api/facade.py`; `tools/audit/replay_capture.py` | `TASK-TEST-001` | `NOT_STARTED` | A new route returns the contract header, rejects a bad token with 401 and returns 400 for a validation failure; a stored window replays identically; every prior schema version migrates with no row loss. |
| `TASK-TEST-003` | The T01–T15 builders, the matrix reconciliation and the performance and accuracy harnesses | `REQ-TEST-003`, `REQ-TEST-004`, `REQ-TEST-009`, `REQ-TEST-010` | `tools/simulator/scenarios.py` (new); `tests/simulation/test_simulator_fixtures.py` (new); `tests/unit/test_simulator_fixtures.py` (new); `tests/unit/test_health_probe_cost.py`; `tests/unit/test_nesting_perf.py`; `tests/simulation/test_accuracy.py`; `docs/test_scenario_matrix.md` | `TASK-TEST-001` | `NOT_STARTED` | Each of T01–T15 has a named deterministic builder and the ground truth is never visible to the engine path; every scenario-dependent requirement names both the `T##` identifier and the existing matrix row; each stated budget is asserted by a test; no artefact claims a real-house per-device accuracy. |

---

## 5. Wave order

Indicative. The `TASK-TEST-*` family runs continuously as the verification
vehicle for every wave, not as a terminal wave.

| Wave | Content | Tasks | Shared files and how they are avoided |
| --- | --- | --- | --- |
| 0 | Contract and process foundation | `TASK-FOUNDATION-001`, `TASK-FOUNDATION-002` | `contracts.py` is first claimed here; later waves add fields only, in their own tasks |
| 1 | Measurement, residual and storage substrate | `TASK-MEASURE-001` … `003`, `TASK-RESIDUAL-001`, `TASK-STORAGE-001` | `config/settings.py` is claimed by `TASK-MEASURE-003` and re-claimed by `TASK-STORAGE-001` in wave 3 |
| 2 | Topology, events and the disaggregation core | `TASK-TOPOLOGY-001` … `004`, `TASK-EVENT-001` … `002`, `TASK-DISAGG-001` … `002`, `TASK-POWER-001` … `002` | `disaggregation/engine.py` is serialised inside wave 2 by dependency, never in parallel |
| 3 | Analysis, learning, mapping and change | `TASK-RESIDUAL-002`, `TASK-LEARNING-001` … `002`, `TASK-CONFIDENCE-001` … `002`, `TASK-MAPPING-001` … `002`, `TASK-CHANGE-001` … `002` | `disaggregation/audit.py` is owned by `TASK-TOPOLOGY-003` in wave 2 and by `TASK-CONFIDENCE-002` and `TASK-CHANGE-001` in wave 3, in that order |
| 4 | Panel, deployment and the end-to-end harnesses | `TASK-UI-001` … `003`, `TASK-DEPLOY-001` … `002`, `TASK-TEST-002` … `003` | `addon/ui/app.js` is owned by one `TASK-UI-*` task at a time |

**Rule.** Where two tasks name the same file, the later task depends on the
earlier one and the plan assigns them different waves
(`docs/implementation_plan_v2.md` §5).

---

## 6. Test-ID scheme

Defined before the matrix so that the `Test ID` column is meaningful.

| Family | Vehicle | Selector form | Used for |
| --- | --- | --- | --- |
| `UT-<subject>` | `pytest tests/unit/test_<subject>.py` | `UT-<subject>::test_<name>` | a rule observable at unit level |
| `IT-<subject>` | `pytest tests/integration/test_<subject>.py` | `IT-<subject>::test_<name>` | an HTTP surface, a wiring path, a safety invariant |
| `E2E-<subject>` | `pytest tests/end_to_end/test_<subject>.py` | `E2E-<subject>::test_<name>` | a whole cycle over persisted state |
| `SIM-<subject>` | `pytest tests/simulation/test_<subject>.py` | `SIM-<subject>::test_<name>` | simulator strata, replay, accuracy |
| `T##` | the canonical synthetic scenario catalogue | `T01` … `T15` | any requirement whose `Scenario.` line names it |
| `GATE-<name>` | `python tools/dev/verify.py:<name>` | `GATE-contract_schema_sync`, `GATE-deployment_valid` | a static, contract or packaging gate |
| `MANUAL-<subject>` | a named human review or rehearsal | `MANUAL-wave-ownership-review` | a requirement whose only verification method is a review |

**Rule.** `(new)` after a selector marks a proposed test function or file that
does not exist yet. An unmarked selector is a test that exists today.

**Rule.** A requirement carrying a `Scenario.` line names its canonical `T##`
identifier first, then the concrete selector, separated by `·`.

**Rule.** `GATE-contract_schema_sync` and `GATE-deployment_valid` are the only
gate names quoted from the specification set. Any other gate named here is
proposed and is created in `tools/dev/verify.py` by the task that owns that
file.

**Rule.** A requirement whose vehicle is `MANUAL-*` still has a verification
method. It is listed in §9 as weak-verification, never as untraced.

---

## 7. The matrix — 130 requirements

Architecture components are anchored to `02-architecture.md` §3–§4. Code
locations are **proposed**; `(new)` marks a module that does not exist yet.

### 7.1 Foundation — 6 requirements

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-FOUNDATION-001` — The specification set carries one declared status | Specification set — status header | `TASK-FOUNDATION-001` | `docs/specs/load-disaggregation/README.md`; `tools/dev/verify.py` | `GATE-spec_status` (new) | `PASS (orchestrator gate, bfb9938)` |
| `REQ-FOUNDATION-002` — Contract changes are additive and optional | Contract layer — `contracts.py` | `TASK-FOUNDATION-001` | `engine/wattmosaic_engine/contracts.py`; `data/schemas/` | `UT-contracts::test_pre_change_payload_still_validates` (new) | `PASS (orchestrator gate, bfb9938)` |
| `REQ-FOUNDATION-003` — The contract change workflow is followed exactly | Contract workflow — schema generator and gate | `TASK-FOUNDATION-001` | `tools/dev/export_schemas.py`; `docs/interface_contracts.md`; `engine/wattmosaic_engine/contracts.py` | `GATE-contract_schema_sync` | `PASS (orchestrator gate, bfb9938)` |
| `REQ-FOUNDATION-004` — No new runtime dependency, database, service or message bus | Process boundary — engine process and SQLite file | `TASK-FOUNDATION-002` | `pyproject.toml`; `engine/wattmosaic_engine/main.py`; `engine/wattmosaic_engine/storage/database.py` | `UT-app-startup::test_one_process_one_database` (new) | `PASS (orchestrator gate, f5889b4)` |
| `REQ-FOUNDATION-005` — The four power states are used strictly | Contract layer — `Source` and `MeasurementBasis` | `TASK-FOUNDATION-002` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `UT-disaggregation::test_four_state_label_matches_source_and_basis` (new) | `PASS (orchestrator gate, f5889b4)` |
| `REQ-FOUNDATION-006` — One file, one owner, per wave | Wave ownership — implementation plan | `TASK-FOUNDATION-002` | `docs/implementation_plan_v2.md`; `docs/specs/load-disaggregation/15-implementation-plan.md` (new — sibling agent) | `MANUAL-wave-ownership-review` | `NOT RUN` |

### 7.2 Measurement semantics — 10 requirements

Scenario vehicles: `T13` (leg pairs), `T14` (missing and delayed data),
`T15` (bidirectional power).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-MEASURE-001` — Only active power and cumulative energy are measurable | Contract layer — `Measurement`, `MeasurementBasis` | `TASK-MEASURE-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/ingestion/normalize.py` | `UT-contracts::test_measurement_unit_whitelist` (new) | `PASS (orchestrator gate, cd71d4e)` |
| `REQ-MEASURE-002` — Unmeasurable electrical quantities are never synthesised | Contract layer — `Measurement` | `TASK-MEASURE-001` | `engine/wattmosaic_engine/contracts.py`; `addon/ui/app.js` | `UT-contracts::test_no_unmeasurable_quantity_field` (new) | `PASS (orchestrator gate, cd71d4e)` |
| `REQ-MEASURE-003` — Phase is a declared property of a reader, never inferred | Electrical — phase coefficients | `TASK-MEASURE-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/electrical/factors.py` | `UT-electrical::test_phase_coefficients_l1_l2_both` | `PASS (orchestrator gate, cd71d4e)` |
| `REQ-MEASURE-004` — Polarity is preserved, signed and never clamped | Ingestion — normalisation and correlation | `TASK-MEASURE-001` | `engine/wattmosaic_engine/ingestion/normalize.py`; `engine/wattmosaic_engine/ingestion/correlation.py` | `T15` · `UT-disaggregation::test_11_reversed_ct_polarity` | `PASS (orchestrator gate, cd71d4e)` |
| `REQ-MEASURE-005` — A leg pair is usable only as a pair | Disaggregation — leg-pair exclusion | `TASK-MEASURE-002` | `engine/wattmosaic_engine/disaggregation/engine.py` | `T13` · `UT-disaggregation::test_a_pair_whose_legs_were_sampled_far_apart_is_excluded` | `PASS (orchestrator gate, 415ef9e)` |
| `REQ-MEASURE-006` — Reverse and bidirectional power flow is preserved | Electrical — `MeasurementMatrix` | `TASK-MEASURE-002` | `engine/wattmosaic_engine/electrical/matrix.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `T15` · `UT-disaggregation::test_negative_export_is_preserved_in_reconciliation` | `PASS (orchestrator gate, 415ef9e)` |
| `REQ-MEASURE-007` — Resolution, reliability and expected interval travel with the reader | Contract layer — `CTChannel` | `TASK-MEASURE-002` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/api/server.py` | `UT-correlation::test_reader_declaration_is_published` (new) | `PASS (orchestrator gate, 415ef9e)` |
| `REQ-MEASURE-008` — Three timestamps, one staleness definition | Synchronization — `SampleAligner` | `TASK-MEASURE-003` | `engine/wattmosaic_engine/synchronization/`; `engine/wattmosaic_engine/config/settings.py` | `UT-synchronization::test_window_age_uses_state_timestamp_not_arrival` | `PASS (orchestrator gate, 76af12d)` |
| `REQ-MEASURE-009` — A repeated value is not new evidence | Ingestion — `HASource` | `TASK-MEASURE-003` | `engine/wattmosaic_engine/ingestion/ha_source.py`; `engine/wattmosaic_engine/storage/database.py` | `UT-event-intake::test_a_repeated_state_is_not_a_transition` | `PASS (orchestrator gate, 76af12d)` |
| `REQ-MEASURE-010` — Cadence is measured, never assumed at two seconds | Synchronization — `ChannelHealth` | `TASK-MEASURE-003` | `engine/wattmosaic_engine/synchronization/`; `engine/wattmosaic_engine/config/settings.py` | `T14` · `UT-synchronization::test_cadence_is_derived_from_observed_history_not_hardcoded` | `PASS (orchestrator gate, 76af12d)` |

### 7.3 Measurement topology — 11 requirements

Scenario vehicles: `T01`, `T02` (containment), `T03` (unobserved parent load),
`T11`, `T12` (relocation and replacement), `T13`.

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-TOPO-001` — The relationship set is typed and the kinds never imply each other | Contract layer — `TopologyRelation`, `RelationKind` | `TASK-TOPOLOGY-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/association.py` | `T13` · `UT-association::test_relation_kind_is_single_and_typed` (new) | `PASS (orchestrator gate, 74b686d)` |
| `REQ-TOPO-002` — An edge has a lifecycle, and the lifecycle never implies confirmation | Disaggregation — `AssociationFinder` | `TASK-TOPOLOGY-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/association.py` | `T12` · `UT-association::test_edge_lifecycle_states` (new) | `PASS (orchestrator gate, 74b686d)` |
| `REQ-TOPO-003` — Parent and child are treated asymmetrically | Electrical — `MeasurementMatrix` | `TASK-TOPOLOGY-002` | `engine/wattmosaic_engine/electrical/matrix.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `T01`, `T02` · `UT-disaggregation::test_07_nested_branch_ct_does_not_double_count` | `PASS (orchestrator gate, 036ffbc)` |
| `REQ-TOPO-004` — A child-to-parent correlation is containment evidence only when repeated and validated | Electrical — `HierarchyFinder` | `TASK-TOPOLOGY-002` | `engine/wattmosaic_engine/electrical/hierarchy.py` | `T01` · `UT-hierarchy::test_hot_tub_equal_deltas_yield_overlap_only` | `PASS (orchestrator gate, 036ffbc)` |
| `REQ-TOPO-005` — Exact parent–child power conservation is never enforced | Electrical — `HierarchyFinder` | `TASK-TOPOLOGY-002` | `engine/wattmosaic_engine/electrical/hierarchy.py` | `T03` · `UT-hierarchy::test_violation_within_tolerance_is_recorded` (new) | `PASS (orchestrator gate, 036ffbc)` |
| `REQ-TOPO-006` — Edge inference is residual-aware | Electrical — `HierarchyFinder` | `TASK-TOPOLOGY-002` | `engine/wattmosaic_engine/electrical/hierarchy.py`; `engine/wattmosaic_engine/models/residual_power.py` | `T03` · `UT-residual-power::test_residual_only_evidence_does_not_promote` (new) | `PASS (orchestrator gate, 036ffbc)` |
| `REQ-TOPO-007` — Nested children cannot be counted twice | Electrical — `MeasurementMatrix` | `TASK-TOPOLOGY-002` | `engine/wattmosaic_engine/electrical/matrix.py` | `T02`, `T06` · `UT-electrical::test_three_level_nesting_sums_only_the_root` | `PASS (orchestrator gate, 036ffbc)` |
| `REQ-TOPO-008` — Topology is a versioned object with diffing and rollback | Electrical — `TopologyGraph` and the version store | `TASK-TOPOLOGY-003` | `engine/wattmosaic_engine/electrical/topology.py`; `engine/wattmosaic_engine/storage/database.py` | `T11`, `T12` · `UT-topology-persistence::test_topology_version_diff` (new) | `PASS (orchestrator gate, 4bac434)` |
| `REQ-TOPO-009` — Topology change is detected and reconciled | Disaggregation — `AuditPass` | `TASK-TOPOLOGY-003` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/storage/migrations.py` | `T11`, `T12` · `UT-inference-records::test_records_keep_their_topology_version` (new) | `PASS (orchestrator gate, 24e07fb)` — `test_records_keep_their_topology_version` green; detection in `293038b` |
| `REQ-TOPO-010` — The topology graph may be incomplete | Disaggregation — `CircuitClassifier` | `TASK-TOPOLOGY-004` | `engine/wattmosaic_engine/disaggregation/circuits.py`; `engine/wattmosaic_engine/api/server.py` | `UT-topology-bootstrap::test_incomplete_topology_still_loads` (new) | `NOT RUN` |
| `REQ-TOPO-011` — A circuit is an electrical scope with its own identity and class | Disaggregation — `CircuitClassifier` | `TASK-TOPOLOGY-004` | `engine/wattmosaic_engine/disaggregation/circuits.py`; `engine/wattmosaic_engine/api/server.py` | `UT-circuits::test_circuit_class_status_and_member_sets` (new) | `NOT RUN` |

### 7.4 Residuals — 8 requirements

Scenario vehicles: `T03`, `T04` (standby and baseline), `T10` (unknown device
discovery), `T15` (bidirectional power).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-RESIDUAL-001` — The signed residual is the only subtraction | Models — `ResidualAnalyzer` | `TASK-RESIDUAL-001` | `engine/wattmosaic_engine/models/residual_power.py` | `T15` · `UT-residual-power::test_signed_residual_is_never_clamped` (new) | `PASS (orchestrator gate, 14f7b20)` |
| `REQ-RESIDUAL-002` — A residual decomposes into baseline, dynamic and noise | Models — `ResidualAnalyzer` | `TASK-RESIDUAL-001` | `engine/wattmosaic_engine/models/residual_power.py` | `T03` · `UT-residual-power::test_baseline_plus_dynamic_equals_residual` (new) | `PASS (orchestrator gate, 14f7b20)` |
| `REQ-RESIDUAL-003` — A residual is first-class data with history | Storage and API — residual read path | `TASK-RESIDUAL-001` | `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/api/server.py`; `engine/wattmosaic_engine/api/facade.py` | `UT-api::test_residual_history_route` (new) | `PASS (orchestrator gate, 14f7b20)` |
| `REQ-RESIDUAL-004` — A residual may have its own operating pattern | Models — `ResidualTrend` | `TASK-RESIDUAL-001` | `engine/wattmosaic_engine/models/residual_power.py` | `T03`, `T10` · `UT-residual-power::test_residual_trend_publishes_its_pattern` (new) | `PASS (orchestrator gate, 14f7b20)` |
| `REQ-RESIDUAL-005` — Residual drift and change are detected and reported | Models — residual drift detector | `TASK-RESIDUAL-002` | `engine/wattmosaic_engine/models/residual_power.py`; `engine/wattmosaic_engine/disaggregation/events.py` | `UT-residual-power::test_residual_drift_emits_a_named_event` (new) | `NOT RUN` |
| `REQ-RESIDUAL-006` — An unknown residual event is consumable by the learning system | Disaggregation to learning — unknown-residual event | `TASK-RESIDUAL-002` | `engine/wattmosaic_engine/disaggregation/events.py`; `engine/wattmosaic_engine/learning/pipeline.py` | `T10` · `UT-learning::test_unknown_residual_event_reaches_the_ledger` (new) | `NOT RUN` |
| `REQ-RESIDUAL-007` — A residual is never redistributed onto devices | Disaggregation — `DisaggregationEngine` | `TASK-RESIDUAL-002` | `engine/wattmosaic_engine/disaggregation/engine.py` | `T04` · `IT-safety-invariants::test_residual_is_never_redistributed` | `NOT RUN` |
| `REQ-RESIDUAL-008` — An unresolved residual never renders as zero | Models — `ResidualPower` | `TASK-RESIDUAL-001` | `engine/wattmosaic_engine/models/residual_power.py`; `engine/wattmosaic_engine/api/server.py` | `UT-residual-power::test_unresolved_residual_is_none_not_zero` (new) | `PASS (orchestrator gate, 14f7b20)` |

### 7.5 Disaggregation — 7 requirements

Scenario vehicles: `T06` (simultaneous devices), `T07` (non-identifiable
devices).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-DISAGG-001` — Inference is joint over every available source | Disaggregation — `DisaggregationEngine` | `TASK-DISAGG-001` | `engine/wattmosaic_engine/disaggregation/engine.py` | `E2E-full-inference-chain::test_result_names_every_source_used_and_unused` (new) | `PASS (orchestrator gate, f558bde)` — B4a source ledger; E2E case 8 passed (doc 16 §3) |
| `REQ-DISAGG-002` — Simultaneous device changes are handled without a fabricated split | Electrical — solver and null space | `TASK-DISAGG-001` | `engine/wattmosaic_engine/electrical/solver.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `T06`, `T07` · `UT-disaggregation::test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated` | `PASS (orchestrator gate, 0dd4d27)` — B4b: `test_16…` + `test_12…` green, no 250 W split (doc 16 §3, §4) |
| `REQ-DISAGG-003` — Identifiability is computed and published | Electrical — `analyse_identifiability` | `TASK-DISAGG-001` | `engine/wattmosaic_engine/electrical/solver.py` | `T07` · `UT-electrical::test_identifiability_rank_and_ambiguous_group` | `PASS (orchestrator gate, 0dd4d27)` — named identifiability gate green at B4b HEAD (doc 16 §2) |
| `REQ-DISAGG-004` — Where separation is impossible, a range or a group estimate is published | Electrical — `augment_with_unknown_slack` | `TASK-DISAGG-001` | `engine/wattmosaic_engine/electrical/solver.py` | `T07` · `UT-electrical::test_ambiguous_group_publishes_min_and_max` (new) | `PASS (orchestrator gate, 0dd4d27)` — B4b `min_w`/`max_w`, 56 passed (doc 16 §3) |
| `REQ-DISAGG-005` — A per-device number is never fabricated | Disaggregation — `DisaggregationEngine` and confidence | `TASK-DISAGG-002` | `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/disaggregation/confidence.py` | `T07` · `UT-disaggregation::test_12_missing_sensor_samples_do_not_abort_or_fabricate` | `PASS (orchestrator gate, 0dd4d27)` — mutation proof: guard catches a forced per-device split (doc 16 §3, §4) |
| `REQ-DISAGG-006` — One physical load seen by several nested readers is counted once | Electrical — `MeasurementMatrix` | `TASK-DISAGG-002` | `engine/wattmosaic_engine/electrical/matrix.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `T06` · `UT-electrical::test_nested_main_sums_only_the_outermost_feed` | `NOT RUN` — named gate exists and is green, but it is not part of the B4a/B4b mutation-proven set; kept as-is, not overclaimed |
| `REQ-DISAGG-007` — The home-level balance identity holds exactly | Disaggregation — `_reconcile` | `TASK-DISAGG-002` | `engine/wattmosaic_engine/disaggregation/engine.py`; `engine/wattmosaic_engine/contracts.py` | `UT-reconciliation-split::test_balance_identities_hold` (new) | `NOT RUN` — named gate does not exist in the repo; no commit evidence |

### 7.6 Device and load models — 8 requirements

Scenario vehicles: `T01` (measured step), `T04` (standby), `T08` (variable
power), `T10` (a device with no HA entity).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-POWER-001` — A device's power model has one named class | Contract layer — load-shape class | `TASK-POWER-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/models/registry.py` | `T08` · `UT-device-taxonomy::test_load_shape_class_is_published` (new) | `PASS (orchestrator gate, 8b70933)` |
| `REQ-POWER-002` — Standby, idle, active and transient power are distinct | Models — device power profile | `TASK-POWER-001` | `engine/wattmosaic_engine/models/device_profile.py` (new); `engine/wattmosaic_engine/learning/pipeline.py` | `T04` · `UT-models::test_binary_model_median_ignores_inrush_outlier` | `PASS (orchestrator gate, 8b70933)` |
| `REQ-POWER-003` — State-based models use the measured power difference | Disaggregation — device attachment | `TASK-POWER-002` | `engine/wattmosaic_engine/disaggregation/attachment.py` (new) | `T01` · `UT-disaggregation::test_01_single_device_activation` | `NOT RUN` |
| `REQ-POWER-004` — An OFF device may still consume standby power | Models — device power profile | `TASK-POWER-001` | `engine/wattmosaic_engine/models/device_profile.py` (new); `engine/wattmosaic_engine/disaggregation/engine.py` | `T04` · `UT-disaggregation::test_17_standby_power_is_preserved` | `PASS (orchestrator gate, 8b70933)` |
| `REQ-POWER-005` — A device with no Home Assistant integration is supported | Contract layer — `HaConnectivity` | `TASK-POWER-002` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/attachment.py` (new) | `T10` · `UT-device-taxonomy::test_device_without_an_entity_is_first_class` (new) | `NOT RUN` |
| `REQ-POWER-006` — The five entities are never conflated | Contract layer — `HaEntityRole` | `TASK-POWER-002` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/api/server.py` | `UT-device-links::test_the_five_identities_stay_separate` (new) | `NOT RUN` |
| `REQ-POWER-007` — A declared wattage bounds a shared row and is never relaxed | Disaggregation — row budget guard | `TASK-POWER-002` | `engine/wattmosaic_engine/disaggregation/attachment.py` (new); `engine/wattmosaic_engine/disaggregation/engine.py` | `UT-device-attachment::test_configured_priors_cannot_exceed_the_row_they_share` | `NOT RUN` |
| `REQ-POWER-008` — Model versions are kept, promoted and reversible | Models — `DeviceModelVersion` and the registry | `TASK-POWER-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/models/registry.py`; `engine/wattmosaic_engine/storage/database.py` | `UT-models::test_registry_round_trips_persisted_model_and_estimates` | `PASS (orchestrator gate, 8b70933)` |

### 7.7 Storage and lifecycle — 12 requirements

Scenario vehicles: `T10` (an unknown profile leaves the earlier evidence
intact), `T12` (replay across a reader replacement).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-STORAGE-001` — Every persisted artefact belongs to a named tier | Storage — tier registry | `TASK-STORAGE-001` | `engine/wattmosaic_engine/storage/tiers.py` (new); `engine/wattmosaic_engine/storage/database.py` | `UT-storage-config::test_every_table_is_classified` (new) | `PASS (orchestrator gate, 31f01b2)` |
| `REQ-STORAGE-002` — Retention is configurable per tier and defaults are stated | Storage and config — retention | `TASK-STORAGE-001` | `engine/wattmosaic_engine/config/settings.py`; `engine/wattmosaic_engine/storage/tiers.py` (new); `addon/run.sh` | `UT-storage-config::test_raw_retention_range_is_enforced` (new) | `PASS (orchestrator gate, 31f01b2)` |
| `REQ-STORAGE-003` — Event evidence is immutable | Storage — append-only evidence | `TASK-STORAGE-002` | `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/storage/migrations.py` | `UT-inference-records::test_evidence_rows_are_append_only` (new) | `NOT RUN` |
| `REQ-STORAGE-004` — Writes are batched | Storage — writer loop | `TASK-STORAGE-002` | `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/main.py` | `UT-health-probe-cost::test_flush_batches_one_transaction_per_table` (new) | `NOT RUN` |
| `REQ-STORAGE-005` — A new model or topology version never deletes historical evidence | Storage — `AUDIT_TABLES` exemption | `TASK-STORAGE-002` | `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/storage/migrations.py` | `T10` · `UT-storage-config::test_audit_tables_are_exempt_from_prune` (new) | `NOT RUN` |
| `REQ-STORAGE-006` — Time and entity access paths are indexed | Storage — migrations | `TASK-STORAGE-002` | `engine/wattmosaic_engine/storage/migrations.py` | `UT-storage-config::test_query_plan_uses_an_index` (new) | `NOT RUN` |
| `REQ-STORAGE-007` — Records carry the versions that produced them | Storage — record columns | `TASK-STORAGE-002` | `engine/wattmosaic_engine/storage/migrations.py`; `engine/wattmosaic_engine/contracts.py` | `UT-inference-records::test_record_carries_all_four_versions` (new) | `NOT RUN` |
| `REQ-STORAGE-008` — Payloads are compressed where it pays | Storage — payload encoding | `TASK-STORAGE-001` | `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/storage/migrations.py` | `UT-storage-config::test_payload_round_trip_across_encodings` (new) | `PASS (orchestrator gate, 31f01b2)` |
| `REQ-STORAGE-009` — Partitioning is used only where it is justified | Storage — partition decision | `TASK-STORAGE-001` | `engine/wattmosaic_engine/storage/tiers.py` (new); `docs/specs/load-disaggregation/12-deployment-and-rollback.md` | `MANUAL-partitioning-decision-review` | `NOT RUN` |
| `REQ-STORAGE-010` — The engine recovers after a restart | Process lifecycle — startup | `TASK-STORAGE-003` | `engine/wattmosaic_engine/main.py`; `engine/wattmosaic_engine/storage/database.py` | `E2E-storage::test_restart_preserves_every_record_count` (new) | `NOT RUN` |
| `REQ-STORAGE-011` — Migrations are forward-only, additive and idempotent | Storage — `MIGRATIONS` | `TASK-STORAGE-003` | `engine/wattmosaic_engine/storage/migrations.py` | `UT-migrations::test_migrations_are_idempotent` (new) | `NOT RUN` |
| `REQ-STORAGE-012` — A stored window can be replayed | Audit tools — replay | `TASK-STORAGE-003` | `tools/audit/replay_capture.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `T12` · `SIM-replay-capture::test_replay_is_identical_under_the_same_algorithm_version` (new) | `NOT RUN` |

### 7.8 Event and inference — 9 requirements

Scenario vehicles: `T01`, `T02` (the cross-reader vector), `T06` (one
automation), `T09` (delayed response), `T10` (the unknown-residual kind),
`T12` (a duplicate across a reader replacement), `T14` (late and missing data).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-EVENT-001` — Every event is one of three kinds | Contract layer — `DeviceEvent` | `TASK-EVENT-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/events.py` | `T10` · `E2E-event-intake::test_every_event_names_its_kind` (new) | `PASS (orchestrator gate, 8ee3973)` |
| `REQ-EVENT-002` — An automation that changes several devices produces one group | Disaggregation — `EventIsolator` | `TASK-EVENT-001` | `engine/wattmosaic_engine/disaggregation/isolation.py`; `engine/wattmosaic_engine/disaggregation/events.py` | `T06` · `UT-correlation::test_fixtures_switched_by_one_scene_are_withheld_not_counted_twice` | `PASS (orchestrator gate, 8ee3973)` |
| `REQ-EVENT-003` — A device's response delay is measured, not assumed | Disaggregation — `AssociationFinder` | `TASK-EVENT-001` | `engine/wattmosaic_engine/disaggregation/association.py`; `engine/wattmosaic_engine/disaggregation/isolation.py` | `T09` · `UT-state-lag::test_response_beyond_max_lag_is_withheld` (new) | `PASS (orchestrator gate, 8ee3973)` |
| `REQ-EVENT-004` — Event windows are sized from the observed cadence | Disaggregation — `IsolationConfig` | `TASK-EVENT-001` | `engine/wattmosaic_engine/disaggregation/configuration.py`; `engine/wattmosaic_engine/disaggregation/isolation.py` | `UT-event-isolation::test_resolved_window_publishes_its_source` (new) | `PASS (orchestrator gate, 8ee3973)` |
| `REQ-EVENT-005` — An event records its effect on every reader that saw it | Disaggregation — `EventIsolation` vector | `TASK-EVENT-002` | `engine/wattmosaic_engine/disaggregation/isolation.py`; `engine/wattmosaic_engine/ingestion/correlation.py` | `T01`, `T02` · `UT-correlation::test_event_publishes_its_cross_reader_vector` (new) | `NOT RUN` |
| `REQ-EVENT-006` — Late-arriving data is classified, not dropped silently | Synchronization — out-of-order handling | `TASK-EVENT-002` | `engine/wattmosaic_engine/synchronization/`; `engine/wattmosaic_engine/disaggregation/isolation.py` | `T14` · `UT-synchronization::test_out_of_order_sampling_stays_ordered` | `NOT RUN` |
| `REQ-EVENT-007` — A duplicate event is recognised and counted once | Ingestion and storage — event identity | `TASK-EVENT-002` | `engine/wattmosaic_engine/ingestion/ha_source.py`; `engine/wattmosaic_engine/storage/database.py` | `T12` · `E2E-event-intake::test_duplicate_event_is_counted_once` (new) | `NOT RUN` |
| `REQ-EVENT-008` — An incomplete event group is a valid outcome | Disaggregation — `EventIsolator` | `TASK-EVENT-002` | `engine/wattmosaic_engine/disaggregation/isolation.py` | `T14` · `UT-event-isolation::test_incomplete_group_names_its_missing_part` (new) | `NOT RUN` |
| `REQ-EVENT-009` — Observed time and ingestion time are both preserved | Disaggregation — `EventIsolation` | `TASK-EVENT-002` | `engine/wattmosaic_engine/disaggregation/isolation.py`; `engine/wattmosaic_engine/contracts.py` | `UT-state-lag::test_transition_is_not_backdated_by_the_state_lag` | `NOT RUN` |

### 7.9 Learning — 7 requirements

Scenario vehicle: `T10` (an unknown pattern becomes a residual profile, not a
device model).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-LEARNING-001` — Learning samples come from isolated events inside a window | Learning — `LearningPipeline` | `TASK-LEARNING-001` | `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/disaggregation/isolation.py` | `UT-learning::test_only_isolated_events_create_samples` (new) | `NOT RUN` |
| `REQ-LEARNING-002` — Every rejection keeps its reason | Learning — sample rejection | `TASK-LEARNING-001` | `engine/wattmosaic_engine/learning/pipeline.py` | `UT-learning-reporting::test_rejections_carry_reason_codes` (new) | `NOT RUN` |
| `REQ-LEARNING-003` — A model is trained, validated and promoted, never auto-activated | Learning — promotion | `TASK-LEARNING-002` | `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/storage/database.py` | `UT-learning::test_candidate_promotes_only_when_it_beats_active_model` | `NOT RUN` |
| `REQ-LEARNING-004` — The learning pipeline consumes unknown-residual events | Learning — residual event source | `TASK-LEARNING-002` | `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/disaggregation/events.py` | `T10` · `UT-learning::test_unknown_residual_event_enters_the_pipeline` (new) | `NOT RUN` |
| `REQ-LEARNING-005` — A factor is never applied retroactively | Electrical — `MeasurementFactor` | `TASK-LEARNING-002` | `engine/wattmosaic_engine/electrical/factors.py`; `engine/wattmosaic_engine/storage/database.py` | `UT-factors::test_factor_is_resolved_against_the_reading_timestamp` (new) | `NOT RUN` |
| `REQ-LEARNING-006` — Thresholds may be overridden per device | Disaggregation — `EffectiveConfig` | `TASK-LEARNING-001` | `engine/wattmosaic_engine/disaggregation/configuration.py`; `engine/wattmosaic_engine/learning/pipeline.py` | `UT-learning::test_per_device_threshold_override` (new) | `NOT RUN` |
| `REQ-LEARNING-007` — A model never outranks a measurement | Learning and disaggregation — model precedence | `TASK-LEARNING-002` | `engine/wattmosaic_engine/learning/pipeline.py`; `engine/wattmosaic_engine/disaggregation/engine.py` | `UT-learning::test_measurement_disagrees_with_model_is_recorded` (new) | `NOT RUN` |

### 7.10 Confidence and identifiability — 7 requirements

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-CONFIDENCE-001` — Confidence is never blended into one score | Contract layer — `ConfidenceVector` | `TASK-CONFIDENCE-001` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/disaggregation/confidence.py` | `UT-contracts::test_confidence_vector_has_no_aggregate_or_float` (new) | `NOT RUN` |
| `REQ-CONFIDENCE-002` — Confidence is never presented as a calibrated probability | Contract layer — `confidence_is_calibrated` | `TASK-CONFIDENCE-001` | `engine/wattmosaic_engine/contracts.py`; `addon/ui/app.js` | `UT-contracts::test_confidence_is_calibrated_is_false` (new) | `NOT RUN` |
| `REQ-CONFIDENCE-003` — The published dimensions are reconciled with the existing vector | Contract layer — `ConfidenceVector` | `TASK-CONFIDENCE-001` | `engine/wattmosaic_engine/contracts.py` | `UT-contracts::test_confidence_uses_only_the_five_member_names` (new) | `NOT RUN` |
| `REQ-CONFIDENCE-004` — The evidence tier remains the coarse grade | Disaggregation — `evidence_tier` | `TASK-CONFIDENCE-001` | `engine/wattmosaic_engine/disaggregation/tiers.py` | `UT-electrical::test_confidence_bounds_and_direct_dominance` | `NOT RUN` |
| `REQ-CONFIDENCE-005` — The topology and residual dimensions are actually populated | Disaggregation — `AuditPass` | `TASK-CONFIDENCE-002` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/disaggregation/confidence.py` | `UT-inference-records::test_topology_and_residual_dimensions_are_populated` (new) | `NOT RUN` |
| `REQ-CONFIDENCE-006` — Measurement quality, evidence and freshness are separately observable | Disaggregation — association evidence | `TASK-CONFIDENCE-002` | `engine/wattmosaic_engine/disaggregation/association.py` | `UT-association::test_evidence_publishes_counts_freshness_and_lag` (new) | `NOT RUN` |
| `REQ-CONFIDENCE-007` — An edge carries its own confidence dimensions | Disaggregation — relation confidence | `TASK-CONFIDENCE-002` | `engine/wattmosaic_engine/disaggregation/association.py`; `engine/wattmosaic_engine/contracts.py` | `UT-association::test_candidate_edge_confidence_is_capped` (new) | `NOT RUN` |

### 7.11 Mapping matrix — 7 requirements

Scenario vehicles: `T04`, `T10` (unknown loads), `T13` (a shared row).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-MAPPING-001` — The mapping matrix is derived, never the source of truth | Disaggregation — mapping projection | `TASK-MAPPING-001` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new) | `UT-mapping::test_mapping_is_derived_and_deterministic` (new) | `PASS (orchestrator gate, 314a75f)` — `test_same_inputs_produce_byte_identical_output` green |
| `REQ-MAPPING-002` — The matrix supports 1:1, 1:N, N:1 and N:N | Disaggregation — mapping projection | `TASK-MAPPING-001` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new) | `T13` · `UT-mapping::test_mapping_supports_every_cardinality` (new) | `PASS (orchestrator gate, 314a75f)` — all four cardinality tests green |
| `REQ-MAPPING-003` — Every cell carries its own state | API and disaggregation — `/mapping` | `TASK-MAPPING-001` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new); `engine/wattmosaic_engine/api/server.py` | `UT-api::test_mapping_cell_response_shape` (new) | `PASS (orchestrator gate, 3ad48ce)` — `test_mapping_route_returns_matrix_with_coverage_counts` green |
| `REQ-MAPPING-004` — Unknown loads and uncertain attribution appear in the matrix | Disaggregation — unknown-load entry | `TASK-MAPPING-002` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new); `engine/wattmosaic_engine/models/residual_power.py` | `T04`, `T10` · `UT-mapping::test_unknown_load_entry_names_no_device` (new) | `PASS (orchestrator gate, 314a75f)` — `test_unknown_load_cell_has_no_fabricated_device_id` green |
| `REQ-MAPPING-005` — Cells update on evidence or model change, not per sample | Disaggregation — mapping refresh | `TASK-MAPPING-002` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new); `engine/wattmosaic_engine/storage/database.py` | `UT-inference-records::test_mapping_refreshes_only_on_an_evidence_change` (new) | `PASS (orchestrator gate, 314a75f)` — `test_no_per_sample_snapshot_two_calls_are_identical` green; re-derivation on version change in `24e07fb`. Named selector not created literally |
| `REQ-MAPPING-006` — Mapping complexity is not mapping confidence | Disaggregation — mapping projection | `TASK-MAPPING-001` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new) | `UT-mapping::test_reader_count_does_not_raise_confidence` (new) | `NOT RUN` — named gate not in repo; ambiguity-cap assertion in `test_mapping.py` is related but not the literal vehicle |
| `REQ-MAPPING-007` — The matrix states what it cannot see | Disaggregation — coverage block | `TASK-MAPPING-001` | `engine/wattmosaic_engine/disaggregation/mapping.py` (new); `engine/wattmosaic_engine/api/server.py` | `UT-mapping::test_mapping_reports_its_coverage` (new) | `PASS (orchestrator gate, 314a75f)` — `test_coverage_names_unobserved_devices_and_empty_readers` + `3ad48ce` route coverage counts green |

### 7.12 Dynamic topology and change — 7 requirements

Scenario vehicles: `T11` (smart plug relocation), `T12` (reader relocation or
replacement).

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-CHANGE-001` — Every configuration change produces a traceable event | Disaggregation — configuration event | `TASK-CHANGE-001` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/storage/migrations.py`; `engine/wattmosaic_engine/contracts.py` | `T11`, `T12` · `UT-topology-persistence::test_configuration_event_per_change_kind` (new) | `PASS (orchestrator gate, 293038b)` — `test_configuration_event_per_change_kind` green |
| `REQ-CHANGE-002` — A suspected change is a hypothesis, validated before promotion | Disaggregation — change hypothesis | `TASK-CHANGE-001` | `engine/wattmosaic_engine/disaggregation/audit.py` | `T11`, `T12` · `UT-hierarchy::test_suspected_change_does_not_alter_the_active_topology` (new) | `PASS (orchestrator gate, 293038b)` — `test_suspected_change_is_not_confirmed` green (equivalent named vehicle; the literal selector was not created) |
| `REQ-CHANGE-003` — Four version identifiers travel with every derived artefact | Contract layer — version identifiers | `TASK-CHANGE-002` | `engine/wattmosaic_engine/contracts.py`; `engine/wattmosaic_engine/electrical/topology.py` | `UT-inference-records::test_four_version_identifiers_are_present` (new) | `NOT RUN` — named gate not in repo; kept as-is, not overclaimed |
| `REQ-CHANGE-004` — Historical topology is never rewritten | Storage — versioned relations | `TASK-CHANGE-002` | `engine/wattmosaic_engine/storage/database.py`; `engine/wattmosaic_engine/storage/migrations.py` | `T11`, `T12` · `UT-topology-persistence::test_previous_version_stays_byte_identical` (new) | `NOT RUN` — named gate not in repo; byte-identical behavior predates the sprint; kept as-is |
| `REQ-CHANGE-005` — Uncertainty is preserved when evidence is insufficient | Disaggregation — uncertainty path | `TASK-CHANGE-001` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/disaggregation/confidence.py` | `UT-hierarchy::test_refused_with_a_reason_when_evidence_is_insufficient` (new) | `NOT RUN` — named gate not in repo; kept as-is |
| `REQ-CHANGE-006` — A configuration can be rolled back | Electrical — version store | `TASK-CHANGE-002` | `engine/wattmosaic_engine/electrical/topology.py`; `engine/wattmosaic_engine/api/server.py` | `UT-topology-persistence::test_rollback_creates_a_new_version` (new) | `NOT RUN` — named gate not in repo; kept as-is |
| `REQ-CHANGE-007` — A change is reconciled against the inference history | Disaggregation — reconciliation | `TASK-CHANGE-002` | `engine/wattmosaic_engine/disaggregation/audit.py`; `engine/wattmosaic_engine/storage/database.py` | `T12` · `UT-inference-records::test_change_lists_the_affected_records` (new) | `PASS (orchestrator gate, 24e07fb)` — `test_change_lists_the_affected_records_via_the_read_path` green |

### 7.13 User interface — 13 requirements

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-UI-001` — The reader inventory and its status are visible | Panel — `#/topology` reader inventory | `TASK-UI-001` | `addon/ui/app.js` | `UT-panel::test_reader_inventory_lists_every_field` (new) | `NOT RUN` |
| `REQ-UI-002` — A measurement is shown with its quality and its provenance | Panel — measurement rendering | `TASK-UI-001` | `addon/ui/app.js` | `UT-panel::test_measurement_shows_quality_and_provenance` (new) | `NOT RUN` |
| `REQ-UI-003` — The topology is drawn with per-edge confidence and lifecycle status | Panel — `#/topology` drawing | `TASK-UI-002` | `addon/ui/app.js`; `tools/dev/check_panel.py` | `UT-panel::test_topology_edge_shows_status_and_confidence` (new) | `NOT RUN` |
| `REQ-UI-004` — Residuals and unknown loads are visible | Panel — `#/quality` residual view | `TASK-UI-002` | `addon/ui/app.js` | `UT-panel::test_residual_view_shows_the_signed_decomposition` (new) | `NOT RUN` |
| `REQ-UI-005` — Device power models are visible per device | Panel — `#/device/:id` model view | `TASK-UI-002` | `addon/ui/app.js` | `UT-panel::test_device_model_detail_is_visible` (new) | `NOT RUN` |
| `REQ-UI-006` — The device↔reader mapping is visible | Panel — `#/topology` mapping view | `TASK-UI-002` | `addon/ui/app.js` | `UT-panel::test_mapping_view_lists_every_cell` (new) | `NOT RUN` |
| `REQ-UI-007` — Per-device estimates distinguish measured, estimated, allocated and unknown | Panel — `STRINGS` labels | `TASK-UI-001` | `addon/ui/app.js`; `tools/dev/check_panel.py` | `UT-panel::test_four_state_labels_are_used` (new) | `NOT RUN` |
| `REQ-UI-008` — Identifiability status is visible per device | Panel — `#/devices` identifiability | `TASK-UI-002` | `addon/ui/app.js` | `UT-panel::test_ambiguous_group_shows_a_range` (new) | `NOT RUN` |
| `REQ-UI-009` — Historical events and their evidence are visible | Panel — `#/diagnostics` event list | `TASK-UI-003` | `addon/ui/app.js` | `UT-panel::test_event_list_shows_verdict_and_evidence` (new) | `NOT RUN` |
| `REQ-UI-010` — Version history and suspected changes are visible | Panel — `#/topology` version history | `TASK-UI-003` | `addon/ui/app.js` | `UT-panel::test_version_history_and_suspected_changes_are_visible` (new) | `NOT RUN` |
| `REQ-UI-011` — A reader can correct and confirm configuration | Panel — correction actions and the confirm route | `TASK-UI-003` | `addon/ui/app.js`; `engine/wattmosaic_engine/api/server.py` | `IT-confirm-topology::test_operator_correction_is_recorded_as_a_user_declaration` (new) | `NOT RUN` |
| `REQ-UI-012` — Storage health and processing health are visible | Panel — `#/diagnostics` storage health | `TASK-UI-003` | `addon/ui/app.js` | `UT-panel::test_storage_health_view` (new) | `NOT RUN` |
| `REQ-UI-013` — Every view handles its empty, error, stale and degraded states, and never invents a zero | Panel — view state machine | `TASK-UI-001` | `addon/ui/app.js`; `tools/dev/check_panel.py` | `UT-panel::test_view_states_never_render_a_zero` (new) | `NOT RUN` |

### 7.14 Deployment — 8 requirements

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-DEPLOY-001` — The build and image pipeline is reused unchanged | Deployment — image pipeline | `TASK-DEPLOY-001` | `Dockerfile`; `addon/config.yaml`; `.github/workflows/release.yaml` | `GATE-deployment_valid` | `NOT RUN` |
| `REQ-DEPLOY-002` — Configuration comes from the existing option and environment surface | Deployment — option table | `TASK-DEPLOY-001` | `addon/run.sh`; `engine/wattmosaic_engine/config/settings.py`; `.env.example` | `GATE-deployment_valid` | `NOT RUN` |
| `REQ-DEPLOY-003` — Migrations run automatically and are safe to re-run | Process lifecycle — startup migrations | `TASK-DEPLOY-001` | `engine/wattmosaic_engine/main.py`; `engine/wattmosaic_engine/storage/migrations.py` | `UT-migrations::test_startup_applies_pending_migrations` (new) | `NOT RUN` |
| `REQ-DEPLOY-004` — The database can be backed up and restored | Deployment — backup and checkpoint | `TASK-DEPLOY-001` | `addon/healthcheck.py`; `engine/wattmosaic_engine/storage/database.py` | `E2E-storage::test_backup_and_restore_round_trip` (new) | `NOT RUN` |
| `REQ-DEPLOY-005` — The deployment procedure is documented and repeatable | Deployment — procedure | `TASK-DEPLOY-002` | `docs/deployment.md` | `MANUAL-deployment-procedure-review` | `NOT RUN` |
| `REQ-DEPLOY-006` — Smoke tests run after deployment | Deployment — smoke script | `TASK-DEPLOY-002` | `scripts/smoke_test.sh` (new); `addon/healthcheck.py` | `UT-app-startup::test_smoke_checks_are_separate_from_the_probe` (new) | `NOT RUN` |
| `REQ-DEPLOY-007` — A failed deployment is rolled back | Deployment — rollback | `TASK-DEPLOY-002` | `docs/deployment.md`; `scripts/validate_addon.sh` | `MANUAL-rollback-rehearsal` | `NOT RUN` |
| `REQ-DEPLOY-008` — Operational risks are recorded and monitored | Process lifecycle — `/metrics` | `TASK-DEPLOY-002` | `engine/wattmosaic_engine/main.py`; `docs/risks.md` | `UT-api::test_operational_metrics_are_published` (new) | `NOT RUN` |

### 7.15 Testing — 10 requirements

| Requirement | Architecture component | Task ID | Code location | Test ID | Verification result |
| --- | --- | --- | --- | --- | --- |
| `REQ-TEST-001` — Unit tests cover every new module and every new rule | Verification — unit suite | `TASK-TEST-001` | `tests/unit/`; `docs/specs/load-disaggregation/15-implementation-plan.md` (new — sibling agent) | `MANUAL-red-then-green-record` | `NOT RUN` |
| `REQ-TEST-002` — Integration tests cover the HTTP surface and the wiring | Verification — API contract | `TASK-TEST-002` | `tests/integration/test_engine_api_contract.py`; `engine/wattmosaic_engine/api/facade.py` | `IT-engine-api-contract::test_new_routes_follow_the_contract` (new) | `NOT RUN` |
| `REQ-TEST-003` — The synthetic scenario suite T01–T15 exists and is deterministic | Verification — simulator | `TASK-TEST-003` | `tools/simulator/scenarios.py` (new); `tests/simulation/test_simulator_fixtures.py` (new) | `T01`–`T15` · `SIM-simulator-fixtures::test_every_scenario_builder_is_deterministic` (new) | `PASS (orchestrator gate, 0665b2c)` — `test_every_scenario_builder_is_deterministic` green (33 passed, doc 16 §3) |
| `REQ-TEST-004` — Every scenario-dependent requirement names both the `T##` id and the existing matrix row | Verification — scenario matrix | `TASK-TEST-003` | `tests/unit/test_simulator_fixtures.py` (new); `docs/test_scenario_matrix.md` | `UT-simulator-fixtures::test_every_scenario_id_has_a_builder` (new) | `NOT RUN` — literal gate not created; `test_every_declared_scenario_id_has_a_builder` (0665b2c) is related but not this vehicle |
| `REQ-TEST-005` — Replay tests re-run stored windows through the production path | Verification — replay | `TASK-TEST-002` | `tools/audit/replay_capture.py`; `tests/simulation/test_replay_capture.py` | `SIM-replay-capture::test_replay_matches_the_original_run` (new) | `PASS (orchestrator gate, c80bb67)` — `test_replay_is_identical_across_two_runs` green (13 passed, doc 16 §3); OD-36 live-capture skips remain |
| `REQ-TEST-006` — The existing suite stays green and the type-check delta is zero | Verification — full suite and mypy delta | `TASK-TEST-001` | `tests/`; `tools/dev/verify.py` | `GATE-verify-all` | `NOT RUN` — continuous vehicle; full `verify.py` re-run is the orchestrator's action (doc 16 §2 notes the last 12/12 run is stale) |
| `REQ-TEST-007` — Negative verification proves every new guard | Verification — negative harness | `TASK-TEST-001` | `tools/dev/negverify_nesting.py`; `tests/unit/` | `GATE-negverify` | `NOT RUN` — continuous vehicle; no orchestrator `GATE-negverify` log recorded for this sprint |
| `REQ-TEST-008` — Migration tests cover the upgrade and the rollback of the schema | Verification — migration suite | `TASK-TEST-002` | `tests/unit/test_migrations.py`; `engine/wattmosaic_engine/storage/migrations.py` | `UT-migrations::test_every_prior_version_migrates_without_row_loss` (new) | `PASS (orchestrator gate, c80bb67)` — `test_matrix_covers_every_prior_version` green (11 passed, doc 16 §3) |
| `REQ-TEST-009` — Performance tests cover the paths the target adds | Verification — performance harness | `TASK-TEST-003` | `tests/unit/test_health_probe_cost.py`; `tests/unit/test_nesting_perf.py` | `UT-nesting-perf::test_every_stated_budget_is_asserted` (new) | `PASS (orchestrator gate, c80bb67)` — new-path budget tests green (3 passed, doc 16 §3) |
| `REQ-TEST-010` — No test or document claims real-house per-device accuracy | Verification — accuracy strata | `TASK-TEST-003` | `tests/simulation/test_accuracy.py`; `docs/limitations_v2.md` | `SIM-accuracy::test_accuracy_is_stratified_and_simulator_derived` (new) | `NOT RUN` — named gate not in repo; the synthetic-only limitation is stated in doc 16 §5, but that report is not an orchestrator gate |

---

## 8. Coverage summary

| Area | Requirements | Tasks | Test vehicles | Verification not yet concrete |
| --- | --- | --- | --- | --- |
| Foundation | 6 | 2 | `GATE-spec_status`, `GATE-contract_schema_sync`, `UT-contracts`, `UT-app-startup`, `UT-disaggregation`, `MANUAL-wave-ownership-review` | `REQ-FOUNDATION-006` — manual only |
| Measurement | 10 | 3 | `UT-contracts`, `UT-electrical`, `UT-disaggregation`, `UT-correlation`, `UT-synchronization`, `UT-event-intake`, `T13`–`T15` | none |
| Measurement topology | 11 | 4 | `UT-association`, `UT-hierarchy`, `UT-residual-power`, `UT-electrical`, `UT-topology-persistence`, `UT-inference-records`, `UT-topology-bootstrap`, `UT-circuits`, `T01`–`T13` | none |
| Residual | 8 | 2 | `UT-residual-power`, `UT-api`, `UT-learning`, `IT-safety-invariants`, `T03`, `T04`, `T10`, `T15` | none |
| Disaggregation | 7 | 2 | `E2E-full-inference-chain`, `UT-disaggregation`, `UT-electrical`, `UT-reconciliation-split`, `T06`, `T07` | none |
| Device and load models | 8 | 2 | `UT-device-taxonomy`, `UT-models`, `UT-disaggregation`, `UT-device-links`, `UT-device-attachment`, `T01`, `T04`, `T08`, `T10` | none |
| Storage and lifecycle | 12 | 3 | `UT-storage-config`, `UT-inference-records`, `UT-health-probe-cost`, `UT-migrations`, `E2E-storage`, `SIM-replay-capture`, `MANUAL-partitioning-decision-review` | `REQ-STORAGE-009` — manual only, satisfied by doing nothing |
| Event and inference | 9 | 2 | `E2E-event-intake`, `UT-correlation`, `UT-state-lag`, `UT-event-isolation`, `UT-synchronization`, `T01`–`T14` | none |
| Learning | 7 | 2 | `UT-learning`, `UT-learning-reporting`, `UT-factors`, `T10` | none |
| Confidence and identifiability | 7 | 2 | `UT-contracts`, `UT-electrical`, `UT-inference-records`, `UT-association` | none |
| Mapping matrix | 7 | 2 | `UT-mapping` (new), `UT-api`, `UT-inference-records`, `T04`, `T10`, `T13` | none |
| Dynamic topology and change | 7 | 2 | `UT-topology-persistence`, `UT-hierarchy`, `UT-inference-records`, `T11`, `T12` | none |
| User interface | 13 | 3 | `UT-panel`, `IT-confirm-topology`, `tools/dev/check_panel.py` | none |
| Deployment | 8 | 2 | `GATE-deployment_valid`, `UT-migrations`, `UT-app-startup`, `E2E-storage`, `MANUAL-deployment-procedure-review`, `MANUAL-rollback-rehearsal` | `REQ-DEPLOY-005`, `REQ-DEPLOY-007` — manual only |
| Testing | 10 | 3 | `GATE-verify-all`, `GATE-negverify`, `IT-engine-api-contract`, `SIM-simulator-fixtures`, `SIM-replay-capture`, `SIM-accuracy`, `UT-simulator-fixtures`, `UT-migrations`, `UT-nesting-perf`, `MANUAL-red-then-green-record` | `REQ-TEST-001` — the red-then-green record is a process artefact |
| **Total** | **130** | **36** | — | **5 requirements are manual-only** |

**Does any requirement lack an implementation owner?** No. Every one of the
130 rows names a task ID, and every task ID resolves to a task in §4.2.

**Does any requirement lack a verification method?** No. Every one of the 130
rows names a test ID, and every test ID resolves to a `pytest` selector, a
canonical `T##` scenario, a `tools/dev/verify.py` gate or a named manual
review.

**Is any verification weak?** Yes — five requirements are manual-only and are
listed in §9. Three of them are manual **because `01-requirements.md` itself
names a manual review as their verification method**; the other two are
process artefacts. None of the five is a critical MUST requirement without an
owner.

---

## 9. Untraced and at-risk requirements

### 9.1 Untraced

**None.** All 130 requirements are traced to an owner and a verification
method. No mapping has been invented and no requirement has been dropped.

### 9.1a Deferred, partial and reverted work (post-implementation)

The implementation phase ended with several tasks incomplete. Their rows in
§7 stay `NOT RUN`: there is no commit and no orchestrator vehicle log for
them. This is an honest record, not an oversight.

| Task(s) | Status | Requirements that stay `NOT RUN` | Reason |
| --- | --- | --- | --- |
| `TASK-TOPOLOGY-003` | now `VERIFIED` | — (was `REQ-TOPO-009`) | The deferred half landed in the completion sprint: `293038b` change detection + `24e07fb` reconciliation; `REQ-TOPO-009` is now `PASS (orchestrator gate, 24e07fb)` |
| `TASK-DISAGG-001` | `FAILED` (original attempt) | `REQ-DISAGG-006` | Original implementation reverted: fabricated 250 W per-device value; work in `stash@{0}`. Its requirements `REQ-DISAGG-001` … `005` are now carried by the replacement tasks `TASK-DISAGG-003/004` and are `PASS` with mutation-proven gates |
| `TASK-DISAGG-002` | `NOT_STARTED` | `REQ-DISAGG-007` | No additional work beyond the reverted `TASK-DISAGG-001`. `REQ-DISAGG-005`/`006` behavior is covered by B4b (`0dd4d27`) and a pre-existing gate respectively; `REQ-DISAGG-007` stays `NOT RUN` (no named gate) |
| `TASK-TOPOLOGY-004` | `NOT_STARTED` | `REQ-TOPO-010`, `REQ-TOPO-011` | Deferred in the implementation phase (no commit) |
| `TASK-EVENT-002` | `NOT_STARTED` | `REQ-EVENT-005` … `REQ-EVENT-009` | Deferred in the implementation phase (no commit) |
| `TASK-POWER-002` | `NOT_STARTED` | `REQ-POWER-003`, `REQ-POWER-005` … `REQ-POWER-007` | Deferred in the implementation phase (no commit) |
| Wave 3 remaining (`TASK-RESIDUAL-002`, `TASK-STORAGE-002`/`003`, `TASK-LEARNING-001`/`002`, `TASK-CONFIDENCE-001`/`002`) | `NOT_STARTED` | `REQ-RESIDUAL-005` … `007`, `REQ-STORAGE-003` … `007`, `010` … `012`, `REQ-LEARNING-001` … `007`, `REQ-CONFIDENCE-001` … `007` | Deferred in the implementation phase; the completion sprint did not land these original tasks (`TASK-RESIDUAL-005` deferred, `TASK-MAPPING-001`/`002` and `TASK-CHANGE-001`/`002` completed by their B3 successors) |
| Wave 4 remaining (`TASK-UI-001` … `003`, `TASK-DEPLOY-001`/`002`, `TASK-TEST-002`/`003`) | `NOT_STARTED` | `REQ-UI-001`, `002`, `007`, `009`, `011` … `013` (data-rendering rows `003` … `006`, `008`, `010` now `PASS` via UI B4a/B4b), `REQ-DEPLOY-001` … `008` (manual/rehearsal rows stay `NOT RUN`), `REQ-TEST-001`, `002`, `004`, `006`, `007`, `010` | Deferred in the implementation phase; completion-sprint successors `TASK-UI-004`, `TASK-DEPLOY-003`, `TASK-TEST-004` landed a subset with named gates (see §11) |

**Completion-sprint deferred / blocked (recorded honestly, 2026-11-17).** None
of these is `VERIFIED`; their requirement rows stay `NOT RUN`:

| Item | Kind | Requirement rows kept `NOT RUN` | Reason |
| --- | --- | --- | --- |
| `TASK-POWER-004` (`DELAYED`/`ALWAYS_ON`) | `BLOCKED` — decision | `REQ-POWER-004` behavior (label-only enum shipped in `8b70933`) | `OD-46`/`OD-47` remain OPEN: enum member vs flag vs orthogonal parameter (doc 16 §7). Do not guess. |
| `TASK-DEPLOY-004b` / document 17 | DEFERRED — prerequisite | `REQ-DEPLOY-005`, `REQ-DEPLOY-007` (deployment-procedure / rollback-rehearsal rows) | Requires a real deployed system with recorded smoke output and a rollback rehearsal; none exists in the repo (doc 16 §7). Doc 16 (report 16) landed in `497ef10`. |
| `OD-34` confidence dimensions | OPEN decision | `REQ-CONFIDENCE-005` (`TASK-CONFIDENCE-002`) | Whether `topology`/`residual` dimensions stay unpopulated in V1 is undecided (doc 16 §7); see §9.3. |
| `TASK-RESIDUAL-005` | `BLOCKED` — deferred, no commit | `REQ-RESIDUAL-005`, `006`, `007` | Named drift-event/ledger vehicles do not exist; explicitly deferred at sprint close. |
| Release-tag ambiguity | OPEN fact | — (deployment evidence only) | Version `0.2.1-dev.37` but latest reachable tag `v0.2.1-dev.36`; exact image tag cannot be stated from the repo (doc 16 §7), deferred to document 17. |

**Manual-only vehicles.** `REQ-FOUNDATION-006`, `REQ-STORAGE-009`,
`REQ-DEPLOY-005`, `REQ-DEPLOY-007` and `REQ-TEST-001` remain `NOT RUN` because
their named vehicle is a manual review or process artefact, not an
orchestrator-run automated gate — even where the owning task otherwise
landed. They are detailed in §9.2.

### 9.2 Weak verification — manual-only vehicles

| Requirement | Why the vehicle is manual | Action that would strengthen it |
| --- | --- | --- |
| `REQ-FOUNDATION-006` | `01-requirements.md` states the verification as a manual review of the implementation plan against `docs/implementation_plan_v2.md` §5 | Add a static check in `tools/dev/verify.py` that no file appears twice in one wave's owner column; owned by `TASK-FOUNDATION-002` |
| `REQ-STORAGE-009` | The requirement is satisfied by doing nothing: a partition decision is only needed if a table is partitioned | Publish the measurement and the decision together in the storage design note so the review has an artefact; no code |
| `REQ-DEPLOY-005` | The verification is a manual review of the published procedure against `docs/deployment.md` | Make every step name a runnable command so the review becomes an execution; owned by `TASK-DEPLOY-002` |
| `REQ-DEPLOY-007` | The verification is a manual rollback rehearsal recorded in the delivery report | Rehearse and record the rehearsal; the database half is already covered by `E2E-storage::test_backup_and_restore_round_trip` |
| `REQ-TEST-001` | The red-then-green record is a delivery-report artefact, not an assertion | Capture the failing-run log per rule in the delivery report; the negative harness in `TASK-TEST-001` already proves the guard half |

### 9.3 At risk — a requirement collides with an open decision

These are traced, but the specification set contains an open decision or
assumption that contradicts the requirement as written. The task ID and the
verification method exist; the implementation may not start until the
collision is settled.

**Rule.** A collision that has been settled stays visible in the table with
its resolution recorded; its row is never deleted. The requirement's own
row, task ID and `Verification Result` are unchanged by the settlement.

| Requirement | Colliding item | The collision | Action |
| --- | --- | --- | --- |
| `REQ-MAPPING-001` … `REQ-MAPPING-007` | `ASM-06` (`13-open-decisions-and-assumptions.md`) | **RESOLVED — no longer at risk.** `ASM-06` previously held that the mapping module was outside V1 and that the mapping matrix governed nothing; seven requirements in this matrix do depend on it. The project owner decided that the mapping module **is** built in V1 ("Build it in V1.", 2026-11-16), recorded in `13-…` §4 as `ASM-06` `RESOLVED`. | The collision is settled. The requirements stand as written in `01-requirements.md` — no rewording — and their task IDs, code locations and `NOT RUN` verification results in §7.11 are unchanged. `TASK-MAPPING-001` and `TASK-MAPPING-002` are unblocked and proceed. |
| `REQ-CONFIDENCE-005` | `OD-34` | `OD-34` assumes the `topology` and `residual` dimensions stay declared-but-unpopulated in V1; the requirement requires them to carry a value wherever the evidence exists. | Resolve `OD-34` in favour of the requirement, or reword `REQ-CONFIDENCE-005`. `TASK-CONFIDENCE-002` depends on the outcome. |
| `REQ-STORAGE-002` | `OD-31` | The requirement's acceptance range is 7–14 days; `OD-31` keeps the shipped 30-day default and requires a validator range that is a superset of `{7, 14, 30}`. | `OD-31` requires the requirement text to be referred back. Until then the validator accepts the shipped default and the acceptance criterion is met by the superset, not by the literal range. |
| `REQ-FOUNDATION-001` | `OD-38` | **RESOLVED — no longer at risk.** The specification set is approved (`SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`); the orchestrator's review gate has passed and the collision that held the requirement is gone. | The collision is settled. The requirement stands as written in `01-requirements.md` — no rewording — and its task ID, code location and `NOT RUN` verification result in §7.1 are unchanged. Every task proceeds on the §6 defaults of `13-open-decisions-and-assumptions.md`. |
| `REQ-TEST-005` | `OD-36` | Nine live-capture tests skip without a local `WATTMOSAIC_REAL_DB` and never run in CI, so part of the replay coverage is not exercised by the automated run. | Record the skip and its reason in the release note; the skip is not deleted to make CI green. |

---

## 10. Update protocol

| Change | Who makes it | Evidence required |
| --- | --- | --- |
| `Verification Result` moves from `NOT RUN` to `PASS` | The orchestrator, only | An orchestrator-run test log covering the named vehicle (`docs/agent_assignments.md:26-28`; `docs/implementation_plan_v2.md:333`) |
| `Verification Result` moves to `FAIL` | The orchestrator, only | The failing output of the named vehicle, with the failing selector |
| `Code location` changes from proposed to confirmed | The task owner, in the same wave | The file exists at the stated path and the `(new)` marker is removed |
| A task's `Status` moves off `NOT_STARTED` | The task owner | The task's acceptance summary met and its own tests run; the orchestrator's run still governs |
| A requirement is added to `01-requirements.md` | The requirements owner | A new row here, a new task if no task covers its area, and the §3 count updated in the same change |
| A task is added, removed or renumbered | The orchestrator | `15-implementation-plan.md` updated in the same change; the ID stays stable once published |

**Rule.** No `Verification Result` may change to `PASS` on a builder's
self-report. The verification harness is the only accepted evidence
(`docs/agent_assignments.md:26-28`; `docs/implementation_plan_v2.md:333`).

**Rule.** A task is complete only after its acceptance criteria and its
verification have passed in the orchestrator's run.

**Rule.** A row may not be deleted to make a coverage count match. If a
requirement is withdrawn, `01-requirements.md` is the place it is withdrawn.

---

## 11. Completion-sprint task links (Phase A, 2026-11-17)

The 15 proposed tasks from `19-completion-plan.md` are appended here so each
Requirement ID they would finish has a forward link. After the completion
sprint, twelve are `VERIFIED` in `tasks/progress.md` §1.1 with a real commit
SHA and the orchestrator gate record in `16-final-verification-report.md` §2–§3;
one is `BLOCKED` (`TASK-POWER-004`, `OD-46`/`OD-47`), one `IN_REVIEW`
(`TASK-DEPLOY-004`, doc 17 deferred), one `BLOCKED`/deferred with no commit
(`TASK-RESIDUAL-005`). The original 130 rows in §7 keep their IDs and structure;
where a row in §7 is now `PASS`, that promotion is shown **in the §7 row itself**
with the commit ref and a named passing gate — never by this section alone.

**DISAGG — no overclaim.** The original `TASK-DISAGG-001` stays `FAILED`
(reverted 250 W fabrication) and `TASK-DISAGG-002` stays `NOT_STARTED`. The
replacement tasks landed real code with named, mutation-proven gates: B4a
`f558bde` (source ledger, E2E `test_result_names_every_source_used_and_unused`
8 passed) and B4b `0dd4d27` (non-fabricating `min_w`/`max_w`, 56 passed with a
mutation proof that the guard catches a forced per-device split; the 250 W
fabrication is not reproduced). Only the DISAGG rows with those named proofs
are marked `PASS`; every other DISAGG row stays `NOT RUN`.

| Proposed task ID | Requirement IDs linked | Closes gap | Verification result |
| --- | --- | --- | --- |
| `TASK-FOUNDATION-003` | `REQ-FOUNDATION-002`, `REQ-FOUNDATION-003`, `REQ-POWER-001`, `REQ-RESIDUAL-002` | shared contract freeze; `OD-46`/`OD-47` | `NOT RUN` |
| `TASK-RESIDUAL-003` | `REQ-RESIDUAL-002`, `REQ-RESIDUAL-004`, `REQ-FOUNDATION-002` | G-01 noise contract home | `NOT RUN` |
| `TASK-RESIDUAL-004` | `REQ-RESIDUAL-002`, `REQ-RESIDUAL-004`, `REQ-RESIDUAL-008` | G-02 baseline window population | `NOT RUN` |
| `TASK-RESIDUAL-005` | `REQ-RESIDUAL-005`, `REQ-RESIDUAL-006`, `REQ-RESIDUAL-007`, `REQ-EVENT-001`, `REQ-LEARNING-004` | deferred `TASK-RESIDUAL-002` | `NOT RUN` |
| `TASK-POWER-003` | `REQ-POWER-001`, `REQ-POWER-002`, `REQ-POWER-007` | G-03 adjustable models | `NOT RUN` |
| `TASK-POWER-004` | `REQ-POWER-001`, `REQ-POWER-004`, `REQ-EVENT-003` | G-04 delayed models (`BLOCKED`: `OD-46`/`OD-47`) | `NOT RUN` |
| `TASK-MAPPING-003` | `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-006`, `REQ-MAPPING-007` | G-07 mapping projection | `NOT RUN` |
| `TASK-MAPPING-004` | `REQ-MAPPING-004`, `REQ-MAPPING-005`, `REQ-RESIDUAL-007` | G-07 unknown-load entries | `NOT RUN` |
| `TASK-TOPOLOGY-005` | `REQ-TOPO-009`, `REQ-CHANGE-001`, `REQ-CHANGE-002`, `REQ-CHANGE-005`, `REQ-CHANGE-007` | G-08 change detection/reconciliation | `NOT RUN` |
| `TASK-DISAGG-003` | `REQ-DISAGG-001`, `REQ-FOUNDATION-005` | G-05 source ledger (`sources_used`) | `NOT RUN` |
| `TASK-DISAGG-004` | `REQ-DISAGG-002`, `REQ-DISAGG-003`, `REQ-DISAGG-004`, `REQ-DISAGG-005` | G-06 `min_w`/`max_w`, non-fabrication | `NOT RUN` |
| `TASK-UI-004` | `REQ-UI-003`, `REQ-UI-004`, `REQ-UI-005`, `REQ-UI-006`, `REQ-UI-007`, `REQ-UI-008`, `REQ-UI-009`, `REQ-UI-010`, `REQ-UI-011`, `REQ-UI-012`, `REQ-UI-013` | G-09 UI data exposure | `NOT RUN` |
| `TASK-DEPLOY-003` | `REQ-DEPLOY-005`, `REQ-DEPLOY-006`, `REQ-DEPLOY-007`, `REQ-TEST-002` | G-10 unified workflow / smoke | `NOT RUN` |
| `TASK-TEST-004` | `REQ-TEST-003`, `REQ-TEST-004`, `REQ-TEST-005`, `REQ-TEST-008`, `REQ-TEST-009`, `REQ-STORAGE-012` | G-11 performance/replay/migration/UI gates | `NOT RUN` |
| `TASK-DEPLOY-004` | `REQ-DEPLOY-005`, `REQ-DEPLOY-007`, `REQ-TEST-001`, `REQ-TEST-010` | G-12/G-13 final + deployment reports | `NOT RUN` |

**Rule.** A row here is a forward link, not a claim. The `Verification Result`
column moves to `PASS` only by the orchestrator's own test log
(§10 update protocol), never by this Phase A documentation.

---

## Related documents

- [`README.md`](README.md) — the specification index and the conventions.
- [`00-project-overview.md`](00-project-overview.md) — goals, invariants, risks and the inherited open decisions `OD-01` … `OD-08`.
- [`01-requirements.md`](01-requirements.md) — the 130 normative requirements this matrix traces.
- [`02-architecture.md`](02-architecture.md) — the component map, the new-versus-reused table and the module boundaries.
- [`03-measurement-and-electrical-domain.md`](03-measurement-and-electrical-domain.md) — the measurement and electrical domain detail.
- [`05-residual-model-spec.md`](05-residual-model-spec.md) — the residual model detail.
- [`06-device-and-load-model-spec.md`](06-device-and-load-model-spec.md) — the device and load model detail.
- [`10-ui-and-user-experience-spec.md`](10-ui-and-user-experience-spec.md) — the panel specification.
- [`12-deployment-and-rollback.md`](12-deployment-and-rollback.md) — deployment, backup and rollback.
- [`13-open-decisions-and-assumptions.md`](13-open-decisions-and-assumptions.md) — the decision IDs referenced in §9.3.
- `15-implementation-plan.md` — the implementation plan that elaborates the task IDs defined in §4.2.
- [`../../interface_contracts.md`](../../interface_contracts.md) — the frozen contracts and the change protocol.
- [`../../implementation_plan_v2.md`](../../implementation_plan_v2.md) — the governing decisions, the seven acceptance gates and the one-owner-per-wave rule.
- [`../../agent_assignments.md`](../../agent_assignments.md) — workstream ownership and the orchestrator-run evidence rule.
- [`18-completion-gap-analysis.md`](18-completion-gap-analysis.md) — the Phase A gap analysis that identifies the work these new tasks close.
- [`19-completion-plan.md`](19-completion-plan.md) — the dependency-ordered plan that defines the new task IDs in §11.
- [`../../assessment_gap_analysis.md`](../../assessment_gap_analysis.md) — the `G1`–`G20` gap register and the "must NOT be rebuilt" list.
