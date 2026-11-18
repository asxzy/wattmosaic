# Gap analysis C: Measurement Topology and Residual / Unknown Load

Scope: HB §3.1, §3.3, §4 (4.1–4.4), §14, rule 5 / §9.4 D–E, §8.2 (unknown-load penalty), acceptance tests 1, 2, 14, 15, 16.
Repo: `<repo>` (branch `spec/realtime-topology-disaggregation`). Paths below are relative to `engine/wattmosaic_engine/` unless they start with `tests/`, `tools/` or `docs/`.

**Headline:** the repo already has most of the *vocabulary* for a typed, versioned, lifecycle-bearing topology (`EdgeLifecycle`, `TopologyEdge`, `TopologyVersion`, `ConfigurationChangeEvent`, `UnknownLoadProfile`, `detect_configuration_changes`, `make_version`, diffing). Almost none of it is on the live path:
- The runtime topology is still a **pointer tree on `CTChannel`**: `parent_ct_id`, `paired_ct_id` and `alias_of_ct_id`, plus `DeviceTopology.branch_ct_ids`. Whatever is declared there is used unconditionally.
- `load_topology` drops relations entirely.
- Configuration-change detection is never fed an observed topology.
- No code produces an unknown-load profile or an unknown-residual event.
- Residual history has no scope key, so every scope's baseline is computed from one install-wide window.

---

## 1. Current-state map

