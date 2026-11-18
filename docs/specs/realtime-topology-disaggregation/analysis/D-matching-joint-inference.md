# Gap analysis D: dynamic matching, joint inference, identifiability, estimate status, confidence

Scope: HB §7, §8 (8.1–8.5), §12.2–12.3, acceptance tests 5, 6, 7, 8, 9, 10, 18.
Repo: `<repo>` (branch `spec/realtime-topology-disaggregation`). This was a read-only pass. I modified nothing in the repo and did not apply any stash. Paths below are relative to `engine/wattmosaic_engine/` unless stated otherwise. HB = `docs/specs/realtime-topology-disaggregation/handbook.md`.


## 0. Short answers to the key questions

1. **Is today's solve a per-snapshot absolute NNLS?** Yes. `DisaggregationEngine.infer` (`disaggregation/engine.py:704-966`) is stateless (class docstring at `:661-667`). It builds `y = A x` from the latest absolute reading of each channel (`electrical/matrix.py:213-265`) and solves bounded NNLS with `lower=0.0` hard-coded (`engine.py:1424-1434`, `:1463-1473`, `:1487-1493`). The only per-row noise model is diagonal `1/sigma` weights (`matrix.py:46-59`, `:392-393`). The unknown "slack" columns exist only on main rows and on rows the user declared shared (`engine.py:1278-1292`, `electrical/solver.py:258-296`).
   - Event deltas (`disaggregation/events.py` `detect_changes` and `StepChange`) are used only as side evidence: anchoring, the ADR-0026 measured step, and ambiguity detection. They never feed a delta equation `ΔY = H ΔP + ΔU`.
   - No temporal kernel, robust loss, covariance or signed-delta path exists.
2. **Where are the greedy single-candidate associations?**
   - (a) `_device_step` (`engine.py:314-334`) and `_attribution_step` (`:337-356`) each pick *one* best step per device.
   - (b) The ADR-0026 measured-step path (`engine.py:1930-2010`) credits the whole row step to the single device that changed, provided its siblings "held" (`_siblings_held` `:374-392`). It has no unknown-load competitor: a non-HA compressor starting in the same window is absorbed.
   - (c) `_bound_configured_priors_by_row` (`:2303-2416`) shares a row *in proportion to declarations* (`:2393-2401`). That is an allocation published as `CONFIGURED_PRIOR`, which maps to `ESTIMATED`.
   - (d) `_anchor_steps` (`:1004-1133`) *drops* a CT step that matches no HA transition (`:1116-1124`). It never becomes an unknown-load candidate.
   - (e) `AssociationFinder` (`disaggregation/association.py`) learns device→CT edges from isolated events only. Joint events are parked under `split_with` and never solved jointly.
   - (f) The per-device loop `engine.py:879-905` evaluates each device independently after the solve.
3. **How is "ambiguous" handled, and can it fabricate?**
   - `_detect_ambiguity` (`engine.py:1577-1620`) fires only when **≥2 devices changed state** in the window (`:1584-1590`) and share an SVD null-space group.
   - The members become `Source.UNKNOWN` with `power_w=0.0`, the placeholder (`:1872-1903`, `_unknown_result` `:2646-2673`).
   - `_group_bounds` (`:1622-1703`) then attaches the group's `min_w`/`max_w`/`group_members` to **each member** (`:907-929`).
   - The group's measured total is **never published as a group estimate**. It goes into `Reconciliation.unknown_residual_w`.

   A read-only probe confirmed the failures below (script outside the repo: `(probe script, not committed)`; it uses the helpers from `tests/unit/test_disaggregation.py`). Setup: `shared_branch_topo(2)`, both lights ON on one 500 W row, models `on_w=250` each.
   - **Transition cycle.** Members are `unknown, power_w=0.0, min_w=250, max_w=337.5`, and the residual is 500 W.
     - The published group range [250, 337.5] **excludes the measured 500 W**. `max_w` is the *min* over the members' individual caps (`engine.py:1688-1692`), but the group cap should be their *sum*.
     - The floor is `max(on_power)` (`:1693`), a model claim and not a mathematically justified bound.
   - **Next steady-state cycle (no transition).** Each member is `model_estimated, power_w=250.0, power_state=ESTIMATED` with `measurements_do_not_identify_device`, and the residual is 0. This is exactly the "two devices each 250 W" fabrication HB §8.4 forbids. It comes from the Level-3 path `engine.py:2142-2198`, which runs when `identifiable=False` and a model exists. It is pinned as *expected* behaviour by `tests/unit/test_disaggregation.py:1102` (`test_both_on_shared_branch_uses_model_estimate_not_fabricated_split`, which asserts 300/200).
   - **Same scenario without models.** Members are `unknown 0.0` with no group range at all (ambiguity needs a transition), and the residual is 500 W. No group estimate is produced.
   - **Degenerate collapse.** With no usable shared row and no caps, `max_w = min_w` (`engine.py:1698-1699`). That publishes a zero-width range such as [250, 250] on every member, which is a point value in disguise.
4. **What was wrong in the reverted attempt?** The stashes do **not** preserve the failing code verbatim.
   - `stash@{0}` ("wave2-wip-events-models-joint-orchestrator", base `036ffbc`) holds:
     - the source ledger (`_source_ledger`, which later landed as `f558bde` with modifications);
     - event kinds and `group_events` in `isolation.py` (later landed);
     - the cadence-derived isolation window (`audit.py`, `configuration.py`);
     - the `LoadShape`/`PowerLevel` schema;
     - the untracked `models/device_profile.py` in `stash@{0}^3`.
   - `stash@{1}` (base `74b686d`) holds the first group-range attempt, `_group_estimate` (`git show stash@{1}:engine/wattmosaic_engine/disaggregation/engine.py`, lines ~1439-1555), attached to `diagnostics["ambiguity"]["estimates"]`.

   What I can reconstruct about the fault in that design:
   - When no shared row is usable, `capacity=0.0` and `cap = max(cap, floor)`. The "range" then collapses to `[on_power, on_power]`, e.g. `[250, 250]`: a per-device-looking exact watt sourced from a model, not a measurement.
   - The floor is `max(abs(on_power_w))` and the cap is `min(capacity, *member_caps)`. Both mix per-member model claims into a group bound.
   - The bolt-on design left the per-device loop untouched. In any cycle without two transitions, the same members fall through to Level-3 `MODEL_ESTIMATED` 250 W each.

   The "fix" that landed (`0dd4d27`, now HEAD `engine.py:907-929`, `:1622-1703`) moved the range onto member results and added `(None, None)` for the no-evidence case. It **kept** all of the following:
   - the `max_w = min_w` collapse (`:1698-1699`);
   - the min-of-member-caps cap (`:1688-1692`);
   - the model-floor (`:1650-1654`, `:1693`);
   - the Level-3 per-member model estimate for non-identifiable members (`:2142-2198`).

   So the class of defect is reduced, not eliminated. Its tests (`test_disaggregation.py:646`, `:1024`, `:1058`) only cover the transition-plus-usable-row case.

