# 16 — Final verification report (COMPLETION SPRINT, wave B4-WF)

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**DELIVERED VERSION: `0.2.1-dev.37` (`pyproject.toml:7`) · CONTRACT VERSION: `2.4.0`
(`engine/wattmosaic_engine/contracts.py:32`).**

This is document 16, `TASK-DEPLOY-004a` of the completion sprint. It records the
**delivered version, the exact HEAD, and a full gate table whose commands, exit
codes and counts come from real runs performed at that HEAD** — not from an
earlier report and not from inference. Gate values below were produced by the
authoring session on 2026-11-17 with `.venv/bin/python`; the project's own
`tools/dev/verify.py` remains the orchestrator's authority for moving any
`14-…` requirement-matrix row to `PASS`. **A builder-authored report never moves
a matrix row or sets a task to `VERIFIED`**
(`tasks/progress.md:21-25`, `docs/agent_assignments.md:26-28`).

---

## 1. Delivered versions, exact HEAD and tree state

| Item | Value | Source |
| --- | --- | --- |
| Delivered version | `0.2.1-dev.37` | `pyproject.toml:7` (read at authoring time) |
| Contract version | `2.4.0` | `engine/wattmosaic_engine/contracts.py:32` (read at authoring time) |
| Addon version | `0.2.1-dev.37` | recorded by `18-completion-gap-analysis.md:34`; unchanged in this docs-only range |
| Schema files | 30 JSON in `data/schemas/` (29 generated + `index.json`) | `ls data/schemas`; `export_schemas.py` reported `contract_version 2.4.0, schemas 29` |
| Exact HEAD SHA | `edb02112fd6c7f9775beb58a96e5e11b293dbb2f` | `git rev-parse HEAD` |
| Short HEAD | `edb0211` | `git log --oneline` |
| Branch | `main` | `git branch --show-current` |
| Working tree | **clean** — `git status --porcelain` empty before and after all gate runs | `git status --porcelain` |
| Latest reachable tag | `v0.2.1-dev.36` (the `dev.37` release commit is not yet tagged) | `18-completion-gap-analysis.md:35`; see §7 |

Exact commands and their captured output for the state snapshot:

```
$ git rev-parse HEAD
edb02112fd6c7f9775beb58a96e5e11b293dbb2f
$ git status --porcelain          # empty
$ git branch --show-current
main
```

**Clean-tree statement.** The tree was clean at the start of evidence gathering
and remained clean after every gate below, including after `export_schemas.py`
(no schema drift). This report and the README index edit are the only changes
made by this task; they are committed separately from the gate runs.

---

## 2. Full gate table — real runs at HEAD `edb0211`

Every row was executed during authoring. Exit codes were captured directly from
the command (not from a `| tail` pipeline, whose exit is that of `tail`). Counts
are copied verbatim from the captured output.

| Gate | Command | Exit | Real result / counts |
| --- | --- | --- | --- |
| Unit + integration | `.venv/bin/python -m pytest tests/unit tests/integration -o addopts="" -q` | `0` | `2777 passed, 15 skipped, 23 warnings in 41.72s` |
| Simulation | `.venv/bin/python -m pytest tests/simulation -o addopts="" -q` | `1` | `168 passed, 9 errors in 49.82s` — 9 pre-existing `test_real_topology.py` errors (see §4) |
| Lint | `.venv/bin/ruff check engine tools tests` | `0` | `All checks passed!` |
| Type check (delta) | `.venv/bin/python -m mypy engine/wattmosaic_engine` | `1` | `Found 109 errors in 27 files (checked 84 source files)` — unchanged baseline (see §4) |
| Schema sync | `.venv/bin/python tools/dev/export_schemas.py && git diff --exit-code data/schemas` | `0` | export `{"contract_version": "2.4.0", "schemas": 29}`; `SCHEMA_EXIT=0` — no drift |
| Secret scan | `.venv/bin/python tools/dev/check_secrets.py` | `0` | `secret scan: clean` |
| Addon validation | `bash scripts/validate_addon.sh --no-build` | `0` | `passed: 71  failed: 0  skipped: 3` / `all checks passed` |
| Panel check | `.venv/bin/python tools/dev/check_panel.py` | `0` | `panel check: 4 assets, self-contained and CSP-safe` |
| Smoke failure propagation (negative) | `bash scripts/smoke_test.sh http://127.0.0.1:1` | `1` **(expected)** | `FAIL topology / devices / metrics … curl: (7) Failed to connect to 127.0.0.1 port 1` / `smoke: 9 check(s) FAILED` — proves non-zero propagation |

