# Final Validation Report — Event-Driven Load Disaggregation & Topology Discovery

**Date:** 2026-11-13
**Baseline:** `0.2.1-dev.24`, contract `1.5.0`, schema v8, **2177 tests collected**
**Delivered:** contract `1.6.0`, schema **v10**, **2762 tests collected / 2753 passed / 9 skipped / 0 failures**
**Verification:** every number below was produced by the orchestrator running the suite itself, not taken from a subagent report.

---

## 1. Outcome

The requirement was met by **extending the existing engine**, not replacing it. Six new inference
packages, one audit layer, additive contracts, two additive migrations, and 585 new tests. No new
runtime dependency, no new database, no new service, no message bus.

| Metric | Baseline | Delivered | Delta |
| --- | --- | --- | --- |
| Tests collected | 2177 | 2762 | **+585** |
| Passed | 2168 | 2753 | +585 |
| Failed | 0 | **0** | 0 |
| Skipped | 9 | 9 | 0 (all live-capture gates) |
| `ruff check engine tools tests` | clean | **clean** | — |
| `mypy engine` | 120 errors | 120 errors | **0 new** |
| `--check-config` | ok | **ok** | — |
| Contract version | 1.5.0 | 1.6.0 | additive |
| Schema version | 8 | 10 | additive |

> The 9 skips are **all** `tests/simulation/test_real_topology.py`, each with the identical reason
> `real capture unavailable: data/wattmosaic.db is local-only and git-ignored`. Verified with
> `pytest -rs`. No new coverage is hidden behind a skip.

---

## 2. What was implemented, by work package

| WP | Deliverable | New module | Tests |
| --- | --- | --- | --- |
| 1 | Contracts, migration 9/10, versioned inference, confidence vector | `contracts.py`, `storage/*` | 29 |
| 2 | Refoss factor simulation, Hot Tub fixture, 21 household strata, ground-truth seam | `tools/simulator/{factors,frames,hot_tub,labhouse,expectations}.py` | 41 |
| 3 | Factor semantics, four measurement bases, historical reconstruction | `electrical/factors.py` | 50 |
| 4 | Event isolation, physical-device identity, concurrency-guard repair | `disaggregation/isolation.py` | 45 |
| 5 | Device→CT association, evidence-bearing relations | `disaggregation/association.py` | 52 |
| 6 | Dedicated / shared / partial / unknown circuit classification | `disaggregation/circuits.py` | 34 |
| 7 | Signed residual, baseline/dynamic split, decomposition | `models/residual_power.py` | 65 |
| 8 | Conservative CT hierarchy, cycle prevention, declared-wins merge | `electrical/hierarchy.py` | 34 |
| 9 | Audit pass, 5 read-only endpoints, device-history fix | `disaggregation/audit.py`, `main.py`, `api/*` | 7 E2E + integration |
| 10 | Defect remediation (D1–D6) | — | 20 |
| 11 | Isolation wiring completeness | `audit.py`, `config/settings.py` | 19 |

---

## 3. Requirements traceability

### 3.1 Refoss factor — the mandatory Hot Tub case
Implemented as a permanent regression fixture and asserted in `tests/unit/test_factors.py:713-748`:
HA 0→500 W, CT1 1000→1250 W, CT2 900→1150 W.

- Each leg's delta is asserted at **250 W** (`:716`, `:717`).
- The ×2 estimate explains the 500 W appliance total; CT1+CT2 are **not** summed as independent
  consumption (`:732` — the naive sum is documented *as* the double count).
- Equal deltas produce `CT_MEASUREMENT_OVERLAP` and **zero** parent/child claims (`:748`).
- Monitor-applied ×2 is not re-applied (`scale_reading(1250)→1250`, `:531-537`).
- User-applied ×2 is applied exactly once (`scale_reading(250)→500`).

Verified by execution:
```
monitor basis: MONITOR_REPORTED  applied: 1.0  reported: 2.0   scale(250) -> 250
user    basis: FACTOR_ADJUSTED   applied: 2.0                  scale(250) -> 500
pre-history:  UNKNOWN                                          (never an implicit ×1)
```

### 3.2 Non-negotiable principles — status

