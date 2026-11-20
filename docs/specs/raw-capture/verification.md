# T10 verification — raw CT capture (ADR-0042)

Verified on 2026-11-19. This document maps every acceptance criterion in
`design.md` §4 to evidence. Tests were run with the repository's editable
install; the CI gates were run on Python **3.11.12**, **3.12.10** and the shared
venv (**3.14.5**). All commands were run from the repository root.

## AC mapping

| AC | Evidence |
|---|---|
| AC1 | `tests/unit/test_runtime_raw_capture.py::test_default_does_not_persist_readings` (real `EngineCore`, `ct_samples` empty, `samples_persisted == 0`, snapshot + inference still produced). Full suite green on 3.11/3.12/3.14. |
| AC2 | `tests/unit/test_runtime_raw_capture.py::test_capture_persists_readings` and the capture-horizon tests in that module. |
| AC3 | `tests/unit/test_capture_purge.py::*` + the upgrade drain rehearsal below (100k legacy rows drained to 0 in bounded batches). |
| AC4 | `tests/unit/test_capture_prune.py::*`. |
| AC5 | `tests/unit/test_runtime_raw_capture.py::*` plus source inspection of `_prune_evidence` (`core/runtime.py:2304-2337`): it calls `apply_evidence_retention` only; no `DELETE FROM ct_samples` remains. The "no hidden reader" search below confirms no runtime read path. |
| AC6 | `tests/unit/test_raw_capture_settings.py::*` (T01) + new `tests/unit/test_raw_capture_parity.py` (App↔engine↔storage parity) + `bash scripts/validate_addon.sh --no-build` (App option bounds/default verified). |
| AC7 | `tests/unit/test_replay_ha_history.py::*`. |
| AC8 | **pending T05 merge (verified by orchestrator)** — `tools/audit/check_recorder_coverage.py` / `tests/unit/test_recorder_coverage.py` are not present on this branch (T05 is being fixed on another branch). Not tested here. |
| AC9 | `perf_runtime.py --quick` numbers below: `ct_samples` growth ≈ 0 with the default. |
| AC10 | All CI gates green on 3.11.12, 3.12.10 and 3.14.5 below; full suite green on all three. |

## Cross-unit parity tests (new)

```
$ python -m pytest -q tests/unit/test_raw_capture_parity.py
...                                                                      [100%]
```

`tests/unit/test_raw_capture_parity.py` adds:
- `test_addon_and_engine_capture_bounds_match` — `schema.raw_capture_days` `int(0,90)` == `RAW_CAPTURE_DAYS_RANGE`.
- `test_addon_capture_default_matches_engine` — App default == `EngineConfig().raw_capture_days` == 0 and `raw_capture_days|WM_RAW_CAPTURE_DAYS|0` is in `run.sh`.
- `test_tier_env_name_matches_settings` — `tiers.RAW_CAPTURE_ENV == "WM_RAW_CAPTURE_DAYS"` and setting that env var sets the field.

## No hidden reader

```
$ grep -rnE "ct_samples|repos\.samples\.(readings_between|ct_samples_between|latest_|sample_exists)" \
    engine/wattmosaic_engine addon/ui custom_components | grep -v migrations/
```

Every hit is a **write path** (`samples.py` INSERT / repository method definitions retained
per design.md §2 non-goals), the **purge** (`storage/capture.py`), the **tier registry**
(`storage/tiers.py`), the deliberate write-site in `core/runtime.py:234`, or a **docstring**.
A follow-up search for actual *calls* to the sample read methods outside
`storage/repositories/samples.py` returned no hits:

```
$ grep -rnE "\.(readings_between|ct_samples_between|latest_ct_sample|latest_ct_samples|latest_reading|sample_exists)\s*\(" \
    engine/wattmosaic_engine addon/ui custom_components \
    | grep -v "storage/repositories/samples.py" | grep -v migrations/
(no hits)
```

Conclusion: no read path remains. AC5 / design §6 "hidden reader" risk cleared.

## CI gates

Shared venv (Python 3.14.5), also the exact command set the caller runs:

```
ruff check engine tools tests                         -> All checks passed! (0)
python tools/dev/export_schemas.py                    -> 0; git diff --exit-code data/schemas -> 0
python tools/dev/render_validation_docs.py --check    -> {"status": "up_to_date", "files": 4} (0)
bash scripts/validate_addon.sh --no-build             -> passed: 87 failed: 0 skipped: 3 (0)
python tools/dev/check_panel.py                       -> 5 assets, 13 modules, self-contained, CSP-safe (0)
python tools/dev/check_secrets.py                     -> secret scan: clean (0)
python tools/dev/check_release.py                     -> version 0.3.0-dev.4, release checks clean (0)
python tools/dev/check_hacs.py                        -> hacs validation: ok (0)
test -s docs/specs/raw-capture/verification.md        -> 0
```