## 1. Current-state map

| HB concept | Location (file:line) | What it does today | Verdict |
|---|---|---|---|
| §7.1 candidate generation (HA state, attrs, learned profiles, prior associations, plugs, unknown profiles, topology visibility, delayed responses, known groups) | `engine.py:261-311` `_collect_transitions`; `:741` every topology device is a column | The "candidate set" is every device in the topology, solved at once in an absolute snapshot. HA transitions and attribute levels are evidence only. There is no event-group-scoped candidate set, no delayed-response candidates, no unknown-load-profile candidates, no device-group candidates. Non-HA devices exist only as topology devices with no state. | PARTIAL (implicit and absolute, not event-scoped) |
| §7.1 non-HA candidates | `contracts.py:2494` `UnknownLoadProfile` (no runtime producer: grep shows only `contracts.py` and `mapping.py`); `engine.py:2447-2462` `unexplained_residual_without_transition` diagnostic | An event with no HA transition becomes a diagnostic only. No profile is created and nothing is matched. | ABSENT |
| §7.2 topology-based filtering, provisional edges soft | `electrical/topology.py:309-397` (`most_specific_branch_group`, `attribution_ct_ids`), `matrix.py:271-400` | H is built **hard** from `DeviceTopology.branch_ct_ids` and the CT tree. Edge lifecycle/confidence (`EdgeLifecycle`, `contracts.py:634`) does not influence H. `topology_status` only scales confidence (`confidence.py` TOPOLOGY_SCORE). There are no alternative H hypotheses. | CONFLICTS (provisional edges act as hard constraints) |
| §7.3 temporal kernels h_i(τ) | `models/device_profile.py:223-227,387-415` (`response_delay_s` declared); `engine.py:1004-1133` `_anchor_steps` window = `±(after_s + cadence)` | A declared delay exists in the profile but is not used by matching. The anchor window is symmetric and cadence-based, and ignores per-device delay or kernel. Only the first or best step is compared, never the whole window. `association.py:483-529` `_best_lag` searches lag 0..max_lag_s, but only in learning. | ABSENT for inference (PARTIAL asset in association) |
| §7.4 evidence A–H | spread out: sign `engine.py:1832-1839`; amplitude `:2088-2093` (MODEL_MATCH_REL_TOL); declared ceiling `:1955-1958`; quality `:1845-1865`; topology via confidence; no context (H) | Ad-hoc booleans and explanation strings. There is no per-candidate likelihood, no calibration, and no handling of evidence dependence. | PARTIAL (pieces exist; no structured score) |
| §7.5 unknown always competes | `solver.py:258-296` `augment_with_unknown_slack`; `engine.py:1278-1292` | Slack exists only on **main** rows and user-declared shared rows. Ordinary branch rows have no unknown column. The measured-step path (`:1930-2010`) has no unknown competitor. The slack has no learned penalty (only an upper bound, `:1407-1409`). | PARTIAL / CONFLICTS (greedy step path) |
| §7.6 match lifecycle UNMATCHED..EXPIRED | none (grep finds no `PROPOSED`/`UNMATCHED`/`EXPIRED`). Nearest: association verdicts `association.py:216-219` (supported/split/ambiguous/single_event); `EdgeLifecycle` `contracts.py:634-665`; `MappingCellStatus` `:705-744` | Engine output is a per-cycle snapshot with no match state. The association edge lifecycle belongs to topology learning, not per-event matching. | ABSENT |
| §7.7 simultaneous events evaluated jointly | `engine.py:1577-1620` (ambiguity), `isolation.py:934-1045` `EventGroup`/`group_events` (no engine consumer; grep finds no import outside isolation.py) | Simultaneity is detected, then members are refused (UNKNOWN). The groups that event isolation forms are not passed to inference. No joint explanation over candidate combinations. | PARTIAL (joint absolute solve exists; event-level joint matching absent) |
| §8.1 ΔY = H ΔP + ΔU + ε | none | Only the absolute `y = A x` exists. Deltas are never solved. | ABSENT |
| §8.2 robust objective with λ_m model, λ_u unknown, λ_t temporal penalties | `solver.py:378-575` `solve_constrained_nnls` (L2 plus optional ridge to prior, `engine.py:1421-1431`; `model_ridge` default 0.0 `:678`) | Plain weighted LS with box bounds, plus a refit that relaxes the upper bound (`:1449-1485`). No robust ρ. Model penalty is a ridge toward a prior and is off by default. Unknown penalty is absent (slack is free). No temporal term. | PARTIAL (solver reusable) |
| §8.2 signed deltas, reverse flow | `engine.py:1429,1468,1491` `lower=0.0`; `solver.py` accepts per-variable `lower` arrays | Device variables are forced ≥0. Reverse flow is preserved only at the main reconciliation (`main_power.py`). Solar and battery devices cannot be negative. | CONFLICTS |
| §8.3 covariance and nested double counting | `matrix.py:1-29`, `:213-265` (parent branch row minus children), `:392-393` diagonal weights | Nested branch rows are residualised (good for single counting). The child's noise propagates into the differenced row, but that row's sigma is not inflated. Main rows contain all devices plus slack, and are weighted as independent of the branch rows. No Σ, no conservative weighting, and no plug-anchor weighting. | PARTIAL |
| §8.4 identifiability (rank, cond, SVD, correlation, posterior, diversity, event history, anchors) | `solver.py:136-256` `analyse_identifiability`; `engine.py:1495-1513` | SVD rank, null-space share, condition number and union-find coupling groups on the **current snapshot's static H only**. No independent-event-history diversity (the case where two devices co-occur on a shared row but have switched independently before). No group-functional test (is `1ᵀx_G` identifiable?). No posterior. Results are published only in `diagnostics["identifiability"]`, not per result. | PARTIAL |
| §8.4 states IDENTIFIABLE..INSUFFICIENT_EVIDENCE | none (only `MappingCellStatus.NON_IDENTIFIABLE` `contracts.py:744`, `group_fit.GroupFit.individually_identifiable` `group_fit.py:236-244`) | No enum. Results carry a bool in provenance (`"identifiable": True/False`, `engine.py:2133`, `:2192`). | ABSENT |
| §8.4 mandatory group output (group 250 W; members null; justified bounds; NON_IDENTIFIABLE; evidence refs; source readers) | `engine.py:907-929`, `:1622-1703`; `contracts.py:2135-2144` (`min_w`, `max_w`, `group_members` on InferenceResult) | No group object and no group point estimate (the sum goes to residual). Members carry `power_w=0.0` instead of null (`contracts.py:2072` `power_w: float`, non-optional). Member min/max describe the **group sum**, not the member, and are wrong as shown in §0. The steady-state cycle fabricates per-member model values. Only `group_fit.py` (offline discovery) does this correctly (`GroupFit.total_w` plus `GroupMember.estimated_w=None`, `group_fit.py:205-244`), and it is consumed only by `main.py:2496,2773` placement discovery. | CONFLICTS |
| §8.5 MEASURED / ESTIMATED / ALLOCATED / UNKNOWN | `contracts.py:76-146` `PowerState`, `power_state_for` | Label derived from `Source`. `INFERRED → ALLOCATED` always (`:131-132`), even for an identifiable joint solve or a measured step. `CONFIGURED_PRIOR → ESTIMATED` even after proportional row sharing. `MEASURED` is **never produced**: `Source.DIRECT_MEASURED` has no producer since ADR-0034 (`docs/decisions/README.md:2003`, commit `0602b74`), so a smart-plug-only device is `INFERRED`/`ALLOCATED`. Bounds, measurement source and inference source are not first-class fields. | CONFLICTS |
| §8.5 confidence vs identifiability separation | `disaggregation/confidence.py` (whole file), `tiers.py` | Engineering score: source base × blend × penalties; `ambiguous` is a ×0.5 penalty. Tiers grade evidence kind. Identifiability is folded into source choice, not published as its own axis. | PARTIAL |
| §12.2 event inference record | `contracts.py:1292-1408` `InferenceRecord` (per **device**, append-only, `previous_record_id`, `revision`); `contracts.py:2791-2860` `EventEvidenceRecord` (per HA event, immutable); `audit.py:1970-2100` `_records` | Records are per device per cycle, built in the audit pass. Nothing carries per event group: ΔY vector, candidate set, chosen and rejected hypotheses, unknown contribution, group estimates, identifiability, final event status. | ABSENT (versioning pattern reusable) |
| §12.3 DETECTED→MATCHING→PROVISIONAL→CONFIRMED→REVISED→UNRESOLVED | `isolation.py:1311-1380` `EventIsolator.reclassify` (late event revises isolation verdict); `audit.py:1144-1199` `_mature_events`; `audit.py:931-989` `reanalyze` | Late data revises *isolation* verdicts and the learning intake. Inference output has no lifecycle: every snapshot is final and silently replaced. No provisional/confirmed flag on results. | ABSENT (PARTIAL assets) |
| Source ledger (HB §18 provenance) | `engine.py:448-617`, attached `:942-960` | One batch-wide ledger is copied onto every result (`:958-960`), so it is not per-estimate provenance. `residual_events` is reported as "consumed" (`:157-170` in `f558bde`) although the solver never uses it. | PARTIAL / overclaims |
| HB test 5 (response delays) | none | No kernel. The anchor window would drop a delayed HVAC compressor step as "outside transition anchor" (`engine.py:1116-1124`). | ABSENT |
| HB test 6 (simultaneous joint) | `test_disaggregation.py:259`, `:288`, `:683` (separable case via absolute solve) | Works only when separable by distinct rows. | PARTIAL |
| HB test 7 (group not fabricated) | `test_disaggregation.py:646`, `:999`, `:1024`, `:1058`; `tests/simulation/test_accuracy.py:289` | Asserts placeholder 0.0 and residual. No group estimate. `:1102` asserts per-member model values on the non-identifiable steady state. | CONFLICTS |
| HB test 8 (plug anchors) | none (ADR-0034 removed direct) | | ABSENT/CONFLICTS |
| HB test 9 (covariance) | `test_disaggregation.py:351`, `:816` (no double count only) | | PARTIAL |
| HB test 10 (unknown alternative) | `test_disaggregation.py:383`, `:794`, `:1058`, `:1726` | Residual preserved at the main and shared levels. No per-event unknown hypothesis. | PARTIAL |
| HB test 18 (late events revise provisional) | `tests/unit/test_event_isolation.py` (reclassify), `tests/simulation/test_state_lag.py` | Covers isolation, not inference revision. | ABSENT |

