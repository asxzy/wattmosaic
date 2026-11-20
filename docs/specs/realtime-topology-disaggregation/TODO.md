# TODO — future work after the 3.0 cutover

Known limitations recorded at the end of the realtime-topology-disaggregation
build (branch `feat/realtime-v3`, released as `v0.3.0-dev.1`). Evidence for each
item is in [verification-report.md](verification-report.md) §8 and §10 and in
[tasks/progress.md](tasks/progress.md). Ordered by expected impact.

## 1. Disk use on Pi-class hosts (highest priority)

- **Fact.** Full-scale benchmark (40 readers × 1 Hz, 500 HA entities, 30 events/s):
  ~807 MB/day growth. Raw `ct_samples` (+2 indexes) ≈ 0.71 GB/day (~274 B/sample);
  `inference_snapshots` ≈ 0.05 GB/day.
- **Implied steady state** with the 3.0 defaults (raw 7 days, derived 90 days):
  ≈ 5 GB raw + ≈ 4.5 GB snapshots.
- **TODO.**
  - [x] Raw CT readings are no longer persisted by default; see ADR-0042 and the
        implementation plan in `docs/specs/raw-capture/`. Measured with
        `tools/dev/perf_runtime.py --quick` (T10, 2026-11-19): at the default
        `raw_capture_days=0`, `ct_samples` growth is **0 B/day** (table absent from
        the per-table report) and total DB growth is **≈ 537 MB/day**; with
        `raw_capture_days=7` the raw share returns (**≈ 507 MB/day** in
        `ct_samples`, **≈ 1.39 GB/day** total). Evidence and full numbers in
        `docs/specs/raw-capture/verification.md` §AC9.
  - [ ] Store snapshots as change points or deltas, or shorten the default
        snapshot horizon for Pi-class installs.
  - [ ] Expose the derived-table retention settings (`WM_RETENTION_DAYS_*`) as
        App options (today they are engine/env settings only).
  - [ ] Re-run `tools/dev/perf_runtime.py` and record per-table growth after each change.

## 2. Group estimates need co-switch history

- The HB §17 lights never get a group estimate: the 12-minute script co-switches
  them once, below I4's `MIN_HISTORY_EVENTS`. Group estimates are demonstrated on
  `lab_shared` (live runtime) instead.
- [ ] Add a longer HB §17 scenario (or a lead-in with repeated co-switches) so the
      walkthrough asserts a published lights group estimate.

## 3. Live group value assumes other loads are constant

- A live group's current value is its reader's level minus the level recorded
  with the group off; it drifts if other loads on that reader change meanwhile.
- [ ] Re-anchor the group's off-level on every confirmed event on that reader, or
      solve the group jointly in the steady pass.

## 4. Matrix 500 × 100 GET latency

- p95 varies between ~210 and ~570 ms on a shared laptop (M3 budget 300 ms).
- [ ] Profile `/api/v1/matrix` payload build + serialization; cache the payload by
      ETag; measure on Pi-class hardware.

## 5. Groups closed by a promotion are not re-delivered to learning

- CONFIRMED groups closed by a topology/mapping promotion are finalized under
  their old triple and not re-delivered to the L6 learner (deliberate, so no
  result carries a stale version).
- [ ] Re-deliver them to L6 tagged with their original triple.

## 6. Accuracy harness declares some scopes for the user

- `tests/simulation/accuracy_v3.py` declares single-subject scopes for exclusive
  lab CTs on the user's behalf (DEC-05 requires a user declaration).
- [ ] Drive those declarations through the real devices/endpoint-confirm API in
      the harness.

## 7. Detection: OFF inside settling

- A short pulse's OFF inside the ON step's settling window is detected only when
  the level returns within `max(k·σ, return_frac·|step|)` of the pre-step level
  (Z10b, `return_frac = 0.2`). Loads whose OFF lands on a different baseline are
  reported after settling.
- [ ] Validate `return_frac` on recorded real-device traces.

## 8. Real-data verification (operator-run)

- [ ] Run the real-data replay on a copy of the production DB
      (verification-report §7 commands; `tools/audit/replay_capture.py --db`).
- [ ] Re-create a v3 replacement for the removed `tools/dev/replay_nesting.py --score`
      and record the pre-3.0 baseline it is compared against.
- [ ] Full container rollback rehearsal against a real HA (restore the
      pre-migration backup, reinstall the previous App version).

## 9. Optional / deferred

- [ ] L8 active-learning suggestions (DEC-36; suggestions only, never actuation).
- [ ] DEC-38: a plug relocation may or may not remap the device; if the
      topology-move reading is wanted later, mapping must support more than one
      endpoint per subject.
