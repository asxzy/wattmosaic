# Implementation plan — real-time, topology-aware disaggregation (contract 3.0)

This plan turns `handbook.md` (HB) into 69 tasks across 7 waves. It is designed for **parallel coding agents**: within a wave, tasks own disjoint files, and the handoffs between them are typed and defined up front (`interfaces.md`). That lets each task build against fakes instead of waiting for its neighbour.

---

## 0. Start here (orchestrator)

```bash
# 0. Decisions: every ★ row in decisions.md must be APPROVED before Wave 0 merges.
#    Non-★ rows proceed on their default and may be revised later.

# 1. Integration branch (DEC-01). Never land this work on main: every push to main publishes a release.
git fetch origin && git switch -c feat/realtime-v3 origin/main
git push -u origin feat/realtime-v3

# 2. Baseline must be green before any task starts.
pip install -e ".[dev]"
python tools/dev/verify.py          # record the pass count in tasks/progress.md

# 3. Dispatch Wave 0 (8 tasks, all parallel).
```

**Builder protocol (every task):**
1. Branch from the current `feat/realtime-v3` tip: `git switch -c rt/<TASK-ID> origin/feat/realtime-v3`.
2. Read `README.md`, the task file, the `interfaces.md` sections it cites, and the `analysis/` report it cites.
3. Edit **only** files listed under *Owns*. If a change outside them is genuinely required, stop and record a `BLOCKED` note in `tasks/progress.md` naming the file and its owner. Do not edit it.
4. Write the task's named tests first. They must fail on the unmodified tree, unless the task is marked *no behaviour change*.
5. Run `ruff check engine tools tests`, the task's tests, and the full `pytest -q`. A wave-0 task must also show byte-identical outputs (see its acceptance).
6. Open a PR into `feat/realtime-v3` titled `<TASK-ID>: <title>`. Set your row to `IN_REVIEW`. Only the orchestrator sets `VERIFIED`, after running the named checks itself.

**Merge rule:** the orchestrator merges PRs within a wave in any order. All PRs are rebased on the integration tip. If two PRs in the same wave conflict, that is an ownership bug in this plan: fix the plan, don't hand-merge silently.

---

## 1. Why there is a Wave 0

Six files concentrate the work, and each would be edited by 3–6 tasks:

| Hot file | Lines | Touched by areas |
|---|---|---|
| `engine/wattmosaic_engine/contracts.py` | 3 155 | all |
| `engine/wattmosaic_engine/main.py` | 3 380 | pipeline, domain, topology, matrix, models |
| `engine/wattmosaic_engine/disaggregation/engine.py` | 2 723 | inference, pipeline, topology |
| `engine/wattmosaic_engine/storage/database.py` + `migrations.py` | 1 564 + ~830 | all |
| `engine/wattmosaic_engine/api/server.py` + `facade.py` | 1 150 + 600 | matrix, topology, domain |
| `addon/ui/app.js` | 5 490 | matrix, views |

Wave 0 splits each of these into modules **with no behaviour change**. Later tasks then own whole small files. Package facades keep re-exporting old import paths, so the ~66 test modules that do `from wattmosaic_engine.contracts import X` keep working.

---

## 2. Wave DAG

