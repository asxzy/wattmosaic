# E6: one placement per child at any confidence, calibrated thresholds, current and rating rules, synchronous differencing

Status: approved (user, 2026-11-21). It applies on top of f329841. It is
delivered in two runs:

- **E6a**: §1–§5.
- **E6b**: §6–§7.

All numbers come from the 10-day reference replay and two data studies, using
the data-checked ground truth.

The final replay at f329841 made **no false placements at PROVISIONAL or
STRUCTURAL**. Every error is a miss, so thresholds can come down if the
vetoes get stronger. Engine code must not use names or id patterns.

## E6a

### 1. Exactly one placement per child, shown at any confidence (API + panel)

- In `TopologyInference` (or a new pure module `electrical/topology_placement.py`),
  compute a **placement** for each non-root reader at every settle, in this order:
  1. **Candidate parents** are readers with a CONTAINS edge to the child that is
     not CONFLICTED, REJECTED or RETIRED, that no veto blocks (§2, §3), and that
     is not a declared alias or declared paired leg of the child.
  2. **Score** each candidate as `score = max(wlift, 0) * (s + 0.5) / (s + k + 1)`.
     - `s` and `k` are the edge's support and conflict counts.
     - `wlift` comes from E4 `PairStats`. When `n < 8`, use `0.05`.
  3. **Prefer the deeper parent in a chain.** If the top candidates form a
     CONTAINS chain whose links clear PROVISIONAL, place the child under the
     deepest one.
  4. **Exactly one parent per child.** The argmax wins. The placement is
     acyclic: never place a reader under its own descendant.
  5. **Confidence band**:

     | Band | Condition |
     |---|---|
     | `high` | edge STRUCTURAL, or score ≥ 0.15 |
     | `medium` | 0.05 ≤ score < 0.15 |
     | `low` | score < 0.05 |
     | `unplaced` | no candidate |

     On the reference install: 17 of 17 at ≥ 0.15, 4 of 4 in the medium band,
     and 2 of 2 low were all correct.
  6. **Leg-unidentified children** (E3, balanced 240 V legs, one-leg ×2 loads)
     get placement `both_legs` with the two sibling parents listed, band
     `medium`, and reason `leg_unidentified`.
- Placement is *display and diagnostic* only. H still uses STRUCTURAL edges. Do
  not change H.
- API (`core/topology_api.py`, `GET /topology/tree` and `/topology/v3`): each node
  gets `placement: {parent, band, score, reason, alternatives: [{parent, score}]}`.
  The tree endpoint builds the hierarchy from placements, not only from
  STRUCTURAL edges, and keeps a field telling which links are structural.
- Panel (`addon/ui/view-topology.js` and the hierarchy card):
  - draw every placed child under its single parent;
  - style the link by band: solid for high, dashed for medium, dotted with a
    "low confidence" chip for low;
  - draw `both_legs` children under both roots with a "240 V, leg not
    identifiable" chip;
  - list unplaced readers separately.
  - The panel must keep passing `check_panel.py`: no inline script or style, and
    no external origins. Update the panel tests.

### 2. Calibrated lifecycle thresholds (DEC-10 amended; record it in ADR-0045)

| Constant | Old | New |
|---|---|---|
| Containment veto (E4 informative) | > 0 per 1000 caps at CANDIDATE | **≥ 10 per 1000** over ≥ 120 informative buckets vetoes. Drop the "> 0 caps" rule. CONFLICTED stays at ≥ 5 per 1000 over ≥ 2880. |
| PROVISIONAL | support_w ≥ 3, r < 0.10 | **support_w ≥ 2, r ≤ 0.34, wlift ≥ 0.10** (when n ≥ 8) |
| STRUCTURAL | support_w ≥ 8, r < 0.05 | **support_w ≥ 3, r ≤ 0.10, wlift ≥ 0.13** (when n ≥ 8), plus all vetoes |
| Lift substitution (new) | — | When the pair analysis has n ≥ 8 steps, wlift ≥ 0.13, match ≥ 0.3 and < 2 violations per 1000, the edge may be PROVISIONAL even with support_w < 2. That evidence comes from many steps, so the one-event invariant holds. |

- The invariant still holds: **one event never passes CANDIDATE.** support_w ≥ 2
  needs two verdicts, and lift substitution needs at least 8 steps.
- Keep the hysteresis band proportional: KEEP_SUPPORT_STRUCTURAL = 2 and
  KEEP_CONFLICT_RATIO_STRUCTURAL = 0.15.
- Expected on the reference install: direct recall 0.57 → 0.74 at STRUCTURAL and
  0.87 at PROVISIONAL, with 0 false placements.

### 3. Current rules (needs E5 companions; skip silently without current)

All of these use raw current, which is factor-free and polarity-free, on 1-minute
means from the companion ring.

1. **Current containment veto.** Count minutes where
   `|I_child| > 1.03·|I_parent| + 0.3 A`. At ≥ 10 per 1000 over ≥ 120 minutes,
   veto (the same effect as the power containment veto). AUC 0.999; 0 true pairs
   are vetoed. It catches a reversed CT that the power veto misses.
2. **Set-current KCL** in the structural pass. For a parent and its assigned
   children, `Σ|I_children| ≤ 1.03·|I_parent| + 0.5 A` must hold on ≥ 99 % of
   minutes. It runs alongside the power KCL, which over-counts ×2 children. True
   sets violate it on 0–0.25 % of minutes. If an assignment breaks it, reject the
   child with the smallest score.
