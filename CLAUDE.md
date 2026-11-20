# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

WattMosaic does real-time, topology-aware load disaggregation for Home Assistant. It takes multi-CT power readings plus HA state changes and turns them into per-device power estimates, with bounds and groups when a device is not identifiable. It has three deployables:

- `engine/wattmosaic_engine/`: the standalone Python engine (aiohttp API under `/api/v1`, SQLite WAL). All ingestion, inference, learning and persistence happen here.
- `custom_components/wattmosaic/`: the HA integration. It is a thin HTTP client of the engine, and it **never computes power** (ADR-0001).
- `addon/`: HA OS App packaging. `gateway.py` is a reverse proxy on ingress port 8787 that injects `X-API-Token` for Supervisor-ingress peers and forwards to the engine on loopback 8788. `addon/ui/` is the ingress panel: vanilla-JS **ES modules** (`main.js` loaded as `type="module"`, `view-*.js`, `matrix-*.js`), with no build step, no inline script/style and no external origins (CSP).

## Commands

```bash
uv venv && uv pip install -e ".[dev]"    # or python3 -m venv; always call tools via .venv/bin/
.venv/bin/pytest                          # full suite (addopts -q, asyncio_mode auto)
.venv/bin/pytest tests/unit/test_x.py::test_name
.venv/bin/ruff check engine tools tests   # custom_components/addon/data are excluded
.venv/bin/mypy engine/wattmosaic_engine   # advisory in CI
```

CI gates (`.github/workflows/ci.yml`, Python 3.11 and 3.12):

```bash
python tools/dev/export_schemas.py && git diff --exit-code data/schemas   # schemas match contracts/
python tools/dev/render_validation_docs.py --check
bash scripts/validate_addon.sh --no-build    # App install path; runs tools/dev/check_panel.py
python tools/dev/check_panel.py              # panel: assets/modules present, CSP-safe, no retired vocab
python tools/dev/check_secrets.py
python tools/dev/check_release.py            # version fields agree + RELEASE_NOTES section
python tools/dev/check_hacs.py
```

Dev tools: `tools/dev/perf_runtime.py [--quick]` benchmarks the real three-loop runtime under synthetic load (fast-path µs/sample, ready→published latency, `/healthz` under load, DB growth). `tools/dev/rehearse_migration.py --synthesize` (or `--db <copy>`) rehearses the real startup migration plus backup/rollback on a database copy. `python -m tools.simulator.cli --seed 42 --minutes 120` generates a synthetic household with ground truth.

## Architecture (contract 3.0)

**Contracts are the boundary.** `contracts/` is a package facade: `foundation` (Model base, `CONTRACT_VERSION = "3.0.0"`, version ids, confidence vector), `entities`, `measurement`, `topology`, `inference`, `matrix`, `power_models`, `service`, all star-exported, so callers write `from wattmosaic_engine.contracts import X`. If you change a contract, bump `CONTRACT_VERSION` (a breaking change is a major bump), regenerate `data/schemas/` with `export_schemas.py` (never hand-edit them) and fix every consumer. Contract enums are `class X(str, Enum)`, which is why ruff ignores `UP042`.

**Dependency direction** (spec `architecture.md` §4):
```
main → core → {realtime, inference, mapping, learning, versioning, api}
inference → {electrical, residual, models, runtime.interfaces, contracts}
mapping   → {electrical, runtime.interfaces, contracts, storage.repositories}
realtime  → {synchronization, runtime.interfaces, contracts}
contracts, runtime.interfaces → nothing else in the package
```
`inference` never imports `realtime` or `mapping`. It takes an `EventGroup` and returns a `JointInferenceResult`, and the runtime wires them, which keeps `infer_group` pure and replayable. `inference/` touches no database. `mapping/` and `versioning/` get their time from an injected clock and their state from injected repositories, and that is what makes replay byte-identical.

