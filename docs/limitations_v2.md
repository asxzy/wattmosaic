# Limitations of the v2 feature set

What the shipped v2 code does **not** do, measured against the code rather than
the plan. Every claim here is either a citation to a default in the source or a
consequence of one, so a reader can check it.

The v1 boundary is unchanged by this release and is still binding
(`docs/implementation_plan_v2.md` §8). Where the plan and the code disagree, the
code is documented and the disagreement is listed in §9.

---

## 1. No real-house per-device accuracy claim

Every accuracy number in this repository is measured on the deterministic
synthetic household built by `tools/simulator/` (`tests/simulation/test_accuracy.py:1`).
The simulator mirrors the audited topology, cadence and impairment classes, but
the device power profiles are synthetic.

Consequences, stated plainly:

- `test_accuracy` proves the engine behaves correctly on a house with these
  characteristics. It does not measure accuracy on a physical installation
  (`docs/testing.md` §6).
- The replay tool reports **no per-device error at all**: there is no ground
  truth for a real house, and grading the engine against its own output would
  prove nothing (`tools/audit/replay_capture.py`).
- The v2 artifacts inherit this. An `ISOLATED` verdict means "nothing else in
  the window changed", which is measurable without ground truth; a
  `ct_association` at confidence 0.949 means "this CT responded consistently",
  which is also measurable. Neither means "this device's wattage is correct".
- `ResidualPower` is a signed difference against the declared scope, not a
  measurement of unmonitored load. A small residual means the *model* accounts
  for the reading, not that the physical wiring matches the model.

---

## 2. V1 does not solve automation-aware disaggregation

The plan's non-goal is unchanged in the code: contaminated events are excluded,
never decomposed.

- `EventIsolation` has three statuses (`contracts.py:992`) and
  `CONTAMINATED` carries a reason code, not a decomposed attribution.
- An association is only supported by `ISOLATED` events
  (`association.py:1108`, `evidence["isolation"]["rule"] = "isolated_only"`). A
  `CONTAMINATED` observation is retained as conflicting/withheld evidence, and
  is never used to support a relation (`association.py:599`).
- There is no primitive that attributes a *shared* power change to two devices
  at once. `AssociationFinder` can emit `verdict="split"` when solo events exist
  to apportion a joint response, but that is a confidence penalty
  (`SPLIT_MULTIPLIER = 0.75`, `association.py:153`), not a decomposition.
- Corollary worth stating for an operator: a house where two devices always
  toggle together will produce `verdict="ambiguous"`
  (`AMBIGUOUS_MULTIPLIER = 0.25`, `association.py:154`) or nothing at all — the
  engine declines rather than guessing, by design.

---

## 3. Nine tests are skipped without a local capture

`tests/simulation/test_real_topology.py` skips as a module when
`data/wattmosaic.db` is absent (`test_real_topology.py:61`), because the
reference capture is git-ignored and local-only. Measured on this checkout:
**2725 passed, 9 skipped** (`python -m pytest tests -p no:cacheprovider`), and
all nine skips are that module (lines 147, 182, 218, 268, 315, 353, 431, 450,
465).

What is therefore unverified in CI:

| Gate | Needs |
| --- | --- |
| `test_shipped_topology_declares_nested_parents` | capture |
| `test_nested_check_is_not_vacuous` | capture |
| `test_parentless_topology_double_count_is_caught` | capture |
| `test_real_replay_reports_whole_home_once` | capture |
| `test_real_replay_excludes_refused_totals_from_the_mean` | capture |
| `test_the_real_install_has_a_live_direct_measurement_path` | capture |
| `test_nested_replay_terminates_and_balances` | capture |
| `test_shipped_replay_terminates` | capture |
| `test_shipped_replay_does_not_double_count` | capture |

To run them, set `WATTMOSAIC_REAL_DB` to an existing capture or produce one with
`tools/audit/collect_samples.py`. A worktree has no `data/` directory, so the
variable is the normal path there.

The hierarchy overlap logic itself is *not* untested: the Hot Tub regression and
the synthetic hierarchy suite (`tests/unit/test_hierarchy.py`, 33 tests) cover
the refusal bar without a capture.

