# 17 — Deployment report (HONEST: NO DEPLOYED SYSTEM AVAILABLE)

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**DELIVERED VERSION: `0.2.1-dev.37` (`pyproject.toml:7`) · CONTRACT VERSION:
`2.4.0` (`engine/wattmosaic_engine/contracts.py:32`).** Document 17 is
`TASK-DEPLOY-004b` of the completion sprint. This report is deliberately an
**honest status record, not a successful-deployment claim**.

---

## 1. Honest statement — what did NOT happen

> **No deployed WattMosaic installation was available in this environment, so
> no real deployment, health observation, backup, restore or rollback rehearsal
> was performed. There is no captured smoke run against a live system and no
> post-deploy metric evidence. Nothing in this report claims a deployment that
> did not happen.**

Concretely, the following were **not** performed and have **no evidence** here:

- No add-on image was pulled, installed, started or updated on a Home Assistant
  Supervisor (or any host).
- No migrations were applied to a production database; no backup was taken and
  no restore was rehearsed.
- No live health / status / diagnostics observation was made against a running
  engine; no post-deploy `/metrics` scrape was captured.
- No smoke test was run against a live endpoint and **no rollback rehearsal**
  was executed. `REQ-DEPLOY-005` / `REQ-DEPLOY-007` deployment-rehearsal rows
  remain `NOT RUN` in the traceability matrix.
- No exact deployed image tag can be stated (see §5).

This report therefore stays short on results and explicit about the gap. The
operational design, risks and checklist live in
`12-deployment-and-rollback.md` (§11 risks, §12 checklist); this document
records only what was and was not actually done, plus the runbook still to
execute.

---

## 2. What WAS verified — locally, against stubs / a dead port

The deployment *artifacts* were authored and exercised locally. These are
**local verification facts**, not deployment evidence:

| Artifact | Local verification | Honest scope |
| --- | --- | --- |
| `scripts/smoke_test.sh` | Authored; 9 checks (`health`, `status`, `panel`, `migrations`, `residuals`, `mapping`, `topology`, `devices`, `metrics`). Exit-1 failure propagation proven against a dead port (`http://127.0.0.1:1`): `FAIL topology / devices / metrics … curl: (7) Failed to connect to 127.0.0.1 port 1`, `smoke: 9 check(s) FAILED`. Integration vehicle `tests/integration/test_smoke_script.py`: `6 passed`. | Negative run against a **dead port**, not a live deployment. Proves the script fails loudly, not that a system is healthy. |
| `scripts/run_workflow.sh` | Authored; unified `build → migrate → smoke → observe` command with `set -e` stage gating, configurable via `WM_*` env vars. | Command exists and is documented; **not run end-to-end against a real deployment**. |
| `scripts/validate_addon.sh --no-build` | `passed: 71  failed: 0  skipped: 3` / `all checks passed`. | Static add-on validation; no build or deploy. |
| Panel check (`tools/dev/check_panel.py`) | `panel check: 4 assets, self-contained and CSP-safe`. | Static asset check; no rendered live panel. |
| Unit + integration gate | `.venv/bin/python -m pytest tests/unit tests/integration -o addopts="" -q` → `2784 passed, 15 skipped`. | Local test suite; no live-system verification. |

The smoke script deliberately covers a broader post-deploy surface than the
App watchdog (`/healthz`, which probes only `/api/v1/health` — see
`addon/gateway.py:398-408`): it checks gateway/engine health, status, the
ingress panel, applied migrations, the residual / mapping / topology / devices
endpoints and the `/metrics` scrape. Its non-zero aggregate exit on any failed
check is the intended behavior; the dead-port run is the proof of that.

The unified workflow (`scripts/run_workflow.sh`) is the authored command to
drive `build → migrate → smoke → observe` against a running deployment once one
exists. It has not been executed against a real system.

---

## 3. Operational-risk → metric mapping

The mapping below is retained from `12-deployment-and-rollback.md` §11 / §12.
These are the signals a real deployment **would** watch; none is populated here
because there is no deployment. Metrics come from `GET /api/v1/metrics`
(`engine/wattmosaic_engine/main.py:2940-2975`); disaggregation counters come
from the status payload (`main.py:1500-1552`), not from `/metrics`.