Full suite (shared venv, 3.14.5): `python -m pytest -q -x --ignore=tests/simulation`
-> **2905 passed, 1 skipped** (exit 0). 2902 baseline + 3 new parity tests.

Python 3.11.12 (`uv venv --python 3.11`, editable install):
```
python -m pytest -o addopts='' -q --ignore=tests/simulation -> 2905 passed, 1 skipped (exit 0)
ruff check engine tools tests                              -> All checks passed! (0)
export_schemas / schemas-diff / render / panel / secrets / release / hacs -> all 0
PYTHON=<venv>/bin/python bash scripts/validate_addon.sh --no-build -> passed: 87 failed: 0 skipped: 3 (0)
```
Python 3.12.10 (`uv venv --python 3.12`, editable install): identical results —
2905 passed, 1 skipped; all gates 0; addon 87 passed / 0 failed.

Note on the addon gate: `validate_addon.sh` selects the interpreter via the `PYTHON`
env var, else `<repo>/.venv/bin/python`, else bare `python3`. A first run from the
scratch 3.11/3.12 venvs failed only with `ModuleNotFoundError: No module named
'yaml'` because the script's fallback `python3` was the system interpreter without
PyYAML; re-running with `PYTHON=<scratch venv>/bin/python` (the script's documented
override) passed all 87 checks. This is an environment artifact, not a code failure.

Known-noisy test `tests/integration/test_matrix_api.py::test_500x100_matrix_bounded_queries_and_no_writer_lock`
(TODO §4): passed in the full-suite runs and also when run alone:
```
$ python -m pytest -q tests/integration/test_matrix_api.py::test_500x100_matrix_bounded_queries_and_no_writer_lock
.                                                                        [100%]
```

## AC9 — perf numbers (`tools/dev/perf_runtime.py --quick`, seed 20261119)

`perf_runtime.py` builds `EngineConfig(...)` directly (`tools/dev/perf_runtime.py:302`)
and does **not** read `WM_RAW_CAPTURE_DAYS` from the environment — confirmed by a
run with `WM_RAW_CAPTURE_DAYS=7` showing no `ct_samples` row. Since that tool is not
T10-owned, the capture-on run used an external wrapper that injected the field into
the tool's own factory exactly as the tool sets its other config fields; no repository
file was changed.

| Run | `db_growth_bytes_per_day` | `db_growth_by_table_bytes_per_day["ct_samples"]` |
|---|---|---|
| default config (`raw_capture_days=0`) | 536,739,840 | **absent (0)** |
| `raw_capture_days=7` | 1,386,086,400 | **507,248,640** |

With the default, `ct_samples` contributes zero growth; the raw share (~507 MB/day)
is eliminated, reducing total measured growth by roughly that amount. Per-table
growth (default): event_evidence 176,947,200; inference_snapshots 165,150,720;
event_inference_records 100,270,080; ha_events_v3 53,084,160;
idx_ha_events_v3_entity_time 17,694,720; reader_residual_series 11,796,480
(bytes/day). These are the numbers recorded in TODO §1.

## Upgrade drain rehearsal (AC3)

A real `Database` on a temp DB with **100,000** synthesized legacy `ct_samples`
rows (30 days of history) was driven through the real `EngineCore._purge_raw_capture`
with the default config (capture off, cutoff `None`) until the table was empty.
Per-batch durations were sampled through the injected `monotonic` seam of
`purge_ct_samples`.

```
before=100000 after=0 passes=1 purged_counter=100000
total_wall_s=0.1798 sampled_marks=19
longest_sampled_batch_s=0.020370
PASS before>after==0: True
PASS no batch > budget+one-batch (3s): True
```

The drain completed in one maintenance pass (20 batches ≤ 5000 rows, each in its
own short `BEGIN IMMEDIATE` transaction). The longest batch (~20 ms) is far below
the 2 s time budget; no batch exceeded the budget by more than one batch.

## Operator-run items (not run here; require a live HA)

- `tools/audit/check_recorder_coverage.py --db <prod copy>`
- `tools/audit/replay_capture.py --ha-history --db <prod copy> --minutes 60`

## Test fixes made here

None. No existing assertion was weakened.
