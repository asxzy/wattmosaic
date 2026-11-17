# 19 — Dependency-ordered completion plan (Phase B proposal)

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Status: PLAN ONLY — no Phase B task has started.** This document converts the
findings of [`18-completion-gap-analysis.md`](18-completion-gap-analysis.md)
into a dependency-ordered set of tasks. It maps each proposed task to the
original [`01-requirements.md`](01-requirements.md) Requirement IDs and to the
Task-ID conventions of [`14-requirement-traceability.md`](14-requirement-traceability.md)
(`TASK-<AREA>-<NNN>`) and [`15-implementation-plan.md`](15-implementation-plan.md).
New IDs continue the existing numeric sequence within their area prefix; they
are **proposed** until the orchestrator publishes them in the tracker. All new
tasks are `NOT_STARTED` in [`tasks/progress.md`](tasks/progress.md).

The mission's four waves are grouped here into five dependency tiers (B0–B4) so
that shared-contract work is settled before parallel implementation. The
orchestrator's wave/segment labels remain authoritative.

---

## 1. Governing rules inherited from the original plan

- **Additive contracts only.** `CONTRACT_VERSION` bump, schemas regenerated with
  `tools/dev/export_schemas.py`, `docs/interface_contracts.md` updated, and a
  clean `git diff --exit-code data/schemas` before any new field ships
  (`REQ-FOUNDATION-002/003`). `model_config = extra="forbid"` with neutral
  defaults (`contracts.py:776`).
- **No new runtime dependency, database, service or message bus**
  (`REQ-FOUNDATION-004`).
- **One file, one owner, per wave** (`REQ-FOUNDATION-006`; `15-…` §4.5). Where
  two tasks name the same file they are serialised into different segments.
- **Never fabricate.** Residual never redistributed, missing never zero,
  measurement outranks a model, and no per-device number without evidence
  (`REQ-DISAGG-005`, `REQ-RESIDUAL-007`).
- **Orchestrator-run evidence only.** No task moves to `VERIFIED` on a builder
  report; the named vehicle must be run by the orchestrator
  (`docs/agent_assignments.md:26-28`).
- **The original spec is not reworded.** The four contract-only models
  (`UnknownLoadProfile`, `ResidualDecompositionRecord`, `DeviceReaderMappingCell`,
  `ConfigurationChangeEvent`) already exist; completion tasks consume them rather
  than redefining them.

---

## 2. Proposed tasks

### B0 — Shared contracts and decisions (must precede parallel work)

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-FOUNDATION-003` | Settle the residual noise and model-class contracts | `REQ-FOUNDATION-002`, `REQ-FOUNDATION-003`, `REQ-POWER-001`, `REQ-RESIDUAL-002` | contracts owner | Bump `CONTRACT_VERSION`, regenerate 30 schemas, `verify.py:contract_schema_sync` green; a pre-change payload still validates; record the `OD-46`/`OD-47` decision |
| `TASK-RESIDUAL-003` | Define the noise-component contract home and the residual baseline window shape | `REQ-RESIDUAL-002`, `REQ-RESIDUAL-004`, `REQ-FOUNDATION-002` | residual owner | Additive optional fields with neutral defaults; update `test_residual_power.py:974` (currently pins absence); `UT-residual-power::test_noise_component_round_trips` |

**Dependency edge:** every other tier depends on `TASK-FOUNDATION-003` for the
frozen contract shape. `TASK-RESIDUAL-003` also needs a resolved interpretation
for G-01 (noise term) before the residual production path can publish it.

### B1 — Residual production path

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-RESIDUAL-004` | Populate the residual baseline window on the production audit path | `REQ-RESIDUAL-002`, `REQ-RESIDUAL-004`, `REQ-RESIDUAL-008` | audit/residual owner | `UT-residual-power` + `IT-audit-wiring` assertion that a produced window yields `baseline_estimator=median_of_window` and a non-degenerate `dynamic_w`; update `test_audit_wiring.py:1031-1064`; signed identity still holds |
| `TASK-RESIDUAL-005` | Residual drift events and the unknown-residual hand-off to learning | `REQ-RESIDUAL-005`, `REQ-RESIDUAL-006`, `REQ-RESIDUAL-007`, `REQ-EVENT-001`, `REQ-LEARNING-004` | residual/events/learning owner (serialised) | `UT-residual-power::test_residual_drift_emits_a_named_event`, `UT-learning::test_unknown_residual_event_reaches_the_ledger`, `IT-safety-invariants::test_residual_is_never_redistributed` |

