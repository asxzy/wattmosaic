# E3: live level-containment gate and sibling-parent ambiguity

Status: approved. Applies on top of commit 7a8bd9a (dynamic topology with the
round-2 evidence fixes).

## Background (real-history replay, 10 days)

After the round-2 fixes, step evidence on true pairs is clean (about 1 %
conflict). Three general failures remain. Engine code must still not use names
or id patterns.

1. **Direction is unidentified for feeders.**
   - A sub-panel feeder S under main M gets support for both `M ⊃ S` and
     `S ⊃ M`, because each of S's steps moves M equally.
   - `_direction_cap` therefore keeps the true edge PROVISIONAL (support
     2103/19 against a reverse of 1651/67).
   - The level series decide direction physically: `M(t) >= S(t)` holds always,
     and `S(t) >= M(t)` rarely does.
   - `_containment_gate` exists, but the runtime never passes `absolute`, so it
     is never evaluated live.
   - The same gate blocks wrong cross-leg pairs. Raw containment fractions
     include 0.744 for one main over the other and 0.70 for a main over the
     other leg's sub feeder.
2. **Leg-unidentifiable children become structural under one arbitrary main.**
   The affected children are the two legs of a 240 V pair, or a one-leg ×2 load
   scaled by `r/f`.
   - Both mains move by the same amount for every step.
   - Both mains contain the child's level.
   - No data can tell which main the measured leg sits on.
   - The engine nevertheless made a specific crossing STRUCTURAL; for the pair,
     the opposite crossing to the installer's wiring.
   - The resolve-level P5 rule did not prevent it, because `_lifecycle` reaches
     STRUCTURAL from evidence alone.
3. Grandparent placements (`ancestor_only`) are mostly a consequence of item 1
   and should improve with it.

## 1. Live, skew-tolerant level containment

- `electrical/nesting_math.py`: add
  `containment_tolerant(outer, inner, *, lag=1, tol_w=CONTAINMENT_TOL_W, min_outer_w=CONTAINMENT_MIN_PARENT_W) -> tuple[float, int]`.
  - It works like `containment`, but instant `i` passes when
    `inner[i] <= max(outer[i-lag : i+lag+1]) + tol_w`, taken over the finite
    values only.
  - This absorbs one grid step (30 s) of cross-device skew.
  - Vectorise with numpy, as `containment` does. Unit test it.
- `_containment_gate` (`electrical/topology_inference.py`) uses
  `containment_tolerant`.
  - It is evaluated only when `comparable >= CONTAINMENT_MIN_INSTANTS`, a new
    constant of 120 (one hour of 30 s buckets). Otherwise it returns True ("not
    evaluated"), as today.
  - The threshold stays `CONTAINMENT_MIN` (0.995).
  - Also export a helper `_containment_fraction(parent, child) -> (fraction, comparable)`.
- Runtime (`core/runtime.py`): pass `absolute` into `observe_group` / settle from
  `_t5_aligned_series()`. Mirror how `structure=` is passed today, and reuse the
  same object between rebuilds so caching holds.
  - Before passing it, scale each **child** series by `1 / multiplier` of the
    monitor-applied factor in force at each bucket's time (P3's registry). If a
    reader has one factor interval covering the whole window, a scalar multiply
    is enough.
  - Parent series are used as-is.
  - Keep it cheap. It is rebuilt at most every `_T5_ALIGN_EVERY_S`; never
    rebuild per group.
- Effects inside `TopologyInference`:
  1. Promotion to PROVISIONAL or STRUCTURAL already requires `gates_ok`, which
     includes `_containment_gate`. Keep that.
  2. **Level evidence against containment:** if the gate is evaluated and fails
     with `comparable >= CONTAINMENT_DECISIVE_INSTANTS` (a new constant of 2880,
     one day), the edge state is `CONFLICTED`, with `containment` in the
     transition text. This holds regardless of step support. A child that is
     above its candidate parent's level for more than 0.5 % of a day is not
     inside it. Pinned and user edges follow the existing user rules: a user edge
     may become CONFLICTED and is never deleted.
  3. `_direction_cap` already consults the gate on the reverse edge. Once the
     reverse fails containment, the forward edge is free to become STRUCTURAL.
     Add a test proving this.
- Tests (`tests/unit/test_topology_level_gate.py`, synthetic series only):
  - The tolerant containment passes a child that leads its parent by one bucket,
    where the plain version fails.
  - A feeder pair with symmetric step support: the forward edge becomes
    STRUCTURAL and the reverse does not.
  - A cross-leg candidate whose child exceeds the parent's level 30 % of a day
    ends CONFLICTED even with clean step support.
  - Fewer than 120 comparable instants: the gate is not evaluated.
  - A ×2 child (factor row present) is compared at `r/f` and passes.

## 2. Sibling-parent ambiguity: never pick one of two indistinguishable parents

In `_settle`, after the evidence lifecycles (stage 1) and before the structural
pass:

- For each child, collect its CONTAINS edges whose evidence state is
  PROVISIONAL or STRUCTURAL and whose gate passed. Call the source readers the
  *candidate parents*.
- Two candidate parents P and Q are **ambiguous** when all of these hold:
  - neither is an ancestor of the other, through any non-CONFLICTED CONTAINS
    chain in the ledger;
  - `min(sw_P, sw_Q) >= 0.5 * max(sw_P, sw_Q)`, where `sw` is the child edge's
    `support_w`;
  - both edges have a recent conflict ratio below `CONFLICT_RATIO_PROVISIONAL`.
- When a child has an ambiguous pair, every edge in the ambiguous set is capped
  at PROVISIONAL. The transition text contains `leg_unidentified`, and the child
  is recorded in a new `TopologyInferenceView.leg_unidentified: frozenset[str]`
  (empty by default; exposed through `core/topology_api.py` edge or health
  payloads as `leg_unidentified: true` on those edges).
- Asymmetric evidence breaks the tie in the normal way. If one parent's support
  falls below half the other's, or one parent's conflict ratio rises (steps that
  move only one main), the stronger parent may become STRUCTURAL again on a
  later settle.
- A child whose candidate parents are an ancestor chain (main ⊃ sub ⊃ child) is
  **not** ambiguous. The existing one-direct-parent rule handles it.
- Tests (`tests/unit/test_topology_sibling_ambiguity.py`):
  - Two unrelated roots that both move fully for every step of a child: neither
    edge becomes STRUCTURAL, both are PROVISIONAL and the child is listed in
    `leg_unidentified`.
  - The same set-up plus 20 steps that move only root A: A ⊃ child becomes
    STRUCTURAL.
  - A main ⊃ sub ⊃ child chain is unaffected (the sub edge can be STRUCTURAL).
  - A single event is still never past CANDIDATE.

## Acceptance

- `.venv/bin/pytest -q` is fully green and `ruff check engine tools tests` is
  clean.
- `test_z3a_no_scenario_names.py` passes.
- No contract change. If `TopologyInferenceView` is a contract model, add the
  field with a default, then regenerate schemas and keep `CONTRACT_VERSION`
  4.0.0, because an additive field is a minor change. Bump the minor version if
  the schema export requires it.