---

## 4. A candidate topology still needs user confirmation

Nothing in v2 promotes a hypothesis to fact.

- `TopologyRelation.requires_confirmation` defaults to `True`
  (`contracts.py:796`), and every inferred relation leaves it there.
- `Circuit.classification_status` defaults to `TopologyStatus.CANDIDATE`
  (`contracts.py:713`) and the pass never writes anything else. The circuit is
  classified from associations, and an association is itself a proposal
  (`MAX_CONFIDENCE = 0.99`, `association.py:144` — "confirmation is a user act").
- The hierarchy `declared=` argument carries user-declared parent links, and a
  declared link wins over an inferred one (`hierarchy.py:568`, `:619`); an inferred edge
  is added only when the evidence bar clears.
- `GET /api/v1/relations` reports `requires_confirmation` as a list of subject
  ids precisely so a client can render "proposed, not confirmed" without
  re-deriving it (`main.py:2090`).

Practical consequence: on a fresh install the audit tables contain proposals.
Treating `/circuits` or `/relations` output as configuration is an operator
error, not a supported workflow.

---

## 5. Factor semantics cannot be declared through the integration today

`MeasurementFactor` (the interval-bearing factor with `applied_by` and a
validity window) is a first-class contract object and storage table
(`contracts.py:590`, `migrations.py:121`). But **no code path creates one from
the Home Assistant integration or the HTTP API**:

- `Database.insert_measurement_factor` (`database.py:705`) has exactly one
  non-test caller set: the tests and the simulator builders
  (`tools/simulator/factors.py:389`). Nothing in `custom_components/` or
  `engine/wattmosaic_engine/main.py` inserts one.
- The integration form exposes `CTChannel.measurement_multiplier`
  (`flow_logic.py:58`, `flow_logic.py:320`), which is a single scalar with no
  validity interval. It is a *different* concept: `MeasurementFactor` answers
  "what factor was in force at this sample", the scalar answers "what factor
  applies now".
- Therefore `FactorRegistry.from_database` (`electrical/factors.py:499`) reads an empty
  factor store on a real install, and `factors_for_ct` returns `[]`. Every
  channel then classifies as `no_factor_declared` → `CT_MEASURED`
  (`docs/factor_semantics.md` §3).

**UNKNOWN:** whether the integration will ever expose factor intervals, and in
what UI. Not stated by any shipped code; do not assume it.

What this means operationally: on a real install the factor-aware refinements
(`FACTOR_ADJUSTED`, `MONITOR_REPORTED`, the Refoss ×2 distinction) are reachable
only through the simulator and the unit tests. The engine is not wrong to
default to `CT_MEASURED` — that is the documented answer for "nothing
declared" — but a user cannot yet *declare* anything through the app.

---

## 6. Identifiability limits that v2 does not remove

These are properties of the installed hardware, not bugs.

- **Co-toggling devices are not separable.** Two devices that switch within the
  same isolation window produce a `CONTESTED`/`CONTAMINATED` verdict for each
  other, and the association for both is withheld or downgraded. Widening
  `WM_ISOLATION_WINDOW_S` makes this *worse*, not better: it admits more
  competitors.
- **Shared-branch devices cannot be separated by CT count.** The acceptance
  coverage ceiling (~0.79) is real; higher coverage needs more CTs, not a better
  algorithm (`docs/testing.md` §6).
- **One CT cannot distinguish two appliances on one circuit** — that is exactly
  the `INSUFFICIENT_DATA` case, and the isolator says so instead of splitting
  the watts.
- **A single event never supports an association at full confidence.**
  `min_events` defaults to `2`; `1` is diagnostic-only and caps confidence at
  `SINGLE_EVENT_CAP = 0.5` with `verdict="single_event"`
  (`association.py:285`, `:147`).
- **Multi-CT residual scopes are refused, not summed.** Two clamps may be the two
  legs of one 240 V appliance (sum is right) or two monitors on one wire (sum
  double counts), and nothing in the evidence distinguishes them
  (`audit.py:1497`). The refusal code is `multi_ct_scope`.
