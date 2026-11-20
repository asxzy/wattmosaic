# Topology linking v2: explain-away, capacity, paired legs, edge health

Status: wave 1 (pure modules) in progress. Wave 2 wires them into
`electrical/topology_history.py`, `electrical/topology_inference.py` and
`core/runtime.py`.

## Problem (live, 2026-11-20)

The T5 ledger (`electrical/topology_inference.py`) links a stepping child
reader to a parent reader when the parent moved by the child's delta
(`_classify_step`). Three gaps:

1. **One child at a time.** If a parent steps 100 W while child Y steps 30 W
   and child Z steps 70 W in the same window, neither 30 nor 70 matches 100, so
   neither edge gets support. Correct answer: the parent contains Y and Z
   (the sum of a subset of the children's deltas matches the parent's move).
2. **Known edges are not used.** Once `P -> A` is CONFIRMED, A's delta is a
   known part of every P move. It should be subtracted from P's move before
   matching the *unknown* children (explain-away), and every step of A should
   keep checking that P still follows it (edge health).
3. **Absolute values are ignored.** The children of a parent can never carry
   more power in total than the parent itself (KCL, `sum(children) <= parent`).
   A 240 V load (hot tub) has two legs whose absolute values are highly
   correlated; each leg is on a different hot line, so the legs have different
   parents. Today `_keep_direct_parents` keeps the lowest-level confirmed
   parent per child, which is only right for *nested* parents. For sibling
   parents (main L1 and main L2) it sends both hot tub legs to the same leg.

## Examples (acceptance)

* `P` steps +100 W, `Y` steps +100 W, all others flat: `P -> Y` support.
* `P` steps +100 W, `Y` +30 W, `Z` +70 W, all others flat: `P -> Y` and
  `P -> Z` support.
* `P -> A` confirmed. `P` steps +150 W, `A` +100 W, `B` +50 W: after
  explain-away the residual is 50 W, so `P -> B` support.
* Hot tub legs `H1`, `H2` (correlation of absolute values >= 0.95), parents
  `L1`, `L2` (siblings). `H1` and `H2` are assigned to different parents,
  chosen so no parent's absolute value is exceeded by its children.

## Wave 1 modules (independent, pure, no I/O, no clock)

| Module | Spec |
| --- | --- |
| `electrical/topology_explain.py` | [M1-explain.md](M1-explain.md) |
| `electrical/topology_capacity.py` | [M2-capacity.md](M2-capacity.md) |
| `electrical/topology_health.py` | [M3-health.md](M3-health.md) |
| `electrical/topology_regression.py` | [M4-regression.md](M4-regression.md) |

Rules for every wave 1 module:

* Create only the module file and its test file named in the spec. Do **not**
  edit any existing file.
* Python 3.11 compatible, `from __future__ import annotations`, type hints,
  frozen dataclasses where a type is defined, no third-party imports beyond
  the standard library and `wattmosaic_engine.*`.
* No names from HB §17 scenarios (`ct_main`, `"Main"`, `hb17` ...): use
  neutral ids in code. Tests may use any ids.
* `ruff check engine tests` must pass (line length 120).
* Absent is `None`, never 0: a `None` sample is skipped, never treated as 0 W.