**Runtime.** `main.py` composes `EngineCore` from mixins in `core/` (`runtime`, `snapshot`, `topology_api`, `mapping_api`, `models_api`, `discovery_api`, `diagnostics_api`). `core/runtime.py` is the three-loop runtime:
- **fast** (`on_reading` / `on_ha_event`): P1 `realtime/normalize` (polarity and multiplier applied once) → P2 `reader_state` → P3 `detection` (CUSUM step detector) → candidate ring. It is O(1) and never raises.
- **event** (`_event_loop`): P4 `windows` (immutable event evidence) and P5 `grouping` (groups with late-arrival revision) feed ready groups. These are coalesced per group, and each runs infer → persist → matrix → snapshot → learning on the inference executor.
- **steady** (`_steady_loop`): debounced >3σ steady reads plus a staleness heartbeat.
- Background jobs (learning, residual priors, matrix reconcile and so on) run on `core/executors.BackgroundExecutor` with per-job CPU budgets (DEC-37, ADR-0033).

**Packages:**
- `inference/` I1–I7: candidates/topology filter → temporal/evidence → joint/covariance → identifiability/bounds → estimates/confidence_v3 → matching/records → `orchestrator` (`infer_group`, `infer_steady`).
- `electrical/`: `topology_graph` (lifecycle-filtered v3 edges), `operator` (observation operator `H(T, M, t)`), topology evidence ledger and change detection.
- `mapping/`: the measurement matrix (projection, cells, column/row trees, store/updater/reconcile).
- `models/` holds versioned power-model specs and the registry. `learning/` covers observation → gate → incremental train → promote, plus reconciliation replay.
- `versioning/` holds the independent topology and mapping lifecycles (propose/promote/rollback, `VersionTriple`). A plug move never touches a power model.
- `residual/` covers reader residuals and unknown-load profiles.
- `disaggregation/` now holds only placement helpers (`auto_placement`, `group_fit`).
- `backend/` loads the inference backend lazily. If it is missing, the engine runs degraded and does not crash.
- The legacy absolute solver, `integration.py`/`DisaggregationEngine`, the pointer topology graph and the legacy mapping-matrix projection were removed at the cutover. Don't reintroduce them.

**Storage migrations.** `storage/migrations/` is a package. Versions 1–11 live verbatim in `legacy.py`. Each later version is its own module `vNNN_<slug>.py` that exports `MIGRATION` (whose version must equal NNN) and `TIERS`. Modules are discovered automatically, `LATEST_VERSION` is the maximum (currently 26), and numbering has gaps. `v012` is refused on purpose, because 12 is the payload-encoding version. Each migration runs in `BEGIN IMMEDIATE` and must be idempotent (`add_column_if_missing`), with no auto-committing statements such as `VACUUM`. To change the schema, add a new module and never edit an applied one. `v025` archives legacy tables as `*_legacy`. Coverage comes from `tests/unit/test_migration_matrix.py` and `rehearse_migration.py`.

**HA registry boundary (ADR-0028).** Nothing under `engine/` reads HA's entity or device registry. The integration resolves device links (`custom_components/wattmosaic/state_links.py`) and sends entity ids only. The engine never ingests WattMosaic's own outputs: `ingestion/normalize.py` drops the `wattmosaic` domain and `sensor.wattmosaic_*`.

**Integration tested without `homeassistant`.** The logic lives in pure modules (`flow_logic.py`, `service_logic.py`, `state_links.py`, `entity_values.py`). `tests/integration/test_integration_static.py` AST-checks the rest, including that no service-call or actuation API is used.

## Invariants that are correctness, not style

