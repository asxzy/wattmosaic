# Design — Retire the "dedicated device" CT concept

## 1. Problem statement

The App still displays "Dedicated Device" / "Dedicated CT" in five places. Investigation established:

- Discovery has emitted only `main`/`branch` since ADR-0025 (`ingestion/discovery.py:392-414`).
- The word survives because (a) the HA integration still offers a `dedicated_device` role, (b) stored topologies hold `ct_type == "dedicated_device"` rows that `GET /topology` returns verbatim, and (c) UI copy was never removed.
- Browser cache is ruled out (assets served `no-store`, `addon/gateway.py:410-421`).
- No test pins the literal label strings.

The user's decision: **remove the concept entirely, including the Level-1 direct-measurement capability it gates.**

## 2. Goal

After this change, "dedicated" no longer exists anywhere in the product vocabulary — engine enums, contracts, schemas, API payloads, HA options flow, App UI, docs — and no stored data requires it.

## 3. Non-goals

- Reintroducing a replacement role or boolean for single-device channels. The user explicitly chose full removal over a neutral-capability rewrite.
- Changing discovery behaviour (already correct).
- Touching the live install during this workstream; the migration must be code-complete and unit-tested, with a separately authorised apply step.

## 4. Functional design

### 4.1 What happens to a channel that used to be `dedicated_device`

| Before | After |
| --- | --- |
| `channels[].ct_type = "dedicated_device"` | `channels[].ct_type = "branch"` |
| Device owns it via `dedicated_ct_ids` | Device owns it via `branch_ct_ids` |
| Direct measurement via `dedicated_channel_for` | Constrained inference; whole row credited only when the device is its sole occupant (`_is_solo_row`) |
| Result `source = direct_measured`, confidence floor 0.55 | Result `source = inferred`, graded by the normal tier rules |

A demoted channel keeps its `device_id` if one was set. This preserves tree placement (`panel_tree._device_ct_ids` unions `branch_ct_ids` and `channel.device_id`) and keeps the declaration reachable.

### 4.2 What the App shows instead

| Surface | Current | New |
| --- | --- | --- |
| Declared configuration → Monitoring row | `Dedicated CT` | Row removed. Monitoring level is always `derive_monitoring`; a stored non-`UNKNOWN` declaration wins (§4.4, interfaces.md §3.1), a stored `dedicated` value resolves to `UNKNOWN` and is then derived from the graph. |
| Placement card → *Dedicated CTs* | list of ct_ids | Row relabelled **`Monitored by`** and fed from `branch_ct_ids` only. |
| Topology → Circuits hint | `N feed · N branch · N dedicated` | `N feed · N branch · N unknown` |
| CT channels badge | `Dedicated Device` | `Branch CT` for any demoted/legacy row |
| Hierarchy circuit subtitle | `dedicated_device · L1` | `branch · L1` |
| HA role dropdown | main / branch / **Dedicated device** / unknown | main / branch / unknown |
| HA monitoring dropdown | dedicated / shared / home_total_only / unmonitored / unknown | shared / home_total_only / unmonitored / unknown |

Wording convention: use the codebase's existing phrases — "shared circuit", "Shared CT" (`app.js:226`), "Monitored by" (`app.js:1502`), "Direct measured" (`app.js:219`). Do not invent new terminology.

### 4.3 Measurement semantics after removal

Direct measurement is deleted, not replaced. Consequences that are intentional:

- A genuinely single-device CT still gets a plausible wattage through the constrained fit because `_is_solo_row` (`engine.py:2348-2358`) is role-independent and a solo row skips model upper bounds (`engine.py:1161`).
- The result grades as `Source.INFERRED` at tier 2/3, without the direct floor.
- An OFF device on a solo row can report `unexplained_load_on_device_circuit` and become `unknown` (`engine.py:1820-1853`). This is the honest answer, consistent with ADR-0026's "attribute the measured step".
- `Source.DIRECT_MEASURED` remains in the contract enum for historical rows (see §4.5) but is no longer produced.

### 4.4 Migration behaviour