```
W0 Seams ─────────────────────────────────────────────────────────────────────
  X00 ADRs+tracker   X01 contracts pkg   X02 engine.py split   X03 main/integration split
  X04 api routes split   X05 storage pkg+backup   X06 panel ES modules   X07 simulator+HB§17 house
        │                     │                                     │
W1 Contracts & runtime interfaces (after X01; K07 also after X07) ────────────
  K00 foundation ─┬─ K01 entities   K02 measurement/events   K03 topology/mapping-edges/residual
                  ├─ K04 inference   K05 matrix   K06 power models   K08 runtime interfaces
                  └─ K07 acceptance harness (20 xfail-strict files) ◄── K01..K06, K08, X07
        │
W2 Storage (after X05 + its K task) ─────────────────────────────────────────
  S13 entities  S14 topology  S15 mapping+cells  S16 samples/evidence/groups  S17 results/snapshots
  S18 power models  S19 residual/unknown profiles
        │
W3 Engines (parallel; build against K08 fakes) ───────────────────────────────
  Pipeline: P1 ingest → P2 reader state → P3 detector → P4 windows → P5 groups
  Topology: T1 typed graph → T2 operator H(t) → T3 per-reader residual;  T4 topology service
  Inference: I1 candidates  I2 temporal/evidence  I3 joint solver  I4 identifiability (+250 W test)
  Matrix: M1 projection     Models: L1 codec/registry → L2 fixed/adjustable, L3 dynamic/kernel/components
  Domain: D1 version service  D2 endpoints+plugs  D3 null-semantics sweep     UI: U1 matrix UI (fixtures)
        │
W4 Composition ───────────────────────────────────────────────────────────────
  I5 estimates/status/confidence → I6 match lifecycle+records → I7 infer_group/infer_steady
  T5 topology inference  T6 unknown profiles  T7 self-healing
  M2 matrix store/updater → M3 matrix API     L4 intake gate → L5 incremental/promotion → L6 worker
  D4 snapshot+provenance  H1 HA entities  H2 HA setup flow  U2 other views  P6 three-loop runtime
        │
W5 Cutover ───────────────────────────────────────────────────────────────────
  Z2 legacy engine path removal → Z1 contract 3.0.0 + legacy contract/API removal
  L7 reconciliation/replay   D5 retention   X09 docs
        │
W6 Verification ──────────────────────────────────────────────────────────────
  V1 acceptance un-xfail + HB§17 e2e + accuracy re-baseline   V2 perf/real-data/migration rehearsal
  (L8 active-learning suggestions: optional, after V1)
```

Within a wave, tasks start as soon as their listed dependencies are `VERIFIED`. The wave label is a planning aid, not a barrier.

---

## 3. Task index

Sizes: S ≤ 1 day, M 2–3 days, L 4–6 days (one agent).

