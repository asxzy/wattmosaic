# Progress tracker — load disaggregation implementation

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Single source of truth for task status.**

**Rule.** A task is complete only after its acceptance criteria **and** its
verification have passed. Code having been written is not completion. A task
that has been implemented but not verified is `IN_REVIEW`, not `VERIFIED`.

**Rule.** The status vocabulary is closed. No other value may appear in the
status column.

| Status | Meaning | Who sets it |
| --- | --- | --- |
| `NOT_STARTED` | the task has not been picked up | the default state of every task |
| `READY` | every dependency is `VERIFIED` and the task may start | the orchestrator |
| `IN_PROGRESS` | a builder is working on it | the builder |
| `BLOCKED` | a dependency, a permission or a decision stops it | the builder, with the blocker named below |
| `IN_REVIEW` | the builder delivered and the orchestrator is reviewing | the builder |
| `VERIFIED` | the orchestrator ran the named vehicle and it passed | **the orchestrator only** |
| `FAILED` | the named vehicle failed | the orchestrator |

**Rule.** `VERIFIED` requires an orchestrator-run test log
(`docs/agent_assignments.md:26-28`). A builder's self-report never sets it.

---

## 1. Task status table

| Task ID | Title | Requirement IDs | Wave | Segment | Status | Owner | Last updated | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `TASK-FOUNDATION-001` | Specification status and the additive contract workflow | `REQ-FOUNDATION-001`, `REQ-FOUNDATION-002`, `REQ-FOUNDATION-003` | 0 | 0.1 | `VERIFIED` | — | 2026-11-17 | Landed in `bfb9938`: CONTRACT_VERSION 2.0.0→2.1.0, 10 new contracts + 6 enums, schemas regenerated, `spec_status` gate added; tests + no schema drift verified at 4cd3545 |
| `TASK-FOUNDATION-002` | Runtime boundary, the four-state label and one owner per wave | `REQ-FOUNDATION-004`, `REQ-FOUNDATION-005`, `REQ-FOUNDATION-006` | 0 | 0.2 | `VERIFIED` | — | 2026-11-17 | Landed in `f5889b4`: four-state PowerState label, power_state_for derivation; runtime-boundary tests passed at 4cd3545 |
| `TASK-TEST-001` | Unit-rule coverage, regression and negative verification | `REQ-TEST-001`, `REQ-TEST-006`, `REQ-TEST-007` | 0 (continuous) | continuous | `IN_PROGRESS` | — | 2026-11-17 | Continuous verification vehicle — no single owning commit; gates ran throughout the implementation phase and final verification at 4cd3545 is green. Kept IN_PROGRESS as an ongoing vehicle. |
| `TASK-MEASURE-001` | Measurable quantities, units, phase and polarity | `REQ-MEASURE-001`, `REQ-MEASURE-002`, `REQ-MEASURE-003`, `REQ-MEASURE-004` | 1 | 1.1 | `VERIFIED` | — | 2026-11-17 | Landed in `cd71d4e`: unit whitelist, no-unmeasurable-field, phase declaration and polarity tests (behaviour already existed; tests pin it) |
| `TASK-MEASURE-002` | Leg pairs, reverse flow and the reader declaration surface | `REQ-MEASURE-005`, `REQ-MEASURE-006`, `REQ-MEASURE-007` | 1 | 1.2 | `VERIFIED` | — | 2026-11-17 | Landed in `415ef9e`: leg-pair atomicity tests, publish_reader_declarations helper, reverse-flow sign preserved |
| `TASK-MEASURE-003` | Timestamps, repeat suppression and cadence derivation | `REQ-MEASURE-008`, `REQ-MEASURE-009`, `REQ-MEASURE-010` | 1 | 1.2 | `VERIFIED` | — | 2026-11-17 | Landed in `76af12d`: resolved cadence with source, static 2 s gate (tools/dev/check_cadence.py), 3 tests; cadence-default ruling recorded in `15c9a3f` (docs) |
| `TASK-RESIDUAL-001` | Signed residual, decomposition, history and the unresolved state | `REQ-RESIDUAL-001`, `REQ-RESIDUAL-002`, `REQ-RESIDUAL-003`, `REQ-RESIDUAL-004`, `REQ-RESIDUAL-008` | 1 | 1.4 | `VERIFIED` | — | 2026-11-17 | Landed in `14f7b20`: signed-residual, decomposition, unresolved-is-null tests + API route test |
| `TASK-STORAGE-001` | Tier registry, retention and payload encoding | `REQ-STORAGE-001`, `REQ-STORAGE-002`, `REQ-STORAGE-008`, `REQ-STORAGE-009` | 1 | 1.3 | `VERIFIED` | — | 2026-11-17 | Landed in `31f01b2`: four-tier registry, per-tier retention, additive payload codec, tier-aware prune (carries the `OD-31` assumption) |
| `TASK-TOPOLOGY-001` | Typed edge contract and the lifecycle vocabulary | `REQ-TOPO-001`, `REQ-TOPO-002` | 2 | 2.1 | `VERIFIED` | — | 2026-11-17 | Landed in `74b686d`: typed edge + explicit lifecycle; TopologyRelation.lifecycle; relation_kind_name |
| `TASK-TOPOLOGY-002` | Containment evidence bar, violations and residual awareness | `REQ-TOPO-003`, `REQ-TOPO-004`, `REQ-TOPO-005`, `REQ-TOPO-006`, `REQ-TOPO-007` | 2 | 2.2 | `VERIFIED` | — | 2026-11-17 | Landed in `036ffbc`: containment evidence bar, once-only contribution, residual corroborating marker |
| `TASK-TOPOLOGY-003` | Versioned topology, change detection and reconciliation | `REQ-TOPO-008`, `REQ-TOPO-009` | 2 | 2.2 | `IN_REVIEW` | — | 2026-11-17 | PARTIAL — landed in `4bac434`: versioned topology object + deterministic diff, previous payload byte-identical, records keep version. Automatic change detection/reconciliation and `ConfigurationChangeEvent` storage deferred; do NOT mark VERIFIED. |
| `TASK-TOPOLOGY-004` | Incomplete topology and the circuit scope | `REQ-TOPO-010`, `REQ-TOPO-011` | 2 | 2.3 | `NOT_STARTED` | — | 2026-11-17 | Not attempted in the implementation phase (no commit) |
| `TASK-EVENT-001` | Event kinds, grouping, lag and window sizing | `REQ-EVENT-001`, `REQ-EVENT-002`, `REQ-EVENT-003`, `REQ-EVENT-004` | 2 | 2.3 | `VERIFIED` | — | 2026-11-17 | Landed in `8ee3973`: event kinds, grouping, lag_exceeded, cadence-derived window on audit path |
| `TASK-EVENT-002` | Cross-reader vector, late data, duplicates and incomplete groups | `REQ-EVENT-005`, `REQ-EVENT-006`, `REQ-EVENT-007`, `REQ-EVENT-008`, `REQ-EVENT-009` | 2 | 2.4 | `NOT_STARTED` | — | 2026-11-17 | Not attempted in the implementation phase (no commit) |
| `TASK-DISAGG-001` | Joint inference, identifiability and group estimates | `REQ-DISAGG-001`, `REQ-DISAGG-002`, `REQ-DISAGG-003`, `REQ-DISAGG-004` | 2 | 2.2 | `FAILED` | — | 2026-11-17 | Attempted and reverted: ambiguous group emitted a fabricated per-device watt (250 W), violating REQ-DISAGG-005. Work in stash@{0}. Requires correct min_w/max_w with no per-device value. |
| `TASK-DISAGG-002` | No fabrication, single counting and the balance identity | `REQ-DISAGG-005`, `REQ-DISAGG-006`, `REQ-DISAGG-007` | 2 | 2.4 | `NOT_STARTED` | — | 2026-11-17 | Not attempted (no additional work beyond TASK-DISAGG-001, which was reverted) |
| `TASK-POWER-001` | Load-shape class and the model-version surface | `REQ-POWER-001`, `REQ-POWER-002`, `REQ-POWER-004`, `REQ-POWER-008` | 2 | 2.2 | `VERIFIED` | — | 2026-11-17 | Landed in `8b70933`: LoadShape/PowerLevel enums, DevicePowerProfile, power_level publication, model versions |
| `TASK-POWER-002` | Measured-step attribution, non-HA devices, entity separation and declared bounds | `REQ-POWER-003`, `REQ-POWER-005`, `REQ-POWER-006`, `REQ-POWER-007` | 2 | 2.5 | `NOT_STARTED` | — | 2026-11-17 | Not attempted in the implementation phase (no commit) |
| `TASK-RESIDUAL-002` | Drift events, the unknown-residual hand-off and the non-redistribution guard | `REQ-RESIDUAL-005`, `REQ-RESIDUAL-006`, `REQ-RESIDUAL-007` | 3 | 3.2 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-STORAGE-002` | Evidence immutability, batched writes, indexes and version identifiers | `REQ-STORAGE-003`, `REQ-STORAGE-004`, `REQ-STORAGE-005`, `REQ-STORAGE-006`, `REQ-STORAGE-007` | 3 | 3.1 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-STORAGE-003` | Recovery, migration safety and replay | `REQ-STORAGE-010`, `REQ-STORAGE-011`, `REQ-STORAGE-012` | 3 | 3.6 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-LEARNING-001` | Sample intake, the rejection ledger and per-device thresholds | `REQ-LEARNING-001`, `REQ-LEARNING-002`, `REQ-LEARNING-006` | 3 | 3.1 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-LEARNING-002` | Promotion, factor timing and measurement precedence | `REQ-LEARNING-003`, `REQ-LEARNING-004`, `REQ-LEARNING-005`, `REQ-LEARNING-007` | 3 | 3.4 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-CONFIDENCE-001` | Confidence dimensions, the calibration flag and tier monotonicity | `REQ-CONFIDENCE-001`, `REQ-CONFIDENCE-002`, `REQ-CONFIDENCE-003`, `REQ-CONFIDENCE-004` | 3 | 3.1 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-CONFIDENCE-002` | Live confidence dimensions and per-edge evidence | `REQ-CONFIDENCE-005`, `REQ-CONFIDENCE-006`, `REQ-CONFIDENCE-007` | 3 | 3.3 | `NOT_STARTED` | — | 2026-11-16 | Carries the `OD-34` assumption |
| `TASK-MAPPING-001` | The derived mapping projection | `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-006`, `REQ-MAPPING-007` | 3 | 3.1 | `NOT_STARTED` | — | 2026-11-16 | `ASM-06` RESOLVED — the mapping module is built in V1 |
| `TASK-MAPPING-002` | Unknown-load entries and the evidence-triggered refresh | `REQ-MAPPING-004`, `REQ-MAPPING-005` | 3 | 3.5 | `NOT_STARTED` | — | 2026-11-16 | `ASM-06` RESOLVED — the mapping module is built in V1 |
| `TASK-CHANGE-001` | Configuration events and the hypothesis lifecycle | `REQ-CHANGE-001`, `REQ-CHANGE-002`, `REQ-CHANGE-005` | 3 | 3.2 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-CHANGE-002` | Four version identifiers, immutable history, rollback and reconciliation | `REQ-CHANGE-003`, `REQ-CHANGE-004`, `REQ-CHANGE-006`, `REQ-CHANGE-007` | 3 | 3.3 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-UI-001` | Panel vocabulary: inventory, quality, the four-state label and the view states | `REQ-UI-001`, `REQ-UI-002`, `REQ-UI-007`, `REQ-UI-013` | 4 | 4.1 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-UI-002` | Panel views: topology, residual, model, mapping and identifiability | `REQ-UI-003`, `REQ-UI-004`, `REQ-UI-005`, `REQ-UI-006`, `REQ-UI-008` | 4 | 4.2 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-UI-003` | Panel evidence, version history, operator correction and storage health | `REQ-UI-009`, `REQ-UI-010`, `REQ-UI-011`, `REQ-UI-012` | 4 | 4.3 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-DEPLOY-001` | Build pipeline, option surface, startup migrations and backup | `REQ-DEPLOY-001`, `REQ-DEPLOY-002`, `REQ-DEPLOY-003`, `REQ-DEPLOY-004` | 4 | 4.1 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-DEPLOY-002` | Deployment procedure, smoke tests, rollback and operational metrics | `REQ-DEPLOY-005`, `REQ-DEPLOY-006`, `REQ-DEPLOY-007`, `REQ-DEPLOY-008` | 4 | 4.2 | `NOT_STARTED` | — | 2026-11-16 | — |
| `TASK-TEST-002` | API surface, replay and migration harnesses | `REQ-TEST-002`, `REQ-TEST-005`, `REQ-TEST-008` | 4 | 4.1 | `NOT_STARTED` | — | 2026-11-16 | Carries the `OD-36` assumption |
| `TASK-TEST-003` | The T01–T15 builders, the matrix reconciliation and the performance and accuracy harnesses | `REQ-TEST-003`, `REQ-TEST-004`, `REQ-TEST-009`, `REQ-TEST-010` | 4 | 4.2 | `NOT_STARTED` | — | 2026-11-16 | — |

