# TASK-DEPLOY-002 — Deployment procedure, smoke tests, rollback and operational metrics

**Task ID.** `TASK-DEPLOY-002`
**Title.** Deployment procedure, smoke tests, rollback and operational metrics
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-DEPLOY-005`, `REQ-DEPLOY-006`, `REQ-DEPLOY-007`, `REQ-DEPLOY-008`

**Description.**
Write a repeatable deployment procedure whose every step names a verification
command, add a smoke script separate from the watchdog probe, document and
rehearse the rollback, and name the metric that would reveal each
operational risk.

**Expected output.**
- The procedure names a verification command per step.
- The smoke script exits non-zero on any of the six checks and is separate from the watchdog probe.
- A rollback restores the previous image and the previous database copy and is verified by the same smoke tests.
- Every operational risk names the metric that would reveal it.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `scripts/smoke_test.sh` (new) | the six post-deployment checks |
| `addon/healthcheck.py` | the watchdog probe, kept separate from the smoke script |
| `engine/wattmosaic_engine/main.py` | the operational metrics publication |
| `docs/deployment.md` | the procedure |
| `docs/risks.md` | the operational risk register |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The option surface | `TASK-DEPLOY-001` | the settings the procedure configures |
| The backup and restore | `TASK-DEPLOY-001` | the database copy the rollback restores |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The smoke script | the operator, CI | exits non-zero on any failure |
| The operational metrics | `GET /metrics` | published, named per risk |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-DEPLOY-001` | the option surface, the backup and the image |

**Acceptance criteria.**
- [ ] The procedure names a verification command per step.
- [ ] The smoke script exits non-zero on any of the six checks.
- [ ] The smoke script is separate from the watchdog probe; the probe does no verification work.
- [ ] A rollback restores the previous image and the previous database copy.
- [ ] The rollback is verified by the same smoke tests.
- [ ] Every operational risk names the metric that would reveal it.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `MANUAL-deployment-procedure-review` | review | every step names a command |
| `UT-app-startup::test_smoke_checks_are_separate_from_the_probe` (new) | `pytest tests/unit` | the two are distinct |
| `MANUAL-rollback-rehearsal` | rehearsal | the rollback restores and passes the smoke tests |
| `UT-api::test_operational_metrics_are_published` (new) | `pytest tests/unit` | the metrics are published |

**Builder assignment.**
Unassigned. Wave 4, segment 4.2. Write owner of `scripts/smoke_test.sh`,
`docs/deployment.md` and `docs/risks.md` for wave 4.

**Blockers.**
- None. The `main.py` and `addon/healthcheck.py` writes are **write requests**
  to `TASK-DEPLOY-001`, the wave-4 owner.