| ID | Title | Wave | Depends on | Size | Area report |
|---|---|---|---|---|---|
| X00 | ADRs 0035–0041, old-spec supersession note, tracker baseline | 0 | ★ decisions | S | all |
| X01 | Split `contracts.py` into a package (no behaviour change) | 0 | — | M | A |
| X02 | Split `disaggregation/engine.py` (no behaviour change) | 0 | — | M | D |
| X03 | Split `main.py` EngineCore and `integration.py` (no behaviour change) | 0 | — | L | B |
| X04 | Split `api/server.py` routes and `facade.py` protocols (no behaviour change) | 0 | — | M | E |
| X05 | Storage package: repositories, per-version migrations, pre-migration backup | 0 | — | L | A |
| X06 | Panel as ES modules + gateway/check_panel/test harness port | 0 | — | M | E |
| X07 | Simulator extensions + HB §17 reference house + replay driver skeleton | 0 | — | M | B |
| K00 | Foundation contracts: version refs, evidence refs, provenance, status enums, null policy | 1 | X01 | M | A |
| K01 | Entity contracts: Physical Device, Load Component, HA Entity/Link, Measurement Endpoint | 1 | K00 | M | A |
| K02 | Measurement and event contracts | 1 | K00 | M | B |
| K03 | Topology edge, version, mapping edge, configuration event, residual and unknown-profile contracts | 1 | K00 | M | C |
| K04 | Inference contracts: candidates, matches, estimates, groups, records, snapshot | 1 | K00 | M | D |
| K05 | Matrix contracts: MappingCell v3, column/row trees, drill-down payloads | 1 | K00 | M | E |
| K06 | Power-model contracts: spec union, versions, prediction, kernel, observation | 1 | K00 | M | F |
| K07 | Acceptance harness: 20 HB §21 tests as xfail-strict + HB §17 fixture | 1 | K01–K06, K08, X07 | M | all |
| K08 | Runtime interface types and fakes (`runtime/interfaces.py`, `runtime/fakes.py`) | 1 | K00 | M | all |
| S13 | Migration v013: endpoints, devices, components, HA entities and links (backfill, ids preserved) | 2 | X05, K01 | L | A |
| S14 | Migration v014: topology edges, versions, configuration events (backfill from pointers) | 2 | X05, K03, S13 | M | C |
| S15 | Migration v015: mapping edges/versions and matrix cells/versions | 2 | X05, K03, K05, S13 | M | E |
| S16 | Migration v016: reader samples v2, event evidence, event groups, event inference records | 2 | X05, K02, K04 | M | B |
| S17 | Migration v017: nullable inference results, snapshots, group estimates, legacy flags | 2 | X05, K04 | M | A |
| S18 | Migration v018: power models v2, model events, learning observations, legacy flag | 2 | X05, K06, S13 | M | F |
| S19 | Migration v019: per-reader residual series, unknown-load profiles | 2 | X05, K03 | S | C |
| P1 | Canonical ingestion: ReaderMeasurement + HAEvent, polarity once, quarantine, candidate ring | 3 | K02, S16, X03 | M | B |
| P2 | ReaderStateStore: one ring per reader, held/stale/missing, rolling baseline and noise | 3 | P1 | L | B |
| P3 | Streaming change detector with identity, hysteresis, CUSUM | 3 | P2 | M | B |
| P4 | Event window collector and immutable event evidence | 3 | P3, S16 | M | B |
| P5 | Event group formation and late-arrival revision | 3 | P4, P1 | L | B |
| T1 | Typed topology graph built from edges filtered by lifecycle | 3 | K03, S14, K08 | L | C |
| T2 | Observation operator H(T, M, t) with physics-justified β and time-scoped factors | 3 | T1, K08 | L | C |
| T3 | Per-reader residual engine and learned unknown-load prior | 3 | T2, S19, K08 | M | C |
| T4 | Topology service: declared edges, per-edge confirm/reject, candidate versions, API | 3 | S14, D1, X04 | M | C |
| I1 | Candidate generation and topology-aware filtering | 3 | K04, K08 | M | D |
| I2 | Temporal response matching and evidence A–H | 3 | K04, K06, K08 | M | D |
| I3 | Joint delta and absolute solver: robust, signed, covariance-aware, unknown columns | 3 | K08, X02 | L | D |
| I4 | Identifiability, device groups and justified bounds (250 W test first) | 3 | K04, K08 | L | D |
| M1 | Matrix projection core: cells, columns tree, rows tree, special rows | 3 | K05, K08 | L | E |
| L1 | Power-model codec, registry v2, append-only version lifecycle | 3 | K06, S18 | L | F |
| L2 | Fixed and Adjustable model classes on delta basis | 3 | L1 | M | F |
| L3 | Dynamic, Response-Kernel and Component-sum model classes | 3 | L1 | L | F |
| D1 | Version lifecycle service: topology/mapping/model triple, as-of queries, change workflow states | 3 | S13, S14, S15 | M | A |
| D2 | Endpoint discovery incl. smart plugs; stop minting devices per CT; entity links | 3 | K01, S13 | M | A |
| D3 | Null-semantics and input-quality sweep | 3 | K04, S17, X03 | S | B |
| U1 | Matrix UI against K05 fixtures | 3 | X06, K05 | L | E |
| I5 | Estimate assembly, status, confidence, per-estimate provenance | 4 | I3, I4 | M | D |
| I6 | Match lifecycle and versioned event inference records | 4 | I1, I5, S16 | M | D |
| I7 | `infer_group` / `infer_steady` orchestration | 4 | I1, I2, I3, I4, I5, I6, T2, T3 | M | D |
| T5 | Topology inference engine: asymmetric delta + absolute evidence ledger | 4 | T1, T3, T4, P4 | L | C |
| T6 | Unknown Load Profile engine | 4 | T3, P5, S19 | M | C |
| T7 | Configuration change and self-healing workflow (HB §14) | 4 | T4, T5, D1, M2 | L | C |
| M2 | Matrix store, event-driven updater, deterministic reconciler | 4 | M1, S15, D1 | L | E |
| M3 | Matrix API v2 and drill-down routes | 4 | M2, X04 | M | E |
| L4 | Learning observation intake and quality gate v2 | 4 | L1, S18, K04 | M | F |
| L5 | Per-class incremental learners and promotion v2 | 4 | L2, L3, L4 | M | F |
| L6 | Background learner worker off the event loop | 4 | L5, X03 | S | F |
| D4 | Inference snapshot assembly and provenance resolver | 4 | S17, I5, D1 | M | A |
| H1 | HA integration v3: client, null semantics, status/identifiability, group sensors | 4 | K04, D4 | M | E |
| H2 | HA setup flow v3 (folds in minimal-ha-setup): endpoints, user parents, plug scope | 4 | T4, D2 | M | A, C |
| U2 | Overview, device and topology views aligned to v3 | 4 | U1, M3 | M | E |
| P6 | Three-loop runtime in EngineCore (fast, event, background) | 4 | P5, I7, L6, M2, D4 | L | B |
| Z2 | Remove legacy engine paths (5 s full solve, greedy step, placeholders, old nesting/residual/mapping) | 5 | all W4 | M | all |
| Z1 | Contract 3.0.0: remove legacy contracts/fields/routes, bump + mirror, schemas | 5 | Z2 | M | A |
| L7 | Historical reconciliation and versioned replay | 5 | I7, L1, S17 | L | F |
| D5 | Retention, tiers and storage-health for new tables | 5 | S16, P4 | S | A |
| X09 | Product docs to 3.0 (architecture, data flow, contracts, config, user guide, README) | 5 | Z1 | M | all |
| V1 | Acceptance: un-xfail all 20, HB §17 end-to-end through HTTP and panel model, accuracy re-baseline | 6 | Z1, L7 | M | all |
| V2 | Performance, real-data replay, migration rehearsal on a DB copy, deployment report | 6 | V1 | M | all |
| L8 | Active-learning suggestions (optional, DEC-36) | 6 | V1 | S | F |

