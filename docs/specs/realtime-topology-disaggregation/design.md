# Design — target behaviour and chosen algorithms

`handbook.md` says *what* the system must do. This file pins down *how* the 3.0 implementation does it: concrete algorithms, default parameters and decision rules, so that independent builders converge. Shapes are in `interfaces.md`, and product choices are in `decisions.md` (DEC-nn). Every default parameter is configurable through `config/settings.py` (owned by P6) unless stated otherwise.

The closing rule of the handbook governs every tie-break below: **prefer a correct explanation to a device-level number.**

---

## 1. Canonical model (HB §1, §2, rule 3)

- Four separate entity kinds: **Measurement Endpoint**, **Physical Device**, **Load Component**, **HA Entity**. Relationships are many-to-many through `ha_entity_links` (entity↔subject) and `mapping_edges` (subject↔endpoint). No table carries a single device FK on a reader or a single reader FK on a device.
- A CT is an endpoint, never a device (DEC-04). Unexplained load on a CT is a **Reader Residual** (§6) or an **Unknown Load Profile** (§7). It is never a minted `dev_<ct>` device.
- A **single-subject endpoint** (smart plug or appliance monitor with confirmed `SINGLE_SUBJECT` scope) is the only source of `MEASURED` (DEC-05). Its subject's mapping edge has `observability=DIRECT`.
- Endpoint configuration (phase, leg, polarity, multiplier, calibration, measurement type and window) is an **append-only revision** with a validity interval. A reading is always interpreted with the revision valid at its `observed_at`. This is what makes calibration changes non-retroactive (DEF-06).
- Version lifecycles are independent: topology (`topo-n`), mapping (`map-n`), model set (`mset-…`). Every derived artefact carries the triple (`VersionTriple`).

## 2. Measurement normalization (HB §2, §6.1) — P1

1. Unit canon (existing `ingestion/normalize.py`). Non-finite or unsupported units are flagged and **quarantined**: counted, never passed to the reader state.
2. Look up the endpoint revision valid at `observed_at`. Apply polarity, then the multiplier, **exactly once**, and stamp `calibration_version`. Downstream code never re-applies them (DEF-01).
3. Run the plausibility check: |P| ≤ `reader_max_w` (declared, or 2× the observed p99.9, or 24 kW default) → else `IMPLAUSIBLE_RANGE`, quarantined.
4. Copy the HA `context.{id,parent_id,user_id}` (top level of the state object) into `HAEvent`. Events of unlinked entities go to a bounded in-memory ring (10 000 events or 30 min) and are persisted only if grouped into evidence (DEC-17).
5. Values replayed by the reconnect `_prime` get `PRIMED`. They seed state and are never steps.
6. **Compatibility predicate** `compatible(a, b) -> (bool, reason)`: two readers may share an equation only if they have the same measurement type family (power vs energy), `active_power_w` present on both, and compatible phase (equal, either `both`, or a declared `PAIRED_LEG`). Current (A) never mixes with power unless `voltage_reference_v` is present on both.

## 3. Reader state, noise, change detection (HB §6.2–6.4) — P2, P3

**ReaderStateStore** (one per reader, O(1) amortized per sample):
- A ring keyed by `observed_at`, size covering max(10 min, 40 samples). Out-of-order samples are inserted in order and fire `on_late(reader, t)`.
- **Zero-order hold**: the value is valid from `observed_at` until superseded. `held_since` and `last_confirmed_at` are tracked separately. A reader is *held* until `stale_after_s` (existing setting, default 120 s), *stale* until `missing_after_s` (default 600 s), and *missing* after that. Missing ⇒ `None` everywhere (HB §18).
- **Noise** σ_r = max(1.4826·MAD of innovations over the last 6 h of non-event samples, resolution_w/√12, σ_min = 1 W). It is persisted every 10 min (restart without hidden state, HB §15). **Per-event pre-windows never estimate σ** (DEC-18).
- **Baseline** b_r is an EWMA with time constant 30 min, *frozen* while the detector is in SUSPECT or the post-event settling window. This prevents absorbing a persistent step (HB §6.3).