| HB concept | Code location | What it does today | Verdict |
|---|---|---|---|
| §3.1 Observation operator H, non-binary | `electrical/matrix.py:271-407` `build_measurement_matrix`; coefficients from `electrical/topology.py:519-566` `phase_coefficient`, `:497-517` `leg_coefficient`, `matrix.py:462-489` `_attribution_coefficient` | Builds A (rows = enabled canonical CTs, cols = devices). Coefficients are 1, 0, 0.5 (complete leg pair) or `phase_split_l1` (default 0.5, partly-observed double-pole). Each device is attributed to exactly one deepest branch group (`topology.py:308-343`). Main rows contain every descendant device (`matrix.py:353-359`). | PARTIAL. Non-binary only through the phase/leg split. There is no "partial", "shared-load", "uncertain observation" or "calibrated β" class. `phase_split_l1` is a declared global constant, not a physics-justified per-device coefficient. |
| §3.1 Calibrated coefficient only when physics justifies it | `electrical/factors.py` (time-scoped `FactorRegistry`, `applied_by` monitor/user/unknown); `topology.py:608-653` `multiplier_for` / `scale_w` | The registry is correct (interval-scoped, never applied twice). But the live solve builds its graph **without** the registry (`disaggregation/engine.py:739`: `build_graph(topology, phase_split_l1=...)`), so `matrix.py:401` uses the current scalar `measurement_multiplier` for every timestamp. | PARTIAL / CONFLICTS. A calibration change becomes retroactive on replay. Only audit/association/circuits pass a registry. |
| §3.1 U_r unexplained load per reader | `electrical/solver.py:258-297` `augment_with_unknown_slack`; used at `disaggregation/engine.py:1278-1291`, bounds at `:1403-1410` | Adds one non-negative slack column per **main** row and per declared-shared branch row. Upper bound = row value + 1. Ridge prior is 0 with the generic `model_ridge`. Each row's slack is independent (an unknown load inside a sub-panel gets separate, unlinked slacks in the inner-main and outer-main rows). | PARTIAL. U_r is not topology-propagated (it is not in H). It has no penalty from learned residual behaviour (§8.2). It cannot be negative (HB §8.2: signed deltas are legitimate). Branch rows that are not declared shared have no U at all. |
| §3.3 Parent = Σβ·children + R_p | `matrix.py:213-265` `prepare` | For a branch row with enabled nested children it builds `y_parent − Σ y_child`, with β fixed at 1 and no factor at timestamp. If any child reading is missing, the parent row is **excluded** (`:245-248`). Main rows do not subtract children; they carry descendants as columns. | PARTIAL. The β rule from 05 §2.3 is not implemented (no recorded-multiplier β, no estimated β). Dropping the whole parent row when one child is missing wastes the parent's evidence: HB wants R_p kept and the missing child treated as uncertain. |
| §3.3 Parent not required to be fully explained | `electrical/nesting.py:788-800` closure gate; `electrical/hierarchy.py:227-298` `_scope_geometry` | nesting only rejects a child when the parent is **over**-explained (closure < 0.95). An under-explained parent is fine. hierarchy uses violation fraction (child > parent + 30 W). | MATCHES in spirit for absolute containment. But neither uses delta evidence, so §3.3's event form is ABSENT. |
| §4.1 Topology inferred before attribution, used as a prior | `disaggregation/engine.py:739` builds H from whatever `parent_ct_id` / `branch_ct_ids` are stored | The declared tree is used as hard truth regardless of `TopologyStatus` / edge lifecycle. Inferred edges (`topology_relations` table) are **never read** by `build_graph`. | CONFLICTS. Topology is a declaration, not an inferred, versioned prior. |
| §4.2 Typed directed graph CONTAINS / SHARED_LOAD / PAIRED_LEG / CORRELATED | `contracts.py:418-438` `RelationKind` = {DEVICE_CT_ASSOCIATION, CT_MEASUREMENT_OVERLAP, CIRCUIT_MEMBERSHIP, ELECTRICAL_PARENT_CHILD}. Pointer fields `CTChannel.parent_ct_id` / `paired_ct_id` / `alias_of_ct_id` (`contracts.py:1538-1580`). | CONTAINS ≈ ELECTRICAL_PARENT_CHILD, but the live graph uses the pointer, not the edge. PAIRED_LEG exists only as the symmetric pointer `paired_ct_id` (`topology.py:196-220`). There is no SHARED_LOAD kind (smart plugs are not readers at all, see trap T3). There is no CORRELATED kind: `CT_MEASUREMENT_OVERLAP` is emitted for *any* two CTs sharing a device (`hierarchy.py:542-589`), which conflates SHARED_LOAD, CORRELATED and "child is inside parent". Alias / duplicate (same conductor) has no HB type; it is a pointer. | CONFLICTS (wrong vocabulary, two parallel representations). |
| §4.2 "Only validated containment participates in hierarchy calculations" | `matrix.py:373-385`, `topology.py:441-449` `nested_channel_ids`, `topology.py:124-139` | Any declared `parent_ct_id` is used. `CTChannel.topology_status` is ignored. The only thing the aggregate `Topology.status == CONFIRMED` gates is learning (`storage/database.py:380-385`) and the `topology_confirmed` flag (`engine.py:2626-2629`). | CONFLICTS. |
| §4.3 Asymmetric child→parent evidence, signed delta, repeated events, single event never confirms | `electrical/nesting.py:577-878` `infer_nesting` (live; called by `main.py:1879-2031`); `electrical/hierarchy.py:510-705` `HierarchyFinder.infer` (audit pass, `disaggregation/audit.py:1735-1795`) | Both use **absolute** aligned series only (containment fraction, closure, violation, operating conditions). Neither uses signed ΔY at events. nesting never proposes a parent for a branch CT (`nesting.py:142`, `:726-736`: `load_level_not_measurable`). hierarchy requires ≥2 independent devices (a good guard), takes `residual_history` but discards it (`hierarchy.py:611` `del circuits, residual_history`). Neither keeps per-edge evidence counters across runs; each call recomputes from a window. | PARTIAL (absolute constraint, phase-ignorant). Delta / asymmetric / repeated-event evidence is ABSENT. The progress.md claim that "residual is corroborating" (TASK-TOPOLOGY-002) is **stale**: the residual argument is deleted, and G-TOPO-09 from 04 §6.2 is still open. |
| §4.3 Phase / voltage compatibility | nesting builds split-phase "measurement nodes" by `_l1` / `_l2` suffix (`nesting.py:392-456`) and deliberately ignores declared phase (`main.py:1967-1974`) | Legs are summed into nodes; phase labels are distrusted. | PARTIAL. There is no compatibility layer; it relies on a naming regex (`nesting.py:138`). |
| §4.4 Topology as a versioned prior, H(t)=H(T(t),M(t)) | `storage/migrations.py:308-314` `topology_versions` (autoinc snapshot); `storage/database.py:311-361` `save_topology` (DELETE all `ct_channels` / `devices`, insert, append snapshot); `:363-433` `load_topology` | A snapshot history exists and is append-only. But the active topology is rebuilt from the flat tables, and **`relations` and `circuits` are not restored** (`load_topology` constructs `Topology(channels, devices)` only). `inference_records.topology_version` records the integer. The device→reader mapping M is stored inside T (`DeviceTopology.branch_ct_ids`). `disaggregation/configuration.py:769-779` `project_configuration` says "a configuration change is a new topology version". | PARTIAL / CONFLICTS. T and M are not independent (violates rule 20). Edge-level versioning (`topology.py:720-1195` `make_version`, `diff_*`, `TopologyVersionRecord`) is computed but never persisted. `contracts.py:2245` `TopologyEdge` (with `valid_from` / `valid_until` and per-edge versioning) has no table and no producer. |
| §4.4 Old evidence associated with historical topology version | `storage/database.py:833-860` `inference_records_for_topology_version` | Read path exists by integer version. | PARTIAL. There is no edge-level version, and auto placements are replaced wholesale (`disaggregation/auto_placement.py:274-296`), so the mapping history is lost. |
| Edge lifecycle incl. SUSPECTED_CHANGE | `contracts.py:634-664` `EdgeLifecycle` (8 states); `TopologyRelation.lifecycle` | The vocabulary exists. **The `topology_relations` table has no `lifecycle` column** (`migrations.py:647-662`) and `insert_topology_relation` does not write it (`database.py:924-955`), so lifecycle is lost on persist. Declared parents are synthesised as CONFIRMED on every audit pass (`audit.py:1798-1817`). | PARTIAL (vocabulary only). |
| §14 Configuration Event | `contracts.py:748-812` `ConfigurationChangeKind` (9), `ChangeDetection`, `ChangeStatus`; `contracts.py:2713` `ConfigurationChangeEvent`; `electrical/topology.py:1226-1466` `detect_configuration_changes` | A pure diff of current vs observed topology emits SUSPECTED events. In production `AuditPass._detect_changes` (`audit.py:990-1048`) returns immediately because `observed_topology` is **never passed** (`integration.py:817-823`). Changes go only to `system_events`; there is no table. `main.py:1807-1822` `set_topology` overwrites with no event, no diff and no candidate. `main.py:1824-1840` `confirm_topology` blanket-confirms the aggregate plus every device. | ABSENT on the live path. The function is reusable. |
| §14 steps 2–10 (affected relations → SUSPECTED_CHANGE → reduce confidence → candidate version → collect → validate → promote → reconcile → preserve) | none | Nothing marks edges or mappings SUSPECTED_CHANGE. There are no candidate topology versions, no promotion, and no reconcile driver. | ABSENT |
| §14 Measurement-inferred changes (CT reversed, calibration drift, plug moved) | none (detection only diffs *declared* snapshots) | No detector watches for a persistent sign flip, a β-ratio step, or an association vanishing on one reader and appearing on another. | ABSENT |
| Rule 5: residual vs unknown-load profile vs noise | Residual: `models/residual_power.py` (`ResidualScope` :195, `ResidualAnalyzer` :380, baseline / dynamic split, optional `noise_w`). Profile: `contracts.py:2494` `UnknownLoadProfile`, `:2917` `UnknownResidualEvent`. Mapping: `disaggregation/mapping.py:576-627` `_unknown_cells`. | The residual is signed and never clamped (good). Noise is optional metadata, not a modelled ε (no per-reader noise model). **No code constructs `UnknownLoadProfile` or `UnknownResidualEvent`** (grep: definitions only), and TASK-RESIDUAL-005 is BLOCKED. `_unknown_cells` mints `__unknown__:<reader>` rows from residual events and only *optionally* tags a profile, which merges §9.4 D and E into one row type. | PARTIAL (residual) / ABSENT (profiles) / CONFLICTS (D/E merged). |
| Per-reader residual history | `migrations.py:716-731` `residual_power`; `audit.py:1874-1965` `_residuals` | The table has **no `scope_id` / `reader_id` / `topology_version` / `noise_w` column**. The audit admits it: one install-wide window is fed into every scope (`audit.py:1896-1901`). So "baseline_w" for circuit A is the median of all scopes' residuals. Residuals are computed only for single-CT circuit scopes (`:1903-1906`). Main / parent R_p is not computed here. | CONFLICTS (correctness bug). |
| §8.2 Learned unknown-load penalty, measurable and configurable | `engine.py:1403-1424` | The slack gets prior 0 and the shared generic `model_ridge` (0 unless some device has a prior). Nothing is learned from residual statistics. There is no metric for "too much / too little in residual". | ABSENT |
| Device→reader observability (M) | `disaggregation/auto_placement.py:159-271` (correlation → `monitored_by_ct_ids` on one branch row); `configuration.py:769+` projects it into `DeviceTopology.branch_ct_ids` | One row per device. Main rows are refused. User declarations win. Each auto run replaces the previous auto placements. | PARTIAL. There is no mapping version, no SUSPECTED state for a mapping, and no multi-reader mapping (plug + circuit). |
| Topology proposals for HA (minimal-ha draft) | `main.py:1879-2031` `nesting_candidates`; draft `docs/specs/minimal-ha-setup/design.md §4.3`, `decisions.md D3` | The engine returns read-only proposals. The draft prefills the unambiguous ones and then **silently calls `POST /topology/confirm`**. | CONFLICTS with HB §4.3 / §4.4 (see §2 C6). |
| Acceptance 1 / 14 (one load, nested readers, no double count) | `matrix.py` (most-specific attribution + residualised branch rows + main containment); `tests/unit/test_electrical.py:379` `test_nested_branch_cts_do_not_double_count`, `:1450`, `:1664` `test_nested_children_contribute_once` | Holds for **declared** trees. | PARTIAL. It does not hold for an unknown load under nested mains (independent slacks), and it is not tested for inferred / provisional edges. |
| Acceptance 2 (parent residual does not invalidate child containment) | `nesting.py` closure; `hierarchy.py` violation fraction | Absolute-only. There is no event-level test asserting that an unexplained ΔY_p leaves the edge's confidence unchanged. | PARTIAL / untested. |
| Acceptance 15 (plug relocation changes mapping, not power model) | — | Plugs are not readers (`ingestion/discovery.py:15-28`). A mapping change bumps the topology version. Auto placement history is replaced. | ABSENT |
| Acceptance 16 (reader relocation / calibration triggers reconciliation) | `detect_configuration_changes` (pure, unwired); `inference_records_for_topology_version` | No trigger, no reconcile. The live solve ignores the factor registry. | ABSENT |