**Original task lineage:** completes `TASK-RESIDUAL-001` follow-up G-02 and
`TASK-RESIDUAL-002` (still `NOT_STARTED`).

### B2 — Adjustable and delayed power models

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-POWER-003` | Generalise adjustable models beyond dimmable lights; implement declared load-shape behavior | `REQ-POWER-001`, `REQ-POWER-002`, `REQ-POWER-007` | models owner | `UT-models` + `UT-device-taxonomy` assertions for a non-linear adjustable response and a published `LoadShape`; `test_device_attachment` priors bound still green |
| `TASK-POWER-004` | Implement `DELAYED` and `ALWAYS_ON` load-shape behavior (blocked on `OD-46`/`OD-47`) | `REQ-POWER-001`, `REQ-POWER-004`, `REQ-EVENT-003` | models/configuration owner | `UT-models::test_delayed_response_is_declared_and_honoured`, `UT-device-taxonomy::test_always_on_has_no_off_state`; requires the B0 decision |

**Original task lineage:** extends `TASK-POWER-001`; shared file `contracts.py`
is owned only by `TASK-FOUNDATION-003` in B0 — these tasks consume the settled
shape.

### B3 — Device↔Reader Mapping Matrix and topology self-healing

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-MAPPING-003` | Build the derived mapping projection and its `/mapping` read path | `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-006`, `REQ-MAPPING-007` | disaggregation/API owner | `UT-mapping::test_mapping_is_derived_and_deterministic`, `UT-api::test_mapping_cell_response_shape` incl. coverage block; 1:1/1:N/N:1/N:N cardinality assertions |
| `TASK-MAPPING-004` | Unknown-load entries and evidence-triggered refresh | `REQ-MAPPING-004`, `REQ-MAPPING-005`, `REQ-RESIDUAL-007` | disaggregation owner | `UT-mapping::test_unknown_load_entry_names_no_device`, `UT-inference-records::test_mapping_refreshes_only_on_an_evidence_change` |
| `TASK-TOPOLOGY-005` | Automatic change detection, `ConfigurationChangeEvent` and inference-history reconciliation | `REQ-TOPO-009`, `REQ-CHANGE-001`, `REQ-CHANGE-002`, `REQ-CHANGE-005`, `REQ-CHANGE-007` | topology/audit owner | `UT-topology-persistence::test_configuration_event_per_change_kind`, `UT-inference-records::test_change_lists_the_affected_records`; `TASK-TOPOLOGY-003` moves off `IN_REVIEW` |

**Original task lineage:** completes `TASK-MAPPING-001`/`002` and the deferred
half of `TASK-TOPOLOGY-003`. `disaggregation/audit.py` is the shared file with
B1/B4 and is serialised after them.

### B4 — Joint inference and group estimates (replaces the reverted `TASK-DISAGG-001`)

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-DISAGG-003` | Joint inference source ledger (`sources_used` / available-but-unused) | `REQ-DISAGG-001`, `REQ-FOUNDATION-005` | disaggregation owner | `E2E-full-inference-chain::test_result_names_every_source_used_and_unused`; extend `infer()` additively with an optional residual-events input; no positional signature change |
| `TASK-DISAGG-004` | Identifiability + non-fabricating group estimates with `min_w`/`max_w` | `REQ-DISAGG-002`, `REQ-DISAGG-003`, `REQ-DISAGG-004`, `REQ-DISAGG-005` | disaggregation/solver owner | `UT-electrical::test_identifiability_rank_and_ambiguous_group`, a new `test_ambiguous_group_publishes_min_and_max`, and `test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated`; **must not reproduce the reverted 250 W fabrication** |

**Original task lineage:** supersedes `TASK-DISAGG-001` (`FAILED`) and
`TASK-DISAGG-002` (`NOT_STARTED`). `engine.py` is owned by one builder per
segment; the discarded work in `stash@{0}`/`stash@{1}` is reference only and is
not applied wholesale (it contained the fabrication).

### B4-UI — Panel views for the new backend data

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-UI-004` | Panel views for residuals, mapping, identifiability ranges, model versions, version history and suspected changes | `REQ-UI-003`, `REQ-UI-004`, `REQ-UI-005`, `REQ-UI-006`, `REQ-UI-007`, `REQ-UI-008`, `REQ-UI-009`, `REQ-UI-010`, `REQ-UI-011`, `REQ-UI-012`, `REQ-UI-013` | UI owner | `UT-panel` data-rendering assertions (new — existing `test_panel.py` checks only shell/asset/CSP); `python tools/dev/check_panel.py`; API routes from B3/B4 consumed, not redefined |