| # | Principle | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Existing architecture takes precedence | **Met** | 0 new deps/services; reused scheduler, SQLite, contracts |
| 2 | TDD mandatory | **Met** | every WP reported failing-first; 6 mutations proven to bite |
| 3 | Original timestamps preserved | **Met** | `isolation.py` copies `occurred_at`; assertion in tests |
| 4 | Factor semantics explicitly verified | **Met** | 4 bases + UNKNOWN; `applied_by` decides arithmetic |
| 5 | Factored ≠ independent measurement | **Met** | `summing_allowed=False`; Hot Tub regression |
| 6 | No double counting of duplicate CTs | **Met** | canonical-channel resolution; residual refuses ambiguous scope |
| 7 | Association ≠ topology | **Met** | 4 distinct `RelationKind`s; no cross-construction path |
| 8 | Dedicated **and** shared supported | **Met** | 4 circuit classes + `PARTIALLY_UNDERSTOOD` |
| 9 | Unknown residual is valid | **Met** | `resolved=False`, `unresolved_since`, never invented |
| 10 | Dynamic power supported | **Met** | dynamic strata; stability-tracked response |
| 11 | Contaminated ≠ clean evidence | **Met** | `split_attribution`; retained and retrievable |
| 12 | Correlation ≠ causation | **Met** | hierarchy requires scope containment + ≥2 devices + ≥2 conditions |
| 13 | Insufficient evidence → Unknown/Ambiguous | **Met** | `INSUFFICIENT_DATA`, `UNKNOWN`, `evidence["verdict"]="ambiguous"` |
| 14 | Confidence explainable & evidence-based | **Met** | 5 independent dims, no blended scalar, documented formula |
| 15 | Inference versioned & auditable | **Met** | `InferenceRecord` append-only, revision chain, evidence refs resolve |
| 16 | Incomplete topology supported | **Met** | partial chains; unresolved nodes; empty result + reason |
| 17 | Never invent devices/circuits/relations | **Met** | no clamp, no fabrication, refusal paths tested |
| 18 | Never claim full coverage with residuals | **Met** | residual is first-class and persists |
| 19 | Realistic synthetic household | **Met** | 2 panels, L1/L2, 36 channels, 21 strata |
| 20 | Orchestrator delegates hands-on work | **Met** | all code/tests written by subagents |

### 3.3 Test matrix coverage
`docs/test_scenario_matrix.md` (84 rows: Groups A–J) was the coverage contract. Baseline was
**51 COVERED / 23 PARTIAL / 10 MISSING**. All previously PARTIAL and MISSING rows in Groups
B (factor), G (events), I (identifiability) and J (learning/regression) now have dedicated tests
in the new unit files. Group J's versioning rows are covered by
`tests/end_to_end/test_full_inference_chain.py`.

---

## 4. Defects found and fixed during review

An independent verifier reviewed the work. I reproduced each finding by execution before accepting it.

| ID | Defect | Severity | Status |
| --- | --- | --- | --- |
| D1 | `classify_basis` returned `CT_MEASURED` at a factor gap while `resolve` returned `UNKNOWN` — a silent ×1 | Major | **Fixed** — both now agree, asserted |
| D2 | `resolve_factor_ownership` defaulted `timestamp` to *now* — retroactive by default | Major | **Fixed** — timestamp now required |
| D3 | `abs()` turned a −400 W surplus into "+400 W unattributed load" and could flip SHARED→PARTIALLY_UNDERSTOOD | Major | **Fixed** — signed; negative = conflict, never load |
| D4 | `merge` accepted a cyclic parent/child set (`a→b, b→c, c→a`) | Major | **Fixed** — union validated; rejected with cycle path |
| D5 | `evidence_refs` cited non-persisted events → dangling after restart | Major | **Fixed** — migration 10 persists verdicts; every ref resolves |
| D6 | Wall-clock `created_at`/history stamps broke byte-replay | Minor | **Fixed** — injected clock |
| GAP-1 | Audit pass ignored per-device/class isolation windows and HA entity bindings | Major | **Fixed** — layers + bindings now in force |
| GAP-2 | Phantom `audit_algorithm_version` setting (silent fallback) | Minor | **Fixed** — real `WM_AUDIT_ALGORITHM_VERSION` |

Post-fix reproduction (orchestrator-run):
```
D1 gap: resolve = UNKNOWN | classify = UNKNOWN | AGREE: True
D4: [('a','b','confirmed'), ('b','c','confirmed'), ('c','a',REJECTED)]
```

**Verifier claim rejected on evidence:** the verifier asserted the 9 skips were "not all capture
gates". `pytest -rs` shows all 9 are `test_real_topology.py` capture gates. I did not accept the
claim and did not "fix" a non-defect.

---

## 5. Evidence that acceptance criteria are satisfied