### 1.1 Completion sprint tasks (Phase B proposal — none started)

Added 2026-11-17 by Phase A (`18-completion-gap-analysis.md`, `19-completion-plan.md`).
These are **proposed** task IDs; they are `NOT_STARTED` and carry no commit or
verification evidence. The `Wave` column uses the B0–B4 completion tiers of
`../19-completion-plan.md` §2, which is authoritative for these tasks; the
original wave labels above are unchanged. Acceptance criteria are summarised
here and detailed in `../19-completion-plan.md` §2. No task may be marked
`VERIFIED` without an orchestrator-run test log.

| Task ID | Title | Requirement IDs | Wave | Segment | Status | Owner | Last updated | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `TASK-FOUNDATION-003` | Settle the residual noise and model-class contracts | `REQ-FOUNDATION-002`, `REQ-FOUNDATION-003`, `REQ-POWER-001`, `REQ-RESIDUAL-002` | B0 | 0.3 | `NOT_STARTED` | — | 2026-11-17 | Shared contract freeze: bump `CONTRACT_VERSION`, regenerate 30 schemas, record the `OD-46`/`OD-47` decision. Acceptance: additive payloads still validate; `contract_schema_sync` gate green. |
| `TASK-RESIDUAL-003` | Define the noise-component contract home and the residual baseline window shape | `REQ-RESIDUAL-002`, `REQ-RESIDUAL-004`, `REQ-FOUNDATION-002` | B0 | 0.3 | `NOT_STARTED` | — | 2026-11-17 | Acceptance: additive optional fields with neutral defaults; update `test_residual_power.py:974` which currently pins noise absence. |
| `TASK-RESIDUAL-004` | Populate the residual baseline window on the production audit path | `REQ-RESIDUAL-002`, `REQ-RESIDUAL-004`, `REQ-RESIDUAL-008` | B1 | 1.5 | `NOT_STARTED` | — | 2026-11-17 | Closes G-02. Acceptance: production `ResidualScope` carries `window_w`; `median_of_window` and non-degenerate `dynamic_w`; signed identity holds. |
| `TASK-RESIDUAL-005` | Residual drift events and the unknown-residual hand-off to learning | `REQ-RESIDUAL-005`, `REQ-RESIDUAL-006`, `REQ-RESIDUAL-007`, `REQ-EVENT-001`, `REQ-LEARNING-004` | B1 | 1.6 | `NOT_STARTED` | — | 2026-11-17 | Completes `TASK-RESIDUAL-002`. Acceptance: named drift event, ledger entry with reason, residual-never-redistributed guard green. |
| `TASK-POWER-003` | Generalise adjustable models beyond dimmable lights | `REQ-POWER-001`, `REQ-POWER-002`, `REQ-POWER-007` | B2 | 2.6 | `NOT_STARTED` | — | 2026-11-17 | Closes G-03. Acceptance: non-linear adjustable response published with `LoadShape`; priors bound green. |
| `TASK-POWER-004` | Implement `DELAYED` and `ALWAYS_ON` load-shape behavior | `REQ-POWER-001`, `REQ-POWER-004`, `REQ-EVENT-003` | B2 | 2.7 | `NOT_STARTED` | — | 2026-11-17 | Closes G-04. **Blocked on `OD-46`/`OD-47`** (decision); do not guess. Acceptance: declared delay honoured; always-on has no OFF state; existing enum labels not broken. |
| `TASK-MAPPING-003` | Build the derived mapping projection and its `/mapping` read path | `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-006`, `REQ-MAPPING-007` | B3 | 3.7 | `NOT_STARTED` | — | 2026-11-17 | Completes `TASK-MAPPING-001`. Acceptance: derived/deterministic, all cardinalities, nine cell fields, coverage block. |
| `TASK-MAPPING-004` | Unknown-load entries and evidence-triggered refresh | `REQ-MAPPING-004`, `REQ-MAPPING-005`, `REQ-RESIDUAL-007` | B3 | 3.8 | `NOT_STARTED` | — | 2026-11-17 | Completes `TASK-MAPPING-002`. Acceptance: unknown entry names no device; no write on identical samples. |
| `TASK-TOPOLOGY-005` | Automatic change detection, `ConfigurationChangeEvent` and inference-history reconciliation | `REQ-TOPO-009`, `REQ-CHANGE-001`, `REQ-CHANGE-002`, `REQ-CHANGE-005`, `REQ-CHANGE-007` | B3 | 3.9 | `NOT_STARTED` | — | 2026-11-17 | Completes deferred half of `TASK-TOPOLOGY-003`. Acceptance: one event per change kind; change response lists affected records; no record rewritten. |
| `TASK-DISAGG-003` | Joint inference source ledger (`sources_used` / available-but-unused) | `REQ-DISAGG-001`, `REQ-FOUNDATION-005` | B4 | 4.4 | `NOT_STARTED` | — | 2026-11-17 | Supersedes reverted `TASK-DISAGG-001`. Acceptance: result names every source used and unused with reason; `infer()` changed additively. |
| `TASK-DISAGG-004` | Identifiability + non-fabricating group estimates with `min_w`/`max_w` | `REQ-DISAGG-002`, `REQ-DISAGG-003`, `REQ-DISAGG-004`, `REQ-DISAGG-005` | B4 | 4.5 | `NOT_STARTED` | — | 2026-11-17 | Supersedes reverted `TASK-DISAGG-001`/`002`. Acceptance: rank/null-space published; ambiguous group publishes `min_w`/`max_w` and no per-device watt — must not reproduce the 250 W fabrication in `stash@{0}`. |
| `TASK-UI-004` | Panel views for residual, mapping, identifiability, model versions and version history | `REQ-UI-003`, `REQ-UI-004`, `REQ-UI-005`, `REQ-UI-006`, `REQ-UI-007`, `REQ-UI-008`, `REQ-UI-009`, `REQ-UI-010`, `REQ-UI-011`, `REQ-UI-012`, `REQ-UI-013` | B4-UI | 4.6 | `NOT_STARTED` | — | 2026-11-17 | Depends on stable APIs from B1/B3/B4. Acceptance: `UT-panel` data-rendering assertions (new), `check_panel.py` green. |
| `TASK-DEPLOY-003` | Unified end-to-end workflow entry point and post-deploy smoke script | `REQ-DEPLOY-005`, `REQ-DEPLOY-006`, `REQ-DEPLOY-007`, `REQ-TEST-002` | B4-WF | 4.7 | `NOT_STARTED` | — | 2026-11-17 | Closes G-10. Acceptance: `scripts/smoke_test.sh` exits non-zero on any of six checks; one documented run command; addon validation green. |
| `TASK-TEST-004` | Missing verification gates: performance, replay, migration matrix and UI | `REQ-TEST-003`, `REQ-TEST-004`, `REQ-TEST-005`, `REQ-TEST-008`, `REQ-TEST-009`, `REQ-STORAGE-012` | B4-WF | 4.8 | `NOT_STARTED` | — | 2026-11-17 | Closes G-11. Acceptance: T01–T15 builders; migration matrix; new-path performance budgets; UI data assertions. Runs continuously as the verification vehicle. |
| `TASK-DEPLOY-004` | Author `16-final-verification-report.md` and `17-deployment-report.md` | `REQ-DEPLOY-005`, `REQ-DEPLOY-007`, `REQ-TEST-001`, `REQ-TEST-010` | B4-WF | 4.9 | `NOT_STARTED` | — | 2026-11-17 | Closes G-12/G-13. Terminal gate: both reports exist with version, orchestrator gate table, smoke output, rollback rehearsal; update README file index. |

