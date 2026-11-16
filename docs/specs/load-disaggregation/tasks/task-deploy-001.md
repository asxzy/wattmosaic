# TASK-DEPLOY-001 — Build pipeline, option surface, startup migrations and backup

**Task ID.** `TASK-DEPLOY-001`
**Title.** Build pipeline, option surface, startup migrations and backup
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-DEPLOY-001`, `REQ-DEPLOY-002`, `REQ-DEPLOY-003`, `REQ-DEPLOY-004`

**Description.**
Reuse the existing image and release pipeline unchanged, expose every new
setting through the existing option and environment surface, run the pending
migrations at startup, and make a backup taken while the engine is running a
consistent snapshot.

**Expected output.**
- The existing image and GHCR pipeline are reused; `scripts/validate_addon.sh --no-build` passes.
- Every new setting appears in the option table, in `settings.py` and in `.env.example`, and is validated at startup.
- The pending migrations run at startup and `PRAGMA user_version` equals `LATEST_VERSION`.
- A backup taken while running is a consistent snapshot that restores cleanly.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `Dockerfile` | the reused image |
| `addon/config.yaml` | the option table |
| `addon/run.sh` | the option surface |
| `addon/healthcheck.py` | the backup entry point |
| `engine/wattmosaic_engine/config/settings.py` | the settings and their validation |
| `engine/wattmosaic_engine/main.py` | the startup migration run |
| `engine/wattmosaic_engine/storage/migrations.py` | the migration set |
| `.env.example` | the environment surface |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The tier registry and the retention settings | `TASK-STORAGE-001` | the new options |
| The migration set | `TASK-STORAGE-002` | forward-only, additive, idempotent |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The option surface | the Supervisor, `settings.py`, `.env.example` | one entry per setting |
| The startup migration state | `main.py` | `PRAGMA user_version == LATEST_VERSION` |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-STORAGE-001` | the retention options and the tier registry |

**Acceptance criteria.**
- [ ] The existing image and the GHCR pipeline are reused unchanged.
- [ ] `scripts/validate_addon.sh --no-build` passes.
- [ ] Every new setting appears in the option table, in `settings.py` and in `.env.example`.
- [ ] Every new setting is validated at startup.
- [ ] The pending migrations run at startup and `PRAGMA user_version` equals `LATEST_VERSION`.
- [ ] A backup taken while the engine is running is a consistent snapshot that restores cleanly.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `GATE-deployment_valid` | `python tools/dev/verify.py:deployment_valid` | the packaging is consistent |
| `GATE-deployment_valid` | `python tools/dev/verify.py:deployment_valid` | the option surface is complete |
| `UT-migrations::test_startup_applies_pending_migrations` (new) | `pytest tests/unit` | the startup applies them |
| `E2E-storage::test_backup_and_restore_round_trip` (new) | `pytest tests/end_to_end` | the snapshot restores cleanly |

**Builder assignment.**
Unassigned. Wave 4, segment 4.1. Write owner of the packaging files for
wave 4.

**Blockers.**
- None. The `main.py`, `config/settings.py`, `storage/migrations.py` and
  `addon/run.sh` writes are **write requests** to their wave-4 owners.
