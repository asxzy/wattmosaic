# T5 topology evidence fixes from replaying real history

Status: approved for implementation. Applies on top of the dynamic-topology
D1 rename (`TopologyEdgeState`, STRUCTURAL).

Background: replaying 10 days of real recorder history through the runtime
(`tools/dev/replay_ground_truth.py`) showed that T5 evidence itself is wrong in
several general ways:

- containment edges are made structural from coincidences;
- true feeders collect conflict;
- ×2 children never gain support;
- declared pairs are re-learned as containment.

All of these fixes are general. Engine code must not use names, entity-id
patterns or HB §17 scenario knowledge (`test_z3a_no_scenario_names.py`).

Two fixes are **already applied** in the tree before this work starts. Do not
redo them; add tests only where noted:

- **A.** In `electrical/topology_inference.py::observe_group`, the step-anchored
  one-to-one verdict wins whenever an anchored parent measurement exists. The
  `_multi_child_verdicts` result is only a fallback. A member whose step is
  inside the parent's noise (`|delta| <= _SIGMA_FACTOR * sigma_p`) is never part
  of an explaining subset.
- **S.** Speed fixes: `BackgroundExecutor` drops finished jobs; the declared
  single-subject pairs are cached per clock instant; the ledger fold upserts only
  changed edges; the harness reuses one event loop; and the replay skips day
  files outside the slice.

## P3. Monitor-applied factors in the T5 fold (`r / f`)

The runtime fold (`core/runtime.py::_fold_group_ledger` →
`TopologyInference.observe_group`) never reads the factor store. A ×2 child
reports `r` while each parent moves `r / 2`, so the edge is neutral forever.

- Build a `FactorRegistry` (`electrical/factors.py`; see
  `FactorRegistry.from_database` or the equivalent constructor) once in the
  runtime. Rebuild it when a factor is inserted (`repositories/factors.py`), or
  lazily when the factor table's row count changes.
- In the fold, look up each child's factor for the step time. Use the
  `MeasurementFactor` in force at the child's step timestamp, and only when
  `applied_by == "monitor"`, because engine-applied multipliers are already
  removed in P1 normalize. Pass a per-child `scale = 1 / multiplier` into
  `observe_group`.
- Apply `scale` to the child's step delta **and** sigma before classification
  (`_classify_step` / `ChildStep`). This applies to every pass, the multi-child
  fallback included.
- Missing factor means scale 1.0 (absent is not ×2).
- Test (`tests/unit/test_topology_factor_fold.py`): a synthetic ×2 child under a
  parent. Without a factor row it stays neutral (no support). With a
  `MeasurementFactor(multiplier=2, applied_by="monitor")` it gains support and
  reaches STRUCTURAL after enough clean steps. A factor whose interval ends
  before the step does not apply.

## P4. Declared PAIRED_LEG and SAME_CONDUCTOR constrain T5

- The fold must know the in-force user-declared `PAIRED_LEG` and
  `SAME_CONDUCTOR` edges. Read them from the active topology version or the
  topology repository once per fold, cached on the version id.
- Never create or score a CONTAINS edge between the two legs of a declared pair,
  or between an alias and its canonical reader, in either direction or in any
  pass. An existing inferred CONTAINS edge between them becomes `REJECTED` with
  the reason `declared_pair` in the transition text.
- Feed the declared pairs into the structural pass (`topology_resolve.resolve`,
  `classified.pairs` or its equivalent), so the pair rule applies from the first
  settle and not only after data-classification.
- An alias reader never becomes the parent of a CONTAINS edge while its
  canonical reader has a reading. Its evidence folds under the canonical reader;
  the simplest correct option is to skip the alias as a parent and as a child.
- Test (`tests/unit/test_topology_declared_constraints.py`):
  - with a declared pair, coincident equal steps on both legs never produce
    CONTAINS between them;
  - with a declared alias, the alias gets no CONTAINS edges while the canonical
    reader is present.

## P5. Balanced 240 V legs: never make a specific leg→parent crossing structural without asymmetric evidence

A balanced 240 V load (two measured legs, or one measured ×2 leg plus an
unmonitored leg) steps **both** mains by the same amount. Steps alone cannot
tell which leg sits under which main. The structural pass's "240 V pair
completion" rule and `_disagreement` then flip legs arbitrarily.

- In `topology_resolve.py`, a leg has a fit to two sibling parents P and Q. When
  those fits are within `STRUCTURAL_MARGIN` of each other, do the following for
  both legs of a pair, or for a single child that fits two sibling roots equally:
  - Do not make either crossing STRUCTURAL. Keep both candidate edges
    PROVISIONAL (soft).
  - Expose `leg_unidentified: true` in the transition text and in the
    structure decision record.
- Asymmetric evidence, such as a step that moves only one main, still allows
  STRUCTURAL as before.
- Test: two legs that move both mains identically end up with neither crossing
  STRUCTURAL. Once one-main-only steps for one leg are added, that leg's edge
  can become STRUCTURAL.

## P6. "Parent flat" must mean flat, not net zero, and allow cross-device lag

- `_classify_step` (and the anchored parent level change in `core/runtime.py`,
  `_anchored_level_change` or its equivalent) must return the parent's pre-window
  and post-window spread together with the level change.
- A parent whose pre or post window is not itself flat
  (`spread > _SIGMA_FACTOR * sigma_p`) is **neutral**, never conflict. A
  concurrent opposite step inside the window nets to zero and proves nothing.
- Readers on different physical devices report on different cadences. Start the
  parent's post window **one parent sample interval** after the child's step
  `hi`, using the parent's median sample interval from reader state, capped at
  30 s. That way a parent that shows the step one sample later is still
  measured.
- Test: a true feeder whose window contains an opposite concurrent step on a
  sibling is neutral, not conflict. A parent that shows the step one sample late
  gives support.

## P7. A step must be resolvable at the parent to be support

- Support requires `|beta * child_delta| > _SIGMA_FACTOR * sqrt(sigma_p**2 + sigma_c**2)`
  in addition to the existing match test. A smaller step is neutral. Such
  children stay CANDIDATE on a noisy feeder, which is honest. A wrong
  STRUCTURAL edge is worse.
- Test: a 20 W step under a parent with sigma 11 W is neutral, and a 200 W step
  matched within tolerance is support.

## Regression test for A

`tests/unit/test_topology_multi_child.py` covers two cases:

1. A group with a true feeder: the child's anchored step is matched by the
   parent, while the group's net parent move over the whole span is about 0
   (an unrelated opposite step later in the group). The verdict must be
   **support**, not conflict.
2. A 2 W member inside the parent's noise is never credited support by a
   multi-child subset explanation.

## Acceptance

- `.venv/bin/pytest -q` is fully green.
- `.venv/bin/ruff check engine tools tests` is clean.
- `test_z3a_no_scenario_names.py` passes.
- No contract change is needed. If one is unavoidable, follow CLAUDE.md
  (schemas regenerated).