Task files live in `tasks/<ID>.md`. Status lives in `tasks/progress.md`.

---

## 4. Ownership rules

1. **One owner per file per wave.** The *Owns* list in each task file is authoritative. Wave 0 creates the files that later waves own.
2. **New files are owned by the task that creates them**, and only that task edits them until it is `VERIFIED`. After that, a later task may take ownership only if its own *Owns* list names the file.
3. **Package facades** (`contracts/__init__.py`, `storage/repositories/__init__.py`, `api/routes/__init__.py`, `core/__init__.py`, `runtime/__init__.py`, `addon/ui/main.js`) are written once by their Wave-0 task, using auto-discovery or pre-declared star-imports. They must not need edits in later waves. If one does need an edit, the change goes through the orchestrator.
4. **Generated schemas.** `data/schemas/<model>.json` is owned by the task that owns the model. `tools/dev/export_schemas.py` discovers exports from `SCHEMA_EXPORTS` lists in each contracts submodule (X01), so no task edits the exporter list.
5. **Tests.** A task owns the new test files it names. It may edit existing test files only if they are listed. Existing tests that break because of *another* task's intended change are that other task's responsibility.
6. **`CONTRACT_VERSION`** (engine `contracts/foundation.py` after X01, mirror `custom_components/wattmosaic/const.py:31`) is touched **only by Z1**. Until then new contracts are additive and the version stays `2.4.0`, so the integration keeps working on the branch.
7. **`tasks/progress.md`.** Each builder edits only its own row. The orchestrator edits everything else.

---

## 5. Reserved migration numbers

Migrations become one module per version under `storage/migrations/` (X05). Numbers are reserved here so parallel tasks never collide.

| Version | Owner | Content |
|---|---|---|
| 1–11 | existing | moved verbatim into `storage/migrations/legacy.py` by X05 |
| 12 | **reserved, never used** | `PAYLOAD_ENCODING_VERSION = 12` is a marker (`storage/migrations.py:791-800`) |
| 13 | S13 | endpoints, endpoint config revisions, physical devices, load components, HA entities, HA entity links |
| 14 | S14 | topology edges v2, topology versions v2, configuration events |
| 15 | S15 | mapping edges, mapping versions, mapping cells, mapping cell versions |
| 16 | S16 | reader samples v2 columns, event evidence, event groups, event group members, event inference records |
| 17 | S17 | inference results rebuild (nullable), inference snapshots, device group estimates |
| 18 | S18 | power model versions v2, power model events, learning observations |
| 19 | S19 | reader residual series, unknown-load profiles |
| 20 | L7 | derived runs and derived estimates (versioned replay namespace) |
| 21–24 | spare | assigned by the orchestrator if a W3/W4 task needs a table |
| 25 | Z1 | mark legacy tables read-only (rename `*_legacy`); no drops (DEC-02) |