Notes on the two non-zero gates — both are **expected and honest**, not hidden:

- **Simulation exit 1** is the nine long-standing `tests/simulation/test_real_topology.py`
  errors (local-DB live capture, `OD-36`). They are pre-existing, not a regression
  from this range (§4).
- **Smoke exit 1 is the intended negative result.** Pointing the script at a dead
  endpoint (`127.0.0.1:1`) must fail; it exits 1 and reports nine failed checks.
  This is the proof that the smoke script propagates failure rather than always
  reporting success.

The full positive `tools/dev/verify.py` run is **not** re-recorded here: its last
recorded run (12/12 required gates PASS, 2026-11-11) predates contract `2.4.0`
and the implementation commits and is marked **stale** by
`18-completion-gap-analysis.md:361`. Running it to move any matrix row to `PASS`
is the orchestrator's action, not this report's claim.

---

## 3. Verification vehicles actually run

Beyond the gate table, each named negative/verification vehicle was executed
now at HEAD. Counts and exits are real.

| Vehicle | Real command | Exit | Real counts | Landed in |
| --- | --- | --- | --- | --- |
| Scenario coverage T01–T15 | `.venv/bin/python -m pytest tests/simulation/test_scenario_coverage.py -o addopts="" -q` | `0` | `33 passed in 1.26s` — deterministic builder for every scenario id | `0665b2c` |
| Replay-identical (same algorithm version) | `.venv/bin/python -m pytest tests/simulation/test_replay_versions.py -o addopts="" -q` | `0` | `13 passed in 2.12s` | `c80bb67` |
| Migration matrix | `.venv/bin/python -m pytest tests/unit/test_migration_matrix.py -o addopts="" -q` | `0` | `11 passed in 0.31s` | `c80bb67` |
| Performance budgets (new paths) | `.venv/bin/python -m pytest tests/unit/test_new_path_performance.py -o addopts="" -q` | `0` | `3 passed in 4.16s` | `c80bb67` |
| No-fabrication (B4b group bounds) | `.venv/bin/python -m pytest tests/unit/test_disaggregation.py -o addopts="" -q` | `0` | `56 passed in 0.66s` — incl. `test_ambiguous_group_publishes_min_and_max`, `test_unknown_slack_is_never_assigned_to_a_device`, `test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated` (no fabricated 250/250 W split) | `0dd4d27` (B4b) |
| No-fabrication mutation proofs | `.venv/bin/python -m pytest tests/unit/test_residual_power.py -o addopts="" -q` | `0` | `92 passed in 0.58s` — incl. `test_a_clamping_implementation_is_detected` and `test_a_recompute_from_raw_samples_instead_of_persisted_evidence_is_detected`, which inject a mutation and assert the guard catches it | mutation-proof precedent; B4b commit message `0dd4d27` |
| Smoke script failure propagation | `.venv/bin/python -m pytest tests/integration/test_smoke_script.py -o addopts="" -q` | `0` | `6 passed in 1.57s`; live negative run (§2) exits `1` | `edb0211` |

**Commit cross-reference (verified with `git log -1 --format='%H %s'` during
authoring):**

- `0dd4d27b46581c6d9b9a6df80c0c10265454ee1e` — B4b: non-fabricating group
  estimates with `min_w`/`max_w`; the commit message records the mutation proof
  that the no-fabrication test catches a forced per-device split.