## 2. Conflicts that need a breaking change

| # | Legacy concept | HB rule violated | Proposed replacement | Blast radius |
|---|---|---|---|---|
| C1 | `InferenceResult.power_w: float` (required; `contracts.py:2072`) plus `provenance["power_w_is_placeholder"]` and 0.0 convention (`engine.py:2660-2669`, `:2678-2688`) | §8.4 members null; §18 "absent must not become zero" | `point_w: float \| None` plus `lower_w`/`upper_w` (individual bounds) plus `estimate_status` plus `identifiability`. Remove the placeholder flag. | `aggregation/aggregator.py:12,255-262`, `integration.py:1068-1095`, `electrical/panel_tree.py:196`, storage `database.py:1230-1238` / `migrations.py:493`, API schemas `data/schemas/inference_result.json`, `snapshot_response.json`, HA `custom_components` sensors, `addon/ui/app.js`, 17 test files construct `DisaggregationEngine` |
| C2 | Group range stored on member results (`min_w`/`max_w`/`group_members`, `contracts.py:2135-2144`; `engine.py:907-929`) | §8.4: group estimate is its own object; member bounds must be individually justified | New `DeviceGroupEstimate` {group_id, members, point_w (the identified sum), lower/upper of the sum, identifiability=GROUP_IDENTIFIABLE, source readers, evidence refs}. Members get `point_w=None` and their own bounds (e.g. [0, G] when non-negativity holds). Delete `_group_bounds`. | `mapping.py` (group rows), UI group view, schemas, `test_disaggregation.py:646/1024/1058` |
| C3 | Group sum dumped into `Reconciliation.unknown_residual_w` (`engine.py:2426-2440`) | §8.4 (group 250 W is *known*), §22.13–14 | Accounting identity becomes `main = Σ identified devices + Σ group sums + unknown + residual`. Add `group_w` to `Reconciliation`. | `Reconciliation` contract, `aggregation`, `tests/unit/test_reconciliation_split.py`, `test_accuracy.py:328-352` (residual-preservation ratios move) |
| C4 | Level-3 per-member `MODEL_ESTIMATED` for non-identifiable devices (`engine.py:2142-2198`); test `:1102` | §8.4 "individual estimates null unless separately identifiable" | A model may give an individual estimate only if the model carries independent-event provenance (learned from isolated events; see open question Q1) **and** the identifiability engine marks the device IDENTIFIABLE through history. Otherwise the device is a group member with null. | `test_disaggregation.py:1102`, `test_accuracy.py` strata, learning and model provenance interface |
| C5 | Proportional row sharing of configured priors (`engine.py:2393-2401`) labelled `CONFIGURED_PRIOR→ESTIMATED` | §8.4 equal or proportional split is arbitrary; §8.5 ALLOCATED must not look like an estimate | Either drop proportional clipping and publish the group with null members, or publish the clipped values explicitly as `ALLOCATED` with `allocation_rule="declared_proportional"`. PO decides (Q7). | ADR-0030 must be superseded; `tests/unit/test_*prior*` |
| C6 | `power_state_for`: `INFERRED → ALLOCATED` always (`contracts.py:131-132`); `Source` enum as the hierarchy | §8.5: ALLOCATED = share of an unidentified total; an identified joint solve is ESTIMATED | Replace `Source`+`PowerState` with `EstimateStatus` {MEASURED, ESTIMATED, ALLOCATED, UNKNOWN}, set by the inference step that produced the value, plus a separate `inference_method` (direct_reading / joint_delta / joint_absolute / model / declared). Keep the old `Source` readable for historical rows (HB §16.6–7). | `contracts.py`, `tiers.py`, `confidence.py` SOURCE_BASE, storage, UI strings, `test_disaggregation.py:1757` |
| C7 | ADR-0034: no `DIRECT_MEASURED` producer; smart plug = solo branch row → `INFERRED` | §8.3 plug anchor, §8.5 MEASURED, acceptance 8 | A reader whose topology scope is a single device or load component (from the topology/domain agent's measurement endpoint scope) yields `MEASURED` and acts as a hard or high-weight anchor in the joint solve. Needs an ADR superseding ADR-0034's "concept removed" (structural, not the old `dedicated_device` declaration). | ADR log, `confidence.py` floors, `tiers.py`, sim tests `test_accuracy.py:173-244` (solo-row strata) |
| C8 | Absolute-only NNLS with `lower=0` for every device (`engine.py:1424-1493`) | §3.2 / §8.1 delta model; §8.2 signed deltas, reverse flow | Add an event-delta joint solver with signed bounds per transition direction. Keep the absolute solve for steady state, with per-device sign class (consumer ≥0; PV/battery signed). | `electrical/solver.py` callers; `test_safety_invariants.py:202` regex must still pass |
| C9 | Ambiguity only when ≥2 devices changed (`engine.py:1584-1590`) | §8.4: identifiability is a property of H plus history, not of this cycle's transitions | The identifiability service runs every cycle and on every event group. Group identity persists across cycles (stable `group_id`). | `diagnostics["ambiguity"]` consumers: `integration.py`, `device_links.py`, UI |
| C10 | Hard H from `branch_ct_ids` (`topology.py:309-397`, `matrix.py:271-400`) | §7.2 provisional edges soft | H entries carry `edge_confidence`/`provisional`. Matching keeps alternative H hypotheses (or a soft prior weight) and never rejects a candidate on a provisional edge. | Topology agent's operator interface; `matrix.py` |
| C11 | `_anchor_steps` drops unanchored steps (`engine.py:1116-1124`); measured-step greedy credit (`:1930-2010`) | §7.5, §7.7; §17 step 7 (fridge without HA) | Unanchored steps become event groups with an unknown-load candidate. The measured-step rule becomes one hypothesis scored against an unknown-load alternative. | ADR-0026 superseded or amended; tests `test_disaggregation.py:1554-1726` |
| C12 | Batch-wide source ledger copied to every result (`engine.py:958-960`), with `residual_events` reported as used | §18 per-estimate provenance | Per-estimate `evidence_refs`, `source_reader_ids`, `model_versions`, `topology_version`, `mapping_version`. Ledger reasons derived per estimate. | `tests/end_to_end/test_full_inference_chain.py` |

## 3. Reusable assets to keep

- `electrical/solver.py`:
  - `solve_constrained_nnls` (`:378-575`) is deterministic. It supports per-variable `lower`/`upper` arrays, a prior plus ridge, and row preconditioning (`_precondition_rows` `:299-376`). It is a good inner solver for a robust IRLS loop and supports signed bounds.
  - `analyse_identifiability` (`:136-256`): SVD rank, null-space share, condition number and coupling groups. Extend it with a group-functional test and history stacking; do not replace it.
  - `augment_with_unknown_slack` (`:258-296`) generalises to per-reader ΔU columns.
- `electrical/matrix.py`: nested-row residualisation (`:213-265`), `row_sigma` (`:46-59`), alias handling. The single-counting logic is correct and well tested (`test_disaggregation.py:351`, `:816`).
- `disaggregation/group_fit.py`: the correct group semantics (`GroupFit.total_w`, `GroupMember.estimated_w=None`, derived `individually_identifiable`; `:205-244`), the rotated-null gate, co-toggle grouping (`_co_toggle_groups` `:504-553`). Reuse for history-based group identification and as the background-loop source of persistent device groups.
- `disaggregation/events.py`: robust MAD step detection and `StepTracker`. These are inputs to the pipeline agent's change detection.
- `disaggregation/isolation.py`: `EventGroup`/`group_events` (`:934-1045`), `reclassify` (`:1311-1380`) for late arrivals, `clean_attribution_inputs` (`:1398`). The pipeline agent probably owns these. My area consumes `EventGroup`.
- `disaggregation/association.py`: `_best_lag` (`:483-529`), the conflicting-evidence ledger, and the verdict vocabulary. These are good models for match evidence C (amplitude), A (lag) and repeatability.
- `engine.py` safe guards worth keeping as constraints in the new solver:
  - state pinning of OFF devices with evidence-derived standby caps (`:1336-1357`);
  - declared-ceiling non-relaxable bounds (`:1380-1396`);
  - partial-leg exclusion (`:1135-1210`);
  - stale/missing row exclusion with reasons (`:1232-1253`);
  - the meter-spread decomposition in reconcile (`:2489-2521`).
- `contracts.py`: the `InferenceRecord` append-only revision chain validator (`:1292-1408`) is the pattern for `EventInferenceRecord` versions. `EventEvidenceRecord` (`:2791`) is immutable evidence. `ConfidenceVector` stays per-dimension.
- `confidence.py`: keep it as an engineering score but re-scope its inputs (identifiability becomes its own axis, not a source base). `tiers.py` may be retired or remapped to estimate status (Q10).
- Tests worth porting rather than rewriting: `test_disaggregation.py:259-470` (separable, nested, double-pole, polarity), `:794-872` (residual never distributed), `test_safety_invariants.py:202-307`, and the mutation-proof pattern in `tests/unit/test_residual_power.py`.

## 4. Proposed work packages

Design principle: new package `engine/wattmosaic_engine/inference/` holds the event-driven matching and joint inference. `disaggregation/engine.py` becomes a thin orchestrator. Do the mechanical split first (D0) so that later packages own disjoint files.

### WP-D0: Split `disaggregation/engine.py` with no behaviour change (S/M, do first, one agent)
- **Objective.** Move code out without changing logic, so later packages own separate files:
  - `_solve_system` and `_exclude_partial_legs` → `disaggregation/absolute_solve.py`;
  - `_detect_ambiguity` and `_group_bounds` → `disaggregation/ambiguity.py` (to be deleted by D5/D6);
  - `_infer_device` and `finish` → `disaggregation/device_hierarchy.py`;
  - `_bound_configured_priors_by_row` and `_reconcile` → `disaggregation/reconcile.py`;
  - `_source_ledger` → `disaggregation/ledger.py`;
  - `_anchor_steps`, `_collect_transitions`, `_device_step`, `_attribution_step` → `disaggregation/transitions.py`.
- `engine.py` keeps `infer()` wiring only.
- **Owns:** `disaggregation/engine.py` and the new files listed.
- **Tests:** the whole existing suite green, byte-identical outputs on `tests/simulation/test_replay_versions.py`.
- **Deps:** none.

### WP-D1: Inference contracts (S/M; co-owned with the domain/contracts agent)
- **Objective.** Specify and land the types below, ideally in a new `inference/types.py` re-exported from `contracts.py`, so the hot contracts file is touched once:
  - `IdentifiabilityStatus` {IDENTIFIABLE, PARTIALLY_IDENTIFIABLE, GROUP_IDENTIFIABLE, NON_IDENTIFIABLE, INSUFFICIENT_EVIDENCE};
  - `EstimateStatus` {MEASURED, ESTIMATED, ALLOCATED, UNKNOWN};
  - `MatchState` {UNMATCHED, PROPOSED, PROVISIONAL, CONFIRMED, AMBIGUOUS, CONFLICTED, REJECTED, EXPIRED};
  - `EventInferenceStatus` {DETECTED, MATCHING, PROVISIONAL, CONFIRMED, REVISED, UNRESOLVED};
  - `CandidateHypothesis`;
  - `DeviceEstimate` (point_w nullable, lower_w/upper_w, status, identifiability, confidence vector, measurement_sources, inference_method, evidence_refs, versions);
  - `DeviceGroupEstimate`;
  - `JointInferenceResult`;
  - `EventInferenceRecord` (immutable evidence ref plus a versioned interpretation chain).
- Deprecate `min_w`/`max_w`/`group_members` on `InferenceResult`, the `power_w_is_placeholder` convention and `INFERRED→ALLOCATED`. Add a schema version plus a migration note (HB §16).
- **Owns:** `inference/types.py`, `data/schemas/*` regeneration.
- **Tests:** contract validators (point_w None ⇒ status ∈ {UNKNOWN} or member of group; ALLOCATED requires allocation_rule; MEASURED requires a direct-scope reader ref; group sum bounds contain point).
- **Deps:** domain agent (Physical Device / Load Component ids, Measurement Endpoint scope), storage agent (record persistence).

### WP-D2: Candidate generation and topology filtering (M)
- **Objective.** `inference/candidates.py` builds, for an `EventGroup` from the pipeline agent, a candidate set from all nine sources in HB §7.1:
  - HA transitions and attribute changes in the window;
  - devices with learned response profiles;
  - prior associations (mapping cells);
  - plug readers;
  - unknown load profiles;
  - topology-visible devices;
  - pending delayed responses;
  - known device groups.
- It **always** adds one unknown-load candidate per affected reader scope.
- Topology filter: compute the expected signature h_d from the operator. Reject only on CONFIRMED-edge incompatibility. Down-weight on provisional edges and keep the candidate.
- Bounded candidate count, with rejected candidates recorded with a reason.
- **Owns:** `inference/candidates.py`, `inference/topology_filter.py`.
- **Tests:**
  - fridge step without HA yields an unknown candidate plus the fridge-profile candidate (HB §17.7, acceptance 4/10);
  - a provisional edge keeps the alternative;
  - a confirmed incompatible edge rejects with a reason;
  - the unknown candidate is present in 100 % of groups (property test).
- **Deps:** pipeline (EventGroup with per-reader ΔY, σ, window, member HA events), topology (operator H with edge lifecycle/confidence and reader scope), models (profile index), mapping (current association cells).

### WP-D3: Temporal kernels and matching evidence A–H (M)
- **Objective.** `inference/temporal.py`: evaluate h_i(τ, transition, attributes) over the whole event window (step, delayed step, transient plus plateau, return-to-standby). Default kernel from `response_delay_s`, else instantaneous.
- `inference/evidence.py`: per-candidate log-likelihood terms A–H, computed from the window, with an explicit dependence correction. Group terms sharing the same ΔY so they are not counted twice (e.g. topology consistency and reader-signature agreement share a correction factor). Weights live in a versioned config.
- Scores are passed to the joint step as priors/penalties, never as a winner-take-all choice.
- **Owns:** those two files.
- **Tests:**
  - acceptance 5: light (τ≈0), blind (transient then standby), HVAC (thermostat at t0, compressor at t0+90 s) each matched to the right window; the HVAC step is *not* dropped as unanchored;
  - sign disagreement lowers the score but does not hard-reject unless the device is a confirmed consumer;
  - evidence terms derived from one reader are not double-weighted.
- **Deps:** models agent (kernel parameters, per-transition ΔP prior mean/σ), pipeline (window samples).

### WP-D4: Joint delta solver (L)
- **Objective.** `inference/joint.py`: per event group, solve `min Σ_r w_r ρ((ΔY_r − Σ H_ri ΔP_i − ΔU_r)/σ_r) + λ_m L_model + λ_u L_unknown + λ_t L_temporal`. Requirements:
  - Huber/soft-L1 via IRLS around `solve_constrained_nnls`;
  - signed bounds per transition (ON ⇒ ΔP≥0 for a consumer; OFF ⇒ ≤0; PV/battery unrestricted);
  - one ΔU column per independent reader scope, with a penalty λ_u learned from the residual agent's residual behaviour, measurable and configurable (HB §8.2).
- Covariance: build Σ for nested readers. The parent-minus-child differenced row has variance σ_p²+Σσ_c²; a parent row and a child row observing the same load are correlated. Use GLS when Σ is available, else conservative weighting (inflate correlated rows' σ by √k). Never treat them as independent.
- Plug anchors: a reader whose scope is a single device is a near-hard constraint.
- Also port the absolute steady-state solve onto the same objective, with an absolute H and non-negative absolute bounds. Retain HB §3.2's two models.
- **Owns:** `inference/joint.py`, `inference/covariance.py`, `electrical/solver.py` (add robust/IRLS wrapper and signed bounds; no other agent edits it).
- **Tests:**
  - acceptance 6: HB §7.7 example, Circuit A +45, Circuit B +60, Main +110, three candidates; the joint result explains A and B separately, and 5 W lands in ΔU on Main, not in a device;
  - acceptance 9: one load on Circuit A ⊂ Subpanel ⊂ Main; the estimate's variance with three readers is not 1/3 of single-reader variance (no independence assumption), and the load is counted once;
  - acceptance 8: a plug reading of 3 W standby anchors the TV even when the circuit is noisy;
  - signed: battery discharge ΔP<0 is preserved;
  - an OFF transition yields a negative ΔP, not clipped to 0;
  - `test_safety_invariants.py:202` still passes.
- **Deps:** topology (H_k, reader nesting), residual (σ_u, residual model per reader), pipeline (ΔY_k, σ_r per reader, timestamps aligned), models (ΔP priors).

### WP-D5: Identifiability and device-group service (M/L)
- **Objective.** `inference/identifiability.py`. Stack the H rows from the current group plus the historical independent event groups (with candidate on/off pattern columns) to get an "experiment matrix". Then compute:
  - rank, condition number, singular values, column correlation;
  - null-space share per device;
  - **group functional identifiability**: for each coupling group G, is `1_Gᵀ x` orthogonal to the null space?
  - posterior covariance from the D4 solve.
- Classify every device and group into the five states. Persist stable `group_id`s, reusing `group_fit._co_toggle_groups` for history-derived groups.
- Compute **mathematically justified bounds** by small LPs: min/max of x_i subject to the solved group sums and the physical sign constraints. Under non-negativity alone each member gets [0, G]. Model priors are **not** bounds. Delete `_detect_ambiguity`/`_group_bounds`.
- **Owns:** `inference/identifiability.py`, `inference/bounds.py`; deletes `disaggregation/ambiguity.py` (from D0).
- **Tests:**
  - **mandatory 250 W test** (section 4a below);
  - two lights that switched independently in 5 historical events are IDENTIFIABLE even when co-switched now;
  - three devices where the sum and one member are identifiable give PARTIALLY_IDENTIFIABLE;
  - no events or stale rows give INSUFFICIENT_EVIDENCE;
  - bound LP: a member never gets an upper bound below what the measured sum forces, i.e. the case that reproduced `max_w=337.5 < 500` must fail the old code;
  - a zero-width bound is only allowed when the device is identifiable.
- **Deps:** D4 (posterior), storage (event history query), models (independent-event provenance per model).

### WP-D6: Estimate assembly, status and confidence (M)
- **Objective.** `inference/estimates.py` turns D4 and D5 output into `DeviceEstimate`/`DeviceGroupEstimate` per §8.5:
  - MEASURED only for a direct-scope reader;
  - ESTIMATED for an identifiable solve or a model with independent provenance;
  - ALLOCATED only through an explicit, named allocation rule, and never by default;
  - UNKNOWN otherwise.
- Group sum published as a group estimate, never as residual. Members null.
- Re-scope `confidence.py`: identifiability becomes a separate axis; confidence computed from posterior σ, quality, evidence and topology certainty; not calibrated unless calibrated.
- Replace `Source`/`power_state_for` usage in the engine. Retire Level-3 per-member model estimates for non-identifiable members, Level-3b proportional sharing (per the Q7 decision) and the `power_w=0.0` placeholder.
- Per-estimate provenance: readers, events, model versions, topology version and mapping version (fixing the ledger overclaim).
- **Owns:** `inference/estimates.py`, `disaggregation/confidence.py`, `disaggregation/tiers.py`, `disaggregation/ledger.py`, `disaggregation/device_hierarchy.py` (shrinks or retires).
- **Tests:**
  - acceptance 7 (shared with D5);
  - acceptance 8;
  - HB §17 step 8 (TV standby 3 W is MEASURED, not 0);
  - a status/source matrix test replacing `test_disaggregation.py:1757`;
  - `test_disaggregation.py:1102` rewritten to assert null members plus a 500 W group;
  - a mutation-proof test where a forced equal split and a forced model-fill must each fail.
- **Deps:** D1, D4, D5; the mapping agent consumes the output.

### WP-D7: Match lifecycle and event inference records, provisional→confirmed→revised (M)
- **Objective.** `inference/matching.py` holds a per (candidate, event group) MatchState machine. It is stateful, so it lives outside the stateless solver in an explicit store passed in, keeping `infer()` replay-deterministic from (evidence, store snapshot, versions).
- `inference/records.py`: `EventInferenceRecord` with an immutable evidence part and a versioned interpretation (rev 1 PROVISIONAL is published quickly; a late reader or delayed response gives REVISED; window closure gives CONFIRMED or UNRESOLVED). Use the `InferenceRecord` chain validator pattern. Confirmation rule examples: N repeated consistent events promote PROVISIONAL→CONFIRMED; a contradiction moves CONFIRMED→CONFLICTED. Expiry is applied.
- **Owns:** `inference/matching.py`, `inference/records.py`, plus the storage repository module for these records (coordinate with the storage agent).
- **Tests:**
  - acceptance 18: a provisional estimate published at t0 with only Main; Circuit A arrives 20 s late; revision 2 changes the attribution; revision 1 is preserved and the evidence is unchanged;
  - acceptance 10: the unknown candidate's MatchState is persisted per group;
  - HB §7.6: 1 event gives PROVISIONAL, 5 consistent events give CONFIRMED, a contradictory event gives CONFLICTED, and history stays traceable;
  - replay of stored evidence with a newer model version gives a new interpretation version (acceptance 19 support).
- **Deps:** pipeline (late-arrival notifications, window closure), storage (append-only tables), mapping (consumes CONFIRMED matches to update cells).

### WP-D8: Orchestration wiring and legacy removal (M)
- **Objective.** `disaggregation/engine.py` and `integration.py` call: event groups → D2 → D3 → D4 → D5 → D6 → D7, plus the steady-state absolute pass. Remove the dead legacy paths:
  - the measured-step greedy credit;
  - unanchored-step dropping;
  - `diagnostics["ambiguity"]`;
  - the placeholder flags in `aggregation/aggregator.py`, `integration.py:1068-1095` and `electrical/panel_tree.py`.

  Update the HA-facing projection (null semantics) jointly with the UI/API owners (HB §16.9).
- **Owns:** `disaggregation/engine.py`, `integration.py` (inference section only), `aggregation/aggregator.py` placeholder handling.
- **Tests:**
  - an end-to-end HB §17 scenario, steps 3–8, through `WattMosaicBackend.process`;
  - `tests/simulation/test_accuracy.py` re-baselined so recall gains from group estimates are visible and `forced_attribution_count == 0` still holds;
  - new strata for group-identified energy.
- **Deps:** all of D1–D7; the pipeline and mapping agents.

**Dependency order:**
1. D0 and D1 first.
2. Then D2, D3, D4 and D5 in parallel. D5 needs D4's posterior interface stubbed: agree `JointSolution{x, cov, active_rows, residual}` up front.
3. Then D6 and D7 (parallel).
4. Then D8.

### 4a. Mandatory 250 W group test (D5 owns it; D6 and D8 re-run it end to end)

**Setup:**
- Main CT ⊃ Subpanel CT ⊃ Circuit A CT. Light 1 and Light 2 sit on Circuit A only.
- Twelve historical event groups where both always switch within 1 s. They are never independent.
- Current event: both OFF→ON. Circuit A, Subpanel and Main each read +250 W (±2 W noise).
- Variant B: models declare 125 W each.
- Variant C: a steady-state snapshot with no transition, one cycle later.

**Assertions:**
- Exactly one `DeviceGroupEstimate` with `members={L1, L2}`, point 250 W (±noise), `identifiability=GROUP_IDENTIFIABLE`, `source_readers` = [Circuit A, Subpanel, Main], and non-empty `evidence_refs` naming the event group and readers.
- Each member has `point_w is None`, `identifiability=NON_IDENTIFIABLE` and bounds exactly `[0, 250]` (±noise), derived from non-negativity. No 125 W anywhere.
- Status is never ALLOCATED or ESTIMATED with a value for a member.
- Unknown ΔU ≈ 0. Residual does **not** contain the 250 W. Total attributed = 250 W, not 750 W (no triple counting).
- Variants B and C produce the identical member output. Models do not licence a split; steady state does not fall back to per-member model values.
- Mutation guards, each of which must make the test fail:
  - patching the solver to return the equal split;
  - patching estimate assembly to fill members from models;
  - collapsing the bounds to a zero-width [250, 250].
- Positive control: add 3 historical events where L1 switched alone at 100 W. The result becomes IDENTIFIABLE with L1≈100 and L2≈150 as ESTIMATED with finite bounds.

## 5. Traps and constraints

1. **Do not re-bolt a range onto per-device results.** That was the failure mode in both stash attempts and in `0dd4d27`. Ambiguity was detected *after* the solve, and the per-device hierarchy (`_infer_device` Levels 2–3b) still ran for the same devices. The group must be a first-class output, and the per-device path must not run for group members.
2. **Group bounds are about sums, member bounds are about members.** Do not take `min` over member caps for a group cap (`engine.py:1688-1692`). Do not use a model's on-power as a floor. Never collapse `max = min` without identifiability (`:1698-1699`). Every published range must contain the measured value.
3. **Snapshot inconsistency.** The same devices flip between UNKNOWN (cycle with transition) and MODEL_ESTIMATED 250/250 (next cycle, `engine.py:2142-2198`). Identifiability must be a property of H plus history and persist across cycles; it must not depend on whether a transition happened this cycle (`:1584-1590`).
4. **Hidden allocations.** Proportional prior clipping (`engine.py:2393-2401`) and the ADR-0026 measured step are allocations or greedy credits that look like estimates. Under HB they need an explicit ALLOCATED rule or removal. ADRs 0026 and 0030 need superseding records (HB §16.2).
5. **Null semantics ripple.** `power_w` is a required float, and 0.0-placeholder handling is duplicated in `aggregation/aggregator.py`, `integration.py:1068-1095` and `electrical/panel_tree.py:196`. HA `total_increasing` energy sensors integrate power, so a null must mean "do not integrate", not 0. Coordinate with the storage, API and UI owners and land it in one release (HB §16.9).
6. **Determinism and replay.** The engine is deliberately stateless (`engine.py:661-667`). Match lifecycle and history-based identifiability need state. Inject it as an explicit, versioned store snapshot so stored evidence can be replayed bit-identically (`tests/simulation/test_replay_versions.py`). Do not hide state in `StepTracker`-like singletons.
7. **Signed deltas vs the safety regex.** `test_safety_invariants.py:202-226` flags `max(0, <measurement-name>)`. Delta code must keep the sign, and should avoid naming fitted quantities `observed_w`/`reading` and then clamping them.
8. **Unknown-penalty balance.** HB §8.2 requires λ_u to be learned, measurable and configurable. A free slack (today) is equivalent to λ_u=0 on main rows and λ_u=∞ on unshared branch rows. Both extremes exist now, which is why branch-row unknown load leaks into devices. Expose λ_u in config plus a diagnostic of how much power went to ΔU vs devices per group.
9. **Covariance.** The child's noise currently enters the residualised parent row without being added to that row's σ. Fixing this changes weights and may shift existing numeric tests (`test_disaggregation.py:611` noise tolerance). Re-baseline deliberately.
10. **Combinatorics.** "Evaluate all candidate combinations" (HB §7.7) must be done as one convex joint solve over the candidate set, with an explicit cap and a logged truncation. Do not enumerate subsets. The 15 s CT cadence and 200-entity limits (`group_fit.MAX_ENTITIES`) bound it.
11. **ADR-0034 vs HB test 8.** Reintroducing MEASURED must not resurrect the removed `dedicated_device` declaration. It should derive from a structural endpoint scope (topology/domain agent) and needs a new ADR.
12. **The ledger overclaims.** `residual_events` is marked "used" when it was only passed in (`f558bde`). Any new provenance must list only inputs that changed the estimate.
13. **Process lesson.** `progress.md:47` says the reverted attempt "emitted a fabricated per-device watt (250 W)". The stashes do not retain the failing test. Write the mandatory 250 W test (4a) **first**, including the mutation guards and the steady-state variant, before any solver change.

## 6. Open questions only the product owner can decide

1. **Model-licensed individual estimates.** If a device model was learned from isolated events, may a currently co-switched, non-identifiable member get an individual ESTIMATED value? HB §8.4 lists "independent event history", which suggests yes with provenance. Today it does so unconditionally (`engine.py:2142-2198`). If yes, what is the minimum count of isolated events or model quality?
2. **Direct measurement (reverses ADR-0034 in substance).** Should a reader whose scope is exactly one device (a smart plug) yield MEASURED and act as an anchor (HB test 8)?
3. **Null in HA.** May device power sensors publish `unknown`/unavailable (null) for non-identifiable members, and should a new HA sensor entity be created per device group? This breaks existing entity histories and the energy dashboard.
4. **Default and exposure of λ_u.** What is the default unknown-load penalty, and is it user-tunable (per reader?) or only learned?
5. **Provisional publication.** Should provisional estimates be published to HA, and how long is the confirmation window? HB §12.3 allows fast provisional values but forbids presenting them as final.
6. **Confirmation thresholds.** How many consistent events promote PROVISIONAL→CONFIRMED (HB example: 5)? What expires a match?
7. **Declared priors.** Should proportional sharing of user-declared wattages be kept as an explicit ALLOCATED value (with rule name) or removed in favour of null members plus a group total (ADR-0030)?
8. **Model floors.** May a declared or learned on-power ever bound a group or member? HB says only mathematically justified bounds; current code uses on-power as a floor.
9. **Absolute vs delta precedence.** When the steady-state absolute solve and the event-delta solve disagree on a device's current power, which wins for the published snapshot, and is the disagreement surfaced as CONFLICTED?
10. **Confidence semantics.** Keep the uncalibrated engineering score plus evidence tiers (ADR-0007/0013), or move to a posterior-derived, calibratable confidence? Are tiers retired in favour of EstimateStatus × Identifiability?

## Interfaces needed from other areas

- **Pipeline:** `EventGroup{group_id, window[t0,t1], readers: {reader_id: ΔY, σ, samples[], completeness}, ha_events[], late_update callback, closed flag}`. Also unanchored measurement-only groups.
- **Topology/residual:**
  - `H_k(readers, candidates)` with per-entry `edge_lifecycle/confidence`, reader nesting/scope (including "single-device scope"), topology_version;
  - per-reader residual model giving σ_u and the λ_u prior;
  - an `UnknownLoadProfile` create/update API.
- **Models/learning:** per device and transition `ΔP` prior (mean, σ, sign class), kernel h_i(τ), standby; provenance (count of isolated-event samples, model_version).
- **Mapping:** reads `DeviceEstimate`, `DeviceGroupEstimate`, `MatchState` and identifiability. Provides the current association cells as candidate source 4.
- **Domain/storage:** ids for physical devices and load components; append-only tables for `EventInferenceRecord` versions; history query of past event groups for identifiability stacking.