---

## 2. Conflicts that need a breaking change

| # | Legacy concept | HB rule violated | Proposed replacement | Blast radius |
|---|---|---|---|---|
| C1 | Topology as **pointer fields** on `CTChannel` (`parent_ct_id`, `paired_ct_id`, `alias_of_ct_id`) consumed unconditionally by `TopologyGraph._index` (`topology.py:101-173`) and `matrix.py` | §4.2 (typed graph, only validated CONTAINS used), §4.4 (inferred prior), rule 2 | The authoritative store becomes a **typed edge set** (`TopologyEdge`-like rows: kind ∈ {CONTAINS, SHARED_LOAD, PAIRED_LEG, CORRELATED, SAME_CONDUCTOR}, lifecycle, `valid_from` / `valid_to`, evidence counters, `declared_by`). `TopologyGraph` is built from edges filtered by lifecycle: CONFIRMED is a hard constraint; PROVISIONAL is a soft alternative passed to matching. The pointer fields become **derived views** for UI and HA compatibility, or are removed. A user declaration becomes an edge with `declared_by=user` and lifecycle CONFIRMED (or PROVISIONAL, see Q1). | `contracts.py` (CTChannel, RelationKind, Topology), `electrical/topology.py`, `matrix.py`, `main_power.py` (`effective_feeds`), `panel_tree.py`, `nesting.py`, `hierarchy.py`, `audit.py`, `mapping.py`, `storage/database.py` save/load, migrations, HA `custom_components/wattmosaic/flow_logic.py` / `config_flow.py` (writes `parent_ct_id`), panel JS, ~20 test files (`tests/unit/test_electrical.py` alone has 92 tests built on pointer fixtures), generated schemas, and the simulator (`tools/simulator/behavior.py`). |
| C2 | `RelationKind` 4-member vocabulary and `CT_MEASUREMENT_OVERLAP` meaning "shares a device" | §4.2 (SHARED_LOAD / PAIRED_LEG / CORRELATED distinct, "correlation edge must not be treated as containment") | Replace with the HB vocabulary. Keep DEVICE_CT_ASSOCIATION and CIRCUIT_MEMBERSHIP as **mapping** relations, separate from reader↔reader topology. Retire OVERLAP by splitting it into CORRELATED (repeatable co-response, no containment), SHARED_LOAD (two endpoints on one physical load) and SAME_CONDUCTOR (alias). | `contracts.py:418-438`, `hierarchy.py`, `association.py:1203,1269`, `audit.py`, `mapping.py`, `main.py:2056-2089` `relations()`, stored `topology_relations.kind` values (needs migration), schemas, `tests/unit/test_hierarchy.py` (37 tests). |
| C3 | Device→reader mapping stored inside the topology (`DeviceTopology.branch_ct_ids`; `project_configuration` bumps the topology version) | Rule 20 (independent lifecycles), §4.4 (H=H(T,M)), §14 ("mapping and power model versions independent"), acceptance 15 | Separate the **Mapping store** (device / load-component ↔ reader observability rows with their own version and lifecycle, incl. SUSPECTED_CHANGE). Topology holds only reader↔reader edges. H is composed at solve time from (T version, M version). | `contracts.py:1621` DeviceTopology, `configuration.py` (`project_configuration`, `cts_claimed_by_dedicated`, `materialisation_blockers`), `auto_placement.py`, `topology.py` attribution methods, `matrix.py`, `engine.py` (consumer), `mapping.py`, storage `devices` table, HA flow. Overlaps with the mapping-matrix agent and the domain agent. |
| C4 | Aggregate `TopologyStatus` + blanket `confirm_topology` (`main.py:1824-1840`), plus `set_topology` as destructive replace (`database.py:314-316` DELETE) | §4.3 (single event never confirms an edge), §4.4 / §14 (versioned, preserve prior), §16.4 | Per-edge confirmation API. `PUT /topology` becomes "record a declared ConfigurationEvent + create a candidate version". Promotion is an explicit act. Learning gates on "the edges that matter for this device are CONFIRMED", not on a global flag. | `main.py`, `api/server.py:1101-1102` routes, `database.py`, `learning/` gate (`storage/database.py:380-385` comment; the `AMBIGUOUS_TOPOLOGY` reject reason), HA `topology_confirm.py` / `button.py`, the minimal-ha draft. |
| C5 | `residual_power` table with no scope key and an install-wide baseline window (`audit.py:1896-1901`) | Rule 5, §9.4 E (reader-specific residual), 05 §6 | New per-reader residual series keyed by `(reader_id, topology_version, t)` with `baseline_w`, `dynamic_w`, `noise_sigma_w`, `basis`. Mark legacy rows `scope_unknown` (§16.6). Do not reinterpret them. | `migrations.py`, `database.py:1179-1230`, `models/residual_power.py` `persist`, `audit.py` `_residuals` / `_residual_window`, `main.py:2105` `residual_components`, panel residual view, `tests/unit/test_residual_power.py` (82 tests, mostly unaffected since they are pure). |
| C6 | minimal-ha draft: inferred parent written as `parent_ct_id`, then silent `POST /topology/confirm` | §4.3, §4.4, `TopologyRelation` "never auto-confirm" | HA writes user-chosen parents as `declared_by=user` edges. Inferred proposals stay `declared_by=inference` CANDIDATE / PROVISIONAL edges owned by the engine. There is no HA-side confirmation of inferred edges. | `docs/specs/minimal-ha-setup/*` (untracked draft), `custom_components/wattmosaic/flow_logic.py`, `config_flow.py`. Product decision needed (Q2). |
| C7 | Unknown slack: non-negative, per-main-row, no topology placement, generic ridge | §3.1 (U_r within scope), §8.2 (learned penalty, signed deltas), §7.5 | Model U as **scope-located unknown columns**: one per reader scope, entering every ancestor row through H, with a per-scope prior N(μ_U, σ_U) from the residual engine. Allow signed ΔU in delta inference. | `solver.py:258-297`, `engine.py:1278-1440` (owned by the joint-inference agent; this area supplies the parameters), identifiability analysis (slack columns are nuisance columns, `solver.py:149`). |
| C8 | ADR-0034 "direct measurement removed" and smart plugs excluded from discovery | §1.1 D (a smart plug is a Measurement Endpoint), §8.3 / acceptance 8 (plug anchors), acceptance 15 | Smart-plug power entities become Measurement Endpoints (readers) with a SHARED_LOAD / CONTAINS relation to the circuit CT. This reverses part of ADR-0034 and needs a new ADR. | `ingestion/discovery.py`, contracts (endpoint type), topology, matrix, engine. Shared with the domain and matching agents. |