- **A residual is never clamped.** `ZERO_TOL_W = 1e-9` and only an exactly-zero
  residual is `NEAR_ZERO` (`models/residual_power.py:112`, `contracts.py:521`),
  so a 1 W unexplained load stays positive forever.
- **`resolved=False` is a permanent state, not a queue.** An unresolved residual
  keeps its `unresolved_since` age (`models/residual_power.py:66`); nothing in
  v2 retries or resolves it later.

---

## 7. Storage and scale limits

### 7.1 SQLite, one writer

`Database` opens one connection with `check_same_thread=False` and serialises
every accessor behind a `threading.RLock` (`database.py:1-7`, `:80-91`). WAL mode
(`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=10000`,
`wal_autocheckpoint=1000`, `database.py:92-99`) lets short-lived read-only
connections run beside the writer (`database.py:101`), which is why a long
`ct_samples` scan no longer blocks `/health`.

It does not give you a second writer. Two engine processes on one database file
will contend, and the loser gets a `busy` error after 10 s, not a merge.

### 7.2 Audit tables have no retention horizon

`prune` deletes raw tables past `WM_RETENTION_DAYS_RAW` and aggregate tables past
`WM_RETENTION_DAYS_AGGREGATES`, then reports every table in `AUDIT_TABLES` with
a count of `0` and leaves it untouched (`database.py:1305`). `AUDIT_TABLES` is
seven tables (`migrations.py:121`).

That is the correct choice for audit state — an audit trail that evaporates in
30 days cannot audit — but it means:

- `inference_records`, `residual_power`, `topology_relations` and
  `isolation_verdicts` grow for the life of the database file.
- `inference_records` is bounded per device only by `_changed`
  (`audit.py:1626`): one row per material change plus a 1 h heartbeat
  (`RECORD_HEARTBEAT_S = 3600.0`). A steady 37-device install therefore appends
  roughly 37 rows/hour (~890/day) even when nothing changes.
- Verdict retention is bounded in *memory* (`DEFAULT_VERDICT_HISTORY = 512`,
  `audit.py:93`) but `isolation_verdicts` rows accumulate on disk and are
  upserted by `event_id` (`database.py:828`), so re-analysis does not grow them
  — new events do.
- There is no compaction or export tool for the audit tables beyond
  `GET /export/{device_id}`, which reads `inference_results` and *not*
  `inference_records` (`database.py:1347`).

### 7.3 Read-path costs

`latest_inference_results()` is a covering-index scan priced on the whole table
(~24 ms warm, ~590 ms cold at 343k rows) — deliberately unbounded by timestamp
because a device that stopped reporting still has a legitimate latest row
(`database.py:587`). The `/devices` and `/topology/tree` routes run it in a
thread for that reason. `/devices/{id}/records` is per-device and bounded by
`limit` (default 200, max 5000), so it is not in this class.

---

## 8. What the pass does not populate or check

Each of these is observable in the code and each limits what a reader may
conclude from a record.