**Dependency edge:** UI depends on **stable APIs** from B3 (`/mapping`,
configuration-change read path) and B4 (`min_w`/`max_w`, `sources_used`), plus
the residual decomposition from B1. It proceeds only after those shapes are
frozen in B0 and landed.

### B4-WF — Unified workflow, verification reports and deployment

| New ID | Title | Requirement refs | Builder assignment | Verification method |
| --- | --- | --- | --- | --- |
| `TASK-DEPLOY-003` | Unified end-to-end workflow entry point and post-deploy smoke script | `REQ-DEPLOY-005`, `REQ-DEPLOY-006`, `REQ-DEPLOY-007`, `REQ-TEST-002` | deployment owner | `scripts/smoke_test.sh` exits non-zero on any of the six checks, separate from the watchdog probe; one documented `run` command drives build → migrate → smoke → observe; `validate_addon.sh --no-build` green |
| `TASK-TEST-004` | Missing verification gates: performance, replay, migration matrix and UI | `REQ-TEST-003`, `REQ-TEST-004`, `REQ-TEST-005`, `REQ-TEST-008`, `REQ-TEST-009`, `REQ-STORAGE-012` | verification owner | `tools/simulator/scenarios.py` with T01–T15 builders; `SIM-simulator-fixtures::test_every_scenario_id_has_a_builder`; migration matrix across prior schema versions; new-path performance budgets; `UT-panel` data assertions |
| `TASK-DEPLOY-004` | Author `16-final-verification-report.md` and `17-deployment-report.md` | `REQ-DEPLOY-005`, `REQ-DEPLOY-007`, `REQ-TEST-001`, `REQ-TEST-010` | orchestrator/release owner | Both files exist with the delivered version, the orchestrator-run gate table, the recorded smoke output, the rollback rehearsal and the synthetic-accuracy limitation; update `README.md:63-65` from "forthcoming" to linked |

**Dependency edge:** the reports are the **terminal** gate — they depend on all
of B0–B4-UI plus `TASK-TEST-004` having run in the orchestrator's harness.

---

## 3. Explicit dependency edges

```
B0 (TASK-FOUNDATION-003, TASK-RESIDUAL-003)   [shared contracts frozen here]
 ├── B1 residual production path (004, 005)
 ├── B2 power models (003, 004)               [004 also blocked on OD-46/47]
 ├── B3 mapping (003, 004) ─┐
 └── B3-topology (005)      ├── B4-UI (004)   [UI needs stable APIs]
 B1 ─────────────────────────┤
 B3 mapping + B1 residual ───┼──> B4 joint inference (003, 004)
                             │        [joint inference depends on mapping matrix + residual path]
                             └──> B4-WF (DEPLOY-003, TEST-004, DEPLOY-004)
 B4 + B4-UI + B4-WF ──> 16/17 reports (terminal)
```

Key edges, stated explicitly:

1. **Joint inference (`TASK-DISAGG-003/004`) depends on the mapping matrix
   (`TASK-MAPPING-003`) and the residual production path (`TASK-RESIDUAL-004`)** —
   the source ledger names sources that only those tasks produce, and the group
   range must not be built on a degenerate residual.
2. **UI (`TASK-UI-004`) depends on stable APIs** — `/mapping` and the
   configuration-change read path from B3, `min_w`/`max_w` and `sources_used`
   from B4, and the residual decomposition from B1.
3. **Topology self-healing (`TASK-TOPOLOGY-005`) depends on B0 contracts** — it
   produces the `ConfigurationChangeEvent` shape settled in B0.
4. **`TASK-POWER-004` is blocked on a decision** (`OD-46`/`OD-47`); it is
   `BLOCKED`, not guessed. The label-only enum already shipped in `8b70933` must
   not break.
5. **Reports (`TASK-DEPLOY-004`) depend on every verification task** — no report
   is written from a builder self-report.

---

## 4. Parallelisation — what can run together and only after shared contracts

