# Wave 2: integrating M1-M4 into the T5 ledger

Inputs: the wave 1 modules `electrical/topology_explain.py` (M1),
`topology_capacity.py` (M2), `topology_health.py` (M3),
`topology_regression.py` (M4), and the live ground truth below.

## Live ground truth (7 days of recorder history, 2026-11-20)

* Hot tub CT1/CT2: |abs| correlation 0.9975, dCT2/dCT1 = 1.00. Each main leg
  moves by ONE leg's watts (dL1/dCT1 = 1.02, dL2/dCT1 = 1.00). So one leg is on
  L1 and the other on L2; which is which is not identifiable from deltas.
  The current tree puts both under main L1, which violates capacity 66 % of the
  time *while the tub is on* (0.28 % over all samples: the tub is off 97 % of
  the time, so capacity must be evaluated only while the children are on).
* Sub-panel feeds are crossed: sub L1 under main L2, sub L2 under main L1.
  Event matching cannot confirm them (busy mains, sigma 12-27 W, unsynchronised
  16 s sampling: the parent looks flat in 50-78 % of big sub steps). Lag-0
  regression of differenced series separates them (r 0.55 vs 0.33 and 0.53 vs
  0.42) and capacity kills sub L2 -> main L2 (26 % violations alone).
* Sum-of-children matches are real but rare (<= 1.3 % of parent steps). A
  240 V pair must never be summed into one leg (-402 = tub CT1 -209 + CT2 -211
  on main L2 was spurious).
* A second meter on the mains (r >= 0.98, +60 W offset) is a duplicate, never a
  child.
* HA's recorder writes a row only when a value changes: never cap the hold of
  recorder history (600 s dropped 60 % of sparse readers).

## Structural resolution (new module `electrical/topology_resolve.py`)

`resolve_structure(edges, series, *, levels, scores) -> (edges, transitions)`,
pure, called from `TopologyInferenceView._settle` instead of
`_keep_direct_parents` whenever `self.absolute` holds aligned series (else the
old rule stays as the fallback):

1. Duplicate meters (M4 `duplicate_meters`) are never a parent or child of
   their twin, and a duplicate pair's member with the larger id is ignored as a
   candidate parent of anything (one meter suffices).
2. Paired legs (M2 `paired_legs`).
3. Candidates per child: inferred CONTAINS edges in CONFIRMED or PROVISIONAL;
   user/pinned edges are `fixed`.
4. `assign_parents` (M2) with `nested` from the confirmed parent->parent edges
   (transitive), `pairs`, `fixed`, `score = support - 2*conflict` plus
   `100 * r` of the M4 regression fit when available. Capacity is evaluated
   only on samples where the children assigned to the parent sum to
   >= `CAPACITY_ACTIVE_W = 300` W (add a `min_children_w` keyword to
   `capacity_violation`/`fits`, default 0.0 keeps current behaviour).
5. The chosen parent of each child: CONFIRMED if it already was, or if it is
   PROVISIONAL **and** capacity fits **and** its regression fit has
   `r >= 0.3`, `0.3 <= slope <= 1.3`, and beats every other candidate's r by
   `>= 0.1` (structural confirmation; the transition text says
   `"structural"`; beta is unchanged).
   Every other inferred candidate of that child that is CONFIRMED drops to
   PROVISIONAL. Each change is a version transition string, like
   `_keep_direct_parents`.
6. Health: any edge listed by M3 `demotions(view.health, edges)` drops to
   PROVISIONAL (transition text carries the reason).

## Step counting (live `observe_group`, and the history replay)

* Per parent P with a usable move: `known` = stepping children with a
  CONFIRMED edge P -> child (direct; user or inferred), `unknown` = other
  stepping readers that are not P, not ancestors of P, not duplicates of P.
  A stepping paired-leg couple is collapsed into ONE unknown candidate (the
  mean delta, one leg's sigma); its verdict is credited to both legs.
* Single stepping candidate: keep today's behaviour exactly
  (`_classify_step` with the step-anchored parent change) -- M1 agrees with it.
* Several: `classify_with_explanation` (M1) for the unknown, `verify_known`
  (M1) for the known; known verdicts go to both the edge evidence and
  `view.health` (M3).
* The parent's move for a multi-child group is its change across the whole
  group (earliest member `onset_lo` to latest `onset_hi`).

## Live wiring (`core/runtime.py`)

* `TopologyInferenceView` gets a `health: HealthBook` field (default empty).
  Persist it with `db.set_state("t5_edge_health", book.to_json())` after each
  fold that changed it; load it at ledger start.
* Feed `absolute=` aligned series (M4 `align`, 10 s grid, last 6 h from
  `reader_state`, recomputed at most every 300 s and cached; computing must not
  hold the GIL for more than ~50 ms per call -- ADR-0033).
* `GET /api/v1/topology/v3` edge rows gain `health` (M3 `as_dict` minus
  parent/child) for CONTAINS edges with an entry.

## History replay (`electrical/topology_history.py`, `core/discovery_api.py`)

* Cluster clean steps into groups (steps whose `hi` lie within 20 s).
* Count with the rules above; `HOLD_LOOKBACK_S` -> unlimited for recorder data.
* Before `merge_evidence`, set `absolute` on the live view to
  `align(history, max_hold_s=None)` over the lookback (10 s grid) so the
  structural pass runs on it.
* Keep the replay counting-only and fast (ADR-0033): a week of 46 readers must
  stay under ~5 s of CPU in `_replay`.
