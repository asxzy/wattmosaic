# Decision log — Retire the "dedicated device" CT concept

## ADR-0027 — Every CT is a circuit; direct measurement is removed with the role

**Status:** Accepted · 2026-11-14 · Supersedes ADR-0025 and ADR-0026
**Workspace:** `/Users/asxzy/.local/share/opencode/worktree/d661cf/proud-garden`

### Context

ADR-0025 reclassified discovery's fallback from `CTType.DEDICATED_DEVICE` to `CTType.BRANCH`, on the grounds that a single-channel CT is a conductor, not an appliance, and discovery has no evidence for privacy. It deliberately retained `dedicated_device` as an *explicit user declaration* — the only legitimate source of the fact.

That retention created a two-class vocabulary: the product says "dedicated" in five UI surfaces, backed by three distinct concepts (a role, a monitoring level, and a set of persisted rows), while discovery never produces one. The user concluded the concept itself is deprecated and instructed full removal, including the capability it gates.

Investigation confirmed what that costs:

- `dedicated_channel_for` (`electrical/topology.py:333-349`) is the only resolver feeding `resolve_direct` (`electrical/direct.py:78-100`).
- `EffectiveConfig.allows_direct_measurement` (`configuration.py:142-148`) gates the Level-1 branch (`engine.py:1510-1588`), which is the sole producer of `Source.DIRECT_MEASURED`.
- A device alone on its row still receives a whole-row wattage through `_is_solo_row` (`engine.py:2348-2358`), but as `Source.INFERRED`, without the 0.55 confidence floor (`confidence.py:184-190`).
- The live install *reportedly* stores ~21 `dedicated_device` rows (`PROJECT_STATUS.md:3049`), so the change may affect real devices, not just vocabulary. This figure is uncorroborated: successive snapshots record 21/0/22/26 and a 2026-11-10 reset may have destroyed the data. It must be re-counted before any live apply.

### Decision

1. **`dedicated_device` is removed as a concept, not renamed.** No replacement boolean or neutral flag is introduced. A single-device channel is expressed structurally: it is a `branch` row with exactly one declaring device, which the solver already detects via `_is_solo_row`.
2. **Level-1 direct measurement is deleted.** `resolve_direct`, `DirectReading`, `dedicated_channel_for`, the `direct_is_ours` gate and the Level-1 branch are removed. Results for former dedicated rows grade through the normal tier rules.
3. **`MonitoringLevel.DEDICATED` is removed.** Monitoring is derived by `derive_monitoring`: a stored non-`UNKNOWN` declaration wins; only `UNKNOWN` falls through to the graph (`configuration.py:158-193`). A stored `"dedicated"` declaration resolves to `UNKNOWN` then derives from attribution rows, which yields `SHARED` for any device with rows.
4. **`DeviceTopology.dedicated_ct_ids` is removed.** Attribution lives entirely in `branch_ct_ids`.
5. **Historical `source = "direct_measured"` rows stay readable.** The `Source` enum member is retained so existing database rows do not become invalid.
6. **Both data migrations land before the enum narrowing.** Engine schema v11 demotes rows and moves ids; the HA config-entry migration rewrites stored assignments. Neither is optional.

### Consequences

- Former dedicated devices report `inferred` rather than `direct_measured`, at lower tiers and without the direct confidence floor. On the reference install this is reported as ~21 of 28 devices — a figure that is uncorroborated and must be re-counted before any live apply.
- An OFF device on a solo row can report `unexplained_load_on_device_circuit` and become `unknown`. This is the honest answer under ADR-0026's "attribute the measured step".
- The contract breaks (`CONTRACT_VERSION` 1.5.0 → 2.0.0); clients reading `dedicated_ct_ids` must migrate.
- ~23 unit test files, 5 integration files, 4 simulation/e2e files and all generated schemas require edits.

### Rejected alternatives

- **Keep the capability, retire only the vocabulary** (replace the role with a neutral `single_device` boolean). Preserves direct measurement, but keeps the concept under another name and re-opens the ADR-0025 debate about whether a structural fact can stand in for a declaration. The user explicitly chose full removal.
- **Labels/UI only.** Leaves the enums, the declaration and the stored rows in place; does not satisfy the instruction.
- **Retain `dedicated_device` as a hidden accepted value.** Keeps the vocabulary in the product and contradicts the acceptance criterion that no shipped string contains "dedicated".

### Verification

Covered by `design.md` AC-1 … AC-11, principally:
- pre-change database opens and migrates (AC-2),
- every device keeps an attribution row (AC-3),
- legacy HA entry replays without a 400 (AC-5),
- no write can reintroduce the role (AC-6),
- `tools/dev/verify.py` green end to end (AC-8).

## Relationship to prior ADRs

| ADR | Effect |
| --- | --- |
| ADR-0025 (single-channel CT is a branch) | Its discovery half stands. Its retention of `dedicated_device` as a declaration is superseded. |
| ADR-0026 (attribute the step, not the row) | Stands unchanged; the shared/solo attribution rules survive and become the only path. |
| ADR-0011 rule 2 (a shared CT is an upper bound) | Unaffected. |

The supersession must be recorded in `docs/decisions/README.md` and cross-referenced from ADR-0025 and ADR-0026 headers, which currently state the opposite policy.