**Detector** (state machine per reader):
- z = (Y − b)/max(σ_r, σ_min). Enter SUSPECT at |z| ≥ 4 **and** |Y − b| ≥ max(3·resolution_w, 2 W). Exit to STABLE at |z| < 2 (hysteresis).
- Two-sided CUSUM (k = 0.5σ, h = 5σ) catches small sustained shifts. EWMA drift below the CUSUM threshold over > 1 h is `DRIFT`: it updates the baseline and is not an event.
- **Confirmation**: a step is CONFIRMED when the level holds for ≥ 2 consecutive samples, or for ≥ `confirm_s` = max(2×cadence, 20 s). It is TRANSIENT if the level returns to within 2σ of b before that. A periodic tag is added when ≥ 4 same-sign steps of similar amplitude (±20 %) recur with CV(period) < 0.3.
- Event identity is `hash(reader_id, onset_lo, detector_version)`. A physical step yields **exactly one** `MeasurementEvent` (fixes re-detection every cycle).
- Onset bounds: `onset_lo = last_pre_sample.observed_at − measurement_window_ms`, `onset_hi = first_post_sample.observed_at`.
- Quality score q ∈ [0,1] = min(1, |z|/8) × coverage(pre, post samples ≥ 2) × (1 − staleness_frac) × (1 if no `CLOCK_SKEW_FUTURE`/`OUT_OF_ORDER` else 0.7).

## 4. Event windows and groups (HB §6.5–6.6, §12.3) — P4, P5

- **Window**: on SUSPECT, open an evidence window with the pre-window from the ring (default 30 s, or ≥ 2 samples, whichever is longer) and a post-window (default 120 s per load class). The window is PROVISIONAL at the first post-sample and FINAL at close. Evidence is immutable once FINAL. A late sample creates a new evidence record that supersedes the old one (append, never update).
- **Grouping** (union-find over sub-events). Two sub-events join a group if any of these holds:
  1. They share a `context_id`, or one's `context_parent_id` equals the other's context id. **And** their onset intervals are within `context_max_spread_s` = 60 s (HB §6.6: context is not proof of simultaneity).
  2. Their onset intervals overlap after widening each by `tolerance = max(cadence_r, 5 s)`.
  3. Two reader steps have the same sign, |ΔY_parent − Σ ΔY_children| ≤ 3σ, and readers related by a CONFIRMED or PROVISIONAL CONTAINS edge (co-movement).
- Each sub-event keeps its own timestamps. Two transitions of one entity are two sub-events (fixes `_collect_transitions` collapse).
- **Lifecycle**: DETECTED (first sub-event) → MATCHING (handed to inference) → PROVISIONAL (first inference result) → CONFIRMED (all post-windows closed and no open sub-event within `quiet_s` = 60 s) → REVISED (late sub-event or late sample attaches after CONFIRMED; creates `revision+1`, prior revision kept) / UNRESOLVED (closed with no explanation above the unknown alternative).
- **Late data**: a sample or event older than an open group's window joins that group. If it is older than a closed group's window, it re-opens the group as a revision. It never edits unrelated groups (AT-18).

## 5. Topology (HB §3–4, §14) — T1–T5, T7

- **Edges are the authority** (DEC-11). The graph uses CONFIRMED edges as hard structure. PROVISIONAL edges are **soft alternatives**: listed in the operator, never applied to subtraction, and never used to reject a candidate. CANDIDATE and CORRELATED edges are never structural.
- **Operator H(T, M, t)** (T2):
  - A subject's most specific mapping edge gives its DIRECT/INFERRED reader. Ancestors through CONFIRMED CONTAINS edges get `EXPECTED` with coefficient 1. PAIRED_LEG uses the existing `leg_coefficient`. SHARED_LOAD: the subject appears in both readers' rows with coefficient 1, and the rows' σ are inflated for correlation (§8.3).
  - β is 1 unless a recorded factor (FactorRegistry at t) or an explicitly estimated β (labelled `beta_basis="estimated"`, |β−1| > 3σ_β over ≥ 20 events) applies.
  - Nested parents are residualised against present children. A missing child becomes an unknown column with the child's last held value as a soft prior (DEC-12).
