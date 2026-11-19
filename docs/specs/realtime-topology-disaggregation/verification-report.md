# V2 — Final verification and deployment report (realtime-topology-disaggregation)

**Branch:** `feat/realtime-v3` · **Gate HEAD:** `e5f06084f3842e28386545600abe22e7915c727c`
(`e5f0608`, "docs(progress): Z10/Z10b verified") · **Report HEAD:** `565cd7c` (adds
only the perf-harness fix of §5.1 and the regenerated `verify.py` outputs; no
engine file differs from `e5f0608`: `git diff --stat e5f0608 HEAD` lists
`data/verification.json`, `docs/verification_report.md`,
`tests/unit/test_perf_runtime.py`, `tools/dev/perf_runtime.py`).

| Item | Value | Source |
| --- | --- | --- |
| Contract version | `3.0.0` | `export_schemas.py` output (§2) |
| Latest schema version | `26` | `wattmosaic_engine.storage.migrations.LATEST_VERSION` |
| `main` HEAD / schema | `095b149` / `11` | `git rev-parse main`; main's `LATEST_VERSION` (§4.2) |
| Engine / add-on version string | `0.2.1-dev.42` in `pyproject.toml` and `addon/config.yaml` — **the same as `main`** | see §8, item R1 |
| Release notes heading | `v0.3.0-dev.1 — 2026-11-19` | `RELEASE_NOTES.md:3` |
| Run date / interpreter | 2026-11-19, `.venv/bin/python` (CPython 3.12, macOS arm64, shared developer machine — not Pi-class hardware) | |

Every value below was produced by a command run for this report. Output is
trimmed, never edited; hosts and absolute paths are scrubbed as
`tools/dev/verify.py` `_scrub` does (none appeared in the captured output).

---

## 1. Gate summary

| # | Gate | Command | Exit | Result |
| --- | --- | --- | --- | --- |
| 1 | Lint | `.venv/bin/ruff check engine tools tests` | 0 | `All checks passed!` |
| 2 | Full pytest | `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" -q -rsx` | 0 | `2989 passed, 7 skipped, 26 warnings in 128.48s` — 0 failed, 0 xfailed, 0 xpassed |
| 3 | Schema sync | `.venv/bin/python tools/dev/export_schemas.py` then `git diff --exit-code -- data/schemas ':(exclude)data/schemas/index.json'` | 0 / 0 | `{"contract_version": "3.0.0", "schemas": 65}`; no drift |
| 4a | Panel | `.venv/bin/python tools/dev/check_panel.py` | 0 | `panel check: 5 required assets, 13 modules, self-contained and CSP-safe` |
| 4b | Add-on | `bash scripts/validate_addon.sh --no-build` | 0 | `passed: 87  failed: 0  skipped: 3` / `all checks passed` |
| 5 | Orchestrator gates | `.venv/bin/python tools/dev/verify.py` | 0 | `required gates: 13/13`; optional `config_valid` FAIL — `requires HA_TOKEN in environment` (no token is configured in this environment; optional by design) |
| 6 | Perf (×2, full scale, gates on) | `.venv/bin/python tools/dev/perf_runtime.py --json-out <tmp>` | 0 / 0 | `/healthz` p99 42.45 / 31.20 ms (< 100); fast path p99 0.0578 / 0.0511 ms (< 0.2) — §5 |
| 7 | Migration rehearsal | `.venv/bin/python tools/dev/rehearse_migration.py --synthesize` | 0 | schema 11 → 26 → 11, 8/8 checks OK, `RESULT : PASS` — §4 |
| 8 | Accuracy harness | `.venv/bin/python -m tests.simulation.accuracy_v3` | 0 | `TOTAL forced_attribution_count: 0`; `lab_shared` drifted from `docs/acceptance_report.md` and that file was updated — §2.1 |
| 9 | Real-data replay | — | — | **OPERATOR-PENDING**: no `data/wattmosaic.db` in this checkout — §6 |

The 7 skips are all `tests/simulation/test_real_topology.py`:
`real capture unavailable: data/wattmosaic.db is local-only and git-ignored`.

`verify.py` per-gate lines (trimmed):