- `0665b2c099ea9437dd806d3af2d43c2900b7ecbf` — deterministic builder for every
  scenario id (`tools/simulator/scenarios.py`, `test_scenario_coverage.py`).
- `c80bb67f2bf68553c94d505e57ebfca7a8ba25ea` — replay-identical, migration-matrix
  and new-path performance gates.
- `edb02112fd6c7f9775beb58a96e5e11b293dbb2f` — unified workflow and post-deploy
  smoke script (current HEAD).
- `f558bde350bd80a53f0e3285a436ecfb58f0306d` — B4a: `sources_used` /
  `sources_unused` ledger (E2E chain test added in the same commit).

`TASK-TEST-001` remains `IN_PROGRESS` as the continuous verification vehicle;
these runs are its evidence, not a status change.

---

## 4. Known baseline exceptions (pre-existing, not regressions)

Both non-green gates are long-standing baselines that also failed before this
range; neither was introduced by the completion-sprint commits.

1. **Simulation — 9 errors.** `tests/simulation/test_real_topology.py` raises
   nine errors from `tools/audit/replay_capture.py` because there is no local
   live-capture database (`ValidationError`). The gap analysis already recorded
   this as `122 passed, 9 errors` (`18-…:355`, `progress.md:221`); at this HEAD
   it is `168 passed, 9 errors` — the same nine, with more passing simulation
   tests around them. This is `OD-36`: nine live-capture tests skip/error
   without a local DB (`progress.md:173`). Not a regression.

2. **Type check — 109 errors.** `mypy engine/wattmosaic_engine` reports
   `Found 109 errors in 27 files (checked 84 source files)`, identical to the
   recorded baseline in `progress.md:188`, `18-…:357` and
   `docs/final_validation_report.md`. This is a **baseline, not a target of
   zero**; the meaningful measure is the delta against 109, which is unchanged.

3. **No fabrication guard stays green.** The no-clamp / no-fabrication safety
   invariants (§3) are green at HEAD; the 250 W fabricated-split bug that caused
   `TASK-DISAGG-001` to fail (`progress.md:47`) is caught by these tests and is
   not present in the B4b implementation.

---

## 5. Accuracy limitation (synthetic only)

Stated plainly and consistently with `README.md:11-14`, `README.md:170-172`
and `REQ-TEST-010`:

- All accuracy figures come from **synthetic ground truth** in the seeded lab
  house / scenario simulator. They demonstrate the engine behaves correctly on
  data with known answers.
- The project **does not claim real-world per-device accuracy**. No accuracy
  number in this report (or in the simulator vehicles of §3) is a measurement
  from a real installation.
- `OD-36` live-capture tests skip/error without a local DB; a real-house replay
  with per-device ground truth is not available, so no per-device error figure
  is asserted (`REQ-TEST-005`, `18-…:291`).
- `tests/simulation/test_real_topology.py` itself documents that its tests
  "prove the engine behaves correctly on a house with the *shape* of this
  install. They cannot prove anything about the install itself."
- No confidence number is presented as a probability, and a candidate is never
  promoted automatically (`SPEC README §5`).

---

## 6. Honest requirement status summary

Statuses below are **inspection findings**, not orchestrator gate moves. The
`14-…` matrix `Verification Result` column is unchanged by this report.

**Gap-analysis §2.16 baseline (inspection view, at HEAD `e54ce5d`):**

| Status | Count |
| --- | --- |
| IMPLEMENTED | 57 |
| PARTIAL | 30 |
| NOT IMPLEMENTED | 40 |
| REVERTED | 3 |
| **Total** | **130** |

Since that snapshot, 19 commits landed (B0–B4 completion tiers), including the
DISAGG, mapping, topology-change, UI, residual-window, workflow and test-gate
work. The tracker's completion-sprint tasks (§1.1 of `progress.md`) still carry
their proposal statuses; a builder report does not advance them to `VERIFIED`.