| Criterion | Evidence |
| --- | --- |
| Raw data traceable | `evidence_refs` resolve via `resolve_evidence_ref`; guard test asserts **every** ref resolves |
| Original timestamps preserved | isolation copies `occurred_at`; byte-equality asserted |
| Historical factors preserved | `MeasurementFactor` intervals exempt from pruning; per-sample resolution tested |
| No silent data loss | audit tables exempt from retention; contaminated events retained |
| Known loads estimated within calibrated limits | synthetic strata recover the documented deltas (±tolerance in fixtures) |
| Unidentifiable cases not fabricated | ambiguous/unknown paths tested; no 0 W fabrication |
| Multiple associations supported | many-to-many tests in `test_association.py` |
| Contaminated excluded from V1 | `split_attribution`; concurrency guard now **fires** (was dead) |
| Dedicated circuits discoverable | 34 classification tests, incl. standby and dynamic |
| Shared circuits supported | `SHARED`, `PARTIALLY_UNDERSTOOD` with residual |
| Topology recoverable where evidenced | hierarchy recovery test on `main_subpanel_overlap` |
| Overlap not double-counted | residual refusal + canonical-channel tests |
| Correlation ≠ parenthood | Hot Tub overlap-only regression |
| Unknown topology supported | empty result + reason, no exception |
| History retained & reproducible | `test_same_raw_data_and_algorithm_version_reproduce_identical_records` compares full artifact tuples across two runs |
| Confidence auditable | `confidence_delta` + `reason` + `previous_record_id` |

**Reproducibility is measured, not asserted.** Two independent runs of the same seeded 20-minute
scenario produce byte-identical relations, circuits, records and residuals; record ids are
content-addressed (`rec_` + 24 hex), never allocated.

---

## 6. Known limitations (honest)

1. **No real-house validation.** All accuracy claims are synthetic ground truth. The 9 live-capture
   tests cannot run without a local `data/wattmosaic.db`. This is unchanged from baseline and is
   not a regression — but it means "all green" excludes real-installation coverage.
2. **V1 does not do automation-aware disaggregation.** Contaminated events are excluded from clean
   attribution, not decomposed. This is the brief's explicit V1 simplification.
3. **Candidate topology still requires user confirmation.** Nothing auto-promotes to fact.
4. **Factor semantics depend on the Refoss integration exposing configuration.** When it does not,
   the result is `UNKNOWN` — correct but less useful.
5. **Identifiability limit:** devices that always co-toggle cannot be separated; the system returns
   ambiguous rather than inventing values.
6. **Association duplicate-detection is heuristic** (equal levels + aligned deltas), not a proof.
   Two aliases differing by ~1 % may be treated as independent.
7. **SQLite single-writer** bounds ingestion throughput; unchanged from baseline.
8. **`mypy` has 120 pre-existing errors** in unrelated modules. Delta is 0; they were not fixed to
   avoid unrelated churn.
9. **The association delta-measurement window remains global** by design — switching it per device
   would change algorithm output, so it is documented rather than altered.

---

## 7. Remaining work (not started, by design)

- Real-house data replay once a capture exists (the gate tests are already written).
- Automation-aware contamination decomposition (explicitly out of V1 scope).
- Promotion of `candidate` topology to `confirmed` via the existing UI wizard — the API is ready.
- Surfacing factor intervals in the config surface (no UI exists today).

---

## 8. Deliverables index

| Deliverable | Path |
| --- | --- |
| Existing system assessment | `docs/assessment_gap_analysis.md` |
| Domain model & requirements inventory | `docs/domain_requirements_inventory.md` |
| Test scenario matrix (84 rows) | `docs/test_scenario_matrix.md` |
| Architecture & integration plan | `docs/implementation_plan_v2.md` |
| Factor semantics spec | `docs/factor_semantics.md` |
| Event isolation spec | `docs/event_isolation.md` |
| Association & topology spec | `docs/association_and_topology.md` |
| Residual analysis spec | `docs/residual_analysis.md` |
| Confidence model spec | `docs/confidence_model.md` |
| Inference audit spec | `docs/inference_audit.md` |
| Limitations & future work | `docs/limitations_v2.md` |
| API additions | `docs/api.md` §3.17–3.22 |
| Algorithms | `docs/algorithms.md` §13–§20 |
| Simulator & fixtures | `docs/testing.md` §7–§8, `tools/simulator/*` |
| This report | `docs/final_validation_report.md` |

---

## 9. Orchestration record

Work was delivered in four dependency-ordered waves by nine distinct subagent roles
(assessment, test engineering, contracts, factor, events, association, circuits, residual,
hierarchy, integration, remediation, wiring, documentation, independent verification).

Every cross-agent claim was re-verified by the orchestrator: the full suite was re-run after each
wave (`2386 → 2489 → 2610 → 2641 → 2668 → 2714 → 2734 → 2762`), `ruff` was run repo-wide, and the
four verifier defects were reproduced by execution before remediation was dispatched. No task was
marked complete on a subagent's word alone.