```
  PASS     contract_schema_sync (0.18s)
  PASS     spec_status (0.05s)
  PASS     secret_scan (1.06s)
  PASS     lint (0.1s)
  PASS     unit_tests (66.88s)
  PASS     integration_tests (25.81s)
  PASS     simulation_tests (29.17s)
  PASS     end_to_end_tests (4.83s)
  PASS     manifest_valid (0.05s)
  PASS     release_valid (0.03s)
  PASS     docs_fresh (0.39s)
  PASS     deployment_valid (2.92s)
  PASS     hacs_valid (0.03s)
  FAIL     config_valid (0.54s)
required gates: 13/13
```

`verify.py` rewrote `data/verification.json` and `docs/verification_report.md`
(timestamps and durations only); they are committed as `6e8cc85`.

---

## 2. Acceptance status

| Item | Command | Result |
| --- | --- | --- |
| HB §21 acceptance tests (AT-01…AT-20) | `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" -q -rx tests/acceptance` | `46 passed in 3.90s`; 20 `test_atNN_*.py` files; 0 xfail |
| HB §17 walkthrough | `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" -q tests/end_to_end/test_hb17_walkthrough.py` | `13 passed in 2.10s` |
| Accuracy regression gate | `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" -q tests/simulation/test_accuracy_v3.py` | `10 passed in 14.05s` |
| Zero xfails | full suite with `-rsx` | no `XFAIL`/`XPASS` lines; `grep xfail tests/acceptance/test_at*.py` matches only docstrings ("carries no xfail marker"); `test_closing_task_ids.py` enforces closed-by-task |

The per-AT closing-task table is in `docs/acceptance_report.md` §4 (unchanged
by this run; all 20 pass).

### 2.1 Accuracy harness (`python -m tests.simulation.accuracy_v3`, seed 17)

| Stratum | Records | Published | Attributed % | Group kWh | Unknown kWh | Residual preserved | Forced | MEASURED n | MAE / RMSE (W) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `hb17_co_switched` | 162 | 238 | 11.76 | 0.000000 | 0.008330 | 1.0000 | 0 | 156 | 0.228 / 0.228 |
| `hb17_solo_switches` | 162 | 238 | 11.76 | 0.000000 | 0.008330 | 1.0000 | 0 | 156 | 0.228 / 0.228 |
| `lab_dedicated` | 24 | 72 | 96.09 | 0.000000 | 0.000000 | 0.9583 | 0 | 71 | 1.556 / 2.286 |
| `lab_shared` | 240 | 550 | 54.03 | 0.161975 | 0.347380 | 0.9833 | 0 | 236 | 0.256 / 0.420 |
| `lab_ambiguous_pair` | 160 | 480 | 0.01 | 0.000000 | 0.000000 | 1.0000 | 0 | 160 | 0.058 / 0.075 |
| `lab_automation_burst` | 24 | 888 | 73.34 | 0.000000 | 0.000000 | 0.9583 | 0 | 574 | 5.923 / 88.202 |

`TOTAL forced_attribution_count: 0`. `--json` adds `lab_shared` `group_points: 61`.

**Drift.** `docs/acceptance_report.md` (last regenerated at `50c7fd9`, V1b) recorded
`lab_shared` as `49.23 % / 0.104445 / 0.071016 / 0.9708 / 0.313 / 0.500` and
51 group points. Every other stratum matches exactly. The `lab_shared` row and
its three narrative figures were updated in place, the same way that file was
produced before (hand-copied from the harness output, `50c7fd9`, `5c88be2`).
The new values stay inside the regression thresholds (`test_accuracy_v3.py`:
shared attributed 44–55 %, MAE ≤ 0.6 W, group points ≥ 45, group energy
≥ 0.09 kWh — 10 passed). This report does not attribute the drift to a
specific commit; engine commits since `50c7fd9` include Z4b–Z10b.

---

## 4. Migration rehearsal (DEC-02)

### 4.1 Forward and file-level rollback — `rehearse_migration.py --synthesize`