**Rule.** The `Owner` column names the builder or the orchestrator. It is
`—` until a task is assigned. The `Last updated` column is an ISO date and
changes on every status move.

**Rule.** The `Wave` and `Segment` columns are the placement of the task in
[`../15-implementation-plan.md`](../15-implementation-plan.md) §2 and §4.3. The
plan is authoritative; the tracker copies the labels and never restates them.
Tasks in different segments of the same wave are serialised and must not run
concurrently. `TASK-TEST-001` is segment `continuous`: it starts in wave 0 and
runs alongside every wave.

---

## 2. Wave summary

| Wave | Content | `NOT_STARTED` | `READY` | `IN_PROGRESS` | `BLOCKED` | `IN_REVIEW` | `VERIFIED` | `FAILED` | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Contract and process foundation | 0 | 0 | 1 | 0 | 0 | 2 | 0 | 3 |
| 1 | Measurement, residual and storage substrate | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 5 |
| 2 | Topology, events and the disaggregation core | 4 | 0 | 0 | 0 | 1 | 4 | 1 | 10 |
| 3 | Analysis, learning, mapping and change | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 11 |
| 4 | Panel, deployment and the end-to-end harnesses | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 7 |
| **All** | — | **22** | **0** | **1** | **0** | **1** | **11** | **1** | **36** |

