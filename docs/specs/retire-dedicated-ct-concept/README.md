# Retire the "dedicated device" CT concept

**Status:** Approved · 2026-11-14 · Ready for the build agent
**Workspace:** `/Users/asxzy/.local/share/opencode/worktree/d661cf/proud-garden`
**Direction agreed with user:** Full removal of the concept, **including the Level-1 direct-measurement path** (option A from the design review). Supersedes ADR-0025 and ADR-0026.

## What this is

The product still says "dedicated" in five different places, backed by three distinct concepts:

| Surface | Literal | Source |
| --- | --- | --- |
| Device page → Declared configuration → *Monitoring* row | `Dedicated CT` | declared `monitoring = "dedicated"` |
| Device page → Placement → *Dedicated CTs* row | list of ct_ids | `DeviceTopology.dedicated_ct_ids` |
| Topology tab → Circuits stat hint | `N feed · N branch · N dedicated` | count of `ct_type == "dedicated_device"` |
| Topology tab → CT channels badge | `Dedicated Device` | raw `ct_type` |
| HA integration options flow | `Dedicated device` role option | stored `ct_assignments` |

Discovery has not emitted `dedicated_device` since ADR-0025; it survives only as a **user declaration** and as **persisted topology rows**. The user's instruction: the term is deprecated in full, including the measurement capability it gates.

## Why this is not a label change

`dedicated_device` is the *only* key to Level-1 direct measurement:

```
CTType.DEDICATED_DEVICE
  → TopologyGraph.dedicated_channel_for()   electrical/topology.py:333-349
  → resolve_direct()                        electrical/direct.py:78-100
  → EffectiveConfig.allows_direct_measurement   disaggregation/configuration.py:142-148
  → Level-1 branch                           disaggregation/engine.py:1510-1588
  → Source.DIRECT_MEASURED, confidence floor 0.55 (confidence.py:184-190)
```

Removing the role makes direct measurement structurally impossible. A single-device CT (hot tub, well pump) then falls through to constrained inference via `_is_solo_row` (`engine.py:2348-2358`) and reports `Source.INFERRED` at tier 2/3 with no confidence floor. **This is an accepted, deliberate consequence of the user's decision.**

The "~21 devices" figure is a **premise, not a verified count**: `PROJECT_STATUS.md` records 21/0/22/26 dedicated devices across successive snapshots, and a 2026-11-10 reset may have destroyed the underlying data. The build agent must count the real rows in the live database before any apply step, and must not treat 21 as an expected migration total.

## How to start

Read in this order:

1. `design.md` — functional behaviour and acceptance criteria
2. `architecture.md` — what is deleted, what replaces it, data flow
3. `interfaces.md` — contract, schema, payload and storage changes
4. `decisions.md` — ADR supersession and the reasoning
5. `implementation-plan.md` — DAG, waves, file ownership, testing
6. `tasks/task-01.md` … `tasks/task-14.md` — one file per task (`task-05a` and `task-05b` split Wave 2).

The implementation plan is executable by a fresh build session: it names the exact command to start with and the first wave.

## Non-negotiable constraints discovered during investigation

- **Migration must precede enum deletion.** `load_topology` validates `ct_type` through Pydantic with no fallback (`storage/database.py:292-311`). Deleting the enum member while any stored row holds `"dedicated_device"` makes every engine startup raise.
- **The migration must move ids too.** Leaving an id in `dedicated_ct_ids_json` after demoting its channel to `branch` produces validation problems (`electrical/topology.py:246-276`) and strips the device's attribution (`attribution_ct_ids` falls through to `most_specific_branch_group`, which reads `branch_ct_ids`).
- **HA config entries must migrate too.** There is no `async_migrate_entry` today. Stored `ct_assignments` that say `dedicated_device` mismatch the engine after removal and make `PUT /topology` return 400 forever (`flow_logic.py:1052-1072`, `:1226-1236`).
- **`projected_ct_ids` interaction.** `project_configuration` retracts previously projected ids before re-adding them (`configuration.py:727-736`). A demoted id listed there is removed again unless the migration strips it — except when a surviving device configuration still names it in `monitored_by_ct_ids`.
- **Bare-string comparisons hide from an enum grep.** `main.py:394` (`str(ch.ct_type) != "dedicated_device"`) and `disaggregation/engine.py:317` compare the literal without naming `CTType.DEDICATED_DEVICE`. Task-05a must grep for the literal; the task-14 static gate uses `/dedicat/i`.
- **References inside function bodies fail at call time, not import time.** Known: `storage/database.py:236`/`:317`, `panel_tree.py:659` (every tree render), `device_links.py:166-169`, `auto_placement.py:210-223`. A clean `import wattmosaic_engine` proves nothing.
- **Contract bump + CI.** `.github/workflows/ci.yml:26-29` regenerates schemas and hard-fails on drift. `CONTRACT_VERSION` is currently `1.5.0` (`contracts.py:31`) and is mirrored in the integration's own contract constant (`custom_components/wattmosaic/const.py:31`), which is sent as a header from `api_client.py:135,176`. Both must move to `2.0.0`.
- **UI `titleCase` trap.** Deleting the `STRINGS.dedicated_device` key without remapping makes the badge read "Dedicated Device" instead of "Dedicated CT" (`app.js:202-205`, `:233`). The one surviving `dedicated` token in `app.js` is the intentional alias key that renders `Branch CT`.

## Acceptance at a glance

- No string in the product UI, HA options flow, docs or API schema contains "dedicated". (One deliberate exception: the `STRINGS.dedicated_device` alias key in `app.js`, which renders `Branch CT` and is allowlisted in the static gate.)
- A database written before this change loads and runs without error.
- Every device keeps at least one attribution row after migration.
- CI is green: schemas regenerated, all tests updated.
- `tools/dev/verify.py` passes end to end.