---

## 3. Reusable assets to keep

- `electrical/topology.py:720-1195`: `EdgeView`, `TopologyDiff`, `diff_topologies`, `diff_edge_sets`, `make_version`, `digest_edge_set`, `version_token` / `version_number`. Deterministic and tested (`tests/unit/test_topology_persistence.py:438-607`). This is the right core for versioning.
- `electrical/topology.py:1226-1466` `detect_configuration_changes`: a pure, typed, SUSPECTED-only change detector covering all 9 kinds (tests `test_topology_persistence.py:827-914`). Rewire it; do not rewrite it.
- `contracts.py`: `EdgeLifecycle` (incl. SUSPECTED_CHANGE), `TopologyEdge` (`valid_from` / `valid_until`, version, validator that retired edges carry an end), `TopologyVersion` (append-only rollback), `ConfigurationChangeEvent` / `ChangeStatus` / `ChangeDetection`, `UnknownLoadProfile` (lifecycle with no CONFIRMED state), `UnknownResidualEvent`, `ResidualDecompositionRecord`.
- `electrical/factors.py`: interval-scoped calibration with `applied_by` semantics. This is the correct source for physics-justified β and for calibration-change detection.
- `matrix.py` invariants: most-specific attribution, coefficient-based `_row_excludes_subtree` (`:430-453`), alias-as-fallback-not-row (`:296-317`), missing → excluded with reason (never zero). Keep them as the CONFIRMED-edge path.
- `topology.py:370-393` `double_pole_leg` (a partner must belong to the same device) and `:349-368` `complete_leg_pair` / `leg_coefficient`: the correct PAIRED_LEG semantics; port them to edges.
- `nesting.py` measurement-node grouping (legs summed into nodes, `:392-456`), duplicate / alias detector (`:309-351`, `:643-716`), closure-based level ranking, and ambiguity reporting. Its thresholds were validated on real data (docstring `:13-42`).
- `hierarchy.py` guards: ≥2 independent devices (`:131-140`), scaled-copy refusal (`:779`), basis-unknown refusal, `validate()` rejecting cycles / self / contradiction with reasons (`:930-1050`), `declared_wins`.
- `models/residual_power.py`: signed residual with no clamp (`:320-327`), the basis-compatibility matrix (`:166-192`), typed refusals, the alias / enclosure once-only check (`_counted_contributions`), baseline = median.
- `storage/database.py:833` `inference_records_for_topology_version`: the reconciliation read path.
- Tools: `tools/dev/replay_nesting.py --score` (real-data ground-truth scorer against declared parents) and `tools/dev/negverify_nesting.py` (guard mutation harness). Keep both as the acceptance harness for the new inference.