**Rule.** The seven status columns sum to the wave total. A row that does not
balance is an error in this tracker and is corrected in the same change.

**Completion-sprint tasks.** The 15 proposed Phase B tasks in §1.1 are tracked
separately because their waves use the B0–B4 tiers of `../19-completion-plan.md`,
not the five original waves above. All 15 are `NOT_STARTED`; they are **not**
counted in this §2 table, whose totals remain the original 36 tasks. The
original counts are therefore unchanged by the Phase A documentation.

**Rule.** `VERIFIED` is the only status that counts as done. The delivery
progress figure quoted to a stakeholder is the `VERIFIED` column and nothing
else.

---

## 3. Blockers

**Currently empty.** No task is blocked.

**Rule.** A blocker is an entry in this section, not a note in the status
table. A task is `BLOCKED` only when it has an entry here.

**Format of an entry.**

| Field | Content |
| --- | --- |
| Task ID | the blocked task |
| Raised by | the builder or the orchestrator |
| Date | the ISO date the blocker was raised |
| Kind | `dependency` · `decision` · `permission` · `prerequisite` · `conflict` |
| Description | what is blocked and what exactly is missing |
| Assumed path | the stated assumption the task proceeds under, or `none` |
| Needed to unblock | the concrete decision, artefact or action required |
| Owner | the party who can resolve it |