- **After `TASK-FOUNDATION-003` (contracts frozen) and `TASK-RESIDUAL-003`
  (residual shape):** B1 residual, B2 power models and B3 mapping can run in
  parallel **only if** their file sets are disjoint. `contracts.py` is owned
  solely by `TASK-FOUNDATION-003` in B0; `addon/ui/app.js` solely by
  `TASK-UI-004`; `disaggregation/engine.py` is serialised between
  `TASK-DISAGG-003` then `TASK-DISAGG-004`; `disaggregation/audit.py` is
  serialised between `TASK-RESIDUAL-004`, `TASK-TOPOLOGY-005` and the consumers.
- **Joint inference and UI are not parallel with their producers** — they must
  wait for the mapping/residual/group fields to land, otherwise the UI renders
  shapes that do not exist and the ledger names empty sources.
- **`TASK-TEST-004` runs continuously** alongside B0–B4 as the verification
  vehicle (mirroring `TASK-TEST-001`'s `continuous` segment), with its
  scenario-builder and report assertions finalised before `TASK-DEPLOY-004`.

---

## 5. Verification / deployment checklist for the missing reports

A completion is not accepted until every item below has an orchestrator-run log
or a recorded, version-stamped decision.

**16-final-verification-report.md**
- [ ] Delivered version and contract version stated (expect `0.2.1-dev.37`+ / `2.1.0`+).
- [ ] Exact HEAD SHA and clean-tree confirmation recorded.
- [ ] Unit + integration suite: command, exit code, passed/skipped counts.
- [ ] Simulation suite: command, exit code, the nine live-capture skips with reason (`OD-36`).
- [ ] Lint, mypy delta vs measured baseline, schema sync, secret scan, addon validation.
- [ ] Full `tools/dev/verify.py` gate table (the existing `docs/verification_report.md` is stale — regenerate).
- [ ] Negative verification for every new guard (`tools/dev/negverify_nesting.py` + new harness).
- [ ] Red-then-green record per new rule (`REQ-TEST-001`).
- [ ] New-path performance budgets asserted with margins (`REQ-TEST-009`).
- [ ] Migration matrix across supported prior schema versions (`REQ-TEST-008`).
- [ ] Replay identical under the same algorithm version, scenario-based (`REQ-TEST-005`, `REQ-STORAGE-012`).
- [ ] UI data-rendering assertions, not only shell/asset checks (`REQ-UI-*`).
- [ ] Synthetic-only accuracy limitation stated; no real-house per-device claim (`REQ-TEST-010`).
- [ ] Every DISAGG requirement still honestly `NOT RUN`/unverified unless a named gate passed.

**17-deployment-report.md**
- [ ] Exact image tag pulled (resolve the `dev.37` vs `v0.2.1-dev.36` tag lag).
- [ ] Backup taken before migration; consistent snapshot verified.
- [ ] Migrations applied idempotently; `PRAGMA user_version == LATEST_VERSION`.
- [ ] `scripts/smoke_test.sh` executed and its six checks recorded with output.
- [ ] Panel reachability and one known device's value observed post-deploy.
- [ ] Rollback rehearsed and recorded (previous image + pre-migration DB, re-verified by smoke).
- [ ] Operational risk → metric mapping published (`REQ-DEPLOY-008`).
- [ ] `README.md` file index updated so 16/17 (and 18/19) are linked, not "forthcoming".

---

## 6. Acceptance trace for this plan

| Mandate acceptance | Where satisfied |
| --- | --- |
| Gap analysis and dependencies documented before implementation | This file + `18-…`; no Phase B code changed |
| Dependency-ordered waves mapped to original Requirement/Task IDs | §2, §3 |
| New task IDs follow the existing convention | §2 (`TASK-RESIDUAL-003`, `TASK-DISAGG-003`, `TASK-MAPPING-003`, …) |
| Proposed ID, title, requirement refs, Builder assignment, verification method per task | §2 tables |
| Explicit dependency edges | §3 |
| Parallel-only-after-contracts noted | §4 |
| Verification/deployment checklist for missing reports | §5 |

---

## Related documents

- [`18-completion-gap-analysis.md`](18-completion-gap-analysis.md) — the evidence base for this plan.
- [`01-requirements.md`](01-requirements.md) — the 130 normative requirements.
- [`14-requirement-traceability.md`](14-requirement-traceability.md) — matrix extended with these proposed tasks.
- [`15-implementation-plan.md`](15-implementation-plan.md) — the original 36-task plan and ownership rules.
- [`tasks/progress.md`](tasks/progress.md) — tracker extended with these tasks at `NOT_STARTED`.