```
$ .venv/bin/python tools/dev/rehearse_migration.py --synthesize      # exit 0
Migration rehearsal
  input             : synthetic.db
  schema before     : 11
  schema after      : 26
  pre-migration bak : rehearsal.db.pre-v026.20261119T225523Z.bak
  schema rolled back: 11
  [OK  ] backup_created
  [OK  ] schema_advanced
  [OK  ] row_counts_preserved
  [OK  ] ids_preserved
  [OK  ] ha_unique_ids_unchanged
  [OK  ] legacy_archives_readable
  [OK  ] rollback_schema_restored
  [OK  ] rollback_counts_restored
  RESULT            : PASS
```

The synthetic schema-11 database is built from the engine's released migration
statements and seeded in 13 tables (`ct_channels`, `ct_samples`,
`device_events`, `device_model_versions`, `device_models`, `devices`,
`engine_state`, `ha_entity_bindings`, `inference_results`, `learning_samples`,
`residual_power`, `topology_relations`, `topology_versions`). The forward step
is the real startup path (`Database(path)`); `errors: []`. The backup's name
carries the *target* version (`pre-v026`); its content is the schema-11 file.

What the tool's rollback checks cover: the backup is copied over the migrated
copy and its `user_version` (11) and row counts are compared to the input. They
do **not** start the previous engine. That was done separately (§4.2).

### 4.2 Rollback: the previous engine starts on the restored backup

A throwaway script (outside the repo, not committed) extracted `main`'s
`engine/` read-only with `git archive main engine`, then:

1. at HEAD: synthesized a schema-11 DB, migrated a copy with HEAD's `Database`
   (11 → 26), which wrote `migrated.db.pre-v026.<ts>.bak`;
2. copied the backup to `restored.db` (journal mode reset, as the tool does);
3. in a separate interpreter importing **only** `main`'s package (asserted by
   module path): opened `restored.db` with `main`'s `Database`, built `main`'s
   `EngineCore` with an explicit `EngineConfig` (placeholder token, unreachable
   HA URL — no `.env` read, no HA connection, `core.start()` not called),
   mounted `main`'s `create_app(core)` and issued HTTP GETs.

```
STAGE1 exit 0
{"before": 11, "after": 26, "backups": ["migrated.db.pre-v026.20261119T225640Z.bak"]}
STAGE2 exit 0
{"main_latest_schema": 11, "user_version_after_open": 11, "integrity": "ok",
 "http": {"/healthz": {"status": 404, ...},
          "/api/v1/health": {"status": 200, "body": "{\"status\": \"degraded\",
             \"contract_version\": \"2.4.0\", \"engine_version\": \"0.2.1-dev.42\",
             \"database\": \"ok\", \"ha_connected\": false, ...
          "/api/v1/status": {"status": 200, "body": "{\"status\": \"degraded\",
             \"contract_version\": \"2.4.0\", ...}}}
```

Result: the previous engine (contract 2.4.0, schema 11) opens the restored
backup without migrating it, `integrity_check` is `ok`, and its API answers
`database: ok`. `degraded` is because HA is not connected, as intended.
`/healthz` is 404 on `main`'s API app (on `main` it is not a route of
`create_app`). **Not done:** a full add-on container start of the previous
image against a real HA; that remains part of the operator procedure below.

### 4.3 Operator rollback procedure (DEC-02, `RELEASE_NOTES.md`, `docs/deployment.md` §7)

There are no down-migrations.

1. Stop the WattMosaic add-on/container.
2. In `/data`, replace `wattmosaic.db` with the automatic pre-migration backup
   `wattmosaic.db.pre-vNNN.<UTC-timestamp>.bak` written by the first v3 start,
   and delete any `wattmosaic.db-wal` / `wattmosaic.db-shm` (they belong to the
   migrated file).
3. Reinstall the **previous** add-on version (engine + integration together;
   contract 2.4.0).
4. Start it and check `/api/v1/health`: `database: ok`, the previous contract
   version. Never run an older engine against a newer-schema database.

See §8 R1: the version string has not been bumped, which affects step 3.

---

## 5. Performance against budgets

`perf_runtime.py` defaults: 40 readers, 500 HA entities, 30 events/s total,
3600 s simulated, seed 20261119; 108 000 samples per run. Gates enabled.

### 5.1 Harness defect found and fixed during V2