**Rule.** A task that cannot be specified without a product decision records
the decision as a blocker and proceeds under a stated assumption. The
assumption is recorded here and repeated in the task file's `Blockers` field.

**Known items that are not blockers** — carried from
`14-requirement-traceability.md` §9.3 and recorded in the task files. An open
item becomes a blocker only if the orchestrator rules against the stated
assumption; a **settled** item stays in the table so the history of the
decision remains traceable, and it places no constraint on the task.

| Item | Task(s) | Stated assumption |
| --- | --- | --- |
| `OD-38` — the specification set is not approved (RESOLVED, 2026-11-16) | all tasks | settled — the set is approved (`SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`); every task proceeds on the §6 defaults of `13-open-decisions-and-assumptions.md` and every task still starts `NOT_STARTED` |
| `ASM-06` — the mapping module is built in V1 (RESOLVED, 2026-11-16) | `TASK-MAPPING-001`, `TASK-MAPPING-002` | settled — the mapping module is built; the seven `REQ-MAPPING-*` requirements stand unchanged and both tasks proceed |
| `OD-34` — the two dimensions stay unpopulated in V1 | `TASK-CONFIDENCE-002` | the requirement wins: the dimensions carry a value where derivable, `null` otherwise |
| `OD-31` — the shipped 30-day default | `TASK-STORAGE-001` | the validator accepts the superset `{7, 14, 30}` |
| `OD-36` — the live-capture tests skip without a local database | `TASK-TEST-002` | the skip is recorded in the release note, not deleted |