| Gap | Detail |
| --- | --- |
| `ConfidenceVector.topology` never filled | `_record` builds only `power`, `ct_association`, `circuit_classification` (`audit.py:1724`). A `None` here means "not assessed", not "zero". |
| `ConfidenceVector.residual` never filled | same site; the residual module emits its own `ResidualPower`, not a confidence dimension. |
| Confidence-change detection is `power`-only | `_changed` compares a `ConfidenceVector(power=...)`, so a change confined to another dimension does not by itself write a record (`audit.py:1655`). |
| Audit pass always reports `config_source="global"` | `AuditConfig.isolation` builds a flat `IsolationConfig` with no `defaults`/`per_device` (`audit.py:199`), so `IsolationConfig.resolve` always answers the global layer (`isolation.py:277`). The class and per-device layers exist, but only via the learning pipeline's isolator (`learning/pipeline.py:154`), not the audit pass. |
| Entity bindings are not used by the audit pass | `EventIsolator(self.config.isolation)` is constructed without `bindings=` (`audit.py:918`), so `physical_device_events` sees no role map and every entity counts as potential load evidence. The learning pipeline does pass bindings (`learning/pipeline.py:185`). |
| Hierarchy version is not the audit version | `AuditConfig.hierarchy` sets `f"hierarchy:{algorithm_version}"` (`audit.py:217`) while `HierarchyConfig`'s own default is `"wp8-hierarchy-1.0.0"` (`hierarchy.py:127`). A reader must read the relation, not the class default. |
| `events_expired` is a counter, not a report field | Expired events increment `AuditPass.events_expired` (`audit.py:832`) but do not appear in `AuditReport.as_dict()`, so `/diagnostics` does not show them. |
| Pending events are bounded and lossy | `DEFAULT_PENDING_EVENTS = 256` (`audit.py:85`); the oldest is dropped when the deque is full, silently apart from `stages["pending_events"]["waiting"]`. |
| A reanalysis only records devices with a live result | `_records` iterates `by_device`, which is built from `results` (`audit.py:601`, `:1581`). A device absent from this cycle's inference gets no record even if its stored history changed. |
| No reanalysis on demand | `reanalyze` is called only from `_maybe_reanalyze` (`integration.py:348`) at `WM_REANALYSIS_INTERVAL_S` (default 3600 s). No HTTP route triggers it. |

---

## 9. Plan/code discrepancies found while writing this documentation

Recorded so a reader of the plan does not trust a number the code does not have.

| Claim | Reality | Citation |
| --- | --- | --- |
| "migration 9" is the last schema version | `LATEST_VERSION = 10`; migration 10 adds `isolation_verdicts` | `migrations.py:610`, `test_inference_records.py:77` |
| retention exempts four tables (`inference_records`, `ct_measurement_factors`, `topology_relations`, `ha_entity_bindings`) | `AUDIT_TABLES` has **seven**, adding `circuits`, `residual_power`, `isolation_verdicts` | `migrations.py:121` |
| `docs/api.md` contract version | line 7 is still the literal placeholder `XX`, and the versioning section says `CONTRACT_VERSION` (`1.4.0`) while the code says `1.6.0`; the same stale `1.4.0` appears in the example headers and the `/metrics` sample | `api.md:7`, `api.md:32`, `api.md:809`, `api.md:1059`, `contracts.py:32` |
| association confidence `0.9688 × 0.98 = 0.949` | recomputed from the code: `0.96875 × 0.98 = 0.949375` | `association.py:101` |
| `AuditConfig.event_max_age_s` is 1800 s and the tracker retains ~15 min | consistent, but note the two bounds are different mechanisms (queue age vs `StepTracker.max_samples`); an event can expire for either reason | `audit.py:145`, `audit.py:829` |

`docs/api.md` and the plan are outside this task's write scope, so the stale
lines are reported here rather than edited.

---

## 10. Known open defects

Honest list. Each is a real behaviour, not a documentation gap.

1. **`/api/v1/isolation` reports "no pass yet" from two different sources.**
   `main.py:2132` answers `source="no_analysis_pass"` when the backend has no
   `isolation_status` method, while `integration.py:888` and `audit.py:992`
   answer `"no_analysis_pass_yet"`. Both mean "nothing analysed"; a client that
   string-matches one will miss the other. The end-to-end test accepts either
   (`test_audit_api_e2e.py:227`).
2. **`docs/api.md` is stale** in the places listed in §9. Append-only scope
   prevented the fix.
3. **Factor intervals are unreachable from the app** (§5). Not a bug in the
   factor math, which is tested; a missing exposure path.
4. **The audit pass ignores entity bindings** (§8), so an appliance with two HA
   entities is seen as two potential device changes by the *audit* isolator.
   This is the conservative default (over-report competitors rather than
   under-report), but it is inconsistent with the learning pipeline.
