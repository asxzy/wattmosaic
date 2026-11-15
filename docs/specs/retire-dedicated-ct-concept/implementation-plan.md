# Implementation plan — Retire the "dedicated device" CT concept

## Start here

Workspace: `/Users/asxzy/.local/share/opencode/worktree/d661cf/proud-garden`

```bash
# 1. Confirm the tree is clean and on the intended branch
git status --short && git branch --show-current

# 2. Confirm the baseline is green before any edit
python -m pytest -q                    # or: python tools/dev/verify.py

# 3. Read the spec set
cat docs/specs/retire-dedicated-ct-concept/README.md
```

Create a feature branch, then execute **Wave 1** (the migration seam) first. Waves are ordered so that every data-preserving move lands before the enum narrowing that would otherwise brick startup.

Verification command after each wave:

```bash
python tools/dev/verify.py
```

`tools/dev/verify.py:107-121` runs lint, unit, integration, simulation, e2e, schemas, manifest, release and docs gates. A wave is not done until it passes.

---

## Dependency DAG

```
W1 Migration seam (engine v9 + HA entry migration)
   │
   ├──► W2 Contract narrowing (CTType / MonitoringLevel / dedicated_ct_ids)
   │        │
   │        ├──► W3 Engine direct-path removal (solver, config, topology, nesting)
   │        │        │
   │        │        ├──► W4 API payload cleanup (main.py, facade.py)
   │        │        │        │
   │        │        │        └──► W6 UI wording (addon/ui)
   │        │        │
   │        │        └──► W5 Schemas, examples, contract version, docs
   │        │
   │        └──► W7 Integration cleanup (const/config_flow/flow_logic/strings)
   │
   └──► W8 Tests (unit/integration/simulation/e2e) — interleaved per wave, see §Testing
```

Hard constraints:
- **W1 before W2.** Removing enum members while stored rows hold the old value makes `load_topology()` raise (`storage/database.py:288-324`, validation at `:294`). There is no tolerant path: `_enum_value` (`database.py:49-51`) is only used on write paths for inference rows, never for channel reads.
- **W1 before W7.** The HA config-entry migration must exist before the role disappears from `CT_ROLES`.
- **Import-time failures are only half the story.** Several references live inside function bodies and would raise at runtime: `storage/database.py:236`/`:317` (every topology write/read), `electrical/panel_tree.py:659` (every tree render), `device_links.py:166-169`, `auto_placement.py:210-223`. Wave 2 must repoint these so the engine *runs*, not merely imports.
- **W2 before W3/W4/W7** because those waves delete or reword the same symbols.

---

## Wave 1 — Migration seam

**Goal:** make existing data survivable, before any code assumes the new vocabulary.

| Task | Objective | Files owned |
| --- | --- | ---|
| task-01 | Engine schema v9: demote `ct_type`, move ids, strip `projected_ct_ids`; **plus** migrate `device_configurations.payload_json` and historical `inference_results.monitoring_level` | `storage/migrations.py` (only) |
| task-02 | HA config-entry migration: rewrite stored `ct_assignments` roles; add the temporary `normalise_assignment` alias | `custom_components/wattmosaic/__init__.py`, `config_flow.py` (VERSION only), `flow_logic.py` (`:587-591` only) |
| task-03 | Tests for both migrations | `tests/unit/test_migrations.py` (new) |

Exit criteria: a database written at schema 8 with `dedicated_device` rows opens, migrates, and yields a topology where every device has ≥1 attribution row; stored configurations and historical inference rows survive; a legacy HA config entry upgrades without a replay error.

`storage/database.py` is **deliberately not owned by task-01**: its `DeviceTopology` references are removed in Wave 2 (task-05b). Editing them in Wave 1 would be contract work before the migration seam is proven.

---

## Wave 2 — Contract narrowing and reference cleanup

**Goal:** shrink the public vocabulary and repoint every consumer so the engine *runs*, not merely imports.

| Task | Objective | Files owned |
| --- | --- | --- |
| task-04 | Remove `CTType.DEDICATED_DEVICE`, `MonitoringLevel.DEDICATED`, `DeviceTopology.dedicated_ct_ids`; bump `CONTRACT_VERSION` (engine + integration mirror) | `contracts.py`, `custom_components/wattmosaic/const.py` |
| task-05a | Repoint all engine references in the modules that own the direct path; **delete `dedicated_channel_for` outright together with its callers** | `electrical/topology.py`, `disaggregation/configuration.py`, `disaggregation/engine.py`, `main.py`, `api/facade.py`, `integration.py`, `quality/engine.py`, `electrical/nesting.py`, `electrical/direct.py`, `electrical/__init__.py` |
| task-05b | Repoint the remaining consumers: storage serialisation, panel tree, device links, auto-placement | `storage/database.py`, `electrical/panel_tree.py`, `device_links.py`, `disaggregation/auto_placement.py` |