- **Topology inference evidence ledger** (T5), per ordered reader pair (parent p, child c):
  - *support*: a child step ΔY_c with |ΔY_p − β·ΔY_c| ≤ 3·√(σ_p² + β²σ_c²) within the onset tolerance.
  - *conflict*: a child step with no matching parent step beyond 3σ_p (the parent did not move).
  - *neutral*: a parent-only step. This is recorded as residual evidence and **never counted against the edge** (HB §4.3, AT-2).
  - *independent causes*: distinct event groups whose explaining subject or unknown profile differs.
  - Absolute constraints carry over from `nesting.py` (containment ≥ 0.995, closure) as gates.
  - **Lifecycle (ADR-0043, dynamic)**: each decisive verdict decays both weights by `EVIDENCE_DECAY = 0.95` and adds 1 to the one that fired (`EVIDENCE_WINDOW = 20`); parent-only steps are neutral. From those weights, `r = cw/(sw+cw)`: STRUCTURAL at `sw ≥ 8` and `r < 0.05` with the existing gates (≥ 2 independent causes, absolute containment, no scaled copy, known basis, direction cap), with a hysteresis band at `sw ≥ 6` / `r < 0.10` for an already-STRUCTURAL edge; CONFLICTED when `sw+cw ≥ 6` and `r > 0.25`; PROVISIONAL at `sw ≥ 3` and `r < 0.10`; CANDIDATE otherwise. Every state can move up or down as recent evidence changes. A user declaration keeps STRUCTURAL as a prior unless recent evidence makes it CONFLICTED; a pin is the only freeze. One event never moves an edge past CANDIDATE.
- **Self-healing** (T7) inferred detectors, each producing a SUSPECTED `ConfigurationEvent`:
  - persistent sign inversion: ≥ 80 % of a consuming reader's last 20 steps negative-on-ON → `CT_DIRECTION_CHANGE`;
  - child/parent β-ratio step over ≥ 10 events → `CALIBRATION_CHANGE`;
  - a subject's matched events vanish on reader A and appear on reader B over ≥ 5 events → `SMART_PLUG_RELOCATION` / `CT_READER_RELOCATION`;
  - silence > `missing_after_s` × 6 → `READER_REMOVAL`.

  The workflow follows HB §14 steps 1–10 exactly: affected edges and mapping edges move to SUSPECTED_CHANGE, a candidate version is proposed, evidence is collected through T5, then the version is promoted and reconciliation is enqueued. **Power-model versions are never modified by this flow** (asserted).

## 6. Residual and unknown load (HB §3.3, §8.2, rule 5) — T3, T6

- Per-reader residual R_r(t) = Y_r − Σ_c β·Y_c − Σ_i H_ri·P̂_i, with signed values and no clamping. It is computed for every reader, mains and parents included, and persisted per reader and topology version (fixes DEF-05).
- **Three distinct objects**: noise ε_r (σ_r from P2), the residual R_r (the series), and the Unknown Load Profile (a recurring unexplained step signature, T6).
- **UnknownPrior** per scope: μ_U = median ΔR over the last 7 days of unexplained steps, σ_U = 1.4826·MAD, `step_rate_per_h`. λ_u = `WM_UNKNOWN_PENALTY_SCALE` × 1/(1 + step_rate_per_h), so a scope with frequent unexplained activity makes "unknown" cheap. Initially λ_u = 0.25 × scale (favours residual, DEC-23).
- Diagnostic: per reader per day, the share of |ΔY| assigned to ΔU vs subjects. It is published in `/residuals` and in diagnostics.
- **Unknown Load Profiles**: an unexplained FINAL step (the unknown-scope candidate wins, or the group is UNRESOLVED) is clustered per scope by (sign, |ΔY| ±20 %, duration). OBSERVING → CHARACTERISED (≥ 5 independent occurrences) → CANDIDATE (offered for user promotion to a Physical Device). It is never CONFIRMED automatically. A profile is located at the deepest reader that saw the step.