5. **`AuditConfig.hierarchy` version string differs in shape** from the
   other three (`hierarchy:` prefix plus the audit version, versus
   `HierarchyConfig`'s own `wp8-hierarchy-1.0.0`). Any consumer grouping
   relations by version must not assume the class default.
6. **The manual `dedicated_device` declaration is deprecated but not removable
   yet** (ADR-0029). The role still works and still produces
   `Source.DIRECT_MEASURED`, because the reference installation's ~1032 W of
   measured power depends on it; `WM_DEPRECATE_DEDICATED_DECLARATION` only
   controls the notice. Three storage locations still describe the same fact
   (`CTChannel.device_id`, `DeviceTopology.dedicated_ct_ids`,
   `DeviceConfiguration.monitored_by_ct_ids`) and nothing auto-converts an
   existing declaration. A `branch` CT reaching `CircuitClass.DEDICATED` at
   `CONFIRMED` is the documented replacement, and it must be shown on live data
   before the declaration can be retired.
7. **Configured priors were not bounded by the row that carries them** (fixed
   in this change; kept here so the history and the guard are recorded together).
   ADR-0025 bounds each claimant *individually* (`config_ceiling_w` is a solver
   upper bound) and ADR-0026 bounds a *measured step* by the row's own reading
   (`step_w = min(step_w, |row|)`), but Level 3b had neither check: a declared
   `on_w` was reported whenever the device's state said "on", whether or not the
   circuit carried that much.

   The measured symptom: three fixtures declaring 60 + 120 + 240 W reported
   **420 W** on the `soffit_plug` circuit, which reads 58.01 W at the sampled
   instant (56.72-154.92 W over the capture; median 58.18 W), and two 600 W
   declarations drove the whole-home residual from +873.48 W to **-326.52 W**.
   Because a `configured_prior` is subtracted from the residual exactly like a
   measurement
   (`engine.py:_reconcile`, `accounted = direct + inferred + model + prior`), the
   surplus silently reduced the unknown load and could make the residual
   physically impossible.

   `DisaggregationEngine._bound_configured_priors_by_row` now bounds the **sum**
   of the priors on a row by that row's own usable reading, sharing it in
   proportion to what each claimant declared (never evenly, so a 10 W fixture is
   not credited double). Only `CONFIGURED_PRIOR` results are touched — direct
   readings are observations and fits were already constrained by the solver. A
   stale row or reverse flow skips the guard rather than substituting a wrong
   number. Replaying the 24 h capture with declarations of 420 W and 1200 W on
   that circuit now reports 58.01 W in both cases, with the residual staying
   positive (815.47 W); with the guard reverted they report 420 W / 1200 W and
   the latter drives the residual to -326.52 W.

   Not reachable on the current reference install: it declares no `expected.on_w`
   and no `monitored_by_ct_ids` (0 occurrences in its diagnostics payload), so
   every device takes the direct path and the whole-install replay is identical
   with and without the guard (65.25 % coverage, 34.75 % unknown load). The fix
   guards future and manual configuration; it is proven by the injected
   declaration probe and the tests below, not by the install's aggregate numbers.

   Verification: `tests/unit/test_device_attachment.py` —
   `test_configured_priors_cannot_exceed_the_row_they_share`,
   `test_an_over_declared_row_is_shared_in_proportion_to_the_declarations`,
   `test_a_prior_spends_only_what_the_row_has_left`,
   `test_a_prior_is_not_clipped_below_zero_by_a_reverse_flow_row`, and
   `test_a_prior_within_the_row_is_still_reported`. The previously pinned
   over-attribution in `test_auto_placement.py` is now
   `test_modelled_siblings_share_the_row_instead_of_over_reporting`.

Items 1–6 were not fixed in this task, and every claim above is written from the
shipped code as it stands. Item 7 was fixed; §10.7 records the guard, its
verification and the fact that the reference install cannot reach it today.

---

## 11. Related documents

- `docs/factor_semantics.md` — what a measurement basis means, and the gap in §5
- `docs/event_isolation.md` — why `CONTESTED`/contaminated events are excluded
- `docs/association_and_topology.md` — the evidence bar and refusal reasons
- `docs/residual_analysis.md` — signed residuals, `resolved` semantics
- `docs/confidence_model.md` — the dimensions that are and are not populated
- `docs/inference_audit.md` — the store that has no retention horizon
- `docs/testing.md` §6 — what the suite does not prove