- The engine is **read-only** toward HA. Never actuate, not even to make test data. Use the simulator. Active learning only *suggests*.
- **Absent is `None`, never 0** (HB §18). A non-identifiable member gets null, justified bounds and a group, never a number, and model priors are never bounds (DEC-19).
- The residual is reported, not distributed. Nested CTs are never double counted. A double-pole pair is one load, and export power stays negative.
- **Topology:** one event never promotes an edge past CANDIDATE, a parent-only change is not evidence against containment, and PROVISIONAL edges are soft. Topology, mapping and model versions stay independent.
- Confidence is not a probability. Exceptions crossing a component boundary become a degraded state, never a crash loop. Long work stays off the asyncio loop (ADR-0033). New endpoints require `X-API-Token` when one is configured.
- **No scenario knowledge in engine code.** `tests/unit/test_z3a_no_scenario_names.py` statically bans HB §17 names (`ct_main`, `"Main"`, `hb17`, `_TV_STANDBY_W`, plug-substring tricks) under `engine/wattmosaic_engine`. Derive behaviour from declared topology and models.
- **Privacy:** never commit live-install data (`data/*.json`, `data/raw/`, `data/passive/`, `data/wattmosaic.db` are git-ignored and CI-checked). Published examples come from synthetic `data/examples/reference_house/`.
- **Retired vocabulary:** the "dedicated device" CT role was removed (ADR-0034). `test_docs_vocabulary.py` and `check_panel.py` gate it. `CircuitClass.DEDICATED` is a different, live concept.
- **Python 3.11 compatibility:** dataclass defaults must be hashable. Use `field(default_factory=...)` for mutable or unhashable values, including `MappingProxyType`, because 3.11 rejects them as plain defaults.

## Tests

- `tests/unit`: fast and deterministic.
- `tests/integration`: cross-module checks, the release pipeline and static integration checks.
- `tests/simulation`: seeded accuracy checks against simulator truth.
- `tests/end_to_end`: lifecycle and recovery tests.
- `tests/acceptance`: `test_atNN_*` maps to HB §21 AT-01…AT-20.
- `tests/support/`: test doubles (`fakes.py`) and HB §17 house seeding (`hb17_seed.py`, which seeds through `VersionService` propose/promote). Engine code must never import it (`test_runtime_fakes.py` enforces this). `realtime/harness.EngineHarness` replays scenarios against a real `EngineCore`.
- `tests/conftest.py` provides `tmp_db_path`, `sample_topology`, `fast_scenario`, `household_scenario` and `lab_house` (from `tests/fixtures/labhouse.py`), and puts the repo root and `engine/` on `sys.path`.
- A bug fix comes with a regression test that fails before the fix. Never weaken an assertion.

## Versioning and release

Five version fields must agree: `pyproject.toml`, `custom_components/wattmosaic/manifest.json`, `const.VERSION`, `addon/config.yaml`, and a `## vX.Y.Z` section in `RELEASE_NOTES.md`. `check_release.py` enforces this.

- Every push to `main` publishes a dev prerelease (`.github/workflows/release.yaml`). If the committed version is already tagged, CI bumps `X.Y.Z-dev.N` (`tools/dev/bump_version.py --next`), writes the notes itself and commits `Release … [wm-auto-version]`. Don't bump by hand for routine work.
- To move the core version (e.g. to `0.4.0-dev.1`), set all five fields yourself **and** add its `RELEASE_NOTES.md` section. CI honours an untagged committed version only as-is, and `check_release.py` refuses an undocumented one.
- Use the dotted prerelease form `-dev.10`, never `-dev10`. The `stable` branch carries plain `X.Y.Z` and is released by a human.

## Docs and decisions

- The 3.0 spec is `docs/specs/realtime-topology-disaggregation/`:
  - `handbook.md`: normative, cited as HB §n.
  - `design.md`.
  - `decisions.md`: DEC-nn.
  - `architecture.md` and `interfaces.md`.
  - `TODO.md`: known post-cutover limitations.
  - `verification-report.md`.
- Where they conflict, this spec supersedes `docs/specs/load-disaggregation/`.
- ADRs (ADR-0001…0041) are in `docs/decisions/README.md`. Cite the relevant ADR, DEC or HB section in commits that touch topology or attribution semantics.
- `PROJECT_STATUS.md` and `RELEASE_NOTES.md` are append-only. Document new configuration in `docs/configuration.md`.
- Commits follow Conventional Commits with a scope, e.g. `fix(gateway): …`.