The first full-scale run (at `e5f0608`) logged `background job failed` 295 times:
the harness's timing wrapper hid each job's signature, so the executor's
`budget=` probe passed `budget` to jobs that do not take it
(`TypeError: ... got an unexpected keyword argument 'budget'`). Its reported job
durations were the time to fail. Fixed in `c420a09` (`functools.wraps`; the
executor's failure count is now reported as `background_jobs_failed`; a unit
test pins it and fails with the fix removed). That exposed a second harness bug
(`CandidateRing.prune() takes 1 positional argument but 2 were given`, 59 per
run), fixed in `565cd7c`. Both are in `tools/dev/perf_runtime.py`; the
production executor and runtime were not changed. The two runs below are at
`565cd7c` with `background_jobs_failed: 0` and zero `background job failed` lines.

### 5.2 Results (two runs, `565cd7c`)

| Metric | Budget | Run A | Run B |
| --- | --- | --- | --- |
| Fast path µs/sample p50 / p95 / p99 | p99 < 200 µs (gate) | 25.92 / 37.29 / 57.79 | 25.75 / 35.67 / 51.08 |
| Event loop ready→published ms p50 / p95 / p99 | none set | 501.72 / 1243.54 / 1337.30 | 418.24 / 1172.78 / 1289.07 |
| `/healthz` ms p50 / p95 / p99 (200 probes under load) | p99 < 100 ms (gate) | 11.57 / 25.90 / **42.45** | 11.94 / 25.02 / **31.20** |
| Matrix 500×100 `GET /api/v1/matrix` p95 | < 300 ms (M3) | 208.04 ms | 208.79 ms |
| Matrix payload build (50 000 cells) | — | 49.80 ms | 49.40 ms |
| Groups superseded (coalesced) / dropped | — | 23 407 / 0 | 23 781 / 0 |
| Event cycles published | — | 76 | 74 |
| Gate line | | `/healthz p99 42.45/100.0 OK, fast path p99 0.0578/0.2 OK` | `/healthz p99 31.20/100.0 OK, fast path p99 0.0511/0.2 OK` |

Background jobs (59 runs each except as noted; ms):

| Job | Run A p99 / total | Run B p99 / total |
| --- | --- | --- |
| `prune_evidence` | 23.30 / 698.3 | 23.11 / 690.8 |
| `reader_state_prune` | 0.14 / 2.4 | 0.14 / 2.4 |
| `candidate_ring_prune` (108 059 calls, incl. inline) | 0.00 / 181.4 | 0.00 / 178.1 |
| `self_heal` | 0.02 / 1.1 | 0.03 / 1.1 |
| `learning` | 0.00 / 0.2 | 0.00 / 0.1 |
| `matrix_updater` (per event cycle) | 0.82 / 12.7 | 0.93 / 12.6 |

All are far below the DEC-37 2 s per-job cap. In this synthetic load nothing is
learned and no T7 change is detected, so `learning` and `self_heal` durations
are near zero; they are not evidence of cost under a real training cycle.

Caveats, stated as facts:

- The matrix p95 is over **3** GETs (`get_count: 3`), i.e. effectively the max.
  The two pre-fix runs at `e5f0608` (same engine) measured 544.85 ms and
  570.96 ms, and one of them a 399.50 ms payload build: the 300 ms M3 budget is
  met in both final runs but not in every run on this machine.
- Event-loop latency p50 is 0.4–0.5 s and p95 ~1.2 s under a sustained 30 events/s
  burst with ~23 k superseded revisions; no budget is set for it (design §10).
- The U1 browser budget (render < 1.5 s, < 60 000 DOM nodes for 500×100) is not
  measured by this tool.
- Shared developer laptop, not Pi-class hardware; no CI-hardware run was made
  for this report.

Pre-fix run at `e5f0608` (for completeness; job durations invalid): fast p99
52.83 µs, event p50/p95/p99 414.52/1262.45/1676.30 ms, `/healthz` p99 45.96 ms,
matrix GET p95 544.85 ms. Both gates passed in that run too.

---

## 6. DB growth and retention

### 6.1 Measured growth (dbstat delta after the matrix seed, extrapolated per day)