Each migration module declares the storage tier of every table it creates (`TIERS = {...}`). The tier-completeness test therefore passes without editing `storage/tiers.py` (X05).

---

## 6. Verification per wave

| Wave | Gate (orchestrator runs it) |
|---|---|
| 0 | Full suite green. `python tools/dev/export_schemas.py && git diff --exit-code data/schemas` (byte-identical). `tests/simulation/test_replay_versions.py` fingerprint unchanged. `python tools/dev/check_panel.py`. `bash scripts/validate_addon.sh --no-build`. `/healthz` smoke via `scripts/smoke_test.sh` against a local engine. |
| 1 | Full suite green. New schemas regenerated and committed. K07 acceptance files collected, **all 20 xfail-strict** (an unexpected pass fails the run). The null-policy reflective test is green. |
| 2 | `tests/unit/test_migration_matrix.py` extended: upgrade from schemas 9, 10 and 11 fixtures, run twice (idempotent), and inject a failure mid-apply (rollback leaves `user_version` unchanged). Backup file created. No legacy row lost (counts). |
| 3 | Each task's unit tests + the full suite. Every module is importable without the runtime (pure functions, fakes from K08). |
| 4 | The full suite, plus every acceptance test whose owner task is in W4 flipped to passing. `test_healthz_latency_under_load` (P6/L6) green. |
| 5 | `python tools/dev/verify.py` green. Contract 3.0.0 mirrored. No legacy symbol left: the Z1 static gate. |
| 6 | All 20 acceptance tests pass with no xfail markers left. The simulator accuracy report is regenerated with `forced_attribution_count == 0`. The V2 report has been written. |

---

## 7. Coordination points (agree before code)

These interfaces are fixed in `interfaces.md` and implemented as dataclasses or Protocols plus fakes in K08. A task may **extend** them only through the orchestrator.

| Handoff | Producer → consumer | `interfaces.md` § |
|---|---|---|
| `EventGroup` / `ReaderDelta` / `SubEvent` | P5 → I1, I2, I3, I6, L4, T5, T6 | §3 |
| `ObservationOperator` | T2 → I1, I3, I4, M1 | §4 |
| `UnknownPrior` | T3 → I3 | §4 |
| `PowerPrediction`, `DeltaPrior`, `expected_window` | L1/L2/L3 → I1, I2, I3, M1 | §5 |
| `JointSolution` | I3 → I4, I5 | §6 |
| `JointInferenceResult` | I7 → M2, L4, D4, P6 | §6 |
| `VersionService` | D1 → T4, T7, M2, I7, D4, L7 | §7 |
| `MatrixUpdater.apply` | M2 ← P6 | §8 |
| HTTP v3 routes | M3, T4, D2, D4 → U1, U2, H1, H2 | §9 |

---

## 8. Risks the plan accepts

- **The integration branch diverges from `main`.** Mitigation: the orchestrator rebases `feat/realtime-v3` onto `main` weekly. Any `main` commit touching a hot file is ported by the owning task of the current wave. Concurrent `main` work on matrix, mapping or minimal-ha-setup should pause or target the branch (DEC-13).
- **Accuracy numbers move.** Group estimates, kept parent rows (DEC-12) and λ_u (DEC-23) change coverage and residual shares. V1 re-baselines deliberately. A reduced "attributed %" is acceptable when it comes with `forced_attribution_count == 0` and an increased group-identified share.
- **HA history gaps.** Unknown becomes `None` instead of 0 W (DEC-25). This is intended and is called out in the release notes (X09).