---

## 4. Proposed work packages

Shared hot files (several WPs or agents touch them; they need serialisation or one owner): `contracts.py`, `storage/migrations.py`, `storage/database.py`, `main.py`, `disaggregation/audit.py`, `disaggregation/engine.py`, `electrical/topology.py`.

### WP-C1: Typed measurement-topology domain (edges as authority)
- **Objective:** introduce the HB relation vocabulary (CONTAINS, SHARED_LOAD, PAIRED_LEG, CORRELATED, plus SAME_CONDUCTOR for aliases) as reader↔reader `TopologyEdge`s with lifecycle and validity interval. Make `TopologyGraph` build from edges filtered by lifecycle (CONFIRMED = hard, PROVISIONAL = soft list exposed to consumers, CANDIDATE / CORRELATED = never in hierarchy). The pointer fields become derived read-only views (or are removed, see Q3).
- **Files owned:** `electrical/topology.py` (graph part, lines 1-718), new `electrical/topology_edges.py`. Requests to the domain agent for `contracts.py` (RelationKind replacement, CTChannel pointer deprecation, Topology.edges).
- **Depends on:** domain/contracts agent (canonical Measurement Endpoint id, version contracts).
- **Tests:** a CORRELATED edge never yields `nested_children`; a PROVISIONAL CONTAINS edge is not subtracted by default but is listed as an alternative; cycle / self / contradiction rejection carried over; PAIRED_LEG ownership rule (port `test_electrical.py:1312`); pointer-view round-trip.
- **Size:** L