---

## 4. Change log

**Rule.** This tracker is updated at every status move, by the party named in
the table below, in the same change that moves the status.

| Event | Who updates | What is recorded | Evidence required |
| --- | --- | --- | --- |
| A task starts | the assigned builder | status `NOT_STARTED` → `IN_PROGRESS`; the owner name; the date | the builder's first delivery note |
| A task blocks | the builder | status → `BLOCKED`; a new entry in §3 | the blocker entry with its format |
| A task is delivered | the builder | status `IN_PROGRESS` → `IN_REVIEW`; the date | the delivery note naming the files and the tests run |
| A task is verified | **the orchestrator only** | status `IN_REVIEW` → `VERIFIED`; the date; the test-log reference in the notes | an orchestrator-run test log covering the named vehicle (`docs/agent_assignments.md:26-28`) |
| A task fails verification | the orchestrator only | status → `FAILED`; the failing selector in the notes | the failing output of the named vehicle |
| A task is reopened | the orchestrator | status → `IN_PROGRESS`; the reason | the reason and the date |
| A dependency is settled | the orchestrator | the dependency's status moves; the dependent task may move to `READY` | the orchestrator's run |
| The plan changes | the orchestrator | `15-implementation-plan.md` updated in the same change | the plan diff |

**Rule.** A builder's self-report is never sufficient to set `VERIFIED`. The
verification harness is the only accepted evidence
(`docs/agent_assignments.md:26-28`; `docs/implementation_plan_v2.md:333`).

