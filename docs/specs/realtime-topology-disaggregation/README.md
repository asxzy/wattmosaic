# Real-time, topology-aware load disaggregation — contract 3.0

**Status:** Draft · 2026-11-18 · Awaiting product-owner approval of the ★ decisions in `decisions.md`
**Normative source:** [`handbook.md`](handbook.md) (the technical handbook, verbatim, cited as HB §n)
**Supersedes, where it conflicts:** `docs/specs/load-disaggregation/`. That set was additive-only; this one implements HB §16's breaking-change policy.

## What this is

A spec set that lets a fleet of coding agents implement the handbook **in parallel**. It was written from a read-only gap analysis of the code at `main@27373aa` (`analysis/`, six reports with file:line evidence). The handbook's own §20 stage order is mapped onto 69 tasks across 7 waves, with one owner per file per wave.

The short version of the gap: today's engine is a **timer-driven, per-snapshot absolute solver** on the asyncio loop.
- It uses a **declared pointer tree** as hard topology.
- Its device identity *is* the CT.
- It has one version for topology and mapping.
- It has no delta model.
- It has no unknown-load competitor on branch rows.
- It has no group estimate.
- It uses **0 W where it means "unknown"**.
- It learns models on whole-circuit power.

Most of the handbook's vocabulary already exists in `contracts.py`, but almost none of it is on the live path.

## Defects confirmed while writing this spec

These were found by probe scripts run outside the repo:

| ID | Defect | Fixed by |
|---|---|---|
| DEF-04 | Two lights on one 500 W row: members publish 0 W in the transition cycle; the group range [250, 337.5] **excludes** the measured 500 W; the next steady cycle publishes 250 W + 250 W as `model_estimated`. An existing test asserts the split (`tests/unit/test_disaggregation.py:1102`). | I4, I5 |
| DEF-02 | A composite power model predicts 100.5 W when fitted and **0 W after reload**. A dimmable model reloads with the wrong control variable (2.55× scaling error). | L1 |
| DEF-03 | A clean fixed load (MAE 0.3 W) scores R² −0.03 and **can never be promoted**. | L5 |
| DEF-05 | Every circuit's residual baseline is computed from one install-wide window. | T3 |
| DEF-06 | A calibration change applies retroactively in the live solve. | T2 |
| DEF-07 | Model rollback can reactivate a rejected candidate. | L1 |
| DEF-01 | (Suspected) A declared inverted CT polarity is not applied on the production reading path. | P1 (test first) |

## How to read

1. [`decisions.md`](decisions.md): 37 product and architecture decisions with proposed defaults. **★ rows block their tasks.**
2. [`design.md`](design.md): the chosen algorithms, parameters and decision rules (the "how" behind HB).
3. [`architecture.md`](architecture.md): today vs target, package layout, dependency direction, what is deleted.
4. [`interfaces.md`](interfaces.md): normative shapes: contracts, runtime handoffs, HTTP v3, tables.
5. [`implementation-plan.md`](implementation-plan.md): **start here to execute**. It covers the integration branch, builder protocol, wave DAG, task index, ownership rules, reserved migration numbers and wave gates.
6. [`traceability.md`](traceability.md): HB §21's 20 acceptance tests → closing task, and HB sections → tasks.
7. `tasks/<ID>.md`: one file per task. `tasks/progress.md` is the tracker.
8. `analysis/`: the evidence. Each task cites the report and section it was derived from.

## Non-negotiables (every builder)

- **No work lands on `main` until the cutover.** Every push to `main` publishes a release. Work on `feat/realtime-v3` (DEC-01).
- **Edit only the files your task owns.** If you need another file, set your row to `BLOCKED`; do not hand-merge.
- **Write the task's tests first.** For I4 this is mandatory: the 250 W test with its mutation guards exists before any solver code.
- **Absent is `None`, never 0** (HB §18). The null-policy and grep gates enforce this.
- **A non-identifiable member never gets a number.** It gets null, justified bounds and a group (HB §8.4, DEC-19). Model priors are never bounds.
- **Topology:**
  - one event never promotes an edge past CANDIDATE;
  - a parent-only change is never evidence against containment;
  - PROVISIONAL edges are soft.
- **Topology, mapping and model versions are independent.** A plug move never touches a power model (AT-15).
- **Read-only toward HA, never actuate** (CONTRIBUTING.md). Active learning only *suggests*.
- **Never reuse the retired "dedicated" CT-role vocabulary** (ADR-0034, `tests/unit/test_docs_vocabulary.py`). Single-subject *endpoints* are a different concept (DEC-05).

## Acceptance at a glance

- All 20 HB §21 tests in `tests/acceptance/` pass with no xfail markers left (V1).
- The HB §17 walkthrough runs end to end: HA messages → three-loop engine → HTTP → panel model → HA entity values (V1).
- `python tools/dev/verify.py` is green at contract `3.0.0`, with schemas regenerated and the integration mirror matching (Z1).
- `/healthz` p99 < 100 ms under load, and the fast path < 0.2 ms per sample (V2).
- The migration rehearsal on a real DB copy succeeds both forward and back (V2).
- `forced_attribution_count == 0` in the simulator accuracy report (V1).