| Operational risk (doc 12 §11) | Signal / metric to watch on a real deployment | Where |
| --- | --- | --- |
| R-01 single-writer SQLite contention | `wattmosaic_queue_depth` growth; writer-queue growth | `/metrics` (`main.py:2961-2966`) + status §10.1 |
| R-02 cadence mismatch (15.7 s data, 5 s tick) | `wattmosaic_samples_ingested_total` / `wattmosaic_samples_persisted_total` advancing; `last_successful_processing_at` advancing | `/metrics` (`main.py:2967-2978`) + status |
| R-03 push-on-change HA silence | `wattmosaic_samples_ingested_total` flat **with** reader inactivity in HA (not a fault) | `/metrics` (`main.py:2967-2969`) |
| R-05 audit-table growth without horizon | DB and WAL size after 24 h; audit counters | filesystem + status (doc 12 §12 checks 24–25) |
| R-06 unbounded `latest_inference_results()` scan | query latency; avoid polling that path from monitoring | status / diagnostics |
| R-07 stale-tag guard | release workflow `prepare`/`image`/`publish`/`prune` job results | `.github/workflows/release.yaml` |
| R-09 no in-place downgrade | restore-from-backup is the only supported rollback | doc 12 §9.3 |
| R-10 read-only against HA (bounded blast radius) | no HA writes (by design); outputs only | `addon/config.yaml:69-70` |
| R-11 version-change-only updates | offered add-on version advances | `addon/config.yaml:23,38` |
| General liveness / health | `wattmosaic_up`, `wattmosaic_engine_status{status=…}`, `wattmosaic_ha_connected` | `/metrics` (`main.py:2946-2957`) |
| Persistence failures | `wattmosaic_persistence_errors_total` | `/metrics` (`main.py:2979-2981`) |

**These are a mapping for a future operator, not observed values.** No series
was scraped and no threshold was checked in this environment.

---

## 4. What remains to deploy — runbook still to execute on a real system

None of the following has been done. This is the checklist to execute once a
real target exists, following `12-deployment-and-rollback.md` §12:

1. **Pre-flight** — clean tree; `tools/dev/verify.py` green; `validate_addon.sh
   --no-build`; schema sync clean; `check_release.py --channel main`; CI green.
2. **Backup** — take a cold / volume backup; restore it to scratch and run
   `PRAGMA integrity_check` → `ok`; record previous version, previous image tag
   and backup location. **Do not deploy without a verified backup.**
3. **Migrate** — apply pending DB migrations forward-only and idempotently
   (workflow `migrate` stage / `Database` schema version check).
4. **Deploy image** — resolve the exact image tag (§5), push to `main`, watch
   `release.yaml` jobs, then stop / update / start the add-on via Supervisor.
5. **Smoke** — run `scripts/smoke_test.sh <BASE_URL>` against the live system;
   require `smoke: all checks passed` (not the dead-port failure proven here).
6. **Observe** — run the `observe` stage (`scripts/run_workflow.sh`): poll health
   for cycle count and `last_successful_processing_at` advancing; scrape
   `/metrics` (§3); check audit counters and DB/WAL size after 24 h.
7. **Rollback rehearsal** — rehearse the documented rollback path; note that
   in-place downgrade is not supported (R-09), so the supported path for a
   migrated database is **restore-from-backup** (doc 12 §9.3). Record the
   rehearsal result and the deployed version / backup location.

Until these steps are executed with a captured run, `REQ-DEPLOY-005` /
`REQ-DEPLOY-007` stay `NOT RUN` and `TASK-DEPLOY-004` stays `IN_REVIEW`.

---

## 5. Deployment image-tag ambiguity (unresolved)

The delivered version is `0.2.1-dev.37` (`pyproject.toml:7`), but the latest
reachable git tag is `v0.2.1-dev.36` (`git describe --tags --abbrev=0`); the
`dev.37` release commit carries no tag. The exact image tag a real deployment
would pull is therefore **not inferable from the repository**. This ambiguity
is recorded, not resolved and not fabricated. The operator must state the exact
image tag pulled from the deployment record before this report can be closed
out.

---

## Related documents

- [`12-deployment-and-rollback.md`](12-deployment-and-rollback.md) — §11 risks, §12 checklist, §9.3 rollback.
- [`16-final-verification-report.md`](16-final-verification-report.md) — delivered version, exact HEAD and gate table; §7 records this report as honest.
- [`README.md`](README.md) — the spec-set file index (links this report).
- `scripts/smoke_test.sh`, `scripts/run_workflow.sh` — the authored, locally verified artifacts.