### WP-C2: Topology / config-event store and version lifecycle
- **Objective:** persist edges (with `lifecycle`, `valid_from` / `valid_to`, evidence counters), topology versions (`make_version` output incl. diff), and ConfigurationChangeEvents in dedicated tables. `load_topology` restores edges. `PUT /topology` records a DECLARED ConfigurationEvent and creates a candidate version instead of DELETE + replace. Add per-edge confirm / reject endpoints and a promote-version endpoint. Remove the blanket `confirm_topology` semantics. Legacy pointer data migrates to `declared_by=user` edges; legacy snapshot rows stay readable (§16).
- **Files owned:** `storage/migrations.py` (new migration only), the topology section of `storage/database.py` (`save_topology` / `load_topology` / `insert_topology_relation` / new edge and event methods), the topology facade in `main.py:1800-1840`, `api/server.py` topology routes.
- **Depends on:** WP-C1, storage agent (migration numbering, retention).
- **Tests:** lifecycle survives a restart (the current bug); a previous version payload is byte-identical after advance (keep `test_previous_payload_is_byte_identical_after_advance`); PUT creates a candidate, not a confirmed topology; rollback is append-only; legacy DB migration (`tests/unit/test_migration_matrix.py`).
- **Size:** M–L

### WP-C3: Observation operator H(t) = H(T(t), M(t)) with physics-justified β
- **Objective:** a single `build_observation_operator(topology_version, mapping_version, at)` that:
  - uses only CONFIRMED CONTAINS edges for nested subtraction;
  - resolves β per edge as 1 by construction, the recorded `FactorRegistry` multiplier at time t, or an explicitly labelled estimated β;
  - expresses PAIRED_LEG and SHARED_LOAD coefficients;
  - emits per-cell observability class (full / partial / paired / shared / uncertain) for the matrix agent;
  - always passes the factor registry (fixes `engine.py:739`);
  - keeps the parent row when a child is missing, adding the child's uncertainty instead of excluding the row (Q4).
- **Files owned:** `electrical/matrix.py`, `electrical/main_power.py` (feeds resolved from edges), `electrical/__init__.py` facade.
- **Depends on:** WP-C1. The mapping store (C3 conflict) comes from the mapping-matrix agent. Consumer: joint-inference agent (`engine.py`). Agree the interface `H, row_meta, scope_columns, soft_alternatives`.
- **Tests:** acceptance 1 / 14 on declared **and** promoted-inferred trees; a calibration change mid-window applies the old factor to old samples (no retroactivity); an estimated β is labelled and never distributes residual; replay of the same `(T, M)` version is bit-identical.
- **Size:** L

### WP-C4: Topology inference engine (asymmetric, delta + absolute, evidence-accumulating)
- **Objective:** merge `nesting.py` and `hierarchy.py` into one engine.
  - **Inputs:** signed per-reader event deltas ΔY_r from the event pipeline, plus aligned absolute series.
  - **Per candidate pair, a persisted evidence ledger:**
    - child→parent signed-delta agreement count (ΔY_p ≈ βΔY_c within σ_p);
    - parent-only changes recorded as *residual* and **not** counted against the edge (HB §4.3, acceptance 2);
    - conflicting evidence (ΔY_c with no ΔY_p beyond noise) counted against the edge;
    - absolute containment (`nesting.containment`) and closure kept as constraints;
    - phase compatibility; reader quality.
  - **Lifecycle:** CANDIDATE → PROVISIONAL → CONFIRMED by thresholds on independent events (≥N events, ≥2 independent devices or unknown events, sign-consistent). One event never promotes past CANDIDATE.
  - **Edge types:** emits CORRELATED for co-response without containment and SAME_CONDUCTOR for duplicates.
  - **Scope:** removes the "branch CT parent never inferable" restriction once delta evidence exists. That is what makes the minimal-ha "missing parents inferred" feasible for branch CTs.
- **Files owned:** `electrical/nesting.py`, `electrical/hierarchy.py` (merge into new `electrical/topology_inference.py`), `tools/dev/replay_nesting.py`, `tools/dev/negverify_nesting.py`, `tests/unit/test_nesting.py`, `test_hierarchy.py`, `test_nesting_perf.py`. The hierarchy stage in `audit.py:1735-1817` is a shared hot file.
- **Depends on:** real-time pipeline agent (event windows with signed ΔY per reader, noise σ_r per reader), WP-C1, WP-C2 (ledger storage), WP-C5 (σ_p, R_p).
- **Tests:**
  - acceptance 2 (repeated parent-only steps leave CONTAINS confidence unchanged);
  - one coincident event leaves the edge CANDIDATE;
  - 100 W child steps mirrored in the parent across ≥N events promote it;
  - a CORRELATED pair is never used for subtraction;
  - the duplicate detector stays green;
  - `replay_nesting.py --score` reproduces the declared wiring on the reference DB at ≥ the current score;
  - negverify: each guard removed makes its own test fail.
- **Size:** L