| Table / index | Run A B/day | Run B B/day |
| --- | ---: | ---: |
| `ct_samples` | 490 733 568 | 490 733 568 |
| `idx_ct_samples_ct_time` | 119 144 448 | 119 144 448 |
| `idx_ct_samples_time` | 100 368 384 | 100 368 384 |
| `inference_snapshots` | 52 297 728 | 50 921 472 |
| `event_evidence` | 29 982 720 | 29 982 720 |
| `event_inference_records` | 7 766 016 | 7 176 192 |
| `ha_events_v3` (+ 2 indexes) | 5 406 720 | 5 308 416 |
| `event_groups` (+ 2 indexes) | 2 162 688 | 2 162 688 |
| `reader_residual_series` (+ index) | 786 432 | 786 432 |
| other indexes | 393 216 | 393 216 |
| **Total (file)** | **810 713 088** | **808 550 400** |

Raw samples (`ct_samples` + its two indexes) are **710 246 400 B/day (≈ 710 MB)**,
about 88 % of growth, at the benchmark's rate of 30 samples/s (108 000 per
simulated hour) — ≈ 274 B per sample including indexes. At 40 readers × 1 Hz
(3 456 000 samples/day) the same per-sample cost gives ≈ 947 MB/day.
`event_evidence` is ≈ 30 MB/day (Z8: one row per (event, reader)).

### 6.2 Configured retention and implied steady state

| Data | Configured retention | Source | Implied steady state |
| --- | --- | --- | --- |
| `ct_samples` | `retention_days_raw = 30` (add-on option, `WM_RETENTION_DAYS_RAW`) and `evidence_raw_retention_days = 30` (`WM_EVIDENCE_RAW_RETENTION_DAYS`); both delete `ct_samples` | `config/settings.py:184,227`; `core/runtime.py` `_prune_evidence`, `_maintenance_loop` | ≈ 21.3 GB at 30 samples/s; ≈ 28.4 GB at 40 × 1 Hz |
| `event_evidence` | 400 days (DEC-30) | `settings.py:224` | ≈ 12 GB |
| `inference_snapshots`, `event_inference_records`, `ha_events_v3`, `event_groups`, `reader_residual_series` | **not age-pruned** (absent from `storage/tiers.py` `prunable_tables()`) | `tiers.py` | ≈ 69 MB/day ≈ 25 GB/year, unbounded |

SQLite reuses freed pages, so a pruned table stops growing the file once the
horizon is reached; the file does not shrink without `VACUUM`.

### 6.3 Recommendation (not implemented)

- **Settings now:** set `retention_days_raw` to **7** for installations with
  ~40 readers (≈ 5–6.6 GB of raw samples instead of 21–28 GB). Raw samples are
  only needed for the evidence windows (whose summaries live in
  `event_evidence`) and for replay. Note `WM_EVIDENCE_RAW_RETENTION_DAYS` also
  prunes `ct_samples` but is not an add-on option; the lower of the two wins.
  The add-on schema accepts `retention_days_raw` up to 3650 while the engine
  validates 1–90 (`RETENTION_DAYS_RAW_RANGE`).
- **Follow-up task A — raw samples:** store raw samples as a change-point series
  (skip a sample equal to the previous one within the noise band, as
  `reader_residual_series` already does) and/or downsample to 1-min aggregates
  after N days; check whether both `ct_samples` indexes (≈ 220 MB/day together)
  are needed.
- **Follow-up task B — derived history:** decide a retention horizon for
  `inference_snapshots` (≈ 51–52 MB/day) and `event_inference_records`; today
  they grow without bound.

---

## 7. Real-data replay — OPERATOR-PENDING

`ls data/wattmosaic.db` → `No such file or directory`. No real database exists in
this checkout, so the real-topology tests skip (7 skips, §1) and no replay was
run. No `.env` or secret file was read. No real data is committed.

Findings that affect the operator steps:

- `tools/dev/replay_nesting.py` **does not exist at HEAD**: it was deleted by
  `d49ba7f` (ZC). It exists on `main` (pre-3.0). There is no v3 `--score`
  equivalent in `tools/`. The X00 pre-3.0 baseline score was never recorded
  (`progress.md` Baseline row is `—`).