Exit criteria: `import wattmosaic_engine` succeeds; `load_topology`, `save_topology` and the panel-tree builder run against a migrated database; no `dedicated_channel_for` symbol remains; `ruff check engine` clean.

---

## Wave 3 — Engine direct-path removal

**Goal:** delete Level-1 measurement coherently.

| Task | Objective | Files owned |
| --- | --- | --- |
| task-06 | Delete `resolve_direct`/`DirectReading` and the Level-1 branch (`engine.py:1510-1588`) | `electrical/direct.py`, `electrical/__init__.py`, `disaggregation/engine.py` |
| task-07 | Simplify `derive_monitoring`, remove `allows_direct_measurement` | `disaggregation/configuration.py` |
| task-08 | Remove `_direct_load_series`; reword the `no_dedicated_device_ct` reason, the tier-reason strings and `integration.py`'s `no_dedicated_ct` availability flag | `main.py`, `integration.py`, `disaggregation/tiers.py` |

Depends on W2. Intra-wave ordering: **run task-06 first**, because task-07 (`configuration.py:186`, `:208`) and task-08 (`integration.py:546`) still call `dedicated_channel_for` and must not be left dangling by its deletion. Exit criteria: inference produces no `Source.DIRECT_MEASURED`; a solo-row device still reports a whole-row fit; shared rows use the ADR-0026 step rule; no user-visible engine string contains "dedicated".

---

## Wave 4 — API payload cleanup

| Task | Objective | Files owned |
| --- | --- | --- |
| task-09 | Drop `dedicated_ct_ids` and the stub key; stop emitting `monitoring = "dedicated"` | `main.py`, `api/facade.py` |

Depends on W2–W3. Exit criteria: `GET /devices` has no dedicated key; response contracts match regenerated schemas.

---

## Wave 5 — Schemas, examples, docs

| Task | Objective | Files owned |
| --- | --- | --- |
| task-10 | Regenerate schemas, update examples, bump index version | `data/schemas/**`, `data/examples/**` |
| task-11 | Update contract and product docs; add ADR-0027 and supersede ADR-0025/0026 | `docs/**`, `docs/decisions/README.md` |

Depends on W2. Exit criteria: `git diff --exit-code data/schemas` clean after regeneration; docs gate green.

---

## Wave 6 — App UI

| Task | Objective | Files owned |
| --- | --- | --- |
| task-12 | Replace every dedicated string and dependent construct | `addon/ui/app.js`, `addon/ui/app.css`, `tools/dev/check_panel.py` |

Depends on W4 (payloads). Exit criteria: no `/dedicat/i` in `addon/ui/**`; the static gate added in task-12 fails on regression.

---

## Wave 7 — Home Assistant integration

| Task | Objective | Files owned |
| --- | --- | --- |
| task-13 | Remove the role option, monitoring level, dead mapping step, and re-express `pair_crosses_devices` | `const.py`, `config_flow.py`, `flow_logic.py`, `strings.json`, `translations/en.json` |

Depends on W1 (entry migration) and W2. Exit criteria: options flow offers only main/branch/unknown; strings files match byte-for-byte; pairing cross-device guard still fires for non-dedicated pairs.

---

## Wave 8 — Test suite alignment

| Task | Objective | Files owned |
| --- | --- | --- |
| task-14 | Update pinning assertions across unit/integration/simulation/e2e | `tests/**` |

Runs continuously: each wave updates the tests that touch its files. task-14 is the final sweep for stragglers and for the new AC-mapped tests. Exit criteria: `pytest -q` green; no test asserts a removed value.

---

## File ownership matrix

Each file has exactly one owner. Where a later task edits a file an earlier task touched, that is a *sequential* hand-off, not shared ownership.

