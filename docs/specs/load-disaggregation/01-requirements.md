# Requirements — circuit-aware load disaggregation

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION** — normative requirements for the target
system, written against observable outcomes.

---

## 1. How to read this file

Every requirement carries seven fields: **ID**, **Description**, **Priority**,
**Rationale**, **Acceptance criteria**, **Dependencies**, **Verification
method**. Priorities are `MUST` / `SHOULD` / `MAY`.

Requirements are written against what a reader of the finished system can
observe — an API field, a stored artefact, a rendered UI element, a test. A
requirement that says "implement module X" is forbidden: the topology engine,
the residual model, the device models, the event correlator and the confidence
model all exist (`docs/assessment_gap_analysis.md` §"What must NOT be
rebuilt").

**Rule.** No requirement may contradict the repository's invariants
(`README.md` §5). Where a requirement would violate one, the conflict is
recorded as an open decision in
[`00-project-overview.md`](00-project-overview.md) §12 rather than absorbed
here.

### 1.1 Gap classification

Each requirement names the gap it closes, using the repository's own gap
register `G1`–`G20` (`docs/assessment_gap_analysis.md` §11) and the `G21+`
identifiers introduced in [`00-project-overview.md`](00-project-overview.md)
§13.1 (the source register for the `G21`–`G32` band). A gap found elsewhere in
the specification set takes a **locally-scoped** identifier and is valid only
together with its entry in the canonical reconciliation register at
[`00-project-overview.md`](00-project-overview.md) §13.2, which is the register
of record and the single source of truth for gap identifiers. `EXISTS` means
implemented and traceable; `PARTIAL` means implemented but missing a named
capability; `MISSING` means no code path.

### 1.2 Scenario references

Requirements that depend on a synthetic scenario reference **both** the new
`T##` identifier (§4) and the existing matrix row or group in
`docs/test_scenario_matrix.md` they extend or newly cover.

### 1.3 The canonical scenario catalogue

The fifteen synthetic scenarios are fixed by the master instruction. They are
the contract between this file and the test plan
(`11-test-and-validation-plan.md`); the full table with the existing-matrix
mapping is in §4.

| ID | Canonical scenario | ID | Canonical scenario |
| --- | --- | --- | --- |
| `T01` | Single Child Containment | `T09` | Delayed Device Response |
| `T02` | Multiple Child Contributions | `T10` | Unknown Device Discovery |
| `T03` | Unobserved Parent Load | `T11` | Smart Plug Relocation |
| `T04` | Baseline Standby Consumption | `T12` | Reader Relocation or Replacement |
| `T05` | Noise | `T13` | 240 V Split-Phase Load |
| `T06` | Multiple Simultaneous Devices | `T14` | Missing and Delayed Data |
| `T07` | Non-identifiable Devices | `T15` | Bidirectional Power |
| `T08` | Variable Power Device | | |

---

## 2. Requirement index

| Area | Count | Range |
| --- | --- | --- |
| Foundation | 6 | `REQ-FOUNDATION-001` … `006` |
| Measurement | 10 | `REQ-MEASURE-001` … `010` |
| Measurement topology | 11 | `REQ-TOPO-001` … `011` |
| Residual | 8 | `REQ-RESIDUAL-001` … `008` |
| Disaggregation | 7 | `REQ-DISAGG-001` … `007` |
| Device and load models | 8 | `REQ-POWER-001` … `008` |
| Storage and lifecycle | 12 | `REQ-STORAGE-001` … `012` |
| Event and inference | 9 | `REQ-EVENT-001` … `009` |
| Learning | 7 | `REQ-LEARNING-001` … `007` |
| Confidence and identifiability | 7 | `REQ-CONFIDENCE-001` … `007` |
| Mapping matrix | 7 | `REQ-MAPPING-001` … `007` |
| Dynamic topology and change | 7 | `REQ-CHANGE-001` … `007` |
| User interface | 13 | `REQ-UI-001` … `013` |
| Deployment | 8 | `REQ-DEPLOY-001` … `008` |
| Testing | 10 | `REQ-TEST-001` … `010` |
| **Total** | **130** | |

---

## 3. Requirements

### 3.1 Foundation

#### REQ-FOUNDATION-001 — The specification set carries one declared status

- **Description.** This specification set carries `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`; the orchestrator's review gate has passed and every requirement in it is an implementation commitment.
- **Priority.** MUST
- **Rationale.** The brief asks for a specification of a "future-proof" system over a codebase that already implements much of it; committing to it before review would freeze choices the repository has not made (`docs/specs/retire-dedicated-ct-concept/README.md:3` is the house pattern). The review gate has now passed — every requirement is traced, every identifier resolves to one definition, the plan is internally consistent and executable, and no hard-rule violation was found — so the set is approved for implementation and the status is stated on **every** file of the set.
- **Acceptance criteria.** `README.md` states `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`; **every** file of this specification set — the 18 files in the folder root and the 37 files in `tasks/`, 55 in total at this checkout — carries the token `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION` on its **first status line**, and no file in the set declares a draft status. The number 55 is the measured size of the set at this checkout, not a fixed constant: a file added to the set that carries the status line raises it.
- **Dependencies.** —
- **Verification.** The invariant is measured **per file**, by counting the files whose *first status line* carries the token — not by counting the token's occurrences across the set. The token also appears in body text (it states the status of the set, of a requirement or of a task), so a whole-file occurrence count is larger than the file count and grows with every legitimate in-body reference; `wc -l` over all matches therefore measures prose, not the invariant. The measure is:

  ```bash
  find docs/specs/load-disaggregation -name '*.md' | sort | while read -r f; do
      awk '{ sub(/^[ \t]+/, ""); sub(/[ \t]+$/, "") } $0 == "" || $0 ~ /^#/ { next } { print; exit }' "$f"
  done | grep -c 'SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION'
  ```

  It returns **55** at this checkout: one per file of the set (18 in the folder root, 37 under `tasks/`), each on its first status line. It is the same measure the `spec_status` gate applies — `python tools/dev/verify.py --check-spec-status` reads each file's first status line, fails and names every offending file when one does not carry the token, and prints the number of files that do (`spec_status: 55 specification files carry the status token`). A token that survives only as an in-body mention does **not** satisfy the requirement: the check reads the first status line, not the whole file.

#### REQ-FOUNDATION-002 — Contract changes are additive and optional

- **Description.** Every contract change the target system makes adds a field or an object with a neutral default, so a payload written before the change still validates.
- **Priority.** MUST
- **Rationale.** The governing rule is explicit: "the 1.5.0 compatibility rule (non-negotiable)" — existing tests must stay green after every wave (`docs/implementation_plan_v2.md` §0). `contracts.py` sets `extra="forbid"` but every new model carries a default for every field (`engine/wattmosaic_engine/contracts.py:464-474`).
- **Acceptance criteria.** A payload produced by the pre-change engine validates against the post-change `Topology`, `InferenceResult`, `Reconciliation` and `InferenceRecord` models with no field added by the reader; the currently passing test suite remains green (2 827 collected; the last recorded run of that suite, at HEAD `612e1233` on 2026-11-16, reported 2 803 passed, 15 skipped, 9 errors).
- **Dependencies.** —
- **Verification.** `pytest tests/unit/test_contracts.py tests/unit/test_inference_records.py` plus `python tools/dev/export_schemas.py && git diff --exit-code data/schemas`.

#### REQ-FOUNDATION-003 — The contract change workflow is followed exactly

- **Description.** A contract change bumps `CONTRACT_VERSION`, regenerates `data/schemas/` with the existing generator, updates the contract documentation and runs the full suite.
- **Priority.** MUST
- **Rationale.** "1. Bump `CONTRACT_VERSION` in `contracts.py`. 2. Regenerate `data/schemas/`. 3. Update this document. 4. Run `pytest tests/`" (`docs/interface_contracts.md` §5); CI hard-fails on schema drift (`.github/workflows/ci.yml:28-29`).
- **Acceptance criteria.** `CONTRACT_VERSION` in `engine/wattmosaic_engine/contracts.py:32` is greater than the value at the start of the change; `python tools/dev/export_schemas.py` leaves `git diff --exit-code data/schemas` clean; the new version string appears in `docs/interface_contracts.md`.
- **Dependencies.** REQ-FOUNDATION-002
- **Verification.** `python tools/dev/verify.py` — the `contract_schema_sync` gate passes.

#### REQ-FOUNDATION-004 — No new runtime dependency, database, service or message bus

- **Description.** The target system ships as additions inside the existing engine process, the existing SQLite file and the existing HTTP API.
- **Priority.** MUST
- **Rationale.** "**No new runtime dependencies. No new database. No new service. No message bus.**" (`docs/implementation_plan_v2.md` §0), restated as acceptance gate 4 (§7 of the same document).
- **Acceptance criteria.** `pyproject.toml` `[project].dependencies` is unchanged; no second database file is opened; the engine still runs as one process with one writer (`docs/architecture.md` §2); no inter-process transport is introduced.
- **Dependencies.** REQ-DEPLOY-001
- **Verification.** `git diff pyproject.toml` shows no dependency change; `pytest tests/unit/test_app_startup.py` passes.

#### REQ-FOUNDATION-005 — The four power states are used strictly

- **Description.** Any reported per-device power is labelled **measured**, **estimated**, **allocated** or **unknown**, and the label is a machine-readable field, not prose.
- **Priority.** MUST
- **Rationale.** The repository already separates "how obtained" (`Source`) from "what instrument" (`MeasurementBasis`) (`contracts.py:62-73`, `:186-211`); the master brief requires the same distinction at the product surface. "A system that produces plausible-looking but incorrect per-device power values is not acceptable" (`docs/WATTMOSAIC_MASTER_INSTRUCTIONS.md` §19.5).
- **Acceptance criteria.** A device result exposes a single field carrying one of the four values; a `Source.INFERRED` value is never labelled measured; a solver slack value is never labelled estimated.
- **Dependencies.** —
- **Verification.** `pytest tests/unit/test_disaggregation.py` — a new assertion that the label matches the source and basis of the result.

#### REQ-FOUNDATION-006 — One file, one owner, per wave

- **Description.** Any change the target system makes touches files owned by exactly one agent per wave, and a cross-file need is raised rather than coordinated silently.
- **Priority.** MUST
- **Rationale.** "Any cross-file need is a message to the orchestrator, not an uncoordinated edit" (`docs/implementation_plan_v2.md` §5).
- **Acceptance criteria.** The implementation plan published with this specification names one owner per file per wave; no wave has two agents writing the same file.
- **Dependencies.** —
- **Verification.** Manual review of the implementation plan against `docs/implementation_plan_v2.md` §5.

### 3.2 Measurement semantics

#### REQ-MEASURE-001 — Only active power and cumulative energy are measurable

- **Description.** The system measures active power in watts and cumulative energy in kilowatt-hours, and nothing else; the measurement basis travels with the reading.
- **Priority.** MUST
- **Rationale.** `Measurement.unit` is `Literal["W", "kW", "kWh", "Wh"]` (`engine/wattmosaic_engine/contracts.py:1074`) and `MeasurementBasis` distinguishes CT-measured from monitor-reported from factor-adjusted from estimated-appliance-total (`:186-211`). A declared wattage is a *non-relaxable upper bound* on a shared row.
- **Acceptance criteria.** No reading is accepted with a unit outside `W`/`kW`/`kWh`/`Wh`; every persisted reading carries a `measurement_basis` that is not `UNKNOWN` once a basis is established, or is `UNKNOWN` with the reason recorded.
- **Dependencies.** REQ-TOPO-009
- **Verification.** `pytest tests/unit/test_contracts.py` — an out-of-unit value is rejected; `pytest tests/unit/test_factors.py` — the basis is resolved per sample.

#### REQ-MEASURE-002 — Unmeasurable electrical quantities are never synthesised

- **Description.** Voltage (V), RMS current (A), power factor, reactive power (VAR), apparent power (VA) and frequency (Hz) are not produced, estimated or displayed by the target system; the one exception is the declared nominal voltage constant, which is a label and not a reading.
- **Priority.** MUST
- **Rationale.** No such quantity exists anywhere in the contracts or the code path; `CTChannel.nominal_voltage` is "Optional nominal voltage for current-only channels" and is explicitly not an ingested quantity (`contracts.py:1146-1147`; `engine/wattmosaic_engine/electrical/factors.py:40`). The HA integration's registry vocabulary lists these device classes so a future instrument would be *recognised*, not because the install produces them (`custom_components/wattmosaic/state_links.py:86-99`).
- **Acceptance criteria.** No API response field, no persisted column and no rendered UI element presents a voltage, current, power factor, reactive, apparent or frequency value as a measurement; a `nominal_voltage` is rendered as a declaration and is labelled as such.
- **Dependencies.** REQ-UI-002, REQ-UI-013
- **Verification.** `grep -rniE "\b(reactive_power|power_factor|apparent_power|rms_current|frequency_hz)\b" engine/wattmosaic_engine/contracts.py` returns no field definition; `pytest tests/unit/test_panel.py` — no such label is rendered.

#### REQ-MEASURE-003 — Phase is a declared property of a reader, never inferred

- **Description.** Each reader declares its phase as `L1`, `L2`, `BOTH` or `UNKNOWN`; `UNKNOWN` is a legitimate value and is never resolved from the reader's name, position or reading magnitude.
- **Priority.** MUST
- **Rationale.** `Phase` carries four members (`contracts.py:50-54`) and `CTChannel.phase` defaults to `UNKNOWN` (`:1121`); "it is never inferred from `phase` or `ct_type`" is the rule applied to the measurement basis too (`contracts.py:1167-1170`).
- **Acceptance criteria.** A stored channel with `phase=UNKNOWN` renders as unknown phase; discovery does not set a phase from a name; the matrix coefficient for an `UNKNOWN`-phase device follows the documented fallback and carries a diagnostic.
- **Dependencies.** REQ-TOPO-003
- **Verification.** `pytest tests/unit/test_electrical.py::test_phase_coefficients_l1_l2_both` — existing; a new assertion that a name-derived phase is not written.

#### REQ-MEASURE-004 — Polarity is preserved, signed and never clamped

- **Description.** A reader's polarity is `NORMAL` or `INVERTED`; the sign of the reading is corrected exactly once at read time, and a reading whose sign contradicts the declared polarity is reported rather than clipped.
- **Priority.** MUST
- **Rationale.** "the reversed step is withheld with `opposite_direction`. Silently flipping the sign would hide a wiring fault" (`docs/association_and_topology.md` §2.9); `Measurement.effective_power_w()` applies the sign once and preserves it (`contracts.py:1107-1113`).
- **Acceptance criteria.** An inverted reader's step is matched against the device step with the corrected sign; a negative reading on a `NORMAL` reader produces a quality issue; no path clamps a negative value to zero.
- **Dependencies.** REQ-EVENT-002
- **Scenario.** `T15` — extends matrix group **C** (row `C7`, negative CT values) and matrix group **F** (row `F5`, negative residual); **new coverage** for the reverse-flow direction.
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_11_reversed_ct_polarity`; `pytest tests/unit/test_electrical.py::test_reversed_polarity_is_applied`; scenario `T08`.

#### REQ-MEASURE-005 — A leg pair is usable only as a pair

- **Description.** When two readers are declared as the two legs of one load (`paired_ct_id`), both rows are used together or both are excluded; a stale, missing or skewed partner excludes both, with the reason recorded.
- **Priority.** MUST
- **Rationale.** "a leg alone is not a measurement of that circuit at half its value" (`engine/wattmosaic_engine/disaggregation/engine.py:900-906`); the exclusion is implemented in `_exclude_partial_legs` (`:890-950`).
- **Acceptance criteria.** A window in which one leg is stale produces two excluded rows and a named reason (`leg_skew_exceeded:<ct>:<partner>:<s>s`), not one half-row.
- **Dependencies.** REQ-MEASURE-007, REQ-TOPO-007
- **Scenario.** `T13` — extends matrix group **A** (row `A2`, 240 V device) and matrix group **B** (row `B8`, unbalanced L1/L2); **new coverage** for the pair-only exclusion.
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_a_pair_whose_legs_were_sampled_far_apart_is_excluded`; scenario `T13` (extends matrix group A2/B8/C2).

#### REQ-MEASURE-006 — Reverse and bidirectional power flow is preserved

- **Description.** A reader that reports negative power (export, or a reversed clamp) keeps its sign in every derived number; the system reports the direction rather than discarding it.
- **Priority.** MUST
- **Rationale.** "a negative residual is a real observation … and keeps its sign" (`docs/residual_analysis.md` §1); negative export is preserved in reconciliation (`docs/electrical_model.md` §4.2.4).
- **Acceptance criteria.** A negative reading on a feed row appears as a negative contribution in the balance; the residual is signed accordingly; no path takes `abs()` of a reading.
- **Dependencies.** REQ-RESIDUAL-001
- **Scenario.** `T15` — extends matrix group **C** (row `C7`) and matrix group **F** (row `F5`); **new coverage** for the reverse-flow direction.
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_negative_export_is_preserved_in_reconciliation`; scenario `T15` (new coverage).

#### REQ-MEASURE-007 — Resolution, reliability and expected interval travel with the reader

- **Description.** Each reader declares `resolution_w`, `reliability` and `expected_interval_s`; the declared resolution bounds the smallest difference the engine treats as a physical change.
- **Priority.** SHOULD
- **Rationale.** The fields exist (`contracts.py:1148-1150`); "the response significance in units of the CT's own baseline MAD" (`docs/association_and_topology.md` §2.3) is how resolution is used today.
- **Acceptance criteria.** A step smaller than the declared resolution is not counted as a distinct event; the declared reliability is published with the reader in the API and the panel.
- **Dependencies.** REQ-EVENT-004
- **Scenario.** `T05` — extends matrix group **H** (row `H8`, noise and spikes); **new coverage** for the declared resolution as the noise floor.
- **Verification.** `pytest tests/unit/test_correlation.py::test_a_step_inside_the_noise_floor_is_refused` — existing mechanism; a new assertion that `resolution_w` is published.

#### REQ-MEASURE-008 — Three timestamps, one staleness definition

- **Description.** Every observation carries `state_timestamp`, `received_at` and an optional `physical_timestamp`; staleness is `received_at − state_timestamp`; the three are never conflated.
- **Priority.** MUST
- **Rationale.** "Three distinct clocks per observation. Conflating them is a correctness bug." (`docs/data_flow.md` §2); ADR-0003 makes this a decision, not a convention.
- **Acceptance criteria.** A reading whose `state_timestamp` is older than `stale_after_s` (default 120 s, `engine/wattmosaic_engine/config/settings.py:157`) is `STALE` and excluded from learning; `physical_timestamp` is `null` for polling CT integrations and is never populated with a substitute.
- **Dependencies.** —
- **Verification.** `pytest tests/unit/test_synchronization.py::test_window_age_uses_state_timestamp_not_arrival`; `pytest tests/unit/test_ingestion.py::test_measurement_normalization_and_timestamps`.

#### REQ-MEASURE-009 — A repeated value is not new evidence

- **Description.** A state update whose numeric value equals the previous one is stored as the current value with `repeated=True` and is not counted as an independent observation by the learning, correlation or evidence-count paths.
- **Priority.** MUST
- **Rationale.** ADR-0003: "Repeated identical values are flagged `repeated=True` and are not counted as new evidence for learning"; the field exists at `contracts.py:1080`.
- **Acceptance criteria.** The evidence count of a relation does not increase on a repeated update; the sample count used for quality does not increase either; the repeat ratio is published per reader.
- **Dependencies.** REQ-CONFIDENCE-006
- **Verification.** `pytest tests/unit/test_synchronization.py`; `pytest tests/unit/test_event_intake.py::test_a_repeated_state_is_not_a_transition`.

#### REQ-MEASURE-010 — Cadence is measured, never assumed at two seconds

- **Description.** Event windows, sampling grids and quality thresholds are sized from the observed per-channel cadence; no component assumes a two-second update interval.
- **Priority.** MUST
- **Rationale.** "Typical CT cadence is 15.7 s median, up to >30 s … Adaptive windows from measured cadence; no 2 s assumption anywhere" (`docs/risks.md` R4); ADR-0003 records the same finding. The **15.7 s** median is the audited live figure (`PROJECT_STATUS.md:28` records `two_second_assumption_supported: false`; ADR-0003:42-44); `docs/ct_update_profile.md:26-28` is the committed **synthetic** profile and reports `median_interval_s: 15.8` over 36 channels (`:3-4`, `:6-9`).
- **Acceptance criteria.** The configured isolation window, the sampling grid and the staleness threshold are all derivable from `ChannelHealth.interval_median_s`; a configuration that hard-codes 2 s is rejected by a static gate.
- **Dependencies.** REQ-TEST-003
- **Scenario.** `T14` — extends matrix group **C** (row `C8`, missing samples) and matrix group **H** (rows `H7`, `H9`).
- **Verification.** `pytest tests/unit/test_synchronization.py::test_cadence_is_derived_from_observed_history_not_hardcoded`; `pytest tests/unit/test_event_anchor.py::test_anchor_window_is_used_not_the_observation_horizon`.

### 3.3 Measurement topology

#### REQ-TOPO-001 — The relationship set is typed and the kinds never imply each other

- **Description.** Every edge between a device, a reader and a circuit carries exactly one kind — containment (`CONTAINS`), shared load (`SHARED_LOAD`), leg pairing (`PAIRED_LEG`) or correlation (`CORRELATED`) — and no kind implies another.
- **Priority.** MUST
- **Rationale.** The four existing `RelationKind` members (`contracts.py:254-265`) exist precisely to keep the assertions apart: "nothing can set a parent pointer and thereby assert membership" (`docs/association_and_topology.md` §1). The target vocabulary is the same set under product-facing names.
- **Acceptance criteria.** A stored relation resolves to exactly one kind; an edge of kind `CORRELATED` can never be read as containment by any consumer; the API exposes the kind on every relation.
- **Dependencies.** REQ-CONFIDENCE-001
- **Scenario.** `T13` — extends matrix group **A** (row `A2`) and matrix group **C** (row `C2`, 1 device / many CTs); **new coverage** for the typed relationship set.
- **Verification.** `pytest tests/unit/test_association.py`; `pytest tests/unit/test_hierarchy.py::test_every_candidate_requires_confirmation`.

#### REQ-TOPO-002 — An edge has a lifecycle, and the lifecycle never implies confirmation

- **Description.** Every edge carries a lifecycle state — `CANDIDATE`, `PROVISIONAL`, `CONFIRMED`, `SUSPECTED_CHANGE`, `UNDER_REVIEW`, `CONFLICTED`, `REJECTED` or `RETIRED` — and only `CONFIRMED` is ever presented as established.
- **Priority.** MUST
- **Rationale.** The gap is real: `TopologyRelation.status` has three members (`TopologyStatus.CANDIDATE`/`CONFIRMED`/`REJECTED`, `contracts.py:84-87`) and no state for "this was true and may no longer be" or "two pieces of evidence disagree" (`G21`; §13.1 of `00-project-overview.md`).
- **Acceptance criteria.** An inferred edge is `CANDIDATE` with `requires_confirmation=True` and confidence ≤ 0.99; a withdrawn edge is `REJECTED` with a reason and keeps its evidence readable; a retired edge is `RETIRED` and is excluded from the current view but remains queryable.
- **Dependencies.** REQ-TOPO-001
- **Scenario.** `T12` — extends matrix group **C** (row `C9`, reversed orientation / direction change); **new coverage** for the edge lifecycle across a reader change.
- **Verification.** `pytest tests/unit/test_association.py` — a new assertion for `SUSPECTED_CHANGE` and `RETIRED`; `pytest tests/unit/test_hierarchy.py::test_every_candidate_requires_confirmation`.

> **Completion-sprint amendment — SPEC CHANGE (2026-11-18).** Per the resolved owner decision OD-51, confirmation no longer gates inference or learning: the current topology is always authoritative regardless of `CANDIDATE`/`CONFIRMED` status. The topology is dynamic and publishes its confidence, with the reduced confidence carried as a penalty rather than a gate. Breaking changes remain `SUSPECTED` proposals and are not auto-applied. The original requirement text above is retained for traceability.

#### REQ-TOPO-003 — Parent and child are treated asymmetrically

- **Description.** A parent reader's row carries the aggregate of everything below it; a child reader's row is subtracted from its parent before the parent's remainder is solved; the two roles are never interchangeable.
- **Priority.** MUST
- **Rationale.** "A device is attributed using exactly **one** most-specific CT: its **deepest** enclosing branch CT" (`docs/electrical_model.md` §1.1); nested mains are summed only at the outermost feed (`docs/electrical_model.md` §1.1, "nested mains").
- **Acceptance criteria.** A device under a nested child contributes once to the total; the parent row's remainder excludes the child's measurement; the matrix records the child's subtraction as `nested_children`.
- **Dependencies.** —
- **Scenario.** `T01` and `T02` — extends matrix group **C** (rows `C1`, `C5`, main/sub overlap); **new coverage** for the asymmetric parent/child treatment.
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_07_nested_branch_ct_does_not_double_count`; `pytest tests/unit/test_electrical.py::test_nested_main_sums_only_the_outermost_feed`.

#### REQ-TOPO-004 — A child-to-parent correlation is containment evidence only when repeated and validated

- **Description.** A response observed at a child and a matching response at a parent is recorded as evidence toward a containment hypothesis; the hypothesis is only promoted when the evidence bar is met, and an unexplained parent change never disproves containment.
- **Priority.** MUST
- **Rationale.** "Correlation is not topography, and a bigger reading is not upstream" (`docs/association_and_topology.md` §5); the hierarchy bar requires independent devices, comparable instants, containment and response consistency (`docs/association_and_topology.md` §3.1).
- **Acceptance criteria.** A single coincident response produces an overlap, not parenthood; a parent step that no child explains leaves the confirmed containment edge intact and is reported separately.
- **Dependencies.** REQ-TOPO-002, REQ-TOPO-011
- **Scenario.** `T01` — extends matrix group **C** (row `C1`) and matrix group **I** (row `I8`); **new coverage** for the single-child containment evidence.
- **Verification.** `pytest tests/unit/test_hierarchy.py::test_hot_tub_equal_deltas_yield_overlap_only`; a new assertion that an unexplained parent change does not withdraw a confirmed edge.

#### REQ-TOPO-005 — Exact parent–child power conservation is never enforced

- **Description.** The system never requires a child's reading to sum exactly to the parent's; the containment check tolerates the documented violation fraction and records the violation instead of failing the edge.
- **Priority.** MUST
- **Rationale.** A violation is "an instant where the child exceeds an active parent … a violation, never a skipped sample" (`docs/association_and_topology.md` §3.3), with `max_power_violation_frac = 0.02` and `VIOLATION_TOL_W = 30.0` (`docs/association_and_topology.md` §3.2).
- **Acceptance criteria.** A containment edge survives a violation fraction within the tolerance and publishes the violation count; no code path raises on a parent-child mismatch.
- **Dependencies.** REQ-TOPO-004
- **Scenario.** `T03` — **new coverage** (no existing row asserts that an unexplained parent change preserves a confirmed containment edge).
- **Verification.** `pytest tests/unit/test_hierarchy.py` — the violation accounting assertions.

#### REQ-TOPO-006 — Edge inference is residual-aware

- **Description.** A containment or overlap hypothesis may be formed from the unexplained remainder of the parent row, not only from matched device responses; the residual evidence is recorded with the edge.
- **Priority.** SHOULD
- **Rationale.** The residual module already refuses multi-CT scopes rather than summing them (`docs/residual_analysis.md` §7), and the hierarchy finder accepts `circuits` and `residual_history` for interface stability but explicitly declines to let them establish containment (`docs/association_and_topology.md` §3.1). The target therefore treats residual evidence as **corroborating**, never as sole proof.
- **Acceptance criteria.** A candidate edge cites its residual evidence when it has one and states that residual evidence alone cannot promote it; the promotion path records which evidence types were sufficient.
- **Dependencies.** REQ-RESIDUAL-003, REQ-TOPO-004
- **Scenario.** `T03` — extends matrix group **F** (rows `F3`, `F4`); **new coverage** for residual-aware edge inference.
- **Verification.** `pytest tests/unit/test_residual_power.py`; a new assertion in `tests/unit/test_hierarchy.py` that residual-only evidence does not promote.

#### REQ-TOPO-007 — Nested children cannot be counted twice

- **Description.** A reader that is contained by another reader contributes its measured value to the parent's remainder exactly once, and an alias contributes to a total exactly once.
- **Priority.** MUST
- **Rationale.** "In the measurement matrix the alias gets **no row of its own** — two copies of one conductor would be two equations for one physical quantity" (`docs/electrical_model.md` §1.1); the matrix builder records `nested_children` (`engine/wattmosaic_engine/electrical/matrix.py:97`).
- **Acceptance criteria.** A three-level nesting contributes each physical quantity once at the total; an alias chain contributes once through the whole chain; a missing child excludes the parent row with a named reason rather than using it raw.
- **Dependencies.** REQ-TOPO-003
- **Scenario.** `T02` and `T06` — extends matrix groups **A** (row `A2`), **C** (row `C5`) and **F** (row `F10`).
- **Verification.** `pytest tests/unit/test_electrical.py::test_three_level_nesting_sums_only_the_root`; `pytest tests/unit/test_electrical.py::test_an_alias_chain_contributes_once_through_the_whole_chain`; matrix row `F10`.

#### REQ-TOPO-008 — Topology is a versioned object with diffing and rollback

- **Description.** A topology version is a first-class object with an identifier, a creation time, a status, a parent version and a diff against its predecessor, and a previous version can be restored without rewriting history.
- **Priority.** MUST
- **Rationale.** The gap is real: `topology_versions` stores opaque `payload_json` (`engine/wattmosaic_engine/storage/migrations.py:308-315`) and `Topology.version` is a bare integer (`contracts.py:1217`); `G22` (§13.1 of `00-project-overview.md`).
- **Acceptance criteria.** The API returns, for two versions, the added, removed and changed channels, devices and relations; restoring version *n* creates a new version whose parent is the current one and leaves version *n* readable exactly as it was.
- **Dependencies.** REQ-STORAGE-005
- **Scenario.** `T11` and `T12` — **new coverage** (no existing row asserts a diffable, rollback-able topology version).
- **Verification.** `pytest tests/unit/test_topology_persistence.py` — a new assertion for the diff; `pytest tests/integration/test_confirm_topology_e2e.py`.

#### REQ-TOPO-009 — Topology change is detected and reconciled

- **Description.** When a reader, a device or a declared configuration changes, the system detects the change, states what changed, reconciles the affected inference records against the new configuration and leaves the old records readable with the version they were produced under.
- **Priority.** MUST
- **Rationale.** "no test asserts that changing the topology *after* inferences exist re-derives those inferences / marks them stale, or that old records keep their `topology_version`" (`docs/test_scenario_matrix.md` row J6).
- **Acceptance criteria.** After a topology change, every stored inference record still names the `topology_version` it was produced under; a record produced under an older version is marked as belonging to a superseded configuration; the API exposes both.
- **Dependencies.** REQ-CHANGE-001, REQ-STORAGE-007
- **Scenario.** `T11` and `T12` — **new coverage** (no existing row asserts reconciliation of the inference history after a configuration change).
- **Verification.** `pytest tests/unit/test_inference_records.py` — a new assertion for the version retention; scenario `T12` (new coverage).

#### REQ-TOPO-010 — The topology graph may be incomplete

- **Description.** A topology with unconnected readers, unplaced devices or unresolved circuits is a valid state; the system reports what it does not know rather than requiring complete coverage.
- **Priority.** MUST
- **Rationale.** "Incomplete topology is a valid output" (`docs/association_and_topology.md` §3.7); "An empty result with a reason is preferred to a guess" (same section).
- **Acceptance criteria.** A topology with an unplaced device still loads, still solves and publishes that device as unknown; the API reports the count of unplaced devices and unconnected readers.
- **Dependencies.** REQ-UI-003
- **Verification.** `pytest tests/unit/test_topology_bootstrap.py`; `pytest tests/unit/test_electrical.py::test_unobserved_device_is_listed_not_silently_zero`.

#### REQ-TOPO-011 — A circuit is an electrical scope with its own identity and class

- **Description.** A circuit exists before any device names it, carries a stable identifier, its panel, its reader set, its device set, its measurement basis, a class (`DEDICATED`, `SHARED`, `PARTIALLY_UNDERSTOOD`, `UNKNOWN`) and a status.
- **Priority.** MUST
- **Rationale.** `Circuit` already exists with exactly these fields (`contracts.py:686-723`) and the class validation rules are enforced (`:735-759`); `G10` in the gap register rates the classification PARTIAL because the panel-tree node lacks a `PARTIAL` class and a per-circuit confidence.
- **Acceptance criteria.** `GET /circuits` returns the circuit identifier, class, status, reader set and device set; a `PARTIALLY_UNDERSTOOD` circuit states the unattributed load; the classification confidence is published separately from the class.
- **Dependencies.** REQ-CONFIDENCE-004, REQ-UI-004
- **Verification.** `pytest tests/unit/test_circuits.py`; `pytest tests/unit/test_panel_tree.py`.

### 3.4 Residuals

#### REQ-RESIDUAL-001 — The signed residual is the only subtraction

- **Description.** The residual of a scope is `measured scope power − Σ known load contributions`, computed by the single signed-subtraction routine; no `max(0.0, …)`, no `abs()` and no epsilon clamp is applied to it.
- **Priority.** MUST
- **Rationale.** `_signed_residual` "is the **only** subtraction in the module" and applies "no `max(0.0, ...)`, no `abs()`, no epsilon" (`docs/residual_analysis.md` §1); `ZERO_TOL_W = 1e-9` is used for exactly one purpose — deciding whether the residual is resolved (`engine/wattmosaic_engine/models/residual_power.py:112`, `:436`).
- **Acceptance criteria.** A residual of 1 W is `POSITIVE`; only an exactly-zero residual is `NEAR_ZERO`; a negative residual keeps its sign end to end.
- **Dependencies.** —
- **Scenario.** `T15` — extends matrix group **F** (row `F5`, negative residual) and matrix group **C** (row `C7`).
- **Verification.** `pytest tests/unit/test_residual_power.py`; `pytest tests/unit/test_contracts.py` — `derived_residual_kind` boundary assertions.

#### REQ-RESIDUAL-002 — A residual decomposes into baseline, dynamic and noise

- **Description.** For each reader scope the system publishes the always-present part (`baseline_w`), the varying part (`dynamic_w`) and the observed noise of the scope, and the baseline and dynamic parts always add back to the total.
- **Priority.** MUST
- **Rationale.** `ResidualPower.baseline_w` and `.dynamic_w` exist (`contracts.py:558-561`); the split uses a **median** over recent residual observations, "a robust central tendency is the only estimator that does not turn one spike into a permanent baseline" (`docs/residual_analysis.md` §3).
- **Acceptance criteria.** `baseline_w + dynamic_w == residual_w` exactly for every published decomposition; the estimator used and the number of window samples are published alongside; with no window the estimator is reported as `no_window_current_only` and `dynamic_w` is 0.0.
- **Dependencies.** REQ-RESIDUAL-001, REQ-RESIDUAL-003
- **Scenario.** `T03` — extends matrix group **F** (rows `F1`, `F3`); **new coverage** for the three-way split.
- **Verification.** `pytest tests/unit/test_residual_power.py` — the decomposition assertions; matrix group `F`.

#### REQ-RESIDUAL-003 — A residual is first-class data with history

- **Description.** Each residual observation is persisted with its scope, sign, resolution state, age, basis and quality, and a reader can ask for the residual history of a scope over a period.
- **Priority.** MUST
- **Rationale.** The table exists and is in `AUDIT_TABLES` so it survives retention (`engine/wattmosaic_engine/storage/migrations.py:121-134`; `docs/residual_analysis.md` §8), but the read path is only `latest_reconciliation()` (`G13`, `docs/assessment_gap_analysis.md` §11).
- **Acceptance criteria.** `GET /residuals` returns a time series for a scope over a period, not only the latest value; each point carries the signed value, `resolved` and `unresolved_since`.
- **Dependencies.** REQ-STORAGE-003
- **Verification.** `pytest tests/unit/test_residual_power.py`; `pytest tests/unit/test_api.py` — a new assertion for the history route.

#### REQ-RESIDUAL-004 — A residual may have its own operating pattern

- **Description.** A residual is not assumed to be noise. It may be a fixed baseline, a cycling load, a duty-cycled appliance or an intermittent unknown; the system publishes the pattern it observes without naming a device for it.
- **Priority.** SHOULD
- **Rationale.** The baseline/dynamic split already distinguishes "always there" from "moving" (`docs/residual_analysis.md` §3), and `ResidualTrend` carries `cause_counts`, `sign_changes` and a confidence of `n / (n + 8)` (`docs/residual_analysis.md` §6).
- **Acceptance criteria.** The trend response publishes the current value, the typical value, the dynamic difference, the cause counts and the sign changes; the confidence is `None` when there is no history, never `0.0` or `1.0`.
- **Dependencies.** REQ-RESIDUAL-002
- **Scenario.** `T03` and `T10` — extends matrix group **F** (rows `F3`, `F7`); `T10` is **new coverage**.
- **Verification.** `pytest tests/unit/test_residual_power.py` — the trend assertions.

#### REQ-RESIDUAL-005 — Residual drift and change are detected and reported

- **Description.** A change in the residual's baseline, sign or volatility is detected and reported as a named event with its evidence; the previous residual history is not rewritten.
- **Priority.** SHOULD
- **Rationale.** "residual drift and change detection" is required by the target brief, and the residual trend already carries `sign_changes` and `cause_counts` (`docs/residual_analysis.md` §6) — the drift detection is a consumer of that, not a new source of truth.
- **Acceptance criteria.** A sustained baseline shift produces an event naming the scope, the direction and the magnitude; the shift does not rewrite previously stored residual points.
- **Dependencies.** REQ-RESIDUAL-003, REQ-EVENT-001
- **Verification.** `pytest tests/unit/test_residual_power.py` — a new assertion for the drift event.

#### REQ-RESIDUAL-006 — An unknown residual event is consumable by the learning system

- **Description.** A residual change that no device explains is emitted as an event that the learning pipeline can accept, train on or reject with a reason, exactly like a device event.
- **Priority.** SHOULD
- **Rationale.** The learning pipeline's entry point takes events and a window and returns candidates with rejection reasons (`engine/wattmosaic_engine/learning/pipeline.py:273`); the residual module already exposes a decomposition that names candidate contributors (`docs/residual_analysis.md` §5).
- **Acceptance criteria.** An unexplained residual change appears in the learning ledger with an accepted/rejected decision and a reason code; it never becomes a device.
- **Dependencies.** REQ-EVENT-001, REQ-LEARNING-002
- **Scenario.** `T10` — **new coverage** (no existing row asserts an unknown-residual event reaching the learning ledger).
- **Verification.** `pytest tests/unit/test_learning.py` — a new assertion that an unknown-residual event reaches the ledger.

#### REQ-RESIDUAL-007 — A residual is never redistributed onto devices

- **Description.** Unknown consumption is preserved and reported; it is never allocated to a device, never used to raise a device's estimate and never written back into a device's attribution.
- **Priority.** MUST
- **Rationale.** "A residual is never redistributed onto devices. That would invent attribution the evidence does not support." (`docs/residual_analysis.md` §9); enforced in contract and tests (`docs/assessment_gap_analysis.md` §"What must NOT be rebuilt").
- **Acceptance criteria.** No device's `power_w` changes because a residual was distributed; the unknown-load slack is never written into a device row; the invariant is asserted by a test that fails if the guard is removed.
- **Dependencies.** REQ-DISAGG-005, REQ-RESIDUAL-008
- **Scenario.** `T04` — extends matrix group **F** (rows `F1`, `F2`) and matrix group **E** (row `E6`).
- **Verification.** `pytest tests/integration/test_safety_invariants.py` — the residual-never-redistributed assertion; negative verification per `tools/dev/negverify_nesting.py`.

#### REQ-RESIDUAL-008 — An unresolved residual never renders as zero

- **Description.** A residual that has never been explained is reported as unresolved with its age; it is never presented as `0 W` and never aggregated with absent data.
- **Priority.** MUST
- **Rationale.** "`resolved=False` is a **valid, possibly permanent outcome** … and it is never rendered as `0 W`" (`docs/residual_analysis.md` §4); "No history is not `0 W`" (§5).
- **Acceptance criteria.** A scope with no residual history reports `quality_status=MISSING` and a `None` value, never `0.0`; an unresolved residual carries `unresolved_since`.
- **Dependencies.** REQ-RESIDUAL-001
- **Verification.** `pytest tests/unit/test_residual_power.py` — the `resolved=False` assertions.

### 3.5 Disaggregation

#### REQ-DISAGG-001 — Inference is joint over every available source

- **Description.** One inference cycle uses, together: HA state changes, HA attribute changes, smart-plug measurements, CT measurements, the topology, residual events and learned models.
- **Priority.** MUST
- **Rationale.** The engine already receives the window, the device states, the model registry and the topology in one call (`docs/interface_contracts.md` §3.4) and the audit pass combines association, circuit, residual and hierarchy evidence (`docs/assessment_gap_analysis.md` §11, G8–G13).
- **Acceptance criteria.** The inference request that produces a result names every source it used, and a source that was available but unused is named with the reason.
- **Dependencies.** REQ-EVENT-001, REQ-TOPO-011
- **Verification.** `pytest tests/end_to_end/test_full_inference_chain.py` — a new assertion on the source list.

#### REQ-DISAGG-002 — Simultaneous device changes are handled without a fabricated split

- **Description.** When more than one device changes inside one window, the system either separates them with independent evidence or reports the group as not separable; it never divides the power by assumption.
- **Priority.** MUST
- **Rationale.** "Two devices on one branch with both ON is *not* identifiable … Reporting 250/250 would be fabrication" (`docs/electrical_model.md` §6); "no primitive that attributes a *shared* power change to two devices at once" (`docs/limitations_v2.md` §2).
- **Acceptance criteria.** A window with two co-toggling devices yields a non-identifiable status for the pair; no per-device number is published for them from that window.
- **Dependencies.** REQ-DISAGG-004
- **Scenario.** `T06` and `T07` — extends matrix groups **G** (rows `G2`, `G5`) and **I** (rows `I1`, `I4`); **new coverage** for the simultaneous-change joint inference.
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated`; matrix group `I`.

#### REQ-DISAGG-003 — Identifiability is computed and published

- **Description.** Each inference publishes the rank, the null-space dimension, the condition number, the uniquely identifiable devices, the unidentifiable ones and the ambiguous groups.
- **Priority.** MUST
- **Rationale.** The null-space analysis and its published diagnostics already exist (`docs/electrical_model.md` §6; `analyse_identifiability` at `engine/wattmosaic_engine/electrical/solver.py:136`).
- **Acceptance criteria.** The inference batch diagnostics carry the identifiability block; a device whose null share exceeds the threshold is listed as unidentifiable rather than being given a value.
- **Dependencies.** REQ-CONFIDENCE-005
- **Scenario.** `T07` — extends matrix group **I** (rows `I1`, `I4`) and matrix group **F** (row `F8`).
- **Verification.** `pytest tests/unit/test_electrical.py::test_identifiability_rank_and_ambiguous_group`.

#### REQ-DISAGG-004 — Where separation is impossible, a range or a group estimate is published

- **Description.** An inseparable set of devices is published as a group estimate with a minimum and a maximum expected contribution and an explicit non-identifiable status.
- **Priority.** MUST
- **Rationale.** "when two loads cannot be separated, report a group estimate, a range, or a non-identifiable status" (target invariant, `README.md` §5); the solver's unknown slack is the mechanism (`augment_with_unknown_slack`, `engine/wattmosaic_engine/electrical/solver.py:258`).
- **Acceptance criteria.** An ambiguous group publishes `min_w`, `max_w` and a status naming the members; the members do not carry individual `power_w` values for that window.
- **Dependencies.** REQ-DISAGG-005
- **Scenario.** `T07` — extends matrix group **I** (rows `I1`, `I4`); **new coverage** for the published min/max range.
- **Verification.** `pytest tests/unit/test_electrical.py::test_identifiability_rank_and_ambiguous_group`; a new assertion on the range fields.

#### REQ-DISAGG-005 — A per-device number is never fabricated

- **Description.** No code path produces a device power value where the evidence does not support one; the honest outputs are "unknown", a group estimate, or a refusal with a machine reason code.
- **Priority.** MUST
- **Rationale.** "Never fabricate a per-device number" is a core invariant (`README.md` §5); "Unknown over fabricated is the product's core promise" (`docs/assessment_gap_analysis.md` §"What must NOT be rebuilt").
- **Acceptance criteria.** Every published device value has a `source`, a `quality_status` and a `confidence_tier`; a device with no supporting evidence is published as `Source.UNKNOWN` with `TIER_0_UNKNOWN` and no watt figure presented as a measurement.
- **Dependencies.** REQ-FOUNDATION-005, REQ-CONFIDENCE-004
- **Scenario.** `T07` — extends matrix group **I** (row `I4`) and matrix group **F** (row `F8`).
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_12_missing_sensor_samples_do_not_abort_or_fabricate`; `pytest tests/unit/test_electrical.py::test_unobserved_device_is_listed_not_silently_zero`.

#### REQ-DISAGG-006 — One physical load seen by several nested readers is counted once

- **Description.** When a load is visible to more than one reader, the attribution uses exactly one — the deepest enclosing branch reader — and the ancestors contribute constraint, not a second copy.
- **Priority.** MUST
- **Rationale.** The double-counting rule is stated verbatim in `docs/electrical_model.md` §1.1 and is the reason the matrix carries `nested_children` (`engine/wattmosaic_engine/electrical/matrix.py:97`).
- **Acceptance criteria.** The attributed channel list of a device contains one reader; the ancestors carry the child in `nested_children` and the child's value is subtracted from the ancestor's target.
- **Dependencies.** REQ-TOPO-003, REQ-TOPO-007
- **Scenario.** `T06` — extends matrix groups **C** (row `C5`) and **F** (row `F10`).
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_07_nested_branch_ct_does_not_double_count`; `pytest tests/unit/test_electrical.py::test_nested_main_sums_only_the_outermost_feed`.

#### REQ-DISAGG-007 — The home-level balance identity holds exactly

- **Description.** For every published reconciliation, `accounted + unknown_residual_w == main_power_w` and `unmonitored_w + meter_disagreement_w == unknown_residual_w`, with the signed residual preserved.
- **Priority.** MUST
- **Rationale.** Both identities are contractual (`engine/wattmosaic_engine/contracts.py:1714-1718`; `docs/electrical_model.md` §4.2.4) and `meter_spread_w` is measured, never guessed (`docs/electrical_model.md` §4.2.4).
- **Acceptance criteria.** Both identities hold for every row the engine writes, including when the residual is negative; the reconciliation carries `meter_spread_w` and `meter_spread_exceeds_residual`.
- **Dependencies.** REQ-RESIDUAL-001, REQ-MEASURE-006
- **Verification.** `pytest tests/unit/test_reconciliation_split.py`; `pytest tests/unit/test_reconciliation_persistence.py`.

### 3.6 Device and load models

#### REQ-POWER-001 — A device's power model has one named class

- **Description.** Every device carries a power-model class — `FIXED`, `ADJUSTABLE`, `DYNAMIC`, `DELAYED`, `ALWAYS_ON` or `UNKNOWN` — and the class determines how the device's power is derived.
- **Priority.** MUST
- **Rationale.** The shipped taxonomy has the orthogonal axes `HaConnectivity`, `MonitoringLevel` and `PowerBehavior` (`contracts.py:339-385`) and the model registry's `ModelType` covers `BINARY`, `DIMMABLE`, `VARIABLE_SPEED`, `MULTI_STATE`, `COMPOSITE`, `TIME_VARYING`, `RESIDUAL` (`contracts.py:90-98`). The target class set is the load-shape view of the same thing and must not collapse the three axes.
- **Acceptance criteria.** A device's class is published with its power estimate; a device with no learned model is `UNKNOWN`; no class is inferred from a name.
- **Dependencies.** REQ-FOUNDATION-005
- **Scenario.** `T08` — extends matrix group **A** (row `A5`, dynamic power) and matrix group **D** (row `D5`); **new coverage** for the adjustable class.
- **Verification.** `pytest tests/unit/test_device_taxonomy.py`; `pytest tests/unit/test_models.py`.

#### REQ-POWER-002 — Standby, idle, active and transient power are distinct

- **Description.** A device's power profile distinguishes the standby (off but connected) level, the idle level, the running level and the start-up transient; a value is labelled with the state it belongs to.
- **Priority.** MUST
- **Rationale.** `ExpectedPower` carries `on_w`, `idle_w`, `standby_w`, `startup_w`, `shutdown_w` (`contracts.py:1249-1263`) and `DeviceModelParameters.startup_power_w` exists (`contracts.py:1582-1598`) but is unused by the simulator (`docs/assessment_gap_analysis.md` G19).
- **Acceptance criteria.** A published device power names which of the four levels it is; a start-up transient is excluded from a steady-state model fit and from the energy integral.
- **Dependencies.** REQ-POWER-003, REQ-LEARNING-003
- **Scenario.** `T04` — extends matrix group **F** (rows `F1`, `F2`) and matrix group **A** (row `A7`, standby).
- **Verification.** `pytest tests/unit/test_models.py::test_binary_model_median_ignores_inrush_outlier`; a new assertion that the inrush does not enter the energy integral.

#### REQ-POWER-003 — State-based models use the measured power difference

- **Description.** For a state-based model the attributed power of a transition is the difference between the power after the transition and the power before it, `ΔP = P_new − P_old`, computed from the same reader.
- **Priority.** MUST
- **Rationale.** The anchored step rescan is "the single biggest accuracy mechanism" (`docs/assessment_gap_analysis.md` §"What must NOT be rebuilt"); a step is attributed, not the whole row (ADR-0026).
- **Acceptance criteria.** A device that turns on is credited with the measured step, not with the row total; a device that turns off is credited with the negative step; a stale or missing partner reading suppresses the step with a named reason.
- **Dependencies.** REQ-EVENT-003, REQ-TOPO-004
- **Scenario.** `T01` — extends matrix group **A** (row `A4`, ON/OFF-only device) and matrix group **C** (row `C1`).
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_01_single_device_activation`; `pytest tests/unit/test_disaggregation.py::test_02_single_device_deactivation`; `pytest tests/unit/test_device_attachment.py::test_single_on_claimant_owns_the_whole_row`.

#### REQ-POWER-004 — An OFF device may still consume standby power

- **Description.** A device reported as off may have a non-zero standby draw; the system preserves that draw and never treats "off" as "zero watts".
- **Priority.** MUST
- **Rationale.** "a smart bulb's *radio* still draws power while the light is off" (`contracts.py:1283-1285`); standby preservation is covered today (`docs/test_scenario_matrix.md` row A7).
- **Acceptance criteria.** A configured or learned standby level is published for an off device; the reconciliation counts it once; it is not attributed to a sibling.
- **Dependencies.** REQ-POWER-002
- **Scenario.** `T04` — extends matrix group **A** (row `A7`, standby) and matrix group **D** (row `D4`, dedicated circuit with standby).
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_17_standby_power_is_preserved`; `pytest tests/unit/test_device_attachment.py::test_stated_standby_is_allowed_while_off`.

#### REQ-POWER-005 — A device with no Home Assistant integration is supported

- **Description.** A device that has no HA entity — only a reader observing it — is a first-class device with `HaConnectivity.NOT_CONNECTED`, and its power comes from measurement and learning alone.
- **Priority.** MUST
- **Rationale.** The three orthogonal axes exist precisely so that "a non-smart garage opener can still sit on one circuit CT and behave as a fixed-watt load" (`contracts.py:331-347`; ADR-0011).
- **Acceptance criteria.** A configured device with no state entity is published with its measured or modelled power and an honest confidence; it is not dropped for lack of an entity.
- **Dependencies.** REQ-POWER-001, REQ-CONFIDENCE-003
- **Scenario.** `T10` — **new coverage** (no existing row asserts a device with no HA entity).
- **Verification.** `pytest tests/unit/test_device_taxonomy.py`; `pytest tests/unit/test_device_attachment.py`.

#### REQ-POWER-006 — The five entities are never conflated

- **Description.** The physical device, the HA entity, the measurement endpoint (reader), the load component and the unknown-load profile are five distinct things with distinct identifiers, and no API field merges two of them.
- **Priority.** MUST
- **Rationale.** "Many entities, one appliance" is a contract, not a convention (`HaEntityRole`, `contracts.py:281-304`); at most one binding per device may contribute power (`contracts.py:660-683`).
- **Acceptance criteria.** An API response for a device names the entity ids that describe it, the reader that observes it and the load components it comprises; a single field cannot carry two of those identities.
- **Dependencies.** REQ-MAPPING-001
- **Verification.** `pytest tests/unit/test_device_links.py`; `pytest tests/unit/test_contracts.py` — the one-contributor rule.

#### REQ-POWER-007 — A declared wattage bounds a shared row and is never relaxed

- **Description.** A user-declared wattage is an upper bound on what that device may be credited on a shared row; a row that cannot pay the declared bound is reported as binding and unexplained, not silently relaxed.
- **Priority.** MUST
- **Rationale.** "a declared wattage is a non-relaxable upper bound on a shared row" (target invariant, `README.md` §5); the guard that bounds the **sum** of the priors on a row by the row's own usable reading is implemented and documented (`docs/limitations_v2.md` §10.7).
- **Acceptance criteria.** The sum of the priors on a row never exceeds that row's usable reading; a surplus is reported as unexplained; a stale or reverse-flow row skips the guard rather than substituting a wrong number.
- **Dependencies.** REQ-DISAGG-007
- **Verification.** `pytest tests/unit/test_device_attachment.py::test_configured_priors_cannot_exceed_the_row_they_share` and its four siblings.

#### REQ-POWER-008 — Model versions are kept, promoted and reversible

- **Description.** Every model version is stored with its parameters, its validation metrics, its status and its algorithm version; a model can be frozen, promoted, rejected or rolled back, and the history is never rewritten.
- **Priority.** MUST
- **Rationale.** `DeviceModelVersion` and `ModelStatus` exist (`contracts.py:100-108`, `:1599-1618`); `Database.rollback_model` exists (`engine/wattmosaic_engine/storage/database.py:1213`).
- **Acceptance criteria.** `GET /models/{device_id}` lists the versions with their statuses; a rollback activates an earlier version without deleting the newer one; the active model version is named on every model-estimated result.
- **Dependencies.** REQ-STORAGE-005
- **Verification.** `pytest tests/unit/test_learning.py::test_candidate_promotes_only_when_it_beats_active_model`; `pytest tests/unit/test_models.py::test_registry_round_trips_persisted_model_and_estimates`.

### 3.7 Storage and lifecycle

#### REQ-STORAGE-001 — Every persisted artefact belongs to a named tier

- **Description.** Persisted data is classified into four named tiers — L1 high-resolution raw, L2 immutable event evidence, L3 aggregated measurements and L4 versioned models and topology — and every table declares its tier.
- **Priority.** MUST
- **Rationale.** The gap is real: there is no lifecycle tier concept, only raw tables and aggregate tables, and seven audit tables grow for the life of the file (`G25`; `docs/limitations_v2.md` §7.2).
- **Acceptance criteria.** A single registry names every table and its tier; the API publishes the classification; the tier is what the retention policy reads.
- **Dependencies.** —
- **Verification.** `pytest tests/unit/test_storage_config.py` — a new assertion that every table is classified.

#### REQ-STORAGE-002 — Retention is configurable per tier and defaults are stated

- **Description.** L1 raw retention is configurable in the range 7–14 days; L2 event evidence and L3 aggregates are retained long-term; L4 models and topology are retained indefinitely unless an operator prunes them explicitly.
- **Priority.** MUST
- **Rationale.** The shipped defaults are `WM_RETENTION_DAYS_RAW=30` and `WM_RETENTION_DAYS_AGGREGATES=730` (`addon/run.sh` option table); the target brief names 7–14 days for raw and long-term retention for aggregates and evidence. The difference is recorded as open decision `OD-03` (`00-project-overview.md` §12).
- **Acceptance criteria.** `WM_RETENTION_DAYS_RAW` is accepted in 7–14 and the default is stated in the option table; a value outside the range is refused at startup; the aggregate and evidence horizons are not shortened by the raw change.
- **Dependencies.** REQ-STORAGE-001
- **Verification.** `pytest tests/unit/test_storage_config.py`; `bash scripts/validate_addon.sh --no-build` — the option table agrees with `engine/wattmosaic_engine/config/settings.py`.

#### REQ-STORAGE-003 — Event evidence is immutable

- **Description.** L2 evidence — inference records, isolation verdicts, relation history, residual observations, factor intervals — is append-only and is never updated in place, except for a documented idempotent upsert keyed by a stable event identity.
- **Priority.** MUST
- **Rationale.** `InferenceRecord` is "a versioned, append-only inference record. A revision never overwrites its predecessor" (`contracts.py:870-877`); the isolation verdict is upserted by `event_id` so re-analysis does not grow the table (`docs/limitations_v2.md` §7.2).
- **Acceptance criteria.** Re-running an analysis over the same events does not change an existing evidence row's content; a new revision appends a new record with a strictly greater revision number.
- **Dependencies.** REQ-STORAGE-005
- **Verification.** `pytest tests/unit/test_inference_records.py`; `pytest tests/unit/test_isolation_wiring.py`.

#### REQ-STORAGE-004 — Writes are batched

- **Description.** Per-cycle writes are batched into a single transaction per flush, and the flush interval is configurable.
- **Priority.** MUST
- **Rationale.** The engine already batches through a writer loop and a persist queue (`engine/wattmosaic_engine/main.py:966`, `:988`); the write-amplification risk is registered as R8 (`docs/risks.md`).
- **Acceptance criteria.** A 5-minute window of samples produces one batched insert per table per flush; the number of transactions is observable in the metrics.
- **Dependencies.** REQ-STORAGE-006
- **Verification.** `pytest tests/unit/test_storage_config.py`; `pytest tests/unit/test_health_probe_cost.py`.

#### REQ-STORAGE-005 — A new model or topology version never deletes historical evidence

- **Description.** Publishing a new model version, a new topology version or a new mapping version adds rows and never removes or rewrites the rows that describe earlier versions.
- **Priority.** MUST
- **Rationale.** "an audit trail that evaporates in 30 days cannot audit" (`docs/limitations_v2.md` §7.2); `G16` rates the retention-safe versioned record as MISSING; `Database.prune` already exempts the seven `AUDIT_TABLES` and reports each with a count of `0` (`engine/wattmosaic_engine/storage/database.py:1304-1344`; `engine/wattmosaic_engine/storage/migrations.py:121-134`).
- **Acceptance criteria.** After a model promotion, the superseded version's rows are still readable through the API and the export; `prune` reports the exempt tables with a count of `0` and leaves them untouched.
- **Dependencies.** REQ-STORAGE-001, REQ-STORAGE-011
- **Scenario.** `T10` — **new coverage** (no existing row asserts that a discovered profile version leaves the earlier evidence intact).
- **Verification.** `pytest tests/unit/test_storage_config.py` — the retention exemption assertion; `pytest tests/unit/test_inference_records.py`.

#### REQ-STORAGE-006 — Time and entity access paths are indexed

- **Description.** Every table queried by time or by entity carries an index for that access path, and the index set is declared with the table.
- **Priority.** MUST
- **Rationale.** The shipped schema indexes `ct_samples(ct_id, received_at)`, `ct_samples(received_at)`, `inference_results(device_id, timestamp)` and `inference_results(timestamp)` (`engine/wattmosaic_engine/storage/migrations.py:331-333`, `:381-382`).
- **Acceptance criteria.** `EXPLAIN QUERY PLAN` for the time-range and entity-range access paths names an index rather than a full scan.
- **Dependencies.** REQ-STORAGE-001
- **Verification.** `pytest tests/unit/test_storage_config.py` — a new query-plan assertion.

#### REQ-STORAGE-007 — Records carry the versions that produced them

- **Description.** Every stored inference record names the `algorithm_version`, the `contract_version`, the `topology_version` and the `model_version` it was produced under.
- **Priority.** MUST
- **Rationale.** `InferenceRecord` carries `algorithm_version`, `contract_version`, `topology_version` and `revision` (`contracts.py:887-896`); the storage columns are created in the migration (`engine/wattmosaic_engine/storage/migrations.py:666-690`).
- **Acceptance criteria.** A stored record exposes all four identifiers; a record written under an older contract version still loads.
- **Dependencies.** —
- **Verification.** `pytest tests/unit/test_inference_records.py`.

#### REQ-STORAGE-008 — Payloads are compressed where it pays

- **Description.** Large JSON payloads (topology snapshots, evidence blobs, export bundles) are compressed at rest, and the compression is transparent to every reader.
- **Priority.** SHOULD
- **Rationale.** `topology_versions.payload_json` and the evidence columns store whole JSON documents (`engine/wattmosaic_engine/storage/migrations.py:308-315`); the raw sample table stores one row per channel per cadence and dominates the file.
- **Acceptance criteria.** A stored payload is compressed and readable through the same API; a legacy uncompressed payload still loads.
- **Dependencies.** REQ-STORAGE-001
- **Verification.** `pytest tests/unit/test_storage_config.py` — a new round-trip assertion across both encodings.

#### REQ-STORAGE-009 — Partitioning is used only where it is justified

- **Description.** A table is partitioned only when a measured query or size analysis shows the unpartitioned access path is the bottleneck; no table is partitioned by default.
- **Priority.** MAY
- **Rationale.** SQLite has no native partitioning, and the single-writer design makes a per-partition-file scheme a distributed-storage change in disguise (`docs/implementation_plan_v2.md` §0; `docs/limitations_v2.md` §7.1).
- **Acceptance criteria.** If a table is partitioned, the decision is recorded with the measurement that justified it; otherwise the requirement is satisfied by doing nothing.
- **Dependencies.** REQ-STORAGE-006
- **Verification.** Manual review against the design note published with this specification.

#### REQ-STORAGE-010 — The engine recovers after a restart

- **Description.** After an engine restart the schema is at the current version, the topology, models and configuration are reloaded, the last energy bucket is recomputed and no already-persisted evidence is lost.
- **Priority.** MUST
- **Rationale.** "Engine restarts | Migrations idempotent; topology/models reloaded; last energy bucket recomputed" (`docs/architecture.md` §5); the startup integrity check refuses to run on corruption (§5).
- **Acceptance criteria.** A restart over the same database file preserves every record count; `PRAGMA integrity_check` runs at startup and a corrupt file refuses to start rather than losing data.
- **Dependencies.** REQ-STORAGE-011
- **Verification.** `pytest tests/end_to_end/test_storage_e2e.py`; `pytest tests/unit/test_app_startup.py`.

#### REQ-STORAGE-011 — Migrations are forward-only, additive and idempotent

- **Description.** A schema change is a new numbered migration that only adds; an already-released migration is never edited; applying migrations twice is a no-op.
- **Priority.** MUST
- **Rationale.** "Migration discipline (forward-only, additive, idempotent helpers)" is in the "must NOT be rebuilt" list (`docs/assessment_gap_analysis.md`); the current schema version is 11 (`engine/wattmosaic_engine/storage/migrations.py:792`).
- **Acceptance criteria.** A database at version *n* opens and migrates to *n+1* with no data loss; running the migration twice changes nothing; the migrated database loads and runs.
- **Dependencies.** REQ-FOUNDATION-003
- **Verification.** `pytest tests/unit/test_migrations.py`; `pytest tests/unit/test_reconciliation_persistence.py::test_migration_7_adds_the_split_columns_in_place`.

#### REQ-STORAGE-012 — A stored window can be replayed

- **Description.** Any stored window of samples and events can be re-run through the production inference path and produces the same result as the run that produced it, under the same algorithm version.
- **Priority.** MUST
- **Rationale.** `tools/audit/replay_capture.py` already exists as "the bridge between the synthetic simulator and a real installation" and drives `Measurement` → `SampleAligner` → `DisaggregationEngine.infer` (`tools/audit/replay_capture.py:1-16`).
- **Acceptance criteria.** A replay of a stored window under the same algorithm version yields an identical `power_w`, `confidence` and reason per device; a replay under a different algorithm version is labelled as such rather than compared as equal.
- **Dependencies.** REQ-STORAGE-007
- **Scenario.** `T12` — extends matrix group **C** (row `C9`); **new coverage** for replay across a reader replacement.
- **Verification.** `pytest tests/simulation/test_replay_capture.py`; `python tools/audit/replay_capture.py --help` and a capture replay.

### 3.8 Event and inference

#### REQ-EVENT-001 — Every event is one of three kinds

- **Description.** An event is HA-driven (a state or attribute change), measurement-driven (a detected step in a reader) or unknown-residual (a change in the unexplained remainder); every event carries its kind.
- **Priority.** MUST
- **Rationale.** `DeviceEvent.event_type` already distinguishes `state_changed`, `power_change`, `cycle_start`, `stage_change`, `cycle_end` and `availability` (`contracts.py:1547-1569`), and the step detector produces measurement-driven events (`detect_step`, `engine/wattmosaic_engine/disaggregation/events.py`).
- **Acceptance criteria.** Every emitted event names its kind; an unknown-residual event names the scope it belongs to; no event is emitted without a kind.
- **Dependencies.** REQ-RESIDUAL-003
- **Scenario.** `T10` — **new coverage** for the unknown-residual event kind.
- **Verification.** `pytest tests/end_to_end/test_event_intake.py`; `pytest tests/unit/test_learning.py`.

#### REQ-EVENT-002 — An automation that changes several devices produces one group

- **Description.** A set of device changes caused by one automation is published as one event group; the group does not become several independent pieces of evidence for the same physical change.
- **Priority.** MUST
- **Rationale.** Co-toggling fixtures are "withheld not counted twice" (`docs/test_scenario_matrix.md` row G3); "perfectly collinear members collapse to one group" (row I1).
- **Acceptance criteria.** The members of one group are published together with the group identity; a device in a group of size > 1 is not credited with an independent step.
- **Dependencies.** REQ-DISAGG-002
- **Scenario.** `T06` — extends matrix groups **G** (row `G3`, automation multi-change) and **E** (row `E5`, overlapping schedules).
- **Verification.** `pytest tests/unit/test_correlation.py::test_fixtures_switched_by_one_scene_are_withheld_not_counted_twice`; `pytest tests/unit/test_group_fit.py::test_perfectly_collinear_members_collapse_to_one_group`; matrix group `G`.

#### REQ-EVENT-003 — A device's response delay is measured, not assumed

- **Description.** The delay between a device's state change and the reader's response is measured per device pair and searched over a configured maximum; a response beyond the maximum is withheld with a reason.
- **Priority.** MUST
- **Rationale.** "The response is searched over `0..max_lag_s`; late is not wrong, but a response at or beyond `max_lag_s` is withheld (`lag_exceeded`)" (`docs/association_and_topology.md` §2.2); `lag_step_s = 15.0` matches the audited cadence (§2.3).
- **Acceptance criteria.** A response at or beyond `max_lag_s` produces a `lag_exceeded` refusal, not a silent drop; the measured lag is published with the relation's evidence.
- **Dependencies.** REQ-EVENT-004
- **Scenario.** `T09` — extends matrix group **H** (row `H6`, delayed CT response) and matrix group **G** (row `G10`, configurable offsets); **new coverage** for the per-device measured lag.
- **Verification.** `pytest tests/unit/test_association.py`; `pytest tests/unit/test_state_lag.py`.

#### REQ-EVENT-004 — Event windows are sized from the observed cadence

- **Description.** The isolation window, the power window and the analysis offset are configurable per device class and per device, and the shipped defaults are consistent with the ~15.7 s audited cadence.
- **Priority.** MUST
- **Rationale.** `IsolationConfig` defaults are `analysis_offset_s = 0.0`, `isolation_window_s = 30.0`, `power_window_s = 60.0`, `min_baseline_samples = 3`, `min_event_samples = 3`, resolved per device > per class > global (`engine/wattmosaic_engine/disaggregation/isolation.py:228-277`; `docs/event_isolation.md` §1.1–1.2).
- **Acceptance criteria.** The resolved window and its source (`device`, `class`, `global`) are published with the verdict; the shipped default window admits at least two samples at the audited cadence; the window is wide enough to contain the delay measured under REQ-EVENT-003, so the delay requirement depends on this one and not the reverse.
- **Dependencies.** REQ-MEASURE-010
- **Verification.** `pytest tests/unit/test_event_isolation.py`; `pytest tests/unit/test_event_anchor.py`.

#### REQ-EVENT-005 — An event records its effect on every reader that saw it

- **Description.** For an event, the system publishes the per-reader effect it caused — the before/after level, the delta and the quality — as a vector, so a reader can see which readers responded and which did not.
- **Priority.** SHOULD
- **Rationale.** The gap is real: `DeviceEvent` is per-entity and carries no reader effect (`G30`; `contracts.py:1547-1569`); the correlation module already computes a per-CT delta (`_step`, `engine/wattmosaic_engine/ingestion/correlation.py:397`).
- **Acceptance criteria.** The event's published vector names each candidate reader, its delta and its quality; a reader that did not respond is named as non-responding rather than omitted.
- **Dependencies.** REQ-TOPO-004
- **Scenario.** `T01` and `T02` — extends matrix group **C** (rows `C1`, `C3`); **new coverage** for the published cross-reader vector.
- **Verification.** `pytest tests/unit/test_correlation.py` — a new assertion on the published vector.

#### REQ-EVENT-006 — Late-arriving data is classified, not dropped silently

- **Description.** A measurement or event that arrives after the window it belongs to is classified as late and either reclassified or retained with its original timestamps; it is never silently applied to the current window.
- **Priority.** MUST
- **Rationale.** `EventIsolator.reclassify(previous, late_event)` exists (`docs/event_isolation.md` §6.1); out-of-order sampling stays ordered (`docs/test_scenario_matrix.md` row G6).
- **Acceptance criteria.** A late event does not rewrite an already-published record; it is recorded with `received_at` after `occurred_at` and the classification says it was late.
- **Dependencies.** REQ-MEASURE-008, REQ-STORAGE-003
- **Scenario.** `T14` — extends matrix group **G** (row `G6`, out-of-order arrival) and matrix group **H** (row `H9`).
- **Verification.** `pytest tests/unit/test_synchronization.py::test_out_of_order_sampling_stays_ordered`; `pytest tests/unit/test_event_anchor.py::test_tracker_never_rewrites_history_for_out_of_order_cycles`.

#### REQ-EVENT-007 — A duplicate event is recognised and counted once

- **Description.** The same physical event delivered twice — a Home Assistant reconnect re-sending it, or two entities of one device — produces one event and one evidence contribution.
- **Priority.** MUST
- **Rationale.** "Device events carry `event_id` derived from `sha1(entity_id + last_updated + new_state)` so an HA reconnect that re-sends an event does not duplicate it (`INSERT OR IGNORE`)" (`docs/data_flow.md` §3); "many entities, one appliance" is a contract (`docs/event_isolation.md` §4). **Note:** the sha1 derivation is documented but is not the production construction — the production path builds a uuid4 event id (`engine/wattmosaic_engine/ingestion/ha_source.py:1186`) and idempotency is enforced in storage by the `event_id` primary key with `INSERT OR IGNORE` (`engine/wattmosaic_engine/storage/migrations.py:336`; `engine/wattmosaic_engine/storage/database.py:533`). The requirement is therefore written against the *observable* behaviour, not the documented derivation.
- **Acceptance criteria.** Re-delivering an event does not add a row; two load-state entities of one device firing together credit the device once.
- **Dependencies.** REQ-POWER-006
- **Scenario.** `T12` — extends matrix group **C** (row `C9`); **new coverage** for a duplicate arising from a reader replacement.
- **Verification.** `pytest tests/end_to_end/test_event_intake.py`; `pytest tests/unit/test_state_entity_mapping.py::test_state_episodes_collapses_attribute_updates`; matrix group `G`.

#### REQ-EVENT-008 — An incomplete event group is a valid outcome

- **Description.** When the evidence for an event group is incomplete — one leg missing, a partner stale, a window too short — the group is published as incomplete with the missing part named, and no device is credited from it.
- **Priority.** MUST
- **Rationale.** `INSUFFICIENT_DATA` is one of the three isolation statuses (`contracts.py:307-326`) and "absence of data is never promoted to `ISOLATED`" (`docs/event_isolation.md` preamble).
- **Acceptance criteria.** An incomplete group names the missing reader or the missing sample count; no result is derived from it; the reason code is published.
- **Dependencies.** REQ-MEASURE-005, REQ-DISAGG-005
- **Scenario.** `T14` — extends matrix group **C** (row `C8`, missing samples) and matrix group **H** (row `H9`).
- **Verification.** `pytest tests/unit/test_event_isolation.py`; `pytest tests/unit/test_disaggregation.py::test_12_missing_sensor_samples_do_not_abort_or_fabricate`.

#### REQ-EVENT-009 — Observed time and ingestion time are both preserved

- **Description.** Every event and every measurement keeps the time the observation describes and the time the engine received it, separately and immutably.
- **Priority.** MUST
- **Rationale.** `EventIsolation` carries `occurred_at` (never mutated) and `received_at` (`contracts.py:1006-1008`, `:1008-1010`); "the original `occurred_at` is carried unchanged" is the module's own invariant.
- **Acceptance criteria.** The isolation verdict's `occurred_at` equals the event's `occurred_at`; the analysis offset shifts the window and never the event time.
- **Dependencies.** REQ-MEASURE-008
- **Verification.** `pytest tests/unit/test_state_lag.py::test_transition_is_not_backdated_by_the_state_lag`; `pytest tests/unit/test_event_isolation.py`.

### 3.9 Learning

#### REQ-LEARNING-001 — Learning samples come from isolated events inside a window

- **Description.** A learning sample is created from an isolated device event together with the reader window around it; a contaminated or incomplete event produces no sample.
- **Priority.** MUST
- **Rationale.** "Only isolated events are evidence. A contaminated event is counted as `contaminated_event` and excluded" (`docs/association_and_topology.md` §2.2); the pipeline's entry point is `consider(events, window)` (`engine/wattmosaic_engine/learning/pipeline.py:273`).
- **Acceptance criteria.** A sample's supporting readers, baseline, event power and delta are the values the window held; no sample is created from a contaminated event.
- **Dependencies.** REQ-EVENT-004, REQ-MEASURE-009
- **Verification.** `pytest tests/unit/test_learning.py`; `pytest tests/unit/test_event_isolation.py`.

#### REQ-LEARNING-002 — Every rejection keeps its reason

- **Description.** A learning sample that is not accepted is stored with a machine reason code, and the reason is published.
- **Priority.** MUST
- **Rationale.** "rejected ones retain a reason code" (`docs/architecture.md` §3 step 8); `SampleRejection` enumerates ten reasons (`contracts.py:109-120`).
- **Acceptance criteria.** Every rejected sample has a non-empty reason from the enumeration; the ledger reports counts by reason.
- **Dependencies.** REQ-LEARNING-001
- **Verification.** `pytest tests/unit/test_learning.py`; `pytest tests/unit/test_learning_reporting.py`.

#### REQ-LEARNING-003 — A model is trained, validated and promoted, never auto-activated

- **Description.** A trained model becomes a candidate, is validated against a hold-out, and is promoted only when it beats the active model by the configured margin; the promotion decision is published.
- **Priority.** MUST
- **Rationale.** "Validation and promotion" is a pipeline stage (`docs/WATTMOSAIC_MASTER_INSTRUCTIONS.md` §11.4) and "Model overfitting on small sample counts" is risk R10 with validation and manual review as the mitigation (`docs/risks.md`).
- **Acceptance criteria.** A candidate that does not beat the active model is not activated; the decision carries the metrics that produced it; the active version can be frozen and rolled back.
- **Dependencies.** REQ-POWER-008, REQ-LEARNING-002
- **Verification.** `pytest tests/unit/test_learning.py::test_candidate_promotes_only_when_it_beats_active_model`; `pytest tests/unit/test_learning.py::test_compare_models_requires_margin_and_respects_mae_guard`.

#### REQ-LEARNING-004 — The learning pipeline consumes unknown-residual events

- **Description.** An unexplained residual change enters the learning pipeline as an event source, so a recurring unknown pattern can be characterised without being attached to a device.
- **Priority.** SHOULD
- **Rationale.** See REQ-RESIDUAL-006; the decomposition already names candidate contributors (`docs/residual_analysis.md` §5).
- **Acceptance criteria.** An unknown-residual event appears in the ledger; a recurring pattern produces a residual profile, not a device model.
- **Dependencies.** REQ-RESIDUAL-006, REQ-RESIDUAL-007
- **Scenario.** `T10` — **new coverage** (no existing row asserts an unknown-residual event entering the learning ledger).
- **Verification.** `pytest tests/unit/test_learning.py` — a new assertion.

#### REQ-LEARNING-005 — A factor is never applied retroactively

- **Description.** A factor in force at the time of a reading is resolved against the reading's timestamp; today's factor is never applied to a historical sample.
- **Priority.** MUST
- **Rationale.** "today's factor is never applied to a past sample" (`contracts.py:591`); `MeasurementFactor` carries an interval and `validate_factor_intervals` rejects overlaps (`contracts.py:585-657`).
- **Acceptance criteria.** A reading taken before a factor change is scaled with the earlier factor; a reading with no covering interval is `UNKNOWN` basis, never `1.0` by default.
- **Dependencies.** REQ-MEASURE-001, REQ-CHANGE-005
- **Verification.** `pytest tests/unit/test_factors.py`; `pytest tests/unit/test_measurement_factors.py`; matrix rows `B4`, `B9`.

#### REQ-LEARNING-006 — Thresholds may be overridden per device

- **Description.** The global learning thresholds may be overridden per device through the effective configuration, with the resolution source published.
- **Priority.** SHOULD
- **Rationale.** `G20` rates per-device thresholds MISSING and notes "All constants global" (`docs/assessment_gap_analysis.md` §11); `EffectiveConfig` is the existing resolution point (`engine/wattmosaic_engine/disaggregation/configuration.py:107-148`).
- **Acceptance criteria.** A per-device override changes the threshold for that device only; the resolved source is published with the outcome.
- **Dependencies.** REQ-EVENT-004
- **Verification.** `pytest tests/unit/test_learning.py` — a new assertion.

#### REQ-LEARNING-007 — A model never outranks a measurement

- **Description.** When a reader measured a device's row and the learned model disagrees, the measurement wins, the conflict is recorded, and the window is not used to retrain the model.
- **Priority.** MUST
- **Rationale.** "the engine appends `measurement_disagrees_with_model` … but `grep` finds **no test** referencing that flag" (`docs/test_scenario_matrix.md` row F9); ADR-0026 attributes the measured step, not the row.
- **Acceptance criteria.** The result's explanation carries the conflict flag; the model's confidence is capped for that window; the learning ledger shows the window was not used for training.
- **Dependencies.** REQ-POWER-003, REQ-LEARNING-003
- **Verification.** `pytest tests/unit/test_learning.py` — a new assertion referencing `measurement_disagrees_with_model`.

### 3.10 Confidence and identifiability

#### REQ-CONFIDENCE-001 — Confidence is never blended into one score

- **Description.** The system publishes independent confidence dimensions and offers no aggregate, no weighted blend and no `float()` conversion.
- **Priority.** MUST
- **Rationale.** "The model has **no aggregate field, no `__float__` and no arithmetic**" (`docs/confidence_model.md` §1); "a system that answers '0.87' to *how sure are you?* cannot be audited" (same document, preamble).
- **Acceptance criteria.** The `ConfidenceVector` model has no aggregate field and no arithmetic operators; a consumer that wants one number must name the dimension; no API response contains a blended confidence field.
- **Dependencies.** REQ-CONFIDENCE-003
- **Verification.** `pytest tests/unit/test_contracts.py` — the "no `__float__`" assertion; `pytest tests/unit/test_confidence_model` equivalent in `tests/unit/test_electrical.py::test_confidence_penalties_reduce_score`.

#### REQ-CONFIDENCE-002 — Confidence is never presented as a calibrated probability

- **Description.** Every published confidence is an engineering score; `confidence_is_calibrated` stays `false`, and no surface describes a confidence as a probability or a percentage of correctness.
- **Priority.** MUST
- **Rationale.** "**These are not probabilities.** Nothing in the codebase converts them to a frequency" (`docs/confidence_model.md` §6); the field exists and defaults to `false` (`contracts.py:1669`).
- **Acceptance criteria.** No API field or UI label expresses a confidence as a probability; the field `confidence_is_calibrated` is present and `false`.
- **Dependencies.** REQ-CONFIDENCE-001
- **Verification.** `pytest tests/unit/test_contracts.py`; `pytest tests/unit/test_panel.py` — no percentage rendering of a confidence.

#### REQ-CONFIDENCE-003 — The published dimensions are reconciled with the existing vector

- **Description.** The target's confidence dimensions map onto the existing `ConfidenceVector` members — `power`, `ct_association`, `topology`, `circuit_classification`, `residual` — and `None` remains distinct from `0.0`.
- **Priority.** MUST
- **Rationale.** The five dimensions are the shipped contract (`engine/wattmosaic_engine/contracts.py:476-513`); "A reader must not read absence as `0.0`" (`docs/confidence_model.md` §2).
- **Acceptance criteria.** Every published vector uses only the five existing member names; a dimension that was not assessed is `null` in the payload; `dimensions()` returns only the assessed ones.
- **Dependencies.** —
- **Verification.** `pytest tests/unit/test_contracts.py`; `pytest tests/unit/test_inference_records.py`.

#### REQ-CONFIDENCE-004 — The evidence tier remains the coarse grade

- **Description.** Every device result carries a `ConfidenceTier` from `TIER_0_UNKNOWN` to `TIER_4_VERIFIED`, and the tier is monotone with the evidence, not a decoration.
- **Priority.** MUST
- **Rationale.** ADR-0013 grades evidence; `ConfidenceTier` and `TIER_RANK` exist (`contracts.py:423-456`) and `evidence_tier` computes it (`engine/wattmosaic_engine/disaggregation/tiers.py:126`).
- **Acceptance criteria.** A result's tier follows the source, the monitoring level and the corroboration; a tier never decreases without a recorded reason.
- **Dependencies.** REQ-CONFIDENCE-003
- **Verification.** `pytest tests/unit/test_electrical.py::test_confidence_bounds_and_direct_dominance`; `pytest tests/unit/test_electrical.py::test_confidence_penalties_reduce_score`.

#### REQ-CONFIDENCE-005 — The topology and residual dimensions are actually populated

- **Description.** The `topology` and `residual` dimensions of the confidence vector carry a value whenever the evidence to derive them exists, and remain `None` when it does not.
- **Priority.** MUST
- **Rationale.** "**never populated by the audit pass** … always" for both dimensions (`docs/confidence_model.md` §2; `docs/limitations_v2.md` §8) — two declared-but-dead dimensions.
- **Acceptance criteria.** A record produced from a confirmed topology carries a `topology` dimension; a record produced from a residual observation carries a `residual` dimension; a record with no hierarchy evidence carries `null`, not `0.0`.
- **Dependencies.** REQ-TOPO-011, REQ-RESIDUAL-002
- **Verification.** `pytest tests/unit/test_inference_records.py` — a new assertion that both dimensions are populated where derivable.

#### REQ-CONFIDENCE-006 — Measurement quality, evidence and freshness are separately observable

- **Description.** The system publishes, separately and without blending: the measurement quality of the supporting readers, the evidence count and coverage behind an association, the freshness of the reading, and the uncertainty of the residual contribution.
- **Priority.** MUST
- **Rationale.** "each term is present in published evidence" and the numbers are "recomputable" and "reproducible" (`docs/confidence_model.md` §6); the association evidence already carries `stats`, `supporting` and `conflicting` (`docs/association_and_topology.md` §2.1).
- **Acceptance criteria.** An association's evidence publishes the event count, the supporting and conflicting counts, the stability and the lag; a reading publishes its age and its quality; a residual contribution publishes its uncertainty.
- **Dependencies.** REQ-MEASURE-008, REQ-CONFIDENCE-001
- **Scenario.** `T05` — extends matrix group **H** (row `H8`, noise and spikes); **new coverage** for the noise contribution to measurement quality.
- **Verification.** `pytest tests/unit/test_association.py` — the evidence-shape assertions.

#### REQ-CONFIDENCE-007 — An edge carries its own confidence dimensions

- **Description.** Every relation edge publishes a confidence for the specific claim it makes, together with the evidence terms that produced it; the edge confidence is not a device power score.
- **Priority.** MUST
- **Rationale.** `TopologyRelation.confidence` is "deliberately not a device power score" (`contracts.py:785-786`; `docs/confidence_model.md` §5); `G21` notes the missing per-edge dimensions.
- **Acceptance criteria.** An edge's confidence is ≤ 0.99 while it is a candidate; the evidence terms are published; an edge confidence is never copied into a device's `power` dimension.
- **Dependencies.** REQ-TOPO-001, REQ-CONFIDENCE-001
- **Verification.** `pytest tests/unit/test_association.py`; `pytest tests/unit/test_hierarchy.py::test_every_candidate_requires_confirmation`.

### 3.11 Mapping matrix

#### REQ-MAPPING-001 — The mapping matrix is derived, never the source of truth

- **Description.** The Device↔Reader mapping matrix is computed from the relations, the residual history, the model registry and the topology; it is never written by hand and never consulted as history.
- **Priority.** MUST
- **Rationale.** The gap is real: no mapping artefact exists at all, and `MeasurementMatrix` is the coefficient matrix `A(t)`, a different thing (`G23`; `engine/wattmosaic_engine/electrical/matrix.py:86-97`). "the panel tree as a read-time projection" is in the "must NOT be rebuilt" list (`docs/assessment_gap_analysis.md`).
- **Acceptance criteria.** Rebuilding the matrix from the same inputs yields the same matrix; no write path mutates a cell directly; the matrix cites the relation ids it was derived from.
- **Dependencies.** REQ-TOPO-001
- **Verification.** `pytest tests/unit/test_panel_tree.py::test_persisted_topology_still_yields_the_same_tree` — the existing projection pattern; a new determinism assertion.

#### REQ-MAPPING-002 — The matrix supports 1:1, 1:N, N:1 and N:N

- **Description.** A cell set may express one device on one reader, one device on several readers, several devices on one reader, and several devices on several readers.
- **Priority.** MUST
- **Rationale.** "Many-to-many: one device→many CTs, one CT→many devices. Never 1:1." (`docs/implementation_plan_v2.md` WP5); the overlap report already distinguishes duplicate measurement from independent contributions (`docs/association_and_topology.md` §2.6).
- **Acceptance criteria.** A 1:N cell set publishes `summing_allowed=False` where the contribution would double count; an N:1 cell set publishes the ambiguity rather than splitting the row.
- **Dependencies.** REQ-TOPO-007, REQ-MAPPING-001
- **Scenario.** `T13` — extends matrix group **C** (rows `C2`, `C4`) and matrix group **A** (row `A2`).
- **Verification.** `pytest tests/unit/test_association.py` — the overlap-report assertions; matrix rows `C2`, `C3`, `C4`.

#### REQ-MAPPING-003 — Every cell carries its own state

- **Description.** Each cell publishes the observability of the pairing, the estimated contribution, the minimum and maximum expected contribution, the confidence dimensions, the evidence count, the time of last validation, the version and the status.
- **Priority.** MUST
- **Rationale.** The matrix must be able to answer "how much of this device do I actually see through this reader, and how sure am I?" — the questions the existing association evidence answers for one relation (`docs/association_and_topology.md` §2.1).
- **Acceptance criteria.** A cell response contains all nine fields; the min/max range is consistent with the estimate or explicitly `null` when the cell is not separable.
- **Dependencies.** REQ-CONFIDENCE-006
- **Verification.** `pytest tests/unit/test_api.py` — a new assertion on the matrix response shape.
- **Amendment (2026-11-17, contract 2.3.0).** The cell's own state is published through
  `MappingCellStatus`; its six-state vocabulary is `UNKNOWN`, `LEARNING`, `PROVISIONAL`,
  `CONFIRMED`, `CONFLICTED`, `NON_IDENTIFIABLE`. The cell also carries `relation_type`,
  `sources_used`, `model_version` and `topology_version` (see `09-…` §7.5). These are
  additive/neutral fields; the original nine fields are unchanged.

#### REQ-MAPPING-004 — Unknown loads and uncertain attribution appear in the matrix

- **Description.** The matrix has a representation for load that is observed but not identified, and a cell whose attribution is uncertain is marked uncertain rather than omitted.
- **Priority.** MUST
- **Rationale.** "There is no explicit 'unknown load' entity in the contract" (`docs/electrical_model.md` §10); the gap is `G24`.
- **Acceptance criteria.** A scope with unexplained consumption shows an unknown-load entry with its reader and its contribution; no unknown entry names a device.
- **Amendment (2026-11-17, contract 2.3.0).** An unknown-load cell sets `is_unknown_load = True` and may link to `UnknownLoadProfile.profile_id` via `unknown_load_profile_id`; the frozen `device_id` field stays a required opaque `str` and is never retyped to nullable.
- **Dependencies.** REQ-RESIDUAL-007, REQ-MAPPING-001
- **Scenario.** `T04` and `T10` — `T04` extends matrix groups **E** (row `E6`) and **F** (rows `F1`, `F2`); `T10` is **new coverage**.
- **Verification.** `pytest tests/unit/test_disaggregation.py::test_08_unknown_load_is_preserved_as_residual`; a new assertion that the matrix carries the unknown entry.

#### REQ-MAPPING-005 — Cells update on evidence or model change, not per sample

- **Description.** A mapping cell is recomputed when its evidence, its relations or its model change; a new sample alone does not rewrite it.
- **Priority.** SHOULD
- **Rationale.** "no full matrix snapshot per sample" is a storage requirement of the target brief, and the inference record is already written only on a material change plus a one-hour heartbeat (`docs/limitations_v2.md` §7.2).
- **Acceptance criteria.** A steady stream of identical samples produces no matrix writes; an association withdrawal produces one.
- **Dependencies.** REQ-STORAGE-005, REQ-CONFIDENCE-007
- **Verification.** `pytest tests/unit/test_inference_records.py` — the change-detection assertions.

#### REQ-MAPPING-006 — Mapping complexity is not mapping confidence

- **Description.** A cell with many readers is not thereby more confident; complexity and confidence are published as separate quantities.
- **Priority.** MUST
- **Rationale.** "mapping complexity ≠ mapping confidence" is required by the target brief; `AssociationFinder` already penalises ambiguity with `SPLIT_MULTIPLIER = 0.75` and `AMBIGUOUS_MULTIPLIER = 0.25` (`docs/association_and_topology.md` §2.4).
- **Acceptance criteria.** A cell observed through three readers with an ambiguous verdict publishes an ambiguous verdict and a capped confidence; no arithmetic multiplies the reader count into the confidence.
- **Dependencies.** REQ-CONFIDENCE-007
- **Verification.** `pytest tests/unit/test_association.py` — the ambiguity-cap assertions.

#### REQ-MAPPING-007 — The matrix states what it cannot see

- **Description.** The matrix reports its own coverage: which devices have no reader, which readers observe nothing attributable, and which scopes are unexplained.
- **Priority.** MUST
- **Rationale.** Partial observability is a first-class condition of the target; "Unobserved loads" are already covered behaviour (matrix row I7).
- **Acceptance criteria.** The matrix response carries a coverage block naming unobserved devices and unexplained scopes; the coverage block is empty only when everything is attributed.
- **Dependencies.** REQ-TOPO-010, REQ-MAPPING-001
- **Verification.** `pytest tests/unit/test_electrical.py::test_unobserved_device_is_listed_not_silently_zero`; a new assertion on the coverage block.

### 3.12 Dynamic topology and change

#### REQ-CHANGE-001 — Every configuration change produces a traceable event

- **Description.** A smart-plug relocation, a CT reader relocation, a reader replacement, a CT direction change, a calibration change, a circuit reconfiguration, a reader install or removal, and a measurement-scope change each produce a configuration event naming what changed, when, and which version it produced.
- **Priority.** MUST
- **Rationale.** The gap is real: no configuration-event object exists (`G28`); the risk that a wrong confirmed topology produces confident wrong estimates is registered as R11 and mitigated by "Topology versioning, diagnostics, visible candidate status, easy rollback" (`docs/risks.md`).
- **Acceptance criteria.** Each of the eight change kinds has a named event; the event carries the affected reader or device, the before and after state, the timestamp and the resulting version identifier.
- **Dependencies.** REQ-CHANGE-003, REQ-CHANGE-006
- **Scenario.** `T11` and `T12` — **new coverage** (no existing row asserts a configuration event).
- **Verification.** `pytest tests/unit/test_topology_persistence.py` — a new assertion per change kind; scenarios `T11`, `T12`.

#### REQ-CHANGE-002 — A suspected change is a hypothesis, validated before promotion

- **Description.** When the evidence suggests that the configuration changed, the system records a hypothesis with its evidence and leaves the configuration unchanged until the hypothesis is validated.
- **Priority.** MUST
- **Rationale.** "suspected changes create a hypothesis that is validated before promotion" is required by the target brief, and it is the same discipline as the candidate topology: "Nothing in v2 promotes a hypothesis to fact" (`docs/limitations_v2.md` §4).
- **Acceptance criteria.** A suspected change is published with its status `SUSPECTED_CHANGE` or `UNDER_REVIEW` and its evidence; the live configuration is untouched until validation; `requires_confirmation` remains true.
- **Dependencies.** REQ-TOPO-002
- **Scenario.** `T11` and `T12` — **new coverage** (no existing row asserts the hypothesis-before-promotion discipline).
- **Verification.** `pytest tests/unit/test_hierarchy.py::test_every_candidate_requires_confirmation` — the pattern; a new assertion that a suspected change does not alter the active topology.

#### REQ-CHANGE-003 — Four version identifiers travel with every derived artefact

- **Description.** Every derived artefact names the `topology_version`, the `reader_configuration_version`, the `device_mapping_version` and the `model_version` it was produced under, and each identifier has its own validity interval.
- **Priority.** MUST
- **Rationale.** Only `topology_version` and `model_version` exist (`contracts.py:893`, `:1650`); `G26` records the absence of the other two. `valid_from`/`valid_to` on a relation is the existing precedent (`contracts.py:800-804`).
- **Acceptance criteria.** An inference record, a mapping cell and a published model estimate each name all four identifiers; each identifier resolves to a version with a validity interval.
- **Dependencies.** REQ-STORAGE-007, REQ-TOPO-008
- **Verification.** `pytest tests/unit/test_inference_records.py` — a new assertion on the four identifiers.

#### REQ-CHANGE-004 — Historical topology is never rewritten

- **Description.** A configuration change creates a new version; the previous version's channels, devices, relations and circuits remain readable exactly as they were.
- **Priority.** MUST
- **Rationale.** "Historical topology is never rewritten" is required by the target brief; `save_topology` deletes and re-inserts relations today (`G9`, `docs/assessment_gap_analysis.md` §11), which is exactly what the versioned relation history must replace.
- **Acceptance criteria.** After a change, the previous version's payload and relations are byte-identical to what they were; a read-as-of query returns the configuration in force at that time.
- **Dependencies.** REQ-TOPO-008, REQ-STORAGE-003
- **Scenario.** `T11` and `T12` — **new coverage** (no existing row asserts that the previous version stays byte-identical).
- **Verification.** `pytest tests/unit/test_topology_persistence.py` — a new assertion for the read-as-of path.

#### REQ-CHANGE-005 — Uncertainty is preserved when evidence is insufficient

- **Description.** When a change cannot be established from the evidence, the system keeps the previous state, marks the scope as uncertain and says what evidence is missing.
- **Priority.** MUST
- **Rationale.** "uncertainty preserved when evidence is insufficient" is required by the target brief, and it matches "An empty result with a reason is preferred to a guess" (`docs/association_and_topology.md` §3.7).
- **Acceptance criteria.** An unresolved change leaves the active configuration untouched; the affected scope is published as uncertain with the missing evidence named; no silent default is applied.
- **Dependencies.** REQ-CHANGE-002, REQ-CONFIDENCE-005
- **Verification.** `pytest tests/unit/test_hierarchy.py` — the "refused with a reason" assertions.

#### REQ-CHANGE-006 — A configuration can be rolled back

- **Description.** Any version of the configuration can be restored, and the restore is itself a versioned change rather than an overwrite.
- **Priority.** MUST
- **Rationale.** "easy rollback" is the shipped mitigation for R11 (`docs/risks.md`); `Database.rollback_model` is the existing precedent for a reversible version (`engine/wattmosaic_engine/storage/database.py:1213`).
- **Acceptance criteria.** Restoring version *n* produces a new version whose content equals version *n*'s content and whose parent is the version in force; the intermediate versions remain readable.
- **Dependencies.** REQ-TOPO-008
- **Verification.** `pytest tests/unit/test_topology_persistence.py` — a new assertion for the rollback path.

#### REQ-CHANGE-007 — A change is reconciled against the inference history

- **Description.** After a configuration change, the affected inference records are reconciled: the records produced under the old configuration are marked as belonging to it, and a reader can see which records a change affected.
- **Priority.** MUST
- **Rationale.** Matrix row J6 records that "no test asserts that changing the topology *after* inferences exist re-derives those inferences / marks them stale" (`docs/test_scenario_matrix.md`).
- **Acceptance criteria.** Every record names the version it was produced under; a change response lists the affected device and record counts; no record is rewritten.
- **Dependencies.** REQ-TOPO-009, REQ-CHANGE-003
- **Scenario.** `T12` — **new coverage** (no existing row asserts reconciliation of the inference history after a configuration change).
- **Verification.** `pytest tests/unit/test_inference_records.py` — a new assertion; scenario `T12`.

### 3.13 User interface

#### REQ-UI-001 — The reader inventory and its status are visible

- **Description.** The panel lists every reader with its identifier, name, panel, phase, polarity, resolution, reliability, expected interval, last-seen age, quality and status.
- **Priority.** MUST
- **Rationale.** The panel has five routes today (`addon/ui/app.js:655-660`) and the topology view is the only place a reader appears; the API already publishes the channel list (`engine/wattmosaic_engine/api/server.py:763`).
- **Acceptance criteria.** The topology view lists every reader with the fields above; a reader with no reading is shown as missing, never as 0 W.
- **Dependencies.** REQ-MEASURE-007
- **Verification.** `pytest tests/unit/test_panel.py` — a new assertion for the inventory fields.

#### REQ-UI-002 — A measurement is shown with its quality and its provenance

- **Description.** Every rendered measurement shows its value, its unit, the three timestamps in use, its quality status and whether it is measured, estimated, allocated or unknown.
- **Priority.** MUST
- **Rationale.** REQ-FOUNDATION-005; the quality vocabulary (`GOOD`, `DEGRADED`, `STALE`, `BAD`, `MISSING`) is the contract's (`contracts.py:76-81`).
- **Acceptance criteria.** A stale reading renders with the stale state and its age; a missing reading renders as missing and never as `0 W`.
- **Dependencies.** REQ-FOUNDATION-005, REQ-MEASURE-008
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-003 — The topology is drawn with per-edge confidence and lifecycle status

- **Description.** The topology view draws the readers, the circuits and the devices with each edge's lifecycle status and confidence, and distinguishes a confirmed edge from a candidate one.
- **Priority.** MUST
- **Rationale.** `G27` records that the UI exposes none of the topology version history, suspected changes or per-edge state (`docs/limitations_v2.md` §8); the candidate state is already published by `/relations` (`docs/limitations_v2.md` §4).
- **Acceptance criteria.** An edge in the drawing carries its status label and its confidence; a candidate edge is visually distinct from a confirmed one; the drawing is reachable from the existing `#/topology` route.
- **Dependencies.** REQ-TOPO-002, REQ-CONFIDENCE-007
- **Verification.** `pytest tests/unit/test_panel.py`; `python tools/dev/check_panel.py` — the static panel gate.

#### REQ-UI-004 — Residuals and unknown loads are visible

- **Description.** The panel shows, per scope, the signed residual, its baseline and dynamic parts, its resolution state and age, the unexplained load and the unknown-load entries.
- **Priority.** MUST
- **Rationale.** `G27`: the UI exposes none of the residual decomposition; `ResidualPower` carries all of these fields (`contracts.py:529-561`).
- **Acceptance criteria.** The residual view shows the signed value with its sign, the baseline and dynamic parts, and the unresolved age; an unresolved residual never renders as 0 W.
- **Dependencies.** REQ-RESIDUAL-002, REQ-RESIDUAL-008
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-005 — Device power models are visible per device

- **Description.** The device page shows the device's power-model class, the active model version, the model's validation metrics and the model's status, and lets the reader inspect the model versions.
- **Priority.** SHOULD
- **Rationale.** `G27` records that per-device model detail is not exposed; `GET /models/{device_id}` already returns "versions + active model" (`docs/interface_contracts.md` §2.2).
- **Acceptance criteria.** The device page names the active model version and its status; a frozen or rejected version is distinguishable.
- **Dependencies.** REQ-POWER-001, REQ-POWER-008
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-006 — The device↔reader mapping is visible

- **Description.** The panel shows the mapping cells: which devices a reader observes, which readers observe a device, the observability, the contribution range and the cell's status.
- **Priority.** MUST
- **Rationale.** `G27` records that the mapping matrix is not exposed; without it the product cannot express multiple devices on one reader.
- **Acceptance criteria.** The mapping view lists every cell with its observability, range and status; an unknown-load entry is shown without a device name.
- **Dependencies.** REQ-MAPPING-003, REQ-MAPPING-004
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-007 — Per-device estimates distinguish measured, estimated, allocated and unknown

- **Description.** Every device power figure in the UI is labelled as measured, estimated, allocated or unknown, using the four-state vocabulary of REQ-FOUNDATION-005.
- **Priority.** MUST
- **Rationale.** "Distinguish measured and estimated power" is an acceptance principle (`docs/WATTMOSAIC_MASTER_INSTRUCTIONS.md` §19.5); the shipped panel has a `STRINGS` table for exactly this kind of label (`addon/ui/app.js:264-292`).
- **Acceptance criteria.** A device rendered from `Source.INFERRED` is labelled estimated; a device rendered from a group estimate is labelled allocated; a device with no evidence is labelled unknown and shows no watt figure.
- **Dependencies.** REQ-FOUNDATION-005, REQ-DISAGG-004
- **Verification.** `pytest tests/unit/test_panel.py`; `python tools/dev/check_panel.py` — the `STRINGS` table gate.

#### REQ-UI-008 — Identifiability status is visible per device

- **Description.** A device that cannot be separated from its siblings is shown as non-identifiable, with the group it belongs to and the published range.
- **Priority.** MUST
- **Rationale.** "report a group estimate, a range, or a non-identifiable status" (target invariant); the group is the ambiguous group of the null-space analysis (`docs/electrical_model.md` §6).
- **Acceptance criteria.** A device in an ambiguous group shows the group's minimum, maximum and member list instead of a single watt figure.
- **Dependencies.** REQ-DISAGG-003, REQ-DISAGG-004
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-009 — Historical events and their evidence are visible

- **Description.** The panel can show a device's recorded events with their isolation verdict, the window that was examined, the competing events and the evidence that decided it.
- **Priority.** SHOULD
- **Rationale.** `G27` records that isolation verdicts and association candidates are not exposed; `EventIsolation` carries the window, the competitors and the evidence (`contracts.py:987-1054`).
- **Acceptance criteria.** The event list shows the verdict, the window bounds, the competing device ids and the reason code.
- **Dependencies.** REQ-EVENT-004, REQ-EVENT-008
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-010 — Version history and suspected changes are visible

- **Description.** The panel shows the topology version history with a diff between versions, and a list of suspected configuration changes with their evidence and status.
- **Priority.** MUST
- **Rationale.** `G27` records that version history and suspected changes are not exposed; R11's mitigation requires "visible candidate status" (`docs/risks.md`).
- **Acceptance criteria.** Two versions can be compared in the panel and the differences are named; a suspected change is listed with its status and its evidence and is visibly not yet applied.
- **Dependencies.** REQ-TOPO-008, REQ-CHANGE-002
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-011 — A reader can correct and confirm configuration

- **Description.** From the panel the operator can confirm a candidate topology, correct a reader assignment or a device declaration, and withdraw a relation, and every correction is recorded as a user declaration.
- **Priority.** MUST
- **Rationale.** The panel is "read-only apart from confirming an already-inferred topology" today (`docs/architecture.md` §6); the target adds corrections, which must keep the user's declaration distinct from an inference (`docs/configuration.md`; ADR-0011).
- **Acceptance criteria.** A confirmation writes through the existing confirm route; a correction is stored with `configured_by="user"`; an inferred relation can be withdrawn with a reason.
- **Dependencies.** REQ-TOPO-002, REQ-CHANGE-001
- **Verification.** `pytest tests/integration/test_confirm_topology_e2e.py`; `pytest tests/unit/test_panel.py`.

#### REQ-UI-012 — Storage health and processing health are visible

- **Description.** The panel shows the retention configuration, the size and row counts per storage tier, the last prune, the learning status, the inference cadence and the queue depth.
- **Priority.** SHOULD
- **Rationale.** `G27` records that storage health is not exposed; `Database.stats()` already returns table counts and the schema version (`engine/wattmosaic_engine/storage/database.py:1365-1380`).
- **Acceptance criteria.** The health view names the retention horizons in days, the row counts per tier and the last prune time; a tier that has never been pruned says so.
- **Dependencies.** REQ-STORAGE-001, REQ-STORAGE-002
- **Verification.** `pytest tests/unit/test_panel.py`.

#### REQ-UI-013 — Every view handles its empty, error, stale and degraded states, and never invents a zero

- **Description.** Each view has explicit loading, empty, error, stale, offline and degraded states; missing data renders as missing, never as `0 W`; no view is decorative.
- **Priority.** MUST
- **Rationale.** "missing is never zero" is a repository invariant (`README.md` §5; `docs/electrical_model.md` §8); the panel already distinguishes the quality vocabulary in its `STRINGS` table (`addon/ui/app.js:264-292`).
- **Acceptance criteria.** A view with no data shows the empty state, not zeros; an unreachable engine shows the offline state; a stale reading shows the stale state with its age; a `MISSING` reading renders no watt figure.
- **Dependencies.** REQ-UI-002, REQ-RESIDUAL-008
- **Verification.** `pytest tests/unit/test_panel.py`; `python tools/dev/check_panel.py`.

### 3.14 Deployment

#### REQ-DEPLOY-001 — The build and image pipeline is reused unchanged

- **Description.** The target system ships through the existing image build and the existing GHCR publication; no new artefact type is introduced.
- **Priority.** MUST
- **Rationale.** The Supervisor pulls `ghcr.io/asxzy/wattmosaic-engine:<version>` (`docs/deployment.md`); the App is a Supervisor App with `ingress: true` on port 8787 (`addon/config.yaml`).
- **Acceptance criteria.** `docker build` of the repository root produces an image that starts the engine and the gateway; `bash scripts/validate_addon.sh --no-build` passes.
- **Dependencies.** —
- **Verification.** `bash scripts/validate_addon.sh --no-build`; the `deployment_valid` gate in `python tools/dev/verify.py`.

#### REQ-DEPLOY-002 — Configuration comes from the existing option and environment surface

- **Description.** Any new setting is an App option with an environment variable and a built-in default, listed in the same option table, and validated at startup.
- **Priority.** MUST
- **Rationale.** "options.json > env > defaults" is the entrypoint's precedence (`addon/run.sh` option table), and `scripts/validate_addon.sh` parses that table to compare it with `engine/wattmosaic_engine/config/settings.py`.
- **Acceptance criteria.** A new setting appears in `addon/run.sh`'s option table, in `config/settings.py` and in `.env.example`; a non-numeric value is refused at startup.
- **Dependencies.** REQ-STORAGE-002
- **Verification.** `bash scripts/validate_addon.sh --no-build`; `pytest tests/unit/test_storage_config.py`.

#### REQ-DEPLOY-003 — Migrations run automatically and are safe to re-run

- **Description.** The engine applies pending schema migrations at startup, refuses to start on a corrupt file, and applying them twice is a no-op.
- **Priority.** MUST
- **Rationale.** "Migrations idempotent" is the shipped restart behaviour (`docs/architecture.md` §5); `apply_migrations` is the entry point (`engine/wattmosaic_engine/storage/migrations.py:796`).
- **Acceptance criteria.** A database at version *n* starts and reaches the current version; a corrupt file refuses to start with a clear error; the `PRAGMA user_version` after start equals `LATEST_VERSION`.
- **Dependencies.** REQ-STORAGE-011
- **Verification.** `pytest tests/unit/test_migrations.py`; `pytest tests/end_to_end/test_storage_e2e.py`.

#### REQ-DEPLOY-004 — The database can be backed up and restored

- **Description.** A backup of the database and its WAL is taken on a schedule and before a migration; a restore brings the installation back to the pre-change state.
- **Priority.** MUST
- **Rationale.** `data/backups/` is already the conventional location (`.gitignore:28`); the storage layer exposes `checkpoint()` (`engine/wattmosaic_engine/storage/database.py:169`).
- **Acceptance criteria.** A backup taken while the engine is running is a consistent snapshot; a restore into an empty installation loads the topology, the models and the evidence; the backup contains no secret.
- **Dependencies.** REQ-STORAGE-010
- **Verification.** `pytest tests/end_to_end/test_storage_e2e.py` — a new backup/restore assertion.

#### REQ-DEPLOY-005 — The deployment procedure is documented and repeatable

- **Description.** The deployment is: back up, upgrade the image, let the migrations run, run the smoke tests, and observe. Each step has an expected result and a failure action.
- **Priority.** MUST
- **Rationale.** The deployment shapes and the rollback procedure already exist (`docs/deployment.md`); the target adds steps rather than replacing them.
- **Acceptance criteria.** The procedure is published with the version it applies to; every step names its verification command.
- **Dependencies.** REQ-DEPLOY-001, REQ-DEPLOY-004
- **Verification.** Manual review of the published procedure against `docs/deployment.md`.

#### REQ-DEPLOY-006 — Smoke tests run after deployment

- **Description.** After a deployment the installation is checked for: engine health, contract version, database version, the panel's reachability, one known device's value and the presence of the residual.
- **Priority.** MUST
- **Rationale.** "Liveness probes must not do verification work" (ADR-0018) means the health probe stays cheap and the verification is a separate, explicit step.
- **Acceptance criteria.** A smoke-test script exits non-zero when any of the six checks fails; it is separate from the watchdog probe.
- **Dependencies.** REQ-DEPLOY-003
- **Verification.** `pytest tests/unit/test_app_startup.py`; `bash scripts/validate_addon.sh --no-build`.

#### REQ-DEPLOY-007 — A failed deployment is rolled back

- **Description.** If the smoke tests fail, the previous image is restored and the previous database copy is put back; the rollback is itself documented and rehearsed.
- **Priority.** MUST
- **Rationale.** "Topology versioning, diagnostics, visible candidate status, easy rollback" is the shipped mitigation for R11 (`docs/risks.md`).
- **Acceptance criteria.** A rollback restores the previous image tag and the pre-migration database copy; the restore is verified by the same smoke tests.
- **Dependencies.** REQ-DEPLOY-004, REQ-DEPLOY-006
- **Verification.** Manual rehearsal recorded in the delivery report; `pytest tests/end_to_end/test_storage_e2e.py`.

#### REQ-DEPLOY-008 — Operational risks are recorded and monitored

- **Description.** The installation publishes the metrics needed to detect the operational risks — queue depth, HA connectivity, engine status, inference cadence, dropped samples — and the risk register is updated.
- **Priority.** SHOULD
- **Rationale.** `/metrics` already publishes `wattmosaic_up`, `wattmosaic_engine_status`, `wattmosaic_ha_connected` and `wattmosaic_queue_depth` (`engine/wattmosaic_engine/main.py:2946-2953`); the risk register is R1–R13 (`docs/risks.md`).
- **Acceptance criteria.** Each operational risk in the register names the metric that would reveal it; a metric that does not exist yet is added to `/metrics`.
- **Dependencies.** REQ-DEPLOY-006
- **Verification.** `pytest tests/unit/test_api.py` — the metrics assertions; manual review against `docs/risks.md`.

### 3.15 Testing

#### REQ-TEST-001 — Unit tests cover every new module and every new rule

- **Description.** Every new rule has a unit test that fails before the rule exists.
- **Priority.** MUST
- **Rationale.** Acceptance gate 6: "Every new inference rule has a test that **fails first**" (`docs/implementation_plan_v2.md` §7).
- **Acceptance criteria.** Each new requirement's named test exists and fails against the pre-change tree.
- **Dependencies.** —
- **Verification.** `python -m pytest tests/unit -q`; the red-then-green record is in the delivery report.

#### REQ-TEST-002 — Integration tests cover the HTTP surface and the wiring

- **Description.** The new read paths are covered end to end over HTTP, including the contract header, the token check, the error shape and the `400`-not-`422` rule.
- **Priority.** MUST
- **Rationale.** "Request-validation failures return HTTP **400** … HTTP 422 is never exposed" (`engine/wattmosaic_engine/api/server.py:12-13`).
- **Acceptance criteria.** A new route returns the contract header, rejects a bad token with 401, and returns 400 for a validation failure; the route appears in the facade protocol (`engine/wattmosaic_engine/api/facade.py:94-148`).
- **Dependencies.** REQ-FOUNDATION-003
- **Verification.** `python -m pytest tests/integration/test_engine_api_contract.py -q`; `python -m pytest tests/end_to_end -q`.

#### REQ-TEST-003 — The synthetic scenario suite T01–T15 exists and is deterministic

- **Description.** The fifteen scenarios in §4 are built by named, deterministic builders; each produces the same output for a fixed seed.
- **Priority.** MUST
- **Rationale.** "all builders produce deterministic output for a fixed seed" (`docs/implementation_plan_v2.md` WP2); `LabHouse` already separates `observations()` from `ground_truth()` (`tools/simulator/labhouse.py:122-129`).
- **Acceptance criteria.** Each of `T01`–`T15` has a named builder in `tools/simulator/`; two runs with the same seed produce identical inputs; the ground truth is never visible to the engine path.
- **Dependencies.** REQ-TEST-004
- **Scenario.** all of `T01`–`T15` — the whole catalogue.
- **Verification.** `python -m pytest tests/simulation/test_simulator_fixtures.py -q`; `python -m pytest tests/simulation -q`.

#### REQ-TEST-004 — Every scenario-dependent requirement names both the `T##` id and the existing matrix row

- **Description.** A requirement that depends on a synthetic scenario references the new `T##` identifier and the row or group of `docs/test_scenario_matrix.md` it extends or newly covers.
- **Priority.** MUST
- **Rationale.** The existing matrix uses `A1`–`J9` across groups A–J with 84 rows and has no `T##` namespace (`docs/test_scenario_matrix.md`); the two namespaces must be reconcilable.
- **Acceptance criteria.** Every requirement carrying a `Scenario.` line names both identifiers; the mapping table in §4 covers all fifteen.
- **Dependencies.** —
- **Verification.** Manual review of §4 against this file; a new assertion in `tests/unit/test_simulator_fixtures.py` that every scenario id in §4 has a builder.

#### REQ-TEST-005 — Replay tests re-run stored windows through the production path

- **Description.** A stored window is replayed through `Measurement` → `SampleAligner` → `DisaggregationEngine.infer`, and the replay is compared with the original result.
- **Priority.** MUST
- **Rationale.** The replay tool exists for exactly this purpose (`tools/audit/replay_capture.py:1-16`), and the nine live-capture tests are the ones that skip without a local capture (`docs/limitations_v2.md` §3).
- **Acceptance criteria.** The replay of a stored window under the same algorithm version is identical; the nine capture-gated tests either run or are reported as skipped with the reason.
- **Dependencies.** REQ-STORAGE-012
- **Verification.** `python -m pytest tests/simulation/test_replay_capture.py -q`; `python -m pytest tests/simulation/test_real_topology.py -q` with `WATTMOSAIC_REAL_DB` set.

#### REQ-TEST-006 — The existing suite stays green and the type-check delta is zero

- **Description.** After every change the full suite passes with no regression, and `mypy engine` reports no new error relative to the pre-change baseline.
- **Priority.** MUST
- **Rationale.** Acceptance gate 1 and gate 2 (`docs/implementation_plan_v2.md` §7); CI treats `mypy` as non-blocking (`.github/workflows/ci.yml:26`), so the delta is the meaningful measure. The baseline at HEAD `612e1233` is a measured 112 `mypy engine` errors in 27 of 80 checked files, against a recorded historical baseline of 120 (`docs/final_validation_report.md:23`).
- **Acceptance criteria.** The suite result is not smaller than the pre-change result; the number of `mypy engine` errors does not increase.
- **Dependencies.** REQ-FOUNDATION-002
- **Verification.** `python -m pytest tests -q`; `python -m mypy engine | tail -1` compared with the baseline.

#### REQ-TEST-007 — Negative verification proves every new guard

- **Description.** Each new guard is proved by removing it and watching its own test fail, and the procedure is automated where it is repeated.
- **Priority.** MUST
- **Rationale.** `tools/dev/negverify_nesting.py` exists for this and its header states the principle: "A guard is only proven if *its own* test fails when the guard is gone."
- **Acceptance criteria.** Every new guard has a negative-verification record; a repeated family of guards has an automated harness.
- **Dependencies.** REQ-TEST-001
- **Verification.** `python tools/dev/negverify_nesting.py`; the new harness for the new guards.

#### REQ-TEST-008 — Migration tests cover the upgrade and the rollback of the schema

- **Description.** A database at every supported prior schema version opens, migrates and loads its topology, models and evidence; the migration is idempotent.
- **Priority.** MUST
- **Rationale.** "Migration discipline (forward-only, additive, idempotent helpers)" (`docs/assessment_gap_analysis.md`); the current schema version is 11.
- **Acceptance criteria.** A fixture at each prior version migrates to the current version with no row loss; applying the migration twice changes nothing.
- **Dependencies.** REQ-STORAGE-011
- **Verification.** `python -m pytest tests/unit/test_migrations.py -q`; `python -m pytest tests/unit/test_reconciliation_persistence.py -q`.

#### REQ-TEST-009 — Performance tests cover the paths the target adds

- **Description.** The inference cycle, the residual history query, the mapping projection and the topology diff stay within their stated budgets on a reference-sized installation.
- **Priority.** SHOULD
- **Rationale.** The shipped read-path costs are measured: "≈ 24 ms warm, ≈ 590 ms cold at 343k rows" for `latest_inference_results()` (`docs/limitations_v2.md` §7.3); the health probe must stay cheap (ADR-0018).
- **Acceptance criteria.** Each budget is stated in the requirement's implementation plan and asserted by a test with a margin; the health probe's cost does not regress.
- **Dependencies.** REQ-MAPPING-001, REQ-TOPO-008
- **Verification.** `python -m pytest tests/unit/test_health_probe_cost.py -q`; `python -m pytest tests/unit/test_nesting_perf.py -q`.

#### REQ-TEST-010 — No test or document claims real-house per-device accuracy

- **Description.** The accuracy evidence produced by the target remains simulator-derived; no report, figure or claim states a per-device accuracy for a physical installation.
- **Priority.** MUST
- **Rationale.** "Every accuracy number in this repository is measured on the deterministic synthetic household" (`docs/limitations_v2.md` §1); the replay tool "reports **no per-device error at all**" (same document).
- **Acceptance criteria.** The delivered documentation states the limitation in the same terms; the accuracy report names the strata and the synthetic source; no confidence number is described as a probability.
- **Dependencies.** REQ-CONFIDENCE-002, REQ-TEST-003
- **Verification.** `python -m pytest tests/simulation/test_accuracy.py -q` — the stratified assertions; manual review of the delivered documentation.

---

## 4. Scenario catalogue T01–T15 and its mapping to the existing matrix

### 4.1 The canonical T01–T15 definitions

These fifteen scenarios are fixed by the master instruction. They are the
contract between this requirements file and the test plan
(`11-test-and-validation-plan.md`), and a reader of this file alone must be
able to check any mapping against them.

**Rule.** This table is the only T-catalogue. A scenario identifier means what
it means here and nowhere else; a second catalogue is a defect, not a
restatement. `14-requirement-traceability.md` and the sibling
specifications already read the identifiers this way (for example `T01` =
containment, `T04` = standby, `T08` = variable power, `T11`/`T12` =
relocation and replacement).

**Open decision — OD-09 (resolved).** A T-catalogue conflict between this file
and `11-test-and-validation-plan.md` §5.1 was identified: the test plan's table
titled `T01`, `T02`, `T03`, `T12`, `T13`, `T14` and `T15` differently from the
canonical meanings in §4.1 below, while stating that its titles were taken from
this section verbatim. The choices were (a) rewrite the test plan's §5.1 table
to the canonical titles and setups in this section, keeping its stratum, input,
expected-outcome and verification-vehicle columns; (b) amend the canonical
table, which would change the meaning of the `Scenario.` lines throughout this
file and desynchronise `14-requirement-traceability.md`; or (c) give the two
catalogues disjoint namespaces. **Resolution: (a).** This file keeps the
canonical meanings fixed by the master instruction, and
`11-test-and-validation-plan.md` §5.1 was rewritten to the canonical
definitions in §4.1 — its claim that the IDs and titles are taken from this
section verbatim is now true. The two files agree on all fifteen identifiers
and titles, on the existing matrix rows, on the coverage status and on the
requirements each scenario serves. §4.2 is the single scenario-to-matrix
mapping and §4.3 the single requirement-to-scenario mapping, and §4.3 takes
its matrix rows from §4.2. The conflict is closed.

| ID | Canonical scenario | What it asserts |
| --- | --- | --- |
| `T01` | **Single Child Containment** | parent +100 W when child +100 W; repeated compatible events support a candidate containment relationship |
| `T02` | **Multiple Child Contributions** | parent +100 W, child A +30 W, child B +70 W |
| `T03` | **Unobserved Parent Load** | parent +150 W, all monitored children unchanged; a dynamic residual event, not rejection of the topology |
| `T04` | **Baseline Standby Consumption** | persistent non-zero background on an unmonitored or partially monitored circuit; a baseline residual model, not repeated unknown events |
| `T05` | **Noise** | readings fluctuate within configured measurement uncertainty; the noise model absorbs it without excessive false events |
| `T06` | **Multiple Simultaneous Devices** | several devices change at nearly the same time; joint inference and confidence-aware attribution |
| `T07` | **Non-identifiable Devices** | two devices always operate together and cannot be separated; a group estimate or a non-identifiable status |
| `T08` | **Variable Power Device** | a dimmer or variable-speed device with a non-linear power curve; an adjustable model, not forced linear behaviour |
| `T09` | **Delayed Device Response** | HVAC, blinds or similar equipment responds after a delay; correlation respects the response model |
| `T10` | **Unknown Device Discovery** | an unknown load repeatedly creates a recognisable power pattern with no HA state change; an unknown load profile is discovered |
| `T11` | **Smart Plug Relocation** | a monitored plug moves from circuit A to circuit B; a candidate topology change, validation, a new topology version, preserved history |
| `T12` | **Reader Relocation or Replacement** | a reader is moved, replaced, recalibrated or reversed; a configuration change is detected or registered, affected assumptions invalidated, reconciliation safe |
| `T13` | **240 V Split-Phase Load** | the same physical load represented through paired leg measurements; no duplicate attribution |
| `T14` | **Missing and Delayed Data** | readers become unavailable or deliver data late; an unknown/stale measurement state, not zero-power assumptions |
| `T15` | **Bidirectional Power** | solar or another source creates reverse power flow; signed measurement handling without invalid non-negative-load assumptions |

### 4.2 The scenarios against the existing matrix

The existing matrix uses `A1`–`J9` across groups A–J — 84 rows
(`docs/test_scenario_matrix.md`): **A** basic loads, **B** Refoss factor,
**C** CT relationships, **D** dedicated circuits, **E** shared circuits,
**F** residuals, **G** HA events/automation, **H** dynamic, **I**
identifiability/ambiguity, **J** learning/regression.

A row marked **new coverage** has no existing row that asserts the canonical
scenario; a row marked **fixture only** is already covered and needs a named,
deterministic builder rather than new behaviour.

| T | Canonical scenario | Existing row/group | Status | Requirements |
| --- | --- | --- | --- | --- |
| `T01` | Single Child Containment | `C1`, `C5` | **new coverage** — a single child whose delta matches its parent is not asserted | REQ-TOPO-003, REQ-TOPO-004, REQ-POWER-003, REQ-EVENT-005 |
| `T02` | Multiple Child Contributions | `C5` | **new coverage** — two children summing to the parent is not asserted | REQ-TOPO-003, REQ-TOPO-007, REQ-EVENT-005 |
| `T03` | Unobserved Parent Load | `F3`, `F4` | **new coverage** — the parent change must produce a residual event, not a topology rejection | REQ-TOPO-005, REQ-TOPO-006, REQ-RESIDUAL-002, REQ-RESIDUAL-004 |
| `T04` | Baseline Standby Consumption | `F1`, `F2`, `A7` | **new coverage** — a baseline model rather than repeated unknown events | REQ-POWER-002, REQ-POWER-004, REQ-RESIDUAL-007, REQ-MAPPING-004 |
| `T05` | Noise | `H8` | fixture only | REQ-MEASURE-007, REQ-CONFIDENCE-006 |
| `T06` | Multiple Simultaneous Devices | `G2`, `E5`, `G5` | fixture only | REQ-DISAGG-002, REQ-DISAGG-006, REQ-EVENT-002, REQ-TOPO-007 |
| `T07` | Non-identifiable Devices | `I1`, `I4`, `F8` | fixture only | REQ-DISAGG-002, REQ-DISAGG-003, REQ-DISAGG-004, REQ-DISAGG-005 |
| `T08` | Variable Power Device | `A5`, `D5` | fixture only | REQ-POWER-001 |
| `T09` | Delayed Device Response | `H6`, `G10` | fixture only | REQ-EVENT-003 |
| `T10` | Unknown Device Discovery | none | **new coverage** | REQ-POWER-005, REQ-EVENT-001, REQ-RESIDUAL-004, REQ-RESIDUAL-006, REQ-LEARNING-004, REQ-MAPPING-004, REQ-STORAGE-005 |
| `T11` | Smart Plug Relocation | none | **new coverage** | REQ-CHANGE-001, REQ-CHANGE-002, REQ-CHANGE-004, REQ-TOPO-008, REQ-TOPO-009 |
| `T12` | Reader Relocation or Replacement | `C9` (reversed orientation / direction change, PARTIAL) | **new coverage** — the relocation, replacement and recalibration cases are not asserted | REQ-CHANGE-001, REQ-CHANGE-002, REQ-CHANGE-004, REQ-CHANGE-007, REQ-TOPO-002, REQ-TOPO-008, REQ-TOPO-009, REQ-EVENT-007, REQ-STORAGE-012 |
| `T13` | 240 V Split-Phase Load | `A2`, `B8`, `C2` | fixture only | REQ-MEASURE-005, REQ-TOPO-001, REQ-MAPPING-002 |
| `T14` | Missing and Delayed Data | `C8`, `H9`, `G6` | fixture only | REQ-MEASURE-010, REQ-EVENT-006, REQ-EVENT-008 |
| `T15` | Bidirectional Power | `C7`, `F5` | **new coverage** — the reverse-flow *direction* is not asserted | REQ-MEASURE-004, REQ-MEASURE-006, REQ-RESIDUAL-001 |

### 4.3 Scenario mapping by requirement

Every requirement carrying a `**Scenario.**` line, in requirement order. Each
row names the canonical scenario and the existing matrix row or group it
extends or newly covers.

| Requirement | Scenario | Existing row/group |
| --- | --- | --- |
| REQ-TOPO-001 | `T13` | `A2`, `B8`, `C2` |
| REQ-TOPO-002 | `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-TOPO-003 | `T01`, `T02` | `C1`, `C5` |
| REQ-TOPO-004 | `T01` | `C1`, `C5` |
| REQ-TOPO-005 | `T03` | `F3`, `F4` |
| REQ-TOPO-006 | `T03` | `F3`, `F4` |
| REQ-TOPO-007 | `T02`, `T06` | `C5`, `G2`, `E5`, `G5` |
| REQ-TOPO-008 | `T11`, `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-TOPO-009 | `T11`, `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-MEASURE-004 | `T15` | `C7`, `F5` |
| REQ-MEASURE-005 | `T13` | `A2`, `B8`, `C2` |
| REQ-MEASURE-006 | `T15` | `C7`, `F5` |
| REQ-MEASURE-007 | `T05` | `H8` |
| REQ-MEASURE-010 | `T14` | `C8`, `H9`, `G6` |
| REQ-POWER-001 | `T08` | `A5`, `D5` |
| REQ-POWER-002 | `T04` | `F1`, `F2`, `A7` |
| REQ-POWER-003 | `T01` | `C1`, `C5` |
| REQ-POWER-004 | `T04` | `F1`, `F2`, `A7` |
| REQ-POWER-005 | `T10` | none |
| REQ-RESIDUAL-001 | `T15` | `C7`, `F5` |
| REQ-RESIDUAL-002 | `T03` | `F3`, `F4` |
| REQ-RESIDUAL-004 | `T03`, `T10` | `F3`, `F4` |
| REQ-RESIDUAL-006 | `T10` | none |
| REQ-RESIDUAL-007 | `T04` | `F1`, `F2`, `A7` |
| REQ-DISAGG-002 | `T06`, `T07` | `G2`, `E5`, `G5`, `I1`, `I4`, `F8` |
| REQ-DISAGG-003 | `T07` | `I1`, `I4`, `F8` |
| REQ-DISAGG-004 | `T07` | `I1`, `I4`, `F8` |
| REQ-DISAGG-005 | `T07` | `I1`, `I4`, `F8` |
| REQ-DISAGG-006 | `T06` | `G2`, `E5`, `G5` |
| REQ-STORAGE-005 | `T10` | none |
| REQ-STORAGE-012 | `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-CONFIDENCE-006 | `T05` | `H8` |
| REQ-EVENT-001 | `T10` | none |
| REQ-EVENT-002 | `T06` | `G2`, `E5`, `G5` |
| REQ-EVENT-003 | `T09` | `H6`, `G10` |
| REQ-EVENT-005 | `T01`, `T02` | `C1`, `C5` |
| REQ-EVENT-006 | `T14` | `C8`, `H9`, `G6` |
| REQ-EVENT-007 | `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-EVENT-008 | `T14` | `C8`, `H9`, `G6` |
| REQ-LEARNING-004 | `T10` | none |
| REQ-MAPPING-002 | `T13` | `A2`, `B8`, `C2` |
| REQ-MAPPING-004 | `T04`, `T10` | `F1`, `F2`, `A7` |
| REQ-CHANGE-001 | `T11`, `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-CHANGE-002 | `T11`, `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-CHANGE-004 | `T11`, `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-CHANGE-007 | `T12` | `C9` (reversed orientation / direction change, PARTIAL) |
| REQ-TEST-003 | all of `T01`–`T15` | — |

**Rule.** Every requirement that is scenario-dependent names both the canonical
`T##` identifier and the existing matrix row or group it extends or newly
covers (REQ-TEST-004). A scenario identifier that is not in §4.1 is a defect.

---

## 5. Coverage check against the target brief

| Target area | Requirements |
| --- | --- |
| Measurement semantics (active power and energy only; no V/A/PF/VAR/VA/Hz; phase; polarity; `paired_ct_id`; leg skew; reverse flow; `resolution_w`; `reliability`; three timestamps; ~15.7 s cadence) | REQ-MEASURE-001 … `010` |
| Measurement topology (typed relationships; edge lifecycle; asymmetric parent/child; correlation as evidence; unexplained parent change; no exact conservation; residual-aware inference; nested double-count prevention; versioning and rollback; change detection and reconciliation; partial observability) | REQ-TOPO-001 … `011` |
| Residuals (baseline/dynamic/noise; first-class data; own operating patterns; drift detection; learning-consumable; never redistributed) | REQ-RESIDUAL-001 … `008` |
| Device and load models (six classes; standby/idle/active/transient; `ΔP`; OFF-with-standby; no-HA devices; five distinct entities) | REQ-POWER-001 … `008` |
| Disaggregation (joint inference; simultaneous changes; identifiability; group estimates and ranges; never fabricate; double-count prevention) | REQ-DISAGG-001 … `007` |
| Storage and lifecycle (L1–L4; retention; batch writes; indexes; compression; partitioning; recovery; migration safety; replay; no matrix snapshot; no deletion of evidence) | REQ-STORAGE-001 … `012` |
| Event and inference (three event kinds; automation groups; delays; windows; cross-reader vector; late data; duplicates; incomplete groups; observed vs ingestion time) | REQ-EVENT-001 … `009` |
| Confidence and identifiability (no blend; not a probability; reconciled with the vector and the tier; all dimensions observable) | REQ-CONFIDENCE-001 … `007` |
| Mapping matrix (derived; 1:1, 1:N, N:1, N:N; per-cell fields; unknown loads; evidence-triggered updates; complexity ≠ confidence; partial observability) | REQ-MAPPING-001 … `007` |
| Dynamic topology and self-healing (eight change kinds; four version identifiers; hypothesis before promotion; history never rewritten; uncertainty preserved; rollback; reconciliation) | REQ-CHANGE-001 … `007` |
| UI (inventory; values and quality; topology; residuals and unknown loads; models; mappings; four-state estimates; identifiability; events and evidence; version history and suspected changes; manual correction; storage and processing health; view states; no fabricated zero; no decorative dashboards) | REQ-UI-001 … `013` |
| Deployment (build; configuration; migrations; backup; procedure; smoke tests; rollback; monitoring) | REQ-DEPLOY-001 … `008` |
| Testing (unit; integration; synthetic T01–T15; the T↔matrix rule; replay; regression and type-check delta; negative verification; migration; performance; no real-house claim) | REQ-TEST-001 … `010` |
| Foundation (approved status; additive contracts; the change workflow; no new dependency or service; the four power states; one owner per file) | REQ-FOUNDATION-001 … `006` |

---

## 6. Requirement-to-gap traceability

| Gap | Requirements |
| --- | --- |
| G1 | REQ-MEASURE-001, REQ-LEARNING-005 |
| G2 | REQ-LEARNING-005 |
| G3 | REQ-MEASURE-001, REQ-DISAGG-007 |
| G4 | REQ-EVENT-008 |
| G5 | REQ-EVENT-004, REQ-LEARNING-006 |
| G6 | REQ-EVENT-007 |
| G7 | REQ-EVENT-002 |
| G8 | REQ-TOPO-001, REQ-CONFIDENCE-007 |
| G9 | REQ-CHANGE-004, REQ-STORAGE-003 |
| G10 | REQ-TOPO-011, REQ-CONFIDENCE-004 |
| G11 | REQ-TOPO-004, REQ-TOPO-005 |
| G12 | REQ-RESIDUAL-002 |
| G13 | REQ-RESIDUAL-003 |
| G14 | REQ-CONFIDENCE-001, REQ-CONFIDENCE-003, REQ-CONFIDENCE-005 |
| G15 | REQ-STORAGE-007, REQ-CHANGE-003 |
| G16 | REQ-STORAGE-005, REQ-STORAGE-003 |
| G17 | REQ-STORAGE-012, REQ-STORAGE-009 |
| G18 | REQ-LEARNING-005, REQ-TEST-003 |
| G19 | REQ-POWER-002, REQ-TEST-003 |
| G20 | REQ-LEARNING-006 |
| G21 | REQ-TOPO-002, REQ-CONFIDENCE-007 |
| G22 | REQ-TOPO-008, REQ-CHANGE-006 |
| G23 | REQ-MAPPING-001, REQ-MAPPING-003 |
| G24 | REQ-RESIDUAL-006, REQ-MAPPING-004 |
| G25 | REQ-STORAGE-001, REQ-STORAGE-002 |
| G26 | REQ-CHANGE-003 |
| G27 | REQ-UI-001 … `REQ-UI-013` |
| G28 | REQ-CHANGE-001 |
| G29 | REQ-LEARNING-005, REQ-MEASURE-001 |
| G30 | REQ-EVENT-005 |
| G31 | REQ-FOUNDATION-005, REQ-UI-007 |
| G32 | REQ-TEST-010, REQ-FOUNDATION-005 |

---

## 7. Related documents

- [`README.md`](README.md) — the specification index and the conventions.
- [`00-project-overview.md`](00-project-overview.md) — goals, scope, invariants,
  risks and the open decisions referenced by `OD-03` and `OD-01`.
- [`02-architecture.md`](02-architecture.md) — the architecture that carries
  these requirements.
- `docs/assessment_gap_analysis.md` — the `G1`–`G20` gap register.
- `docs/implementation_plan_v2.md` — the governing decisions and the seven
  acceptance gates.
- `docs/electrical_model.md` — the measurement model, double counting and
  identifiability.
- `docs/residual_analysis.md` — the signed residual, baseline versus dynamic.
- `docs/confidence_model.md` — the five dimensions and why there is no aggregate.
- `docs/association_and_topology.md` — the relation kinds and the evidence bar.
- `docs/event_isolation.md` — the two windows and the isolation verdict.
- `docs/limitations_v2.md` — what the shipped system does not do.
- `docs/test_scenario_matrix.md` — the existing `A1`–`J9` matrix.
- `docs/interface_contracts.md` — the frozen contracts and the change protocol.