A new schema migration (v11, renumbered above main's 9 and 10) runs inside `Database.__init__`, before any topology load. It performs, in one transaction:

1. `UPDATE ct_channels SET ct_type = 'branch' WHERE ct_type = 'dedicated_device';`
2. For each device row, move every id in `dedicated_ct_ids_json` into `branch_ct_ids_json` (union, deduplicated), then empty `dedicated_ct_ids_json`.
3. Strip demoted ids from `metadata_json`'s `projected_ct_ids` list so the next `project_configuration` does not retract them — unless a surviving device configuration still names them in `monitored_by_ct_ids` (interfaces.md §6.1).
4. Rewrite `device_configurations.payload_json` `"monitoring": "dedicated"` → `"shared"` so the row is not dropped wholesale (interfaces.md §6.1).
5. Rewrite historical `inference_results.monitoring_level = 'dedicated'` → `'shared'` so old rows validate.
6. Leave `ct_channels.device_id` untouched.
7. Do **not** insert a `topology_versions` row — the version counter is the AUTOINCREMENT rowid (`database.py:242-248`), and a migration should not fake a client-visible version bump.

A separate Home Assistant config-entry migration (`async_migrate_entry`, new `VERSION`) rewrites stored `ct_assignments` role `dedicated_device` → `branch` **before** the entry's first topology replay. Without this, `assignment_matches_channel` mismatches forever and `PUT /topology` returns 400.

Stored device configurations carrying `monitoring = "dedicated"` are **not** tolerated. The try/except at `configuration.py:345-350` handles topology *metadata*, but `load_device_configurations` (`database.py:371-374`) validates the whole `DeviceConfiguration` payload and silently drops the row on failure — `monitoring = "dedicated"` would be discarded, along with the device's whole configuration. That is why task-01 step D rewrites `device_configurations.payload_json` to `"shared"` before the enum narrows. After migration the metadata path still tolerates any stray value and resolves it via `derive_monitoring` (interfaces.md §3.2); a non-`UNKNOWN` declaration wins over the graph.

### 4.5 Historical rows

Historical `inference_results.source = "direct_measured"` rows remain readable: `row_to_inference_result` passes the raw string (`main.py:449`), and the `Source` contract member stays. Only *future* results stop producing it. Historical `monitoring_level = "dedicated"` rows are rewritten to `"shared"` by the v11 migration (§4.4) so they validate against the narrowed enum; they are not left to fail silently.

## 5. Acceptance criteria

Each criterion is testable.

| # | Criterion | Verification |
| --- | --- | --- |
| AC-1 | No product string contains "dedicated" (case-insensitive) in `addon/ui/**`, `custom_components/wattmosaic/strings.json`, `custom_components/wattmosaic/translations/en.json`, `data/schemas/**`, or the shipped docs. | `grep -ri dedicated` over those paths returns nothing; CI gate added. |
| AC-2 | A pre-change database (schema 10, with `dedicated_device` rows) opens, migrates and loads without error. | New test constructs an old-shape DB and asserts `load_topology()` succeeds. |
| AC-3 | After migration every device retains ≥1 attribution row. | Test asserts `graph.attribution_ct_ids(dev) != ()` for all devices in a fixture that previously had dedicated rows. |
| AC-4 | Migration is idempotent and does not double-add ids. | Invoke the v11 `apply=` callback **directly twice** on one connection; assert `branch_ct_ids` unchanged on the second run. A second full migration run is a no-op (`migrations.py:484-485` skips applied versions), so it cannot detect a double-add. |
| AC-5 | A HA config entry storing `role = "dedicated_device"` survives upgrade and replays without a 400. | Integration test with a legacy entry fixture. |
| AC-6 | No new topology write can introduce the role: `PUT /topology` with `ct_type = "dedicated_device"` is rejected. | Contract test asserting 400. |
| AC-7 | The App renders no "dedicated" text even on a topology containing legacy rows. | Static-string test over `addon/ui/app.js` plus a rendered smoke check. |
| AC-8 | `tools/dev/verify.py` passes: lint, unit, integration, simulation, e2e, schemas, manifest, release, docs. | CI run. |
| AC-9 | Schemas regenerated and committed; `git diff --exit-code data/schemas` clean. | CI job `.github/workflows/ci.yml:26-29`. |
| AC-10 | Contract version bumped and `docs/interface_contracts.md` updated per its own mandatory protocol (`:198-200`). | Docs gate. |
| AC-11 | ADR-0027 records the supersession of ADR-0025/0026 and the accepted loss of direct measurement. | Docs review. |

## 6. Risks

| Risk | Mitigation |
| --- | --- |
| ~21 live devices may lose `direct_measured` status and confidence floors | Accepted by the user; documented in ADR-0027 and `PROJECT_STATUS.md`. The ~21 figure is **uncorroborated** — `PROJECT_STATUS.md` records 21/0/22/26 across snapshots and a 2026-11-10 reset may have destroyed the data. The build agent must count real rows before any live apply. |
| Migration ordering bug bricks startup | v11 migration runs in `__init__` before any load; AC-2/AC-4 cover it. |
| HA replay fights the engine | Config-entry migration lands in the same wave; AC-5 covers it. |
| `projected_ct_ids` retraction removes demoted ids | Migration strips them; covered by AC-3. |
| Scope creep into unrelated tests | Task ownership is exclusive per file; test edits are grouped by wave. |
