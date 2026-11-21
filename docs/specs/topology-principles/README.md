# Topology principles: catalogue, engine status and measured value

Status: reference (2026-11-20). Measured on 250 h of real recorder history
from the reference install: 33 readers, 33 true and 560 false ordered pairs,
scored against a data-checked topology. All numbers are aggregates. The
per-install data stays under `data/raw/` (git-ignored).

**AUC** is the separation of true from false parent→child pairs, where 1.0 is
perfect. **Dir** is the separation of true pairs from their reverses.

The labels in the "Use" column mean:

- **positive**: evidence that adds support for an edge.
- **veto**: a gate that can only remove an edge, never support one.
- **leg**: decides which leg a reader is on.

| # | Principle | Engine (HEAD 48ce886) | AUC | Dir | Data needed | Use |
|---|---|---|---|---|---|---|
| P1 | **Level containment.** A child's level (`r/f`) is at or below its parent's at all times | gate ≥ 0.995, ±1 bucket skew, 30 W; decisive after 1 day (E3) | 0.97–0.99 (informative instants) | **1.00** | 6–72 h | **veto** and direction. A large parent "contains" every small child, so it is never support. |
| P2 | **Child step ⇒ parent step of the same Δ** (± small tolerance) | yes (T5 vote, 20 % band); E6b adds a **synchronous verdict** for co-sampled pairs measured across exactly the child's sample pair (no window, no spread veto; ±10 % + 15 W support, flat-parent conflict) | 0.997 raw; **0.967 chance-corrected**; sync **0.942** | 0.99 | 6–12 h | **positive**, chance-corrected against a time-shifted control |
| P3 | **Parent step = Σ child steps + residual step** (KCL on deltas) | fallback only; credit is not exclusive | 0.983 | 0.93 | 12 h | adds nothing over P2 + P6 as a vote. Use it as *exclusive credit* |
| P4 | **Sparsity**: few devices change at once | subset-sum fallback | — | — | — | Not true at a 20 W floor: 40 % of step bins have ≥ 2 readers, which is chance level. It holds at ≥ 100 W (81 % single). Never use bare coincidence as evidence |
| P5 | **KCL on levels**: parent ≥ Σ children (`r/f`), residual ≥ 0 | structural pass, **unscaled** series | 0.967 | 1.00 | 24–72 h | **set veto**. It is the only rule that rejects a 240 V pair's legs being placed under both mains (double count) |
| P6 | **Residual mirroring**: a child's steps appear in P − Σ(known siblings) | no | **0.995** | 0.97 | 12 h | **positive**, and the best tie-break between candidate parents |
| P7 | **Tree / exclusivity / deepest parent** | E6: exactly one placement per child at any confidence (display only); H still uses STRUCTURAL edges | — | — | — | structure, display |
| P8 | **Leg consistency**: a 120 V child is on one root, a balanced 240 V load is on both equally, and a one-leg ×2 load is undecidable | E6: `both_legs` placement with `leg_unidentified` reason | — | — | — | Which leg a one-leg 240 V CT is on cannot be decided from magnitudes or voltage. Model it as carried by both legs |
| P9 | **Voltage sag**: a 120 V load sags its own leg and lifts the other through neutral shift; a 240 V load sags both equally | E5 ingested; E6 §5 guard: leg from the reader's own ΔP vs reference ΔV only, never its voltage entity's group | 19/19 legs correct | — | 1 h (29 % of readers) to 24 h (70 %) | **leg** classifier and 240 V tag. It is the only rule that decides leg |
| P10 | **Energy over intervals** (kWh): E_p ≥ Σ E_c | kWh ingested, unused | 0.72 single; 0.89 Σ (hourly proxy) | 1.00 | — | weak at hourly resolution; not worth adding |
| P11 | **HA state events as labels** | counted only as independent causes | host lift 3–40× | — | — | hint for the *hosting reader*. Useless for ancestors, because panels step near any random time |
| P12 | **Timing / causality** within cross-device skew | E6b: co-sampling from sample timestamps (≥90 % within 2 s); synchronous verdict + O(1) per-reader pulse ring; contrast ranking discounts co-sampled conflict | sync AUC 0.817 → **0.942**; pulse support precision 93 % | — | one sample pair | **positive** for co-sampled pairs (no window / P6 veto); non-co-sampled unchanged |
| P13 | **Same conductor**: near-identical series | E6 §4 infers an affine copy (r ≥ 0.985, slope 0.9–1.1, offset ≤ 150 W, mutual best, not co-sampled); E6b wires it into the live ledger at aligned rebuilds, and an inferred STRUCTURAL alias is suppressed like a declared one | — | — | ≥ 24 h | **inferred** SAME_CONDUCTOR (declaration wins) |
| P14 | **Discrete device levels** (step-size fingerprints) | disaggregation only | — | — | — | future |
| P15 | **Change points** for real rewiring | verdict-count decay | — | — | — | future: a two-segment test on an edge's verdict stream |
| P16 | **Current containment** \|I_c\| ≤ \|I_p\| (raw current is immune to factor errors) | E6 §3: 1-min veto ≥10/1000, set-current KCL, rating classes | 0.999 | 1.00 | 24–72 h | **veto**, catches a reversed CT the power veto misses |

## Findings that change the engine

1. **Chance correction is what makes step matching specific.** A busy feed steps
   near almost any child step by chance. Score the match rate at the true
   alignment minus the rate at time-shifted controls (lift). Require a lift of at
   least 0.14 and a raw match rate of at least 0.5.
2. **Containment is a veto, never support.** With a zero-violation rule scored on
   informative instants (child above the parent's 5th percentile), true pairs pass
   at TPR 1.0. A high containment fraction alone gives 9–17 % false positives.
3. **The best rule is residual mirroring.** Subtract the known structural siblings
   from the parent and look for the candidate child's steps in what remains.
4. **The set-level KCL must use factor-scaled children.** Today the structural
   pass compares unscaled series, so a ×2 child is double counted.
5. **Voltage decides leg; nothing else does.** Per-channel voltage is published
   by the monitors and ignored by the engine.
6. **Combining P1 (≥ 0.999 informative), P2 lift (≥ 0.14) and P5 (≥ 99 %)**
   gives TPR 0.97 with 0 of 560 false positives on this install.
7. **Sampling.** HA delivers about one update per 15.5–15.7 s per monitor, batched
   per device and recorded on change only. Pulses shorter than one interval cause
   most of the remaining conflicts. Finer data, such as a shorter integration poll
   or direct device reads, would help most where step evidence is concerned.

Implementation: `docs/specs/topology-evidence-fixes/E4-principles.md`,
`E5-companion-quantities.md` and `E6-placement-current-sync.md` (ADR-0045).