### WP-C5: Reader Residual engine (R_r, ε_r and the unknown-load penalty)
- **Objective:**
  - Per-reader residual R_r(t) = Y_r − Σ_c β·Y_c − Σ_i H_ri·P̂_i over validated children. Compute it for main / parent scopes too, not only single-CT circuits.
  - Persist it keyed by reader and topology version (fixes the install-wide window bug).
  - Per-reader noise model σ_r (consume the pipeline's rolling noise; do not reimplement it) kept separate from the residual baseline / dynamic split.
  - Learned per-scope unknown-load prior parameters (μ_U, σ_U, λ_u, and the rate of unknown-step events), published to joint inference.
  - A diagnostic that measures over- and under-assignment to residual (§8.2 "measurable and configurable").
- **Files owned:** `models/residual_power.py`, the residual stage of `audit.py:1874-1965` and `_residual_window` (`:2319`) (shared hot file), the residual section of `storage/database.py` + migration, `main.py:2105` `residual_components`.
- **Depends on:** WP-C3 (β, H), pipeline agent (σ_r), joint-inference agent (consumes λ_u / σ_U; owns `solver.py` slack shape, see C7), storage agent.
- **Tests:**
  - per-scope baselines differ when scopes differ (regression for the shared-window bug);
  - the signed identity holds;
  - noise, residual and profile are three distinct objects;
  - with the λ_u sweep, a known device is not credited with unexplained load at the learned penalty; report the metric.
- **Size:** M–L

### WP-C6: Unknown Load Profile engine
- **Objective:**
  - Produce `UnknownResidualEvent` from residual step events no device explains (the unblocked TASK-RESIDUAL-005).
  - Cluster events into `UnknownLoadProfile` per scope (signature, mean / peak, duration, recurrence), with lifecycle OBSERVING → CHARACTERISED → CANDIDATE → RETIRED and no auto-confirm.
  - Locate each profile in the topology (deepest reader that sees it, via the same asymmetric evidence) so it gets a proper H column.
  - Expose profiles as the persistent unknown candidates for matching.
  - Keep §9.4 D (profile rows) and E (reader residual rows) as **separate** row sources for the matrix agent.
- **Files owned:** new `models/unknown_load.py`, a new storage table (+ migration slot), a producer hook in `audit.py` (shared).
- **Depends on:** WP-C5, pipeline agent (event groups), matching agent (consumes profiles as candidates), mapping agent (renders D vs E; `mapping.py:576-627` `_unknown_cells` must stop minting profile-less unknown rows from residual).
- **Tests:**
  - acceptance 4 (an unknown device is detected without HA) at the topology level;
  - a profile is never CONFIRMED;
  - a retired profile is retained;
  - a residual row is never counted as a device;
  - a refrigerator-like periodic unknown becomes CHARACTERISED after N cycles.
- **Size:** M

### WP-C7: Configuration change and self-healing workflow (§14)
- **Objective:** implement the 10 steps.
  1. Declared events come from WP-C2's PUT.
  2. Inferred detectors:
     - persistent sign inversion on a consuming reader → CT_DIRECTION_CHANGE;
     - a step change in the child/parent β or ratio → CALIBRATION_CHANGE / MEASUREMENT_SCOPE_CHANGE;
     - an association vanishing on reader A and appearing on reader B → SMART_PLUG_RELOCATION / CT_READER_RELOCATION;
     - reader silence → READER_REMOVAL.
  3. Wire `detect_configuration_changes` with a real observed topology (today dead: `integration.py:817` never passes `observed_topology`).
  4. Mark affected edges and mapping rows SUSPECTED_CHANGE and reduce downstream confidence (interface to the mapping and confidence agents).
  5. Spawn a candidate topology version; collect evidence via WP-C4; validate; promote.
  6. Enqueue reconciliation of inference records under the old version (`inference_records_for_topology_version`).
  7. Power-model versions are never touched (assertion).
- **Files owned:** new `electrical/topology_change.py` (move `detect_configuration_changes` + helpers out of `topology.py:1198-1490`), `audit.py:990-1048` `_detect_changes` (shared), and the reconciliation driver (coordinate with the learning agent, who owns historical reconciliation).
- **Depends on:** WP-C2, WP-C4, mapping agent (cell SUSPECTED_CHANGE), learning agent (reconcile / replay), domain agent (version ids).
- **Tests:**
  - **acceptance 15:** move a plug; mapping v+1, power model version unchanged; old events keep the old mapping version.
  - **acceptance 16:** flip CT polarity → a SUSPECTED event is created → affected cells degrade → a new version is promoted after N events → the prior version is preserved and readable.
  - Calibration factor change → the old window is re-solved with the old factor.
  - A suspected change never auto-confirms.
- **Size:** L

### WP-C8: Decouple mapping from topology; align the minimal-ha flow
- **Objective:**
  - Stop projecting device mapping into `Topology.devices.branch_ct_ids` (`configuration.py:769+`). Auto placements write mapping rows with history (supersede, not wholesale replace: `auto_placement.py:274-296`), with mapping version separate from topology version.
  - Amend the minimal-ha draft:
    - user-chosen parents become `declared_by=user` CONTAINS edges;
    - inferred proposals are never written by HA as fact;
    - the silent confirm becomes "confirm user-declared edges only".
- **Files owned:** `disaggregation/auto_placement.py`, `disaggregation/configuration.py` (projection functions), `custom_components/wattmosaic/flow_logic.py` / `config_flow.py` parent handling, `docs/specs/minimal-ha-setup/*` amendments.
- **Depends on:** mapping-matrix agent (owns the mapping store; this WP may fold into theirs), WP-C1, WP-C2.
- **Tests:** a mapping change does not bump the topology version; a re-run of auto placement preserves superseded mappings with `valid_to`; an HA flow test asserts that no inferred parent is posted as confirmed.
- **Size:** M

**Suggested order:** C1 → C2 → C3 ∥ C5 → C4 → C6 ∥ C7 → C8. C8 can start once the mapping agent fixes the store.

---

## 5. Traps and constraints

- **T1. Two nesting engines.** `nesting.py` (live wizard, `/topology/nesting`) and `hierarchy.py` (audit pass) use different thresholds and outputs. Neither feeds H. Merge them; do not add a third.
- **T2. The lifecycle column does not exist** in `topology_relations` (`migrations.py:647-662`), so every inferred relation reloads without lifecycle. Any "lifecycle works" test that does not round-trip through SQLite is lying.
- **T3. Smart plugs are not readers.** `ingestion/discovery.py:15-28` deliberately refuses single-entity power sensors as channels, and ADR-0034 removed direct measurement. Acceptance 8 and 15 and SHARED_LOAD are impossible without reversing that, which needs a new ADR that explicitly supersedes part of ADR-0034. Coordinate with the domain agent.
- **T4. ADR-0030** (a prior is bounded by its row) assumes a fixed attribution row. Once mapping can be multi-reader or provisional, "the row that carries it" must be defined per (T, M) version. Keep the invariant "accounted ≤ measured per row".
- **T5. Retroactive calibration.** `engine.py:739` builds the graph without `FactorRegistry`. Any historical replay after a multiplier edit silently rescales history (it violates §16.7 / §4.4). Fix it in WP-C3 before claiming replay parity.
- **T6. Replacing a missing child with "exclude the parent row"** is conservative today. Changing it (Q4) changes coverage numbers on the reference install (65.25 % coverage / 34.75 % unknown per ADR-0030). Expect diffs and baseline them.
- **T7. Unknown slack is a nuisance column** in identifiability (`solver.py:149`). Making U scope-located and propagated through ancestors changes rank and null space. Coordinate with the joint-inference agent so device identifiability is not reported worse because of correlated U columns.
- **T8. Absolute containment is weak for loads.** Real data showed idle loads are "contained" by every panel (`nesting.py:19-25`). Delta evidence must be the primary signal for branch-level edges, and that depends on pipeline event quality. Do not lower `CONTAINMENT_MIN` (0.995) to get more edges.
- **T9. Split-phase.** Legs are paired by a `_l1` / `_l2` name regex (`nesting.py:138`), and declared phase labels are contradicted by measurement on the reference install (`main.py:1967-1974`). A phase-compatibility gate must not trust declared phase blindly.
- **T10. `negverify_nesting.py` hard-codes `ROOT=/Users/asxzy/src/wattmosaic`**, the main checkout and not the worktree. Running it from a worktree mutates and tests the wrong tree. Parameterise it before use.
- **T11. Learning gate.** Learning is gated on the aggregate topology CONFIRMED (`database.py:380-385`; `AMBIGUOUS_TOPOLOGY`). Removing the aggregate flag (C4) without a per-edge replacement disables learning or enables it everywhere.
- **T12. Stale tracker.** `docs/specs/load-disaggregation/tasks/progress.md` marks TOPOLOGY-002 ("residual corroborating") and TOPOLOGY-005 VERIFIED. The code discards residual history (`hierarchy.py:611`), and change detection is unwired in production. Do not reuse those claims as evidence.
- **T13. Audit pass ordering.** Stages run in `audit.py` within the inference loop, on the event-loop budget (ADR-0031 / 0033). Evidence-accumulating topology inference must be incremental per event, not a full-window recompute (nesting already needed perf work: `tests/unit/test_nesting_perf.py`).
- **T14. HA flow writes `parent_ct_id` today.** Changing the topology contract breaks the integration until it is updated in the same release (§16.9). The minimal-ha draft touches the same files.

---

## 6. Open questions for the product owner

1. **Status of user-declared parents.** Are they CONFIRMED immediately (current behaviour), or PROVISIONAL until measurement corroborates them? HB §4.3 says a single event never confirms an edge, but it is silent on declarations. Should a declaration that measurement contradicts move to CONFLICTED / SUSPECTED_CHANGE?
2. **The minimal-ha "inferred parents".** May HA ever write an engine-inferred parent? Proposed answer: no; HA writes only what the user picks, and the engine keeps inferred edges as its own CANDIDATE / PROVISIONAL prior. Also, is a silent confirm acceptable for user-declared edges only?
3. **Pointer fields (`parent_ct_id`, `paired_ct_id`, `alias_of_ct_id`).** Remove them from the contract (clean break, API v3), or keep them as derived read-only views for one release?
4. **Missing child reading.** Should the parent row stay in the solve with inflated uncertainty for the missing child, or stay excluded (today)? This changes coverage numbers.
5. **Smart plugs.** Should they become first-class readers (Measurement Endpoints)? This partially reverses ADR-0034 and is required for acceptance 8 and 15.
6. **Promotion thresholds and authority.** May the engine auto-promote an inferred CONTAINS edge to CONFIRMED after N independent events, or must a user always confirm? The lifecycle docstrings currently say "only a user may move it to CONFIRMED" (`hierarchy.py:874-876`). The same question applies to promoting a new topology version after a suspected change.
7. **Unknown-load penalty.** What default operating point should λ_u start from: more residual, or more attribution? Which metric should be shown to the user to tune it (e.g. "% of energy in residual" vs "false-attribution rate on held-out plug-measured loads")?
8. **Legacy residual rows.** Keep the scope-less `residual_power` history readable as `scope_unknown`, or archive it?
