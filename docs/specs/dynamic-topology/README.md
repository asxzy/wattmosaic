# Dynamic topology: remove topology "confirmation"

Status: approved for implementation. Contract 3.0 → **4.0.0** (breaking).

## Why

Topology must follow the evidence in both directions, at any time. Today a
topology edge reaches `EdgeLifecycleV3.CONFIRMED` and then mostly stays there:

- `EdgeEvidence` counters are lifetime totals and never decay. A 300-support
  edge needs about 100 conflicts before its ratio moves.
- `_settle` in `electrical/topology_inference.py` keeps an edge CONFIRMED when
  the persisted structural decision chose it, even after the evidence fell to
  PROVISIONAL ("sticky structural confirmation"). `structure_decisions` survives
  restarts, and "missing series never demote a decision".
- `CONFIRMED` is presented as a terminal truth to users through the API, the
  integration and the panel, alongside "requires_confirmation" and
  "No confirmed edges yet".

After this change there is no "confirmed topology". An edge's state is
recomputed from recent evidence at every settle and can move up or down.
"Structural" means only that H uses the edge **right now**.

Invariants that still hold (CLAUDE.md, HB §4):

- One event never moves an edge past CANDIDATE.
- A parent-only change is neutral. It is never evidence against containment,
  and it does not decay evidence.
- PROVISIONAL edges are soft.
- Topology, mapping and model versions stay independent.
- REJECTED and pinned are explicit user overrides and stay hard. Pin is the
  only way to freeze an edge.
- Absent is `None`. Replay stays deterministic: decay is by **verdict count**,
  never wall clock, so quiet periods do not erode a true edge and replay needs
  no clock.

The work is split into two stages, delivered as two separate runs. **D1** is
mechanical and behaviour-preserving. **D2** changes the behaviour.

---

## D1: split the topology state enum and rename CONFIRMED → STRUCTURAL

No behaviour change. Every existing test must still pass after only mechanical
renames in tests.

1. `contracts/topology.py`:
   - Add `class TopologyEdgeState(str, Enum)` with members `CANDIDATE`,
     `PROVISIONAL`, `STRUCTURAL = "structural"`, `CONFLICTED`,
     `SUSPECTED_CHANGE`, `REJECTED` and `RETIRED`. Values are lower-case like
     the existing ones.
   - Change `TopologyEdgeV3.lifecycle` to `TopologyEdgeState`.
   - Add a `field_validator("lifecycle", mode="before")` that maps the legacy
     string/enum `"confirmed"` to `"structural"`, so old stored JSON and old
     version payloads still load.
   - `MappingEdge.lifecycle` **keeps** `EdgeLifecycleV3`, including its
     `CONFIRMED`. Mapping confirmation is a different concept and is out of
     scope.
   - Export `TopologyEdgeState` from the contracts facade.
2. `contracts/foundation.py`: `CONTRACT_VERSION = "4.0.0"`. Grep for consumers
   that compare the contract version (integration `const.py`, api health,
   tests) and update them.
3. Replace every **topology** use of `EdgeLifecycleV3.X` with
   `TopologyEdgeState.X`, and `CONFIRMED` with `STRUCTURAL`. The scope is
   `electrical/`, `inference/`, `realtime/grouping.py`, `mapping/projection.py`
   (where it reads topology edges), `mapping/display_tree.py`, `versioning/`
   (the topology parts), `core/`, `api/`, `runtime/interfaces.py` (rename the
   `CONFIRMED` tuple to `STRUCTURAL`) and `learning/`. Keep `EdgeLifecycleV3`
   where the object is a `MappingEdge` (`mapping/updater.py`,
   `mapping/reconcile.py`, `versioning/service.py::_confirm_mapping_edge`).
   When unsure, check the type of the object whose `.lifecycle` is compared.
   Rename topology-scoped identifiers too, e.g. `MIN_SUPPORT_CONFIRMED` →
   `MIN_SUPPORT_STRUCTURAL`, `CONFLICT_RATIO_CONFIRMED` →
   `CONFLICT_RATIO_STRUCTURAL`, `STRUCTURAL_CONFIRM_*` →
   `STRUCTURAL_PROMOTE_*` and `CONFIRMED_LIFECYCLES` → `STRUCTURAL_LIFECYCLES`.
   Update docstrings and comments that say a topology edge is "confirmed".
   **Do not touch the unrelated uses:**
   - P3 step confirmation and `EventGroup` CONFIRMED in `realtime/`;
   - `MatchState.CONFIRMED` in `inference/matching.py`;
   - learning observations;
   - `ChangeStatus.CONFIRMED` for configuration events;
   - endpoint scope confirm, reset-db confirm and unknown-profile wording.