## 7. Matching and joint inference (HB §7–8) — I1–I7

**Candidates (I1)** come from the nine HB §7.1 sources. There is always **one `unknown_scope` candidate per affected reader scope** (HB §7.5). The candidate count is capped at 32 per group, ranked by evidence. Truncation is logged.

**Topology filtering:** a candidate is rejected only if no CONFIRMED-structure path lets it reach any affected reader *and* its mapping edge is CONFIRMED elsewhere. Provisional incompatibility only down-weights it (term E).

**Evidence (I2)** is a log-likelihood per term, each in nats and clipped to [−10, 2]:
- A — temporal: kernel-predicted window vs observed samples, a Gaussian log-likelihood on the window (not only the first delta);
- B — sign;
- C — amplitude vs `DeltaPrior`;
- D — reader-signature agreement (cosine of h_d and ΔY);
- E — topology consistency;
- F — model agreement;
- G — quality;
- H — context lineage.

**Dependence correction:** C, D and E derive from the same ΔY vector, so their sum is multiplied by 1/√3. Weights live in a versioned config file `inference/evidence_weights.json`. The score is a *prior penalty* for the joint solve, never an argmax.

**Joint solve (I3):**
- Objective = Σ_r w_r·Huber_δ=1.345(r_r/σ̃_r) + λ_m·Σ_i ((ΔP_i − μ_i)/σ_i)² + λ_u·Σ_s ((ΔU_s − μ_U)/σ_U)² + λ_t·Σ_i (1 − temporal_fit_i).
- It is solved by IRLS around `solve_constrained_nnls` with per-variable signed bounds. ON for a consumer gives ΔP ≥ 0, OFF gives ΔP ≤ 0, producer and bidirectional are unrestricted, and ΔU is unrestricted. At most 20 iterations, tolerance 1e-6.
- **Covariance:** nested readers observing the same subject are correlated. When Σ is estimable (≥ 30 shared events), use GLS with Σ. Otherwise σ̃_r = σ_r·√k_r, where k_r is the number of readers in the same CONTAINS chain that observe the same candidate (conservative, AT-9).
- Single-subject endpoints are anchors with σ = max(σ_r, 0.5 W) and weight ×10.
- **Absolute mode** (`infer_steady`) has the same objective on levels: absolute H, consumer P ≥ 0. It is constrained by established groups (sum constraint only) and established identities (DEC-22).

**Identifiability (I4)** uses an experiment matrix X: the rows are the current group's H rows plus, for each historical independent FINAL event group (last 90 days, max 500), the H rows weighted by which candidates transitioned.
- Compute rank, singular values, condition number, column correlation and per-subject null-space share. For each coupling group G (union-find on |corr| > 0.95 or a shared null-space component), check whether 1_Gᵀ is orthogonal to null(X) (group-functional identifiability).
- **States:** IDENTIFIABLE (null-space share < 0.05 and posterior σ < max(5 W, 10 % |x|)); PARTIALLY_IDENTIFIABLE (subject in a group where some linear combination including it is identified but the subject itself is not); GROUP_IDENTIFIABLE (the group sum is identified, members are not); NON_IDENTIFIABLE (a member of such a group); INSUFFICIENT_EVIDENCE (no usable rows, or fewer than 2 historical events touching the subject).
- **Justified bounds:** a small LP per member. min/max of x_i subject to the identified sums (± 3σ), sign classes and declared ceilings. **Model priors are never constraints** (DEC-19). Under non-negativity only, each member gets [0, G]. A zero-width interval is impossible unless the member is IDENTIFIABLE.
- `group_id` = hash(sorted member ids). It is stable across cycles while membership holds.

**Estimate assembly (I5)** — status table (HB §8.5):