- `tools/audit/replay_capture.py --help` (HEAD) states: "Default: read the active
  topology/mapping/model versions from --db", with `--versions` as an explicit
  override — the V2 step 2 requirement (no HB17 version triple) is met in the
  tool's CLI; it has not been exercised on a real DB here.

Commands for the operator (on a **copy**; never commit the DB or outputs):

```bash
# 0. copy the live DB (consistent online backup), e.g. per docs/deployment.md §7
cp /path/to/backup/wattmosaic.db /tmp/wm-copy.db

# 1. migration rehearsal on the real copy (schema N -> 26 and back)
.venv/bin/python tools/dev/rehearse_migration.py --db /tmp/wm-copy.db

# 2. real-topology tests against the copy
WATTMOSAIC_REAL_DB=/tmp/wm-copy.db .venv/bin/python -m pytest -p no:cacheprovider \
  -o addopts="" -q tests/simulation/test_real_topology.py

# 3. pre-3.0 nesting baseline: from a checkout of main (tool removed on feat/realtime-v3)
.venv/bin/python tools/dev/replay_nesting.py --db /tmp/wm-copy.db --score

# 4. v3 replay of the capture (reads the active versions from --db)
.venv/bin/python tools/audit/replay_capture.py --db /tmp/wm-copy.db --out /tmp/wm-replay.json
```

---

## 8. Known limitations and deferred items

Release items:

- **R1 — version not bumped.** `pyproject.toml` and `addon/config.yaml` say
  `0.2.1-dev.42`, identical to `main`, while `RELEASE_NOTES.md` announces
  `v0.3.0-dev.1`. The Supervisor cannot distinguish "the previous add-on
  version" from this one by version, so rollback step 3 is ambiguous until the
  release bump (`tools/dev/bump_version.py`) is done.
- **R2 — real-data replay pending** (§7); `replay_nesting.py --score` has no v3
  successor.
- **R3 — growth** of un-pruned derived tables and raw-sample volume (§6).

Product limitations:

- **DEC-38:** after a Smart Plug relocation the TV may or may not be remapped;
  the shipped behaviour (retire the old mapping edge, promote a new one, topology
  version unchanged) is accepted, as AT-15 asserts. Neither reading is required.
- The HB §17 lights never receive a group estimate: the 12-minute script
  co-switches them once, below `MIN_HISTORY_EVENTS`. Group estimates are
  demonstrated on `lab_shared` (61 group points, 0.161975 kWh).
- A live group value assumes the other loads on that CT stay constant while it
  is computed from the shared CT.
- The accuracy harness declares some single-subject endpoint scopes on the user's
  behalf (a branch CT declared for exactly one lab device; the HB §17 plug).
- CONFIRMED groups closed by a version promotion (Z7) are not re-delivered to the
  L6 learner.
- L8 (active learning / component suggestions, DEC-36, DEC-07) is optional and
  **not done**.
- Accuracy is synthetic only; per-device accuracy in a real home is unverified.

Carried from `tasks/progress.md`:

- D1 writes mappings through the repository's private `_conn`/`_lock` ("tidy later").
- P1: the legacy-path DEF-01 polarity defect was never proven; that path was
  removed in Z2.
- HB §17 `hb17_solo_switches` grades identically to `hb17_co_switched` because
  of a simulator limitation (`docs/acceptance_report.md` §2).
- Optional `config_valid` gate needs `HA_TOKEN`; not run here.

---

## 9. Merge decision

Merging `feat/realtime-v3` into `main` is a **human decision**. This report does
not merge, push or recommend a merge date. Facts for that decision: required
gates 13/13, full suite 2989 passed / 7 skipped / 0 failed / 0 xfail, both perf
gates met in two runs, migration rehearsal passed forward and back on synthetic
data with the previous engine opening the restored backup; open items R1–R3 (§8).

---

## Commits made for V2

- `6e8cc85` — regenerated `data/verification.json`, `docs/verification_report.md` (`verify.py`).
- `c420a09` — `fix(perf)`: timed background jobs keep their signature; failure count reported and tested.
- `565cd7c` — `fix(perf)`: candidate-ring prune instant passed by keyword.
- this report, the `docs/acceptance_report.md` `lab_shared` update and the
  `tasks/progress.md` V2 row (next commit).