**DISAGG — accurate status.** The original `TASK-DISAGG-001` is still recorded
as `FAILED` (reverted 250 W fabrication) and `TASK-DISAGG-002` as `NOT_STARTED`
in `progress.md`. **No DISAGG requirement is marked `PASS` in the original
matrix, and none is upgraded by this report.** What *has* landed since the gap
analysis is real code:

- **B4a — source ledger** (`f558bde`): `infer()` now names every source used and
  unused with reason (`sources_used` / `sources_unused`), superseding the
  reverted attempt. E2E chain test added in the same commit.
- **B4b — non-fabricating group estimates** (`0dd4d27`): ambiguous loads publish
  mathematically justified `min_w`/`max_w` bounds and **no fabricated per-device
  watt**, with a mutation proof that the no-fabrication test catches a forced
  split. Verified green at `56 passed` (§3).

This is delivered code with passing local vehicles. It is **not** a matrix
`PASS`: **the orchestrator must run the named gate for the DISAGG rows before
any `14-…` matrix row is moved to `PASS`.**

Other completion work landed since the snapshot (by commit subject):
`b295fbb` additive residual-noise/inference shapes, `596bf39`/`239a650`
production residual baseline window, `7615132` adjustable power response,
`75e5023`/`314a75f` mapping contract + derived matrix, `3ad48ce` `/mapping`
read path, `4f34a29`/`293038b`/`24e07fb` configuration-change typed status,
detection and history reconciliation, `0ede162`/`fd5f01f` UI renders, plus the
scenario/gate/workflow commits above. Their tracker statuses are not
independently asserted here; each would need its own orchestrator-run log.

Rows that remain genuinely partial or deferred per the gap analysis and tracker
include `REQ-FOUNDATION-006` (per-wave ownership manual review, `NOT RUN`),
`REQ-TOPO-009/010/011`, several `REQ-RESIDUAL-*`, `REQ-MAPPING-*`,
`REQ-CHANGE-*`, `REQ-UI-*`, and `REQ-DEPLOY-004/005/007/008`. This report does
not overclaim them as complete.

---

## 7. Deferred / human-decision items

These remain open and are **not** resolved by this report:

| Item | Why deferred | What is needed |
| --- | --- | --- |
| **Deployment report 17** (`17-deployment-report.md`) | Requires a **real deployed system**: version-stamped deployment/rollback report, recorded smoke output and a rollback rehearsal. No deployed installation or smoke log exists in the repository (`18-…:336`, `:363`, `:392`). | An actual deployment with a captured run; out of scope here. Report 17 stays **named, not linked** in the spec-set README. |
| **OD-34** | Whether `topology`/`residual` confidence dimensions stay unpopulated in V1 (`18-…:390`); carries `TASK-CONFIDENCE-002`. | A recorded product decision. |
| **OD-46 / OD-47** | Whether `ALWAYS_ON`/`DELAYED` are enum members, flags or orthogonal parameters (`18-…:389`); blocks `TASK-POWER-004` (G-04). | A recorded product decision; do not guess. |
| **Deployment image-tag ambiguity** | HEAD version is `dev.37` but the latest reachable tag is `v0.2.1-dev.36` (`18-…:346`, `:391`). | The deployment report must state the exact image tag pulled; not inferable from the repository. |
| **Orchestrator gate moves** | Builder self-reports never set `VERIFIED` or a matrix `PASS` (`progress.md:21-25`). | The orchestrator runs `tools/dev/verify.py` and the named vehicles, then moves rows. |

---

## Related documents

- [`01-requirements.md`](01-requirements.md) — the 130 normative requirements.
- [`14-requirement-traceability.md`](14-requirement-traceability.md) — the matrix whose rows this report does **not** move.
- [`18-completion-gap-analysis.md`](18-completion-gap-analysis.md) — §2 status totals and §4 verification assessment used above.
- [`19-completion-plan.md`](19-completion-plan.md) — the dependency-ordered Phase B plan.
- [`README.md`](README.md) — the spec-set file index (16/18/19 linked; 17 named pending deployment).
- [`tasks/progress.md`](tasks/progress.md) — authoritative task status and the orchestrator-only `VERIFIED` rule.