| Condition | status | point_w | method |
|---|---|---|---|
| Subject has a confirmed single-subject endpoint with a fresh reading | MEASURED | the reading | DIRECT_READING |
| IDENTIFIABLE from the joint solve (delta or absolute) | ESTIMATED | the solution | JOINT_DELTA / JOINT_ABSOLUTE |
| Member of a GROUP_IDENTIFIABLE group | UNKNOWN (member) + group estimate published | None | NONE |
| Explicit user allocation rule (future) | ALLOCATED | the allocation | DECLARED |
| Otherwise | UNKNOWN | None | NONE |

- A model-only estimate (no supporting readers this cycle, device IDENTIFIABLE through history, steady state) is ESTIMATED with method MODEL_PRIOR **only if** the device is IDENTIFIABLE. Otherwise it is UNKNOWN (DEC-19).
- Reconciliation identity: main_w = Σ ESTIMATED/MEASURED (each subject once) + Σ group sums + Σ unknown + residual. Every term is `float | None`.

**Confidence (I5):** seven components (HB §9.6), each in [0,1] or None, never blended into one number for display. A legacy single `confidence` is not kept (DEC-24).

**Match lifecycle (I6)** per (candidate subject, event group):
- PROPOSED at candidacy.
- PROVISIONAL when its joint share is > 50 % of the group's explained |ΔY| on its readers.
- AMBIGUOUS when the subject is NON_IDENTIFIABLE.
- REJECTED when its share is < 10 % and the unknown alternative explains the delta.

Across groups, a (subject, reader) association becomes CONFIRMED after ≥ 5 PROVISIONAL independent groups with consistent amplitude (MAD/median < 0.25) and delay. It becomes CONFLICTED after ≥ 2 contradictions in the last 10, and EXPIRED after 30 days without matches. Interpretations are versioned `EventInferenceRecord`s. Evidence is never edited.

## 8. Mapping matrix (HB §9–10, §19) — M1–M3, U1

- **Columns:** a single primary display tree (L0 domain → L1 panel/source → L2 subpanel → L3 circuit → L4 reader), derived from CONFIRMED CONTAINS edges plus `panel_id`. Every endpoint is exactly one leaf. Unlocated endpoints go under `L1 "Unassigned measurements"`. Secondary relations (SHARED_LOAD, PAIRED_LEG, SAME_CONDUCTOR, CORRELATED, PROVISIONAL CONTAINS) become `annotations` on leaves, **never extra leaves**.
- **Rows** follow DEC-27. Groups appear as GROUP rows whose children reference members (no values on members). The Unknown Profile, Reader Residual and Summary (`derived=true`) row kinds are distinct.
- **Cells are sparse.** A cell exists if any of these is true: a mapping edge, an EXPECTED ancestor, a match history, or a CONFLICTED/RETIRED history. Otherwise the query layer renders NOT_OBSERVABLE without persisting it.
- **Update (M2):** on each `JointInferenceResult`, recompute only the cells (subject × reader) where the subject is in the result and the reader is in the operator rows that the subject enters, plus group cells. Each change appends a `matrix_cell_versions` row and bumps the mapping version **only if a mapping edge changed** (contribution updates do not bump it). Independent-event counting is deduplicated by `event_group_id` (HB §10.3: 100 samples from one event ≠ 100 events).
- **Visual category** (deterministic):

  | Condition | Category |
  |---|---|
  | MEASURED | DIRECTLY_MEASURED |
  | ESTIMATED with mapping CONFIRMED | CONFIRMED_INFERRED |
  | any PROVISIONAL | PROVISIONAL |
  | member of a group | GROUP_ONLY |
  | status UNKNOWN with observability ≠ NOT_OBSERVABLE | UNKNOWN |
  | NOT_OBSERVABLE | NOT_OBSERVABLE |
  | CONFLICTED | CONFLICTED |
  | RETIRED | RETIRED |

- **Aggregation:** reader totals are per column and never summed. The physical total counts each subject once, from row estimates and group sums, plus the main residual.

## 9. Power models and learning (HB §11, §13) — L1–L7

