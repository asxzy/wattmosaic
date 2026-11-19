# Load Disaggregation Algorithms (contract 3.0)

Owner: Agent J (docs). The algorithms themselves are **normatively specified**
elsewhere — this page is a map, not a competing definition:

- The [technical handbook](../docs/specs/realtime-topology-disaggregation/handbook.md)
  (HB §n) states *what* the system must do and why.
- The [design spec](../docs/specs/realtime-topology-disaggregation/design.md)
  pins down *how*: concrete algorithms, defaults and decision rules.
- The [interfaces spec](../docs/specs/realtime-topology-disaggregation/interfaces.md)
  defines the typed handoffs between stages.

All shipped algorithms are deterministic, explainable and classical (robust
statistics, constrained / non-negative least squares, typed evidence ledgers).
No deep learning. Every estimate carries evidence references so a human can
audit *why* a value was produced — or why none was.

> The 2.x algorithms (the 5-second full solve, the greedy step detector, the old
> nesting/residual/mapping builders and the timer-driven `DisaggregationEngine`)
> were removed at the cutover. Do not use older copies of this page as a guide.

---

## 1. The three loops (HB §5)

```text
fast measurement loop (per sample, O(1))
   normalize reader sample → reader state ring → baseline/noise → change detector
        │
        ▼
event inference loop (per event group)
   collect windows → group events (late-arrival revision) → candidates
        → observation operator → joint solver → estimates, groups, residual
        │
        ▼
background loop
   learning on matured delta observations → retention → reconciliation/replay
        → staleness-only heartbeat (never a full solve)
```

The old `WM_INFERENCE_INTERVAL_S` timer is gone; it is accepted and ignored with
a warning (DEC-03). The fast loop must be O(1) per sample so bursts cannot starve
`/healthz`.

## 2. Measurement normalization (HB §2, §6.1)

1. **Unit canon.** Non-finite or unsupported units are quarantined and counted,
   never passed to reader state.
2. **Endpoint revision.** The revision valid at `observed_at` is looked up;
   polarity, then multiplier, is applied **exactly once** and stamped with its
   `calibration_version`. A calibration change is never retroactive (DEF-06,
   HB §18).
3. **Plausibility.** `|P| <= reader_max_w` (declared, else a multiple of observed
   p99.9, else 24 kW); an outlier is quarantined.
4. **Compatibility predicate.** Two readers may share an equation only with the
   same measurement family, `active_power_w` on both, and compatible phase.

## 3. Reader state and change detection (HB §6.2–6.4)

- **ReaderStateStore** keeps one bounded ring per endpoint with zero-order hold;
  a reader is *held* until `stale_after_s`, *stale* until missing, then *missing*
  (`None` everywhere, HB §18).
- **Noise** σ_r is a robust estimate (1.4826·MAD of innovations over recent
  non-event samples, floored by resolution/√12 and σ_min); it is persisted so a
  restart has no hidden state. Per-event pre-windows never estimate σ.
- **Baseline** b_r is an EWMA frozen while the detector is suspect or settling,
  so a persistent step is never absorbed into the baseline (HB §6.3).
- **Detector**: enter SUSPECT at `|z| >= 4` and a minimum absolute change;
  exit at `|z| < 2` (hysteresis). A two-sided CUSUM catches small sustained
  shifts; slow drift is not an event. A step is CONFIRMED only after it holds for
  two samples or `confirm_s`; otherwise it is TRANSIENT. A physical step yields
  exactly one event identity.

The full parameter table and worked detection example are in the
[design spec §3](../docs/specs/realtime-topology-disaggregation/design.md).

## 4. Event windows and groups (HB §6.5–6.6)

A step's onset is bounded by the last pre-sample and first post-sample. Windows
are collected around that onset and grouped by cause; late arrivals can revise a
group rather than creating a new one. Every group carries a status and a bounded
reason tally. See the
[spec §3](../docs/specs/realtime-topology-disaggregation/interfaces.md).

## 5. Candidate generation and topology-aware filtering (HB §7)

Candidates are subjects whose response is consistent with the reader deltas:
correct direction, magnitude within the reader ceiling, timing inside the
window, and allowed by the typed topology. Unknown load is **always** a competing
candidate on every branch row (HB §7.5). Matching is dynamic, not a one-time
assignment, and simultaneous events are handled jointly (HB §7.6–7.7).