3. **Rating classes** per reader from the companion ring: peak current, and the
   maximum of 5-minute means.
   - **Branch limit:** 5-min > 12 A or peak > 16 A ⇒ `not_branch_circuit`.
     This replaces E5's 12 A-only rule. It made 0 false flags on 21 branch
     circuits.
   - **Child above parent:** if the child's 5-min max is greater than the
     parent's 5-min max × 1.05 + 0.5 A, veto. That removed 349 of 637 false
     pairs and no true pair.
   - **Roots:** readers of the highest observed class (above about 60 A sustained)
     that are contained by no reader are the roots. Expose `rating_class` and
     `role_hint` (`service` / `feeder` / `appliance_240v` / `branch`) per reader.
   - **Feeder vs 240 V appliance:** a high-class reader with median current
     ≥ 1 A is a `feeder`; one with median < 0.5 A is an `appliance_240v`. The hint
     only breaks ties when choosing a parent.

### 4. Inferred duplicates (SAME_CONDUCTOR)

- Infer `SAME_CONDUCTOR` between two readers when **all** of these hold:
  - correlation of 2-minute means ≥ 0.985 over ≥ 24 h of overlap;
  - slope between 0.9 and 1.1, and |offset| ≤ 150 W;
  - each is the other's best match;
  - the two are **not co-sampled**, meaning they are on different monitors (§6
    co-sampling).
- Two co-sampled readers that pass are the legs of a balanced 240 V pair, not
  duplicates.
- An inferred edge is `declared_by="inference"` and goes through the normal
  lifecycle. A declaration always wins.
- On the reference install: VEC-1 L1/L2 against the main panel CTs has
  correlation 0.992 / 0.996 and slope 0.95 / 0.97, with VEC reading about 100 W
  per leg higher. That is an affine copy, so the rule must not require equality
  within CT tolerance.
- Expose the fitted slope and offset on the edge (`evidence_detail`).

### 5. Leg classifier guard

A Refoss channel's voltage entity is a copy of its **configured** phase input. It
disagreed with the physical leg on 8 of 28 readers. Make sure
`electrical/leg_classifier.py` derives a reader's leg **only** from the reader's
own ΔP against the reference voltages' ΔV (current-driven sag). It must never use
which voltage group the reader's own voltage entity belongs to. Add a test where
a reader's voltage entity is in the L group but its steps sag R, and assert that
it is classified R.

## E6b

### 6. Co-sampling and synchronous differencing (largest measured gain)

- **Co-sampling.** Two readers are co-sampled when ≥ 90 % of the child's samples
  have a parent sample within 2 s, which is the case for channels of one monitor
  (they publish in one batch). The runtime tracks this cheaply from sample
  timestamps in reader state, and refreshes it at aligned rebuilds.
- **Synchronous verdict.** For co-sampled pairs, the T5 fold measures the parent's
  change across **exactly the child's sample pair**: the parent sample at or just
  before the child's pre-step sample, and at or just after its post-step sample.
  It applies no window and no P6 spread veto.
  - support: |Δp − Δc·s_c| ≤ 0.10·|Δc·s_c| + 15 W, with the same sign;
  - conflict: |Δp| ≤ 3σ_p while |Δc| is resolvable;
  - otherwise neutral.
  - Pairs that are not co-sampled keep today's anchored-window verdict.
  - Measured: AUC 0.817 → 0.942, true-placement count 15 → 20 of 27, false
    2 of 23 (it was 4).
- **Pulse evidence.** For co-sampled pairs, a confirmed single-sample pulse (a
  step reversed at the next sample) produces two synchronous verdicts instead of
  being dropped as a transient. The P3 step detector keeps its behaviour for
  everything else. Add a per-reader pulse ring fed from the fast path (O(1)),
  consumed by the fold.
  - Measured on pulse steps: AUC 0.949 and support precision 93 %.
  - This is what makes automation-alternating and pulsing loads, such as
    bidets, small plugs and the anti-phase pair, visible.
- **Contrast ranking.** In §1 placement, when candidate parents are co-sampled
  with the child, multiply each score by `(1 − sync_conflict_ratio)`. On the
  reference install this picked the right parent for 19 of 20 single-parent
  children.
- Tests (synthetic):
  - two co-sampled readers where the parent's window contains an unrelated
    opposite step still give support (no spread veto);
  - a single-sample pulse gives support on the true parent and conflict on a
    flat wrong parent;
  - non-co-sampled pairs are unchanged;
  - contrast ranking picks the lower-conflict parent.

### 7. Docs

- ADR-0045 records the calibrated thresholds and the "placement versus structure"
  split, and amends DEC-10. Add its README line.
- Update the `docs/specs/topology-principles/README.md` table with these rules.
- Add a `RELEASE_NOTES.md` entry under Unreleased.

## Acceptance (each run)

- Full suite, ruff, `check_panel.py`, `check_release.py`,
  `render_validation_docs.py --check` and `export_schemas.py` all pass, with no
  diff.
- `perf_runtime.py --quick` shows fast-path p99 < 200 µs.
- A one-day replay (`--since 2026-11-12T00:00:00Z --until 2026-11-13T00:00:00Z`)
  finishes in under 6 minutes.
- No contract change unless one is needed. Placement lives in API payload dicts.