| File | Owner | Notes |
| --- | --- | --- |
| `storage/migrations.py` | task-01 | only this file in Wave 1 |
| `custom_components/wattmosaic/__init__.py`, `config_flow.py` (VERSION line only) | task-02 | |
| `custom_components/wattmosaic/flow_logic.py` (alias at `:587-591` only) | task-02 | the rest of `flow_logic.py` is task-13; different lines, no conflict |
| `tests/unit/test_migrations.py` | task-03 | new file; the AC-mapped tests from other tasks are also written here |
| `contracts.py` | task-04 | |
| `custom_components/wattmosaic/const.py` (contract version only) | task-04 | the rest of `const.py` is task-13; different lines, no conflict |
| `electrical/topology.py`, `disaggregation/configuration.py`, `disaggregation/engine.py`, `main.py`, `api/facade.py`, `integration.py`, `quality/engine.py`, `electrical/nesting.py`, `electrical/direct.py`, `electrical/__init__.py` | task-05a | task-06/07/08/09 then edit the same files sequentially; each lists its own steps |
| `storage/database.py` | task-05b | Wave 2 — never Wave 1 |
| `electrical/panel_tree.py`, `device_links.py`, `disaggregation/auto_placement.py` | task-05b | runtime surfaces missed by an import-only check |
| `data/schemas/**`, `data/examples/**` | task-10 | |
| `docs/**`, `PROJECT_STATUS.md`, `RELEASE_NOTES.md` | task-11 | |
| `addon/ui/app.js`, `addon/ui/app.css`, `tools/dev/check_panel.py` | task-12 | |
| `const.py`, `config_flow.py`, `flow_logic.py`, `strings.json`, `translations/en.json` | task-13 | `config_flow.py` after task-02's VERSION bump |
| `tests/**` (all other files) | task-14 | sweeps and AC-mapped tests |

Overlaps are sequential: task-05a repoints `configuration.py`; task-07 then simplifies the same functions. Each task names its own line ranges and must not restructure what the previous one established.

`tools/simulator/topology.py` and `tools/dev/make_reference_house.py` are **not** owned by any task by default, but the "survives unchanged" claim was wrong for the first of them. `tools/simulator/topology.py` builds contract `CTChannel`s at module scope using the removed `CTType.DEDICATED_DEVICE` member, so it failed at import and caused all 12 test-collection errors. It was repointed to `CTType.BRANCH` in Wave 2 (its simulator metadata `d.dedicated_ct` is untouched). Further edits to `tools/simulator/**` are permitted if a test requires them.

---

## Testing strategy

**Test-first requirement:** for each wave, write or update the failing test before the implementation change.

- **Migration tests (W1):** build a schema-8 database **with raw `sqlite3`** (constructing `Database` runs migrations in `__init__`, so it cannot produce a pre-migration DB). Then: assert load + migrate + replay; assert stored configurations and historical inference rows survive; assert idempotency by invoking the v9 `apply=` callback directly twice on one connection (the version guard at `migrations.py:484-485` makes a second full run a no-op, so a naive "run twice" test cannot detect a double-add).
- **Contract tests (W2):** assert `PUT /topology` with `ct_type = "dedicated_device"` returns 400; assert `DeviceTopology(dedicated_ct_ids=[...])` is rejected; assert `CONTRACT_VERSION == "2.0.0"`; assert the engine's header constant (`custom_components/wattmosaic/const.py:31`) matches.
- **Solver tests (W3):** a fixture with a single-device branch row still produces a positive reading via `_is_solo_row`; a shared row still uses the ADR-0026 step rule; no result has `source = direct_measured`.
- **Integration tests (W7):** legacy entry migration; options flow renders without the role; the cross-device pairing guard fires for two different non-null `device_id`s regardless of role.
- **UI tests (W6):** static scan of `addon/ui/app.js` for `/dedicat/i`, with an explicit allowlist exemption for the intentional `dedicated_device` alias key required to prevent the `titleCase` fallback from printing "Dedicated Device". No JS runner exists in the repo, so AC-7 is a static assertion plus a Python-side render smoke check using the engine payload shape.
- **Full gate:** `python tools/dev/verify.py` after every wave.

**Critical path:** W1 → W2 → W3. A failure in the migration seam blocks everything and is the highest-risk item because it operates on real user data.

**Risk areas:**
1. Migration ordering (mitigated by running migrations in `__init__` before loads).
2. `projected_ct_ids` retraction removing demoted ids (task-01 handles explicitly; see interfaces.md §6.1 for the exact rule).
3. Stored configurations dropped wholesale on validation failure (`database.py:371-374`) — task-01 migrates their payloads first.
4. Historical `inference_results.monitoring_level` failing `InferenceResult` validation (`contracts.py:818`, read at `main.py:462`) — task-01 rewrites those rows.
5. HA replay fighting the engine (task-02 lands in W1).
6. `titleCase` producing "Dedicated Device" from a raw legacy value (task-12 adds an explicit alias).