4. Persisted strings: storage and repositories that write or read
   `topology_edges.lifecycle` must write `"structural"`. SQL filters such as
   `lifecycle = 'confirmed'` on `topology_edges` become `'structural'`.
5. New migration `storage/migrations/v029_dynamic_topology.py`, exporting
   `MIGRATION` (version 29) and `TIERS`. Copy the style of `v028_*`.
   - `UPDATE topology_edges SET lifecycle='structural' WHERE lifecycle='confirmed'`.
   - Rewrite `evidence_json` of every `topology_edges` row to add
     `support_w` / `conflict_w` floats (see D2 §1). If decisive
     `n = support + conflict` is at most `EVIDENCE_WINDOW` (20), use
     `support_w = support` and `conflict_w = conflict`. Otherwise scale both by
     `20 / n`. Rows that already have `support_w` are left untouched, which
     keeps the migration idempotent.
   - Delete the persisted structure-decision state. Find the key the runtime
     writes (`t5_structure_decisions` in `core/runtime.py` /
     `core/discovery_api.py`) and the table it lives in. D2 stops using it as a
     lock.
   - `mapping_edges` is untouched. No `VACUUM`. Use the helpers other
     migrations use.
   - Add the migration to the coverage in `tests/unit/test_migration_matrix.py`
     the way v028 is covered. Add a focused test
     `tests/unit/test_migration_v029.py` covering the confirmed→structural
     rewrite, the evidence rescale, the structure-decision key removal and the
     idempotent re-run.
6. Regenerate schemas with `.venv/bin/python tools/dev/export_schemas.py`.
   Never hand-edit `data/schemas/`.
7. Tests: update the topology assertions mechanically (`CONFIRMED` →
   `STRUCTURAL` on topology edges, `"confirmed"` → `"structural"` in topology
   JSON fixtures). Never weaken an assertion. `tests/support/hb17_seed.py` and
   `tests/support/fakes.py` follow the new enum.
8. Panel (`addon/ui/*.js`) and integration (`custom_components/wattmosaic`)
   read topology lifecycle strings. Accept both `"structural"` and the legacy
   `"confirmed"` wherever they compare a topology lifecycle. D2 removes the
   wording.

D1 acceptance:

- `.venv/bin/pytest -q` is fully green.
- `.venv/bin/ruff check engine tools tests` is clean.
- `python tools/dev/export_schemas.py && git diff --exit-code data/schemas` is
  clean after the commit.
- `python tools/dev/check_panel.py` passes.
- `grep -rn "EdgeLifecycleV3.CONFIRMED" engine/wattmosaic_engine` only matches
  mapping-edge code.

---

## D2: dynamic edge state

### 1. Decaying evidence (`EdgeEvidence`, the T5 fold)

- `EdgeEvidence` gains `support_w: float = 0.0` and `conflict_w: float = 0.0`.
  The integer `support`, `conflict`, `parent_only` and `independent_causes`
  stay as lifetime audit counters.
- `EVIDENCE_DECAY = 0.95` lives in `electrical/topology_inference.py` and is
  exported. `EVIDENCE_WINDOW = 20` is `1 / (1 - EVIDENCE_DECAY)`, the
  steady-state ceiling.
- Each **decisive** verdict folded into an edge (support or conflict) does the
  following:
  - `support_w = EVIDENCE_DECAY * support_w + (1 if support else 0)`;
  - `conflict_w = EVIDENCE_DECAY * conflict_w + (1 if conflict else 0)`.
  Find every place the fold increments `support` or `conflict` and update the
  weights there in the same `model_copy`.