**Rule.** A row may not be deleted from §1 to make a count match. If a task is
withdrawn, it is marked `FAILED` with the withdrawal reason, or it is removed
in the same change that updates the plan and this tracker.

### 4.1 Tracker change log

| Date | Change | By | Note |
| --- | --- | --- | --- |
| 2026-11-16 | Tracker created; all 36 tasks set to `NOT_STARTED`; blockers section created and left empty | builder | the plan and the task files were produced in the same change; no implementation has started |
| 2026-11-16 | Wave and segment labels reconciled with the plan; a `Segment` column added to §1 | builder | 16 task files carried a stale segment label, and a seventeenth (`task-storage-002.md`) named another task's segment in its blocker note; the plan's §2 and §4.3 mapping is authoritative. No task ID, requirement ID, dependency, acceptance criterion, test requirement or status changed. |
| 2026-11-17 | Task statuses reconciled with the implementation record; §4.2 verification evidence and §4.3 deferred work added | builder | Documentation-only reconciliation after the implementation phase: 11 tasks set to `VERIFIED` with commit evidence, `TASK-TOPOLOGY-003` to `IN_REVIEW` (partial), `TASK-TEST-001` to `IN_PROGRESS` (continuous vehicle), `TASK-DISAGG-001` to `FAILED` (reverted fabrication), and 22 tasks left `NOT_STARTED`. Wave counts and the traceability matrix reconciled in the same change. No task ID, requirement ID or status vocabulary changed. |
| 2026-11-17 | Phase A of the completion sprint: §1.1 added with 15 proposed completion tasks, all `NOT_STARTED` | builder (PHASE-A-GAP) | Documentation only. Task IDs, titles, requirement refs, Builder assignments and verification methods come from `../19-completion-plan.md`; evidence base is `../18-completion-gap-analysis.md`. No task started, no status moved, no original task ID changed, and no `VERIFIED` recorded. |

---

### 4.2 Verification evidence

Final verification at HEAD `4cd3545` (all green). These are the orchestrator's
recorded gate results for the implementation phase; the seven results are
reproduced verbatim.