## 6. Observation operator (HB §3, §8.1)

`H(T, M, t)` maps candidate contributions to reader rows given the typed topology
`T`, mapping `M` and time. Both absolute (`y = H x + e`) and delta
(`Δy = HΔ Δx + e`) equations are used: delta evidence separates concurrent
changes, while absolute anchors (confirmed single-subject endpoints) bound the
level. Time-scoped factors (e.g. polarity, calibration) come from endpoint
revisions, so a change is never applied retroactively.

## 7. Joint inference, identifiability and groups (HB §8)

- A robust, signed, covariance-aware solver with an unknown-load competitor
  produces a point estimate where the evidence identifies one.
- **Identifiability** is decided from the weighted measurement geometry. A
  non-identifiable subject gets `point_w = None`, justified `Bounds` and (where
  applicable) a group — never a divided-out share (HB §8.4). Model priors are
  never bounds.
- **Status** (HB §8.5): `MEASURED` (confirmed single-subject endpoint, one source
  reader), `ESTIMATED`, `ALLOCATED` (with an allocation rule), or `UNKNOWN`.
- **Groups** (`DeviceGroupEstimate`) report the identified *sum* of a
  non-separable set with justified bounds; individual members keep `None`.

A concrete four-status publication example and the identifiability decision
table are in the [design spec](../docs/specs/realtime-topology-disaggregation/design.md)
and [HB §8.4–8.5](../docs/specs/realtime-topology-disaggregation/handbook.md).

## 8. HA publication rule (DEC-25)

A device power sensor state is `point_w` **only** for `MEASURED`, or `ESTIMATED`
and `IDENTIFIABLE`. Every other combination publishes `None` (`unknown`); bounds,
status, identifiability, group id and version ids are attributes. One group power
sensor per stable `group_id`. Past 0 W history becomes gaps going forward; it is
not rewritten, and energy integration never integrates a null.

## 9. Power models and learning (HB §11, §13)

- Power models use a discriminated spec union: `FIXED`, `ADJUSTABLE`, `DYNAMIC`,
  `RESPONSE_KERNEL`, `COMPONENT_SUM`, each with append-only versions.
- Learning observations are **delta-basis** (`ModelBasis.DELTA`). An
  `ABSOLUTE_ANCHOR` basis is valid only for a confirmed single-subject endpoint.
- Legacy whole-circuit models are flagged `LEGACY_ABSOLUTE`, kept read-only, and
  retrained from delta evidence (DEC-31). They are never served as validated
  power nor used as priors outside a confirmed single-subject endpoint.
- Training and reanalysis are event-driven on matured evidence, running on the
  existing loop (no extra thread). Models promote only when they beat the active
  one on held-out data.

## 10. Residual and unknown load (HB §3.3, §9.4)

Residuals are kept signed and per reader; they are never distributed among
devices. Unexplained load on an endpoint is a **Reader Residual** row or an
Unknown Load Profile, not a fabricated device. The whole-home balance identity
is `main_w = Σ estimates + Σ group sums + Σ unknown + residual`, with every term
`float | None`. See [`docs/residual_analysis.md`](residual_analysis.md).

## 11. Topology inference and self-healing (HB §4, §14)

Topology is inferred from **asymmetric** delta + absolute evidence and typed
edges with a lifecycle: one event never promotes an edge past `CANDIDATE`, a
parent-only change is never evidence against containment, and PROVISIONAL edges
are soft. User pin/reject on an edge is authoritative; configuration changes
trigger a versioned self-healing workflow. See
[`docs/association_and_topology.md`](association_and_topology.md).

## 12. Confidence (HB §9.6, DEC-24)

`ConfidenceVectorV3` has seven independent components, each `[0,1]` or `None`,
with **no blended scalar** and no probability claim. See
[`docs/confidence_model.md`](confidence_model.md).

## 13. Determinism and complexity

- Every stage is a pure function of its inputs plus a caller-supplied `now`;
  the clock is never read inside a decision.
- Sorting is explicit at every persistence boundary, so a replay is byte-identical.
- Bounded memory: rings, event queues and evidence buffers are all capped.
- The fast loop is O(1) per sample; heavier work runs in bounded executors off
  the event loop so the API and `/healthz` stay responsive.
