# E4: chance-corrected step lift, residual mirroring, scaled set-KCL and an informative containment veto

Status: approved. Applies on top of 48ce886. Background and measurements are in
`docs/specs/topology-principles/README.md`. Engine code must not use names or id
patterns. All new analysis runs on the aligned 30 s series (`_t5_aligned_series`
/ `_t5_containment_series`). It is computed **once per aligned rebuild**, at most
every `_T5_ALIGN_EVERY_S`, and cached on the series object like the containment
memo. It is never computed per group.

## 1. Pair analysis module: `electrical/topology_pairs.py` (new, pure, numpy)

`analyse_pairs(series: Mapping[str, np.ndarray], scale: Mapping[str, float], structural_children: Mapping[str, frozenset[str]], *, bucket_s=30) -> PairAnalysis`

- `series` holds the absolute levels on one grid, with NaN for absent buckets.
  `scale[r]` is `1/f`, the monitor factor, applied to `r` whenever `r` is a child.
- **Steps**: for each reader, take the buckets where
  `|Δ| = |x[i] - x[i-1]| > max(3 * sigma_r, 20 W)`. `sigma_r` is the robust
  (MAD-based) σ of that reader's Δ series.
- **Step lift** `lift(p, c)`, P2 chance-corrected:
  - Let `m0` be the fraction of `c`'s steps where `p`'s Δ over the buckets
    `[i-1, i+1]` (best of the three) is within `0.10*|Δc·s_c| + 15 W` of `Δc·s_c`
    with the same sign.
  - Let `mk` be the same fraction with `c`'s step times shifted by
    `k ∈ {±10, ±20, ±40}` buckets (5–20 min).
  - Then `lift = m0 - mean(mk)` and `match = m0`. `n = number of c steps`.
- **Residual mirroring** `mirror(p, c)` (P6): the same lift computation, but
  against `residual_p = p − Σ_{k ∈ structural_children[p], k ≠ c} k·s_k`,
  ignoring NaN. It is defined only when `p` has at least one structural child
  other than `c`. Otherwise it is `None`.
- **Informative containment** `contain(p, c)` (P1, veto form):
  - Over buckets where `c·s_c > percentile_5(p)` (informative), count violations
    `c·s_c > max(p[i-1:i+2]) + 15 W + 0.02 * |p|`.
  - The result is `(violations, informative_n)`.
- **Set KCL** `kcl(p, children)` (P5): the fraction of buckets where
  `p − Σ children·s ≥ −(30 W + 0.03 * |p|)`, using 1-minute means (pairs of
  buckets), together with the count.
- `PairAnalysis` exposes these lookups as dict-backed methods. Compute lift,
  mirror and contain only for candidate pairs (pairs that have a ledger edge, in
  either direction), never for all n² pairs.
- Tests (`tests/unit/test_topology_pairs.py`, synthetic numpy series):
  - a true child has high lift and a coincidental busy feed has lift ≈ 0;
  - mirroring finds a child hidden under siblings;
  - one violation outside the tolerance is counted, and skew of ±1 bucket is not;
  - KCL flags a double-counted set;
  - scale `1/f` is honoured.

## 2. Use in `TopologyInference`

- The runtime computes `PairAnalysis` at each aligned rebuild, using the current
  STRUCTURAL CONTAINS children per parent, and passes it to `observe_group` /
  settle as `pairs=`. Absent means behaviour as today.
- **Promotion gate** (inferred CONTAINS only, in `_lifecycle` `gates_ok`). When
  `pairs` is present and `n ≥ STEP_LIFT_MIN_STEPS` (8):
  - require `lift ≥ STEP_LIFT_MIN` (0.14) and `match ≥ STEP_MATCH_MIN` (0.5) for
    PROVISIONAL and STRUCTURAL.
  - With fewer steps the gate is not evaluated. The existing verdict thresholds
    still apply.
- **Containment veto**: when `informative_n ≥ 120`:
  - more than 0 violations per 1000 informative buckets caps the edge at
    CANDIDATE;
  - at least 5 per 1000 over `≥ 2880` informative buckets makes it CONFLICTED
    (reason `containment`).
  This replaces the E3 fraction rule for edges where `pairs` is available. Keep
  E3 as the fallback.
- **Set-KCL veto in the structural pass** (`topology_resolve` / `topology_capacity`):
  - `_structure_source` must pass **factor-scaled** child series. Fix the
    unscaled `aligned` preference.
  - When assigning a child to a parent would drop that parent's `kcl` below
    `KCL_MIN` (0.99) over at least 1440 one-minute samples, the assignment is
    rejected for that child.
  - This is what stops both legs of a 240 V pair landing under one main, and one
    leg under each main twice.
- **Mirroring as tie-break and support.** In the one-direct-parent choice and in
  E3 sibling ambiguity:
  - when two candidate parents of a child are both valid and one has
    `mirror ≥ MIRROR_MIN` (0.30) while the other's mirror is below `MIRROR_MIN/2`
    or `None`, the mirrored parent wins;
  - this is asymmetric evidence, so it is not ambiguous.
  - A parent's mirror is never computed against itself.
- Expose `lift`, `match`, `mirror`, the containment violations and `kcl` on the
  topology API edge payload (`core/topology_api.py`) under `evidence_detail`,
  for diagnostics.

## 3. One-leg 240 V and balanced pairs

No change to E3's `leg_unidentified` cap. It stays the honest answer for loads
whose leg cannot be decided from magnitudes. Voltage-based leg identification
is a separate stage (E5): it needs voltage ingestion and an ADR.

## 4. Replay tool

`tools/dev/replay_ground_truth.py` adds the new `evidence_detail` fields, when
present, to each edge in the report. No other change.

## Acceptance

- `.venv/bin/pytest -q` is green and `ruff check engine tools tests` is clean.
- `test_z3a_no_scenario_names.py` passes.
- `tools/dev/perf_runtime.py --quick` shows no regression in fast-path latency.
  The pair analysis runs only at aligned rebuilds, off the fast path.
- No contract change. `evidence_detail` lives in API payload dicts, not in a
  contract model.