- **Basis:** a model is fitted on device-attributed **ΔP** from confirmed observations. An ABSOLUTE_ANCHOR level is used only when the endpoint is a confirmed single-subject endpoint for that subject. Legacy models are `LEGACY_ABSOLUTE` and read-only (DEC-31).
- **Classes:**

  | Class | Fit | Incremental update | Promotion criterion |
  |---|---|---|---|
  | FIXED | median/MAD per level, transition ΔP | P² quantile estimators | relative MAE ≤ 15 % and level CI width ≤ 20 % |
  | ADJUSTABLE | isotonic (PAVA) on binned x, piecewise linear | bin-wise PAVA | MAE over the covered x-range ≤ 15 % of span, coverage ≥ 50 % |
  | DYNAMIC | segmentation-initialised states per HA state, transition counts, durations | running state distributions | held-out log-likelihood ≥ active's − 0 and per-state support ≥ 5 |
  | RESPONSE_KERNEL | phase delays and amplitudes per transition | robust running phase stats | window-shape NRMSE ≤ 0.3 |
  | COMPONENT_SUM | sum of component models | via components | via components |

- **Promotion** compares candidate vs active on the **same frozen evidence-id set** (stored in the version). It needs ≥ 5 independent confirmed events, and a single event never changes an ACTIVE version. Versions and events are append-only. Rollback reactivates the last version whose event log shows `activated` (fixes DEF-07).
- **Learning gate (L4)**, in order:
  1. measurement quality;
  2. group CONFIRMED (not PROVISIONAL);
  3. subject IDENTIFIABLE (otherwise the observation goes to the *group* only);
  4. unknown-alternative share < 20 % of |ΔY|;
  5. relationship edges not SUSPECTED_CHANGE;
  6. independent event (no other unexplained sub-event within the window on the same readers);
  7. consistency with the active model within 4σ, or flagged as a possible behaviour change.

  Rejections are kept with a reason.
- **Worker (L6):** one background thread with a bounded queue keyed by subject (dirty set). It is restart-safe: the dirty set is re-derived from unconsumed confirmed observations. The registry swaps atomically by `model_set_id`.
- **Reconciliation (L7):** replay any time range of immutable evidence with an explicit (model set, topology version, mapping version) into a versioned derived namespace (`derived_run_id`). Raw samples and evidence are never written. Triggered on promotion of a topology, mapping or model version, and on late evidence.

## 10. Runtime (HB §5) — P6

- **Fast loop** (asyncio, inline, no DB reads): normalize → reader state → detector → window/group bookkeeping. Budget is < 0.2 ms per sample.
- **Event loop:** an asyncio task awaiting a bounded queue of groups that are ready or revised. It runs `infer_group` in a dedicated single-thread executor on an immutable input bundle, then publishes: persist record → `MatrixUpdater.apply` → snapshot swap (copy-on-write) → enqueue learning.
- **Steady loop:** per-reader level changes beyond the noise band trigger `infer_steady` for the affected operator component. A 60 s heartbeat publishes staleness only (DEC-14).
- **Background loop:** learning worker (L6), reconciliation (L7), topology inference ledger flush (T5), residual priors (T3), unknown profiles (T6), matrix reconciler (M2). It runs in its own executor, CPU-chunked ≤ 2 s per job (DEC-37).
- A clock is injected everywhere, so replay of simulator time drives the same code (AT-19, V1).
- **Gate:** `/healthz` p99 < 100 ms while a synthetic 10 s background job and a 30 events/s HA burst run.

## 11. Home Assistant surface (HB §8.4, §18) — H1, H2

- Device power sensor state = `point_w` iff the status is MEASURED, or ESTIMATED with IDENTIFIABLE. Otherwise it is `None` (unknown).
- Attributes: status, identifiability, lower_w, upper_w, group_id, provisional, versions.
- New enum sensors `estimate_status` and `identifiability` live on the provenance device. Group power sensors live on a synthetic group device per stable `group_id`.
- Energy (`total_increasing`) integrates only non-null power. A None never resets it.
- Setup flow (H2) has three steps: connection → endpoints (CTs with an optional **user-declared** parent; plugs with an optional "measures exactly one appliance → which device" scope) → done. Engine-inferred parents are shown as suggestions and are written only if the user accepts them, as user edges (DEC-13).
