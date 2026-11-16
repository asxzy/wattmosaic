# TASK-STORAGE-001 — Tier registry, retention and payload encoding

**Task ID.** `TASK-STORAGE-001`
**Title.** Tier registry, retention and payload encoding
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-STORAGE-001`, `REQ-STORAGE-002`, `REQ-STORAGE-008`, `REQ-STORAGE-009`

**Description.**
Classify every persisted table into one of the four tiers, make retention a
configurable per-tier property, encode payloads where compression pays, and
record any partitioning decision with its measurement.

**Expected output.**
- One tier registry covering every table, published.
- Per-tier retention with validated ranges and stated defaults.
- A compressed payload readable through the same API; a legacy payload still loads.
- Any partitioning decision carries the measurement that justified it.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/storage/tiers.py` (new) | the tier registry |
| `engine/wattmosaic_engine/config/settings.py` | the retention settings |
| `engine/wattmosaic_engine/storage/database.py` | the tier-aware prune |
| `engine/wattmosaic_engine/storage/migrations.py` | the payload encoding column |
| `addon/run.sh` | the option surface |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The table list | `storage/migrations.py` | every existing table must be classified |
| The option surface | `addon/config.yaml` | the retention options |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The tier registry | `storage/database.py`, `GET /status` | one class per table, L1–L4 |
| The retention policy | `config/settings.py` | validated ranges, stated defaults |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the contract bump must land first |

**Acceptance criteria.**
- [ ] Every table is classified L1–L4 in one registry and published.
- [ ] Raw retention is validated against its stated range.
- [ ] A compressed payload reads through the same API as an uncompressed one.
- [ ] A legacy payload still loads.
- [ ] Any partitioning decision carries the measurement that justified it.
- [ ] `prune` reports each exempt table with a count of `0` and leaves it untouched.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-storage-config::test_every_table_is_classified` (new) | `pytest tests/unit` | no table is unclassified |
| `UT-storage-config::test_raw_retention_range_is_enforced` (new) | `pytest tests/unit` | an out-of-range value is refused |
| `UT-storage-config::test_payload_round_trip_across_encodings` (new) | `pytest tests/unit` | the round trip holds |
| `MANUAL-partitioning-decision-review` | review | a partitioning decision names its measurement |

**Builder assignment.**
Unassigned. Wave 1, segment 1.3. Write owner of `storage/database.py`,
`storage/migrations.py` and `addon/run.sh` for wave 1.

**Blockers.**
- `OD-31` — the requirement states a 7–14 day range; `OD-31` keeps the shipped
  30-day default and requires a validator range that is a superset of
  `{7, 14, 30}`. **Assumption:** the validator accepts the superset, the
  shipped default stays valid, and the acceptance criterion is met by the
  superset rather than by the literal range. The orchestrator settles the
  wording in `01-requirements.md`.