- A parent-only verdict increments `parent_only` only. It neither decays nor
  adds weight.
- `EdgeEvidence.conflict_ratio` uses the weights when `support_w + conflict_w > 0`,
  otherwise the counts.
- Add a read-only property `strength = support_w` for APIs.

### 2. Edge state is a pure function of recent evidence (`_lifecycle`)

Rewrite `TopologyInference._lifecycle` so that it is a function of
`(edge.evidence weights, gates, current state)` only. Let `sw` and `cw` be the
weights and `r = cw / (sw + cw)`:

| Result | Rule |
|---|---|
| STRUCTURAL | `sw >= MIN_SUPPORT_STRUCTURAL` (8) and `r < CONFLICT_RATIO_STRUCTURAL` (0.05), with the existing gates (independent causes ≥ 2, absolute containment, no scaled copy, known basis, direction cap) |
| stays STRUCTURAL (hysteresis band) | current state is STRUCTURAL, `sw >= KEEP_SUPPORT_STRUCTURAL` (6), `r < KEEP_CONFLICT_RATIO_STRUCTURAL` (0.10) and the gates still pass |
| CONFLICTED | `sw + cw >= MIN_EVENTS_CONFLICTED` (6) and `r > CONFLICT_RATIO_CONFLICTED` (0.25) |
| PROVISIONAL | `sw >= MIN_SUPPORT_PROVISIONAL` (3) and `r < CONFLICT_RATIO_PROVISIONAL` (0.10) |
| CANDIDATE | otherwise, while `support >= 1` |

- A CONFLICTED edge whose recent evidence recovers moves back up through
  PROVISIONAL and STRUCTURAL by the same table. CONFLICTED is no longer an
  absorbing state for inferred edges.
- One verdict gives `sw <= 1`, so one event never passes CANDIDATE.
- **User-declared edges:** `declared_by == "user"` keeps STRUCTURAL as a
  *prior*. The edge is STRUCTURAL unless the weighted evidence makes it
  CONFLICTED by the table above, and it returns to STRUCTURAL once the
  evidence recovers. (Today it also never goes below CONFLICTED. Keep that.)
- **Pinned edges** stay frozen at their state. Pin is the explicit override.
  The API must report their conflict ratio so the user can see a disagreement.
- **REJECTED / RETIRED** stay dormant.

### 3. Remove the sticky structural lock

- In `_settle` (`electrical/topology_inference.py`), delete the
  "sticky structural confirmation" block that turns PROVISIONAL back into
  CONFIRMED because `decided` chose the edge. Remove the `sticky` set wherever
  it only exists to carry that lock. Keep the parameter if `resolve()` needs it
  for other reasons, but it must not raise a lifecycle above the evidence
  state.
- In the structural pass (`electrical/topology_resolve.py::resolve` and helpers),
  remembered decisions (`decided`, `previous`) may only be used to:
  - **choose** between candidate parents of one child (tie-break /
    one-direct-parent rule);
  - **demote** a grandparent edge to PROVISIONAL as transitive.
  They must never keep an edge STRUCTURAL whose evidence state (§2) is below
  PROVISIONAL, and never hold one STRUCTURAL when there are no series.
  Regression-based promotion of a PROVISIONAL edge to STRUCTURAL is still
  allowed in a pass where aligned series are available, because that is fresh
  evidence. When the next settle's series no longer support it, it falls back.
- Without series (`_apply_decisions` path), decisions may only choose among
  edges that are already STRUCTURAL by evidence. They must not promote.
- Stop persisting structure decisions as a lifecycle source. Remove the
  persisted `t5_structure_decisions` save and load in `core/runtime.py` and
  `core/discovery_api.py`, or keep it purely as a cache that no lifecycle reads.
  The v029 migration deletes the old key.
- Health demotion (`electrical/topology_health.py`) applies to inferred and
  user-declared unpinned edges. It still demotes to PROVISIONAL.

