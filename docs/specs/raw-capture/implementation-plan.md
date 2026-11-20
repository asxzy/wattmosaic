# Implementation plan

## 1. DAG

```
Wave 1 (7 agents, no dependencies)          Wave 2 (2 agents)        Wave 3 (1 agent)
T01 settings ─────────────────────────┬──▶  T08 runtime wiring ──┐
T02 prune tier (capture_days) ────────┤                          │
T03 batched purge module ─────────────┘                          ├──▶ T10 verification
T01 ──────────────────────────────────────▶ T09 diagnostics ─────┤
T04 HA-history replay source ────────────────────────────────────┤
T05 recorder coverage probe ─────────────────────────────────────┤
T06 App option + .env.example ───────────────────────────────────┤
T07 docs + ADR-0042 ─────────────────────────────────────────────┘
```

Wave 1 tasks do not import each other's new code. Each codes against `interfaces.md` and
tests only its own unit. Cross-unit checks (App↔engine parity, end-to-end runtime) wait
for Wave 2 and Wave 3.

## 2. File ownership (disjoint within each wave)

| Task | Owns (may edit) | New files |
|---|---|---|
| T01 | `engine/wattmosaic_engine/config/settings.py` | `tests/unit/test_raw_capture_settings.py` |
| T02 | `engine/wattmosaic_engine/storage/tiers.py`, `storage/repositories/aggregates.py`, `storage/database.py` (`prune` wrapper only), `tests/unit/test_storage_config.py` (the `ct_samples` tier assertion only, `:183`) | `tests/unit/test_capture_prune.py` |
| T03 | none existing | `engine/wattmosaic_engine/storage/capture.py`, `tests/unit/test_capture_purge.py` |
| T04 | `tools/audit/replay_capture.py`, `tools/audit/ha_client.py` (only if strictly needed) | `tests/unit/test_replay_ha_history.py` |
| T05 | none existing | `tools/audit/check_recorder_coverage.py`, `tests/unit/test_recorder_coverage.py` |
| T06 | `addon/config.yaml`, `addon/run.sh`, `addon/README.md` (options section, if any), `.env.example` | none |
| T07 | `docs/configuration.md`, `docs/decisions/README.md`, `docs/specs/realtime-topology-disaggregation/decisions.md` (DEC-30 row), `docs/specs/realtime-topology-disaggregation/TODO.md` (§1 text) | none |
| T08 | `engine/wattmosaic_engine/core/runtime.py`, `engine/wattmosaic_engine/main.py` (one counter init line) | `tests/unit/test_runtime_raw_capture.py` |
| T09 | `engine/wattmosaic_engine/core/diagnostics_api.py`, `engine/wattmosaic_engine/diagnostics/report.py` (`engine_health` dict only), `addon/ui/view-diagnostics.js` | `tests/unit/test_diagnostics_raw_capture.py` |
| T10 | `docs/specs/realtime-topology-disaggregation/TODO.md` (§1 numbers), `PROJECT_STATUS.md` (append), any test file needing a fix found by the full suite | `tests/unit/test_raw_capture_parity.py`, `docs/specs/raw-capture/verification.md` |

Rule: a task that needs to edit a file it does not own **stops and reports** what it needs
and why. It does not edit the file.

Never edited by any task: `contracts/`, `data/schemas/`, `storage/migrations/`, version
fields, `RELEASE_NOTES.md`.

## 3. Agent protocol

Every agent:

1. Branches from the integration branch `feat/raw-capture` (created from the plan branch
   before Wave 1) in its own worktree: `feat/raw-capture-tNN`.
2. Reads `README.md`, `design.md`, `interfaces.md`, then its task file. Reads `CLAUDE.md`.
3. Writes tests first where the task lists them, then the implementation.
4. Before handoff runs:
   ```bash
   .venv/bin/ruff check engine tools tests
   .venv/bin/pytest <its new test file> <tests named in its task>
   .venv/bin/pytest            # full suite; report any failure outside its scope, do not fix it
   ```
   plus any extra gate listed in its task.
5. Commits with Conventional Commits and a scope, citing ADR-0042 / DEC-30 where
   retention semantics change, e.g. `feat(storage): capture retention tier for ct_samples (ADR-0042)`.
6. Hands back: branch name, files changed, tests added, gate output, open questions.

The orchestrator merges each wave into `feat/raw-capture` in task order (T01→T07, then
T08→T09), runs the full suite after each wave, and only then starts the next wave.

## 4. Wave gates

| After | Must pass on `feat/raw-capture` |
|---|---|
| Wave 1 | `.venv/bin/pytest`, `ruff check engine tools tests`, `bash scripts/validate_addon.sh --no-build`, `python tools/dev/check_panel.py` |
| Wave 2 | the above + `python tools/dev/export_schemas.py && git diff --exit-code data/schemas` (proves no contract drift) |
| Wave 3 | every CI gate in `CLAUDE.md` § Commands, on Python 3.11 and 3.12 |

## 5. Expected merge conflicts

None by construction. Two near misses to watch:

- `TODO.md` is edited by T07 (Wave 1, §1 prose) and T10 (Wave 3, §1 numbers).
  They are sequential.
- `tests/unit/test_storage_config.py:183` (T02) asserts the `ct_samples` tier. T01 adds
  its tests in a new file so it never touches this file.

## 6. Launch

Start with:

```bash
git switch -c feat/raw-capture worktree-raw-retention-plan
```

Then fan out Wave 1 (T01–T07) as seven parallel agents, each given
`docs/specs/raw-capture/tasks/TNN.md` as its brief.