| Gate | Command | Result |
| --- | --- | --- |
| Unit + integration suite | `pytest tests/unit tests/integration -o addopts="" -q` | 2678 passed, 15 skipped |
| Simulation suite | `pytest tests/simulation -o addopts="" -q` | 122 passed, 9 errors (pre-existing `test_real_topology.py`), 0 failures |
| Lint | `ruff check engine tools tests` | all checks passed |
| Type check | `mypy engine/wattmosaic_engine` | 109 errors (baseline) |
| Schema sync | `export_schemas.py` + `git diff --exit-code data/schemas` | no drift |
| Secret scan | `check_secrets.py` | clean |
| Addon validation | `validate_addon.sh --no-build` | 71 passed, 0 failed, 3 skipped |

**Rule.** `VERIFIED` requires an orchestrator-run test log
(`docs/agent_assignments.md:26-28`); a builder's self-report never sets it.
The commit SHAs in the Notes column of §1 identify the delivery that landed
each task; the gate results above are the verification evidence.

### 4.3 Deferred work

The implementation phase ended with the following tasks still `NOT_STARTED`
(22) or otherwise incomplete (`TASK-TOPOLOGY-003` `IN_REVIEW`,
`TASK-DISAGG-001` `FAILED`). No code landed for the `NOT_STARTED` tasks; they
carry no commit evidence and no matrix `PASS`.

| Task | Wave | Blocking reason |
| --- | --- | --- |
| `TASK-TOPOLOGY-004` | 2 | Not attempted in the implementation phase |
| `TASK-EVENT-002` | 2 | Not attempted in the implementation phase |
| `TASK-DISAGG-002` | 2 | No additional work beyond `TASK-DISAGG-001`, which was reverted |
| `TASK-POWER-002` | 2 | Not attempted in the implementation phase |
| `TASK-TOPOLOGY-003` (partial) | 2 | `IN_REVIEW`: version object/diff landed in `4bac434`; automatic change detection/reconciliation and `ConfigurationChangeEvent` storage deferred |
| `TASK-DISAGG-001` | 2 | `FAILED`: implementation reverted for a fabricated 250 W per-device value; work in `stash@{0}`; requires correct `min_w`/`max_w` with no per-device value |
| `TASK-RESIDUAL-002` | 3 | Not attempted in the implementation phase |
| `TASK-STORAGE-002` | 3 | Not attempted in the implementation phase |
| `TASK-STORAGE-003` | 3 | Not attempted in the implementation phase |
| `TASK-LEARNING-001` | 3 | Not attempted in the implementation phase |
| `TASK-LEARNING-002` | 3 | Not attempted in the implementation phase |
| `TASK-CONFIDENCE-001` | 3 | Not attempted in the implementation phase |
| `TASK-CONFIDENCE-002` | 3 | Not attempted in the implementation phase |
| `TASK-MAPPING-001` | 3 | Not attempted in the implementation phase |
| `TASK-MAPPING-002` | 3 | Not attempted in the implementation phase |
| `TASK-CHANGE-001` | 3 | Not attempted in the implementation phase |
| `TASK-CHANGE-002` | 3 | Not attempted in the implementation phase |
| `TASK-UI-001` | 4 | Not attempted in the implementation phase |
| `TASK-UI-002` | 4 | Not attempted in the implementation phase |
| `TASK-UI-003` | 4 | Not attempted in the implementation phase |
| `TASK-DEPLOY-001` | 4 | Not attempted in the implementation phase |
| `TASK-DEPLOY-002` | 4 | Not attempted in the implementation phase |
| `TASK-TEST-002` | 4 | Not attempted in the implementation phase |
| `TASK-TEST-003` | 4 | Not attempted in the implementation phase |

---

## 5. Related documents

- [`../15-implementation-plan.md`](../15-implementation-plan.md) — the plan, the waves, the ownership matrix and the acceptance gates.
- [`../14-requirement-traceability.md`](../14-requirement-traceability.md) — the task IDs, the requirement matrix and the test-ID scheme.
- [`../01-requirements.md`](../01-requirements.md) — the 130 requirements.
- [`../../../agent_assignments.md`](../../../agent_assignments.md) — the orchestrator-run evidence rule.