### 4. Versions

- A topology version is still proposed and promoted when the STRUCTURAL edge set
  changes (entering or leaving STRUCTURAL, or STRUCTURAL→CONFLICTED).
- Debounce: at most one inferred version per `TOPOLOGY_VERSION_DEBOUNCE_S`
  (default 900 s, from the injected clock) unless the change removes an edge
  from STRUCTURAL. Coalesce the transitions inside the window.
- Mapping and model versions are untouched.

### 5. Remove the confirmation concept from APIs, integration, UI and docs

Engine:

- Delete `NestingCandidate.requires_confirmation` and its serialisation.
- Delete `edges_confirmed_for` (`core/topology_api.py`) and the unused
  `edges_confirmed_for` hook in `learning/sample_filter.py`.
- Topology `/health`-style payloads (`core/topology_api.py` around
  `status: confirmed|candidate`): replace it with `"state": <TopologyEdgeState value>`
  plus `"strength": support_w` and `"conflict_ratio"`. Count summaries as
  `structural_edges`.

Integration (`custom_components/wattmosaic`):

- `topology_confirmed` / `CONF_TOPOLOGY_CONFIRMED` / the sensor attribute
  become `topology_structural_edges` (int). Update `coordinator.py`,
  `const.py`, `diagnostics.py`, `util.py`, `entity_values.py`, `sensor.py` and
  the static tests.

Panel (`addon/ui`):

- Remove the "No confirmed edges yet" notice. When there are no STRUCTURAL
  edges, show "Topology is still being learned from events" instead.
- `edgeConfirmed` becomes `edgeStructural`.
- The "Accept" button (which calls `/pin`) becomes "Pin". Keep Reject.
- Show each edge's state and strength.
- `topology_confirm_optional` in `core.js` is removed.
- `check_panel.py` must pass. If `check_panel.py` or `test_docs_vocabulary.py`
  has a retired-vocabulary list, add the phrase "confirmed topology" to it.

Docs:

- New ADR `docs/decisions/` entry, ADR-0043 "Topology is dynamic; there is no
  topology confirmation". It supersedes ADR-0008 and amends ADR-0037. Add the
  README index line.
- Amend HB §4.3 ("A single event must not immediately confirm an edge" becomes
  "must not make an edge structural") and the DEC-10 text in
  `docs/specs/realtime-topology-disaggregation/{handbook,decisions,design}.md`.
- Add a `RELEASE_NOTES.md` entry in the current top section (append-only).

### 6. Tests (D2)

New `tests/unit/test_topology_dynamic.py`, built on synthetic verdicts through
the public `TopologyInference` API, the same way existing
`test_topology_inference.py` drives it:

1. 1 support verdict leaves the edge CANDIDATE. 12 clean supports make it
   STRUCTURAL.
2. Moving a plug: after 20 supports, 8 consecutive conflicts move the edge out
   of STRUCTURAL, and 15 more make it CONFLICTED.
3. Recovery: a CONFLICTED inferred edge that gets 25 clean supports becomes
   STRUCTURAL again.
4. Parent-only verdicts (any number) change neither the state nor
   `support_w`/`conflict_w`.
5. A structurally `decided` edge whose evidence falls to PROVISIONAL is
   PROVISIONAL after settle (the old sticky behaviour is gone).
6. A pinned edge stays frozen and its conflict ratio is reported.
7. A user-declared edge goes to CONFLICTED under sustained conflict and comes
   back once the evidence recovers.
8. The version debounce coalesces two promotions within 900 s into one version.

Existing tests that encode stickiness (`test_topology_structure_memory_fixes.py`,
`test_topology_w2_fixes.py` and others that fail because of §2/§3) must be
**rewritten to assert the new dynamic behaviour**, with a comment citing
ADR-0043. They must not be deleted or loosened. Any assertion that is
unrelated to stickiness must still hold.

D2 acceptance:

- the full `.venv/bin/pytest -q` is green;
- ruff is clean;
- `check_panel.py`, `check_release.py` and `render_validation_docs.py --check`
  pass;
- the schemas match after export.
