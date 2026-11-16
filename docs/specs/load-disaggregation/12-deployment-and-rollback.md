# 12 — Deployment and Rollback

**Status:** Specification, in the approved specification set. **Scope:** how the load-disaggregation capability is built, shipped, backed up, smoke-tested, rolled back and monitored. **Owner:** Builder S-DEPLOY-3. **Date:** 2026-11-16.

**Purpose.** Define the exact, repeatable procedure an operator follows to move the load-disaggregation capability from a source tree into a running WattMosaic deployment, and the exact procedure to reverse it. Every command in this document exists in the repository; every claim about the existing deployment mechanism cites `file:line`.

**Rule.** This document specifies a procedure over the repository's existing release path. It introduces no new infrastructure, no new artefact type and no new packaging format. Where the repository defines no policy, the gap is marked **MISSING** and a proposal is given that is labelled as a proposal, not as an existing fact.

---

## 1. Scope and status of each deployment area

| # | Area | Status | Evidence | Shortfall (when not EXISTS) |
|---|------|--------|----------|---------------------------|
| D-01 | Container image build | **EXISTS** | `Dockerfile:20`, `Dockerfile:25-45`, `Dockerfile:50`, `Dockerfile:68-70`, `Dockerfile:79-80`, `Dockerfile:86-89` | — |
| D-02 | Supervisor App packaging | **EXISTS** | `addon/config.yaml:23-24`, `addon/config.yaml:38`, `scripts/package_addon.sh:40-52` | — |
| D-03 | App entrypoint / option parsing | **EXISTS** | `addon/run.sh:36-37`, `addon/run.sh:38-52`, `addon/run.sh:70-134`, `addon/run.sh:176-194` | — |
| D-04 | Health probe | **EXISTS** | `addon/healthcheck.py` (probes `/healthz` only), `addon/gateway.py:398-408`, `Dockerfile:86-87` | — |
| D-05 | Compose deployment shape | **EXISTS** | `docker-compose.yml:20`, `docker-compose.yml:25`, `docker-compose.yml:30`, `docker-compose.yml:43`, `docker-compose.yml:46`, `docker-compose.yml:81` | — |
| D-06 | CI gates | **EXISTS** | `.github/workflows/ci.yml:9`, `.github/workflows/ci.yml:37`, `.github/workflows/ci.yml:61`, `.github/workflows/ci.yml:78`, `.github/workflows/ci.yml:102`, `.github/workflows/ci.yml:117` | `mypy` is non-blocking (`.github/workflows/ci.yml:25`) |
| D-07 | Release and publish pipeline | **EXISTS** | `.github/workflows/release.yaml:44-52`, `.github/workflows/release.yaml:56-58`, `.github/workflows/release.yaml:196`, `.github/workflows/release.yaml:312`, `.github/workflows/release.yaml:420` | — |
| D-08 | Schema-drift gate | **EXISTS** | `.github/workflows/ci.yml:26-29` | — |
| D-09 | Version consistency tooling | **EXISTS** | `tools/dev/bump_version.py:30-32`, `tools/dev/bump_version.py:229-239`, `tools/dev/check_release.py:409-424` | — |
| D-10 | Database migrations | **EXISTS** | `engine/wattmosaic_engine/storage/migrations.py:792`, `engine/wattmosaic_engine/storage/migrations.py:799-816`, `engine/wattmosaic_engine/storage/database.py:90` | — |
| D-11 | Storage durability controls | **EXISTS** | `engine/wattmosaic_engine/storage/database.py:92-99` (WAL + `busy_timeout=10000`), `engine/wattmosaic_engine/storage/database.py:169` (`checkpoint()`) | — |
| D-12 | Backup / restore documentation | **EXISTS** | `docs/deployment.md:319-393`, `docs/installation.md:397-436` | Restore documented; **cadence and retention are MISSING** (see §6) |
| D-13 | Upgrade / release documentation | **EXISTS** | `docs/deployment.md:397-506` | — |
| D-14 | Backup cadence and retention policy | **MISSING** | — | No repository file defines a cadence or a retention period. See `G-DEPLOY-01` and `OD-99`. |
| D-15 | Per-release rollback runbook | **MISSING** | `docs/deployment.md:358-381` states only the *constraint* | No document lists the ordered restore-from-backup steps for a migrated database. §9 of this document supplies it. |
| D-16 | Pre-flight / smoke-test checklist for this capability | **MISSING** | — | No repository checklist covers the disaggregation path. §8 and §10 of this document supply it. |
| D-17 | Post-deployment monitoring for the disaggregation path | **MISSING** | `engine/wattmosaic_engine/main.py:1500-1552`, `engine/wattmosaic_engine/main.py:2995-3015` expose the counters but no runbook watches them | See §10. |

---

## 2. Deployment shapes

Two shapes are supported. They are not interchangeable and each has its own procedure.

### 2.1 Shape A — Home Assistant Supervisor App (canonical)

**When it applies.** The canonical production shape. The App is installed from the repository by the Home Assistant Supervisor and pulls a prebuilt multi-arch container image published by CI. The App runs under the Supervisor's lifecycle management (`addon/config.yaml:41` `boot: auto`, `addon/config.yaml:40` `startup: application`, `addon/config.yaml:43` `stage: experimental`).

Key properties:

| Property | Value | Evidence |
|---|---|---|
| App version | `0.2.1-dev.36` | `addon/config.yaml:23` |
| App slug | from `addon/config.yaml:24` | `addon/config.yaml:24` |
| Architectures | from `addon/config.yaml:29-31` | `addon/config.yaml:29-31` |
| Image reference | tagless `image:` field | `addon/config.yaml:38` |
| Effective image tag | `<image>:<version>` — the App pulls the version-tagged image | `addon/config.yaml:38` + `addon/config.yaml:23` |
| Ingress | enabled, port 8787 | `addon/config.yaml:53`, `addon/config.yaml:54` |
| Panel title / icon / admin | `WattMosaic` / `mdi:home-lightning-bolt` / admin enabled | `addon/config.yaml:55-57` |
| Host network | disabled | `addon/config.yaml:63` |
| Home Assistant API | enabled | `addon/config.yaml:69` |
| Supervisor API | disabled | `addon/config.yaml:70` |
| Discovery | `wattmosaic` | `addon/config.yaml:80-81` |
| Published ports | `8787/tcp: null` (ingress only) | `addon/config.yaml:87` |
| Watchdog | configured | `addon/config.yaml:95` |
| Mapped paths | `data` + `share` | `addon/config.yaml:97-101` |
| Backup | `cold` | `addon/config.yaml:105` |
| Startup timeout | 30 s | `addon/config.yaml:106` |
| Options | 13 entries | `addon/config.yaml:108-126` |
| Option schema | 13 entries | `addon/config.yaml:128-143` |

**The image is built by the release workflow, not by a separate App-image workflow.** `.github/workflows/app-image.yaml` does not exist. The `image` job in `.github/workflows/release.yaml:196` builds and pushes it. `PROJECT_STATUS.md:672`, `PROJECT_STATUS.md:689`, `PROJECT_STATUS.md:1376`, `PROJECT_STATUS.md:1937`, `PROJECT_STATUS.md:2648` and `PROJECT_STATUS.md:3435` are stale references to that non-existent workflow; no procedure here depends on them.

### 2.2 Shape B — plain Docker Compose

**When it applies.** Local development, CI-like sandboxes and self-hosted deployments where the operator does not use Home Assistant Supervisor.

Key properties (`docker-compose.yml`):

| Property | Value | Evidence |
|---|---|---|
| Compose project name | `wattmosaic` | `docker-compose.yml:10` |
| Service | `engine` | `docker-compose.yml:13` |
| Container name | `wattmosaic-engine` | `docker-compose.yml:21` |
| Build | `context: .`, `dockerfile: Dockerfile`, `PYTHON_IMAGE: ${WM_PYTHON_IMAGE:-python:3.12-slim-bookworm}` | `docker-compose.yml:14-19` |
| Image | `wattmosaic-engine:${WM_IMAGE_TAG:-0.1.3}` | `docker-compose.yml:20` |
| Restart policy | `unless-stopped` | `docker-compose.yml:22` |
| `init` | `true` | `docker-compose.yml:23` |
| Environment file | `.env` | `docker-compose.yml:25-26` |
| Container environment | `WM_DATABASE_PATH`, `WM_API_HOST`, `WM_API_PORT`, `WM_GATEWAY_PORT`, `WM_GATEWAY_ENABLED`, `WM_DROP_PRIVILEGES` | `docker-compose.yml:27-37` |
| Port publish | only the gateway is published; the engine API stays on loopback inside the container | `docker-compose.yml:39-43` |
| Named volume | `wattmosaic-data:/data` (external name `wattmosaic-data`) | `docker-compose.yml:45-46`, `docker-compose.yml:80-82` |
| Privilege escalation | `no-new-privileges:true` | `docker-compose.yml:52` |
| Capabilities | `cap_drop: ALL` | `docker-compose.yml:53-54` |
| Read-only root filesystem | `true` | `docker-compose.yml:55` |
| Temp space | tmpfs `/tmp`, 32 m, mode 1777 | `docker-compose.yml:56-57` |
| Stop grace period | 30 s | `docker-compose.yml:62` |
| Healthcheck | `python3 /app/healthcheck.py`, 5 s interval, 8 s timeout, 60 s start period, 3 retries | `docker-compose.yml:63-68` |
| Log rotation | json-file, 10 m × 3 files | `docker-compose.yml:69-73` |
| Memory limit | 1 g | `docker-compose.yml:75-78` |

The Compose file's own header gives the operator sequence: `cp .env.example .env` (set `HA_URL` or `HA_HOST` and `HA_TOKEN`), `docker compose up -d --build`, `docker compose logs -f wattmosaic` (`docker-compose.yml:3-5`).

**Rule.** Shape B's default image tag (`docker-compose.yml:20`) is *not* the App version (`addon/config.yaml:23`). When deploying Shape B at the current release, set `WM_IMAGE_TAG` explicitly to the released version string.

**Rule.** The Compose file builds from source (`docker-compose.yml:14-19`); it does not pull the released image unless `WM_IMAGE_TAG` is set and the build is skipped. `docker compose up -d --build` always rebuilds from the checkout.

**Rule.** `.env` is git-ignored and `.dockerignore` keeps it out of the image (`docker-compose.yml:7-8`). Secrets come from `.env` or the host environment only.

### 2.3 Shape selection

| Requirement | Shape |
|---|---|
| Runs inside Home Assistant, managed by the Supervisor | A |
| Local development or self-hosted without Supervisor | B |
| Ingress / panel access through HA | A (`addon/config.yaml:53`, `addon/config.yaml:55-57`) |
| Direct HTTP access on a host port | B (`docker-compose.yml:43`) |
| Supervisor-managed backup | A (`addon/config.yaml:105` `backup: cold`) |
| Operator-managed backup of a named volume | B (`docker-compose.yml:46`) |

---

## 3. Build process

### 3.1 Artefact order

The five version sites must agree, plus the generated schemas, plus the release notes. `tools/dev/bump_version.py:30-32` and `tools/dev/bump_version.py:229-239` write all five; `tools/dev/check_release.py:72-76` and `tools/dev/check_release.py:409-424` validate them.

| Order | Artefact | Site | Written by |
|---|---|---|---|
| 1 | Python package version | `pyproject.toml:7` | `tools/dev/bump_version.py` |
| 2 | HA integration constant | `custom_components/wattmosaic/const.py:14` | `tools/dev/bump_version.py` |
| 3 | HA integration manifest | `custom_components/wattmosaic/manifest.json:4` | `tools/dev/bump_version.py` |
| 4 | App manifest | `addon/config.yaml:23` | `tools/dev/bump_version.py` |
| 5 | Engine package version | `engine/wattmosaic_engine/__init__.py:3` | `tools/dev/bump_version.py` |
| 6 | Generated schemas | `data/schemas/` | `python tools/dev/export_schemas.py` |
| 7 | Release notes | repository release | `.github/workflows/release.yaml:135-167` |

**Rule.** The App pulls `<image>:<version>` (`addon/config.yaml:38` with `addon/config.yaml:23`). A change that does not advance the version string does not produce an offered update in the Supervisor. Any release of the disaggregation capability therefore requires a version bump.

**Rule.** The schema regeneration gate is `python tools/dev/export_schemas.py` followed by `git diff --exit-code data/schemas` (`.github/workflows/ci.yml:26-29`). If the diff is non-empty, the tree is stale and the gate fails. Regenerate and commit before releasing.

### 3.2 Local build

The repository's own verification entry point is `tools/dev/verify.py`. It pins `PYTHONPATH` to this checkout's `engine/` directory (`tools/dev/verify.py:69-78`) so that the local tree, not an installed package, is under test. It runs 12 required checks plus the optional `config_valid` check (`tools/dev/verify.py:105-124`).

Commands, and where each runs:

| Step | Command | Runs where |
|---|---|---|
| L-1 | `python tools/dev/verify.py` | Developer workstation / checkout root |
| L-2 | `scripts/validate_addon.sh --no-build` | Developer workstation / checkout root |
| L-3 | `python tools/dev/export_schemas.py` then `git diff --exit-code data/schemas` | Developer workstation / checkout root |
| L-4 | `docker build -t wattmosaic:local .` | Developer workstation / checkout root |
| L-5 | `python tools/dev/bump_version.py --next` (print) or `python tools/dev/bump_version.py --write` (rewrite the five sites) | Developer workstation / checkout root |
| L-6 | `scripts/package_addon.sh --check` (stage the App tree and verify the layout) | Developer workstation / checkout root |

`.github/workflows/ci.yml:33` runs `scripts/validate_addon.sh --no-build`, which is the CI form of L-2. The `--no-build` flag is what CI uses; the full form runs `docker build -t <tag> <stage>` against the staged App context (`scripts/validate_addon.sh:809-810`). `scripts/package_addon.sh:40-52` is the App-packaging entry point: `--out DIR` (default `dist/addon`), `--install HOST` (rsync to `HOST:{/local_apps,/addons}/wattmosaic_engine`), `--keep`, `--check` (`scripts/package_addon.sh:25-29`, `:40-52`).

**Rule.** L-4 builds from the repository root `Dockerfile`. The Dockerfile is two-stage: the builder stage (`:25-45`) installs with `--only-binary=:all:` against `-c constraints.txt`; the runtime stage is `python:3.12-slim-bookworm` (`:20`, `:50`). The image runs as UID 10001 (`:68-70`), declares `VOLUME ["/data"]` (`:79`), exposes `8787` (`:80`), has a `HEALTHCHECK` pointing at `/app/healthcheck.py` (`:86-87`), and `ENTRYPOINT ["/app/run.sh"]` (`:89`).

### 3.3 CI build and publish

`.github/workflows/ci.yml` defines six jobs: `test` (`:9`), `privacy` (`:37`), `secrets` (`:61`), `integration-structure` (`:78`), `hacs` (`:102`), `release` (`:117`).

`.github/workflows/release.yaml` is the publish path:

| Stage | Job | Evidence | What it does |
|---|---|---|---|
| Trigger | — | `.github/workflows/release.yaml:44-52` | Push to `main` or `stable`, plus `workflow_dispatch` with a `force` input |
| Serialisation | — | `.github/workflows/release.yaml:56-58` | `concurrency` group prevents overlapping releases |
| Prepare | `prepare` | `.github/workflows/release.yaml:66` | Bump guard (`:91-103`), version decision (`:105-133`), auto-generated notes (`:135-167`), `check_release.py --channel/--tag` (`:169-177`), bump commit (`:179-194`) |
| Build & push | `image` | `.github/workflows/release.yaml:196` | Image-exists check (`:222-232`), stale-tag guard and runtime-path list (`:234-285`), multi-arch push (`:287-302`), manifest verify (`:304-310`) |
| Publish | `publish` | `.github/workflows/release.yaml:312` | 30 × 10 s pull wait (`:329-356`), annotated tag (`:358-371`), `--prerelease --latest=false` (`:387-408`) |
| Cleanup | `prune` | `.github/workflows/release.yaml:420` | Prunes stale artefacts |

**Rule.** The publish boundary is `.github/workflows/release.yaml:387-408`. Releases are published as **prereleases** and are **not** marked as latest. A release being present on the GitHub Releases page is therefore *not* the same as a release being offered as the stable channel.

**Rule.** A push to `main` triggers the release workflow (`.github/workflows/release.yaml:44-52`), which publishes a prerelease. The project owner has authorized committing and pushing to `main`; CI then publishes the prerelease automatically. Nothing in this procedure requires a separate publish command, because there is none to run. The `prepare` job's bump guard (`.github/workflows/release.yaml:91-103`) and version decision (`.github/workflows/release.yaml:105-133`) determine the version — do not hand-write a version into the artefacts and expect CI to preserve it.

**Rule.** The `image` job refuses to overwrite an existing tag (`.github/workflows/release.yaml:222-232`) and applies a stale-tag guard over a runtime-path list (`.github/workflows/release.yaml:234-285`). If a release with the intended version already exists, the release must be abandoned, not forced. See `G-DEPLOY-02`.

### 3.4 Version bump mechanics

`tools/dev/bump_version.py:30-32` enumerates the version sites and `tools/dev/bump_version.py:229-239` writes them; `tools/dev/bump_version.py:51` is the entry point. `tools/dev/check_release.py:190-193` and `tools/dev/check_release.py:409-424` verify that the resulting version is coherent, and `tools/dev/check_release.py:72-76` reads the declared version. `tools/dev/check_release.py:407-426` defines its CLI: `--tag`, `--channel` (`main` or `stable`) and `--notes-for`; with no arguments it validates the version fields and the release notes coverage (`tools/dev/check_release.py:439-457`).

**Rule.** Version strings must be identical across all five sites. A partial bump is a release failure, not a warning.


---

## 4. Environment configuration

### 4.1 Precedence

The entrypoint resolves configuration with a fixed precedence: **options > environment > defaults** (`addon/run.sh:9`, `addon/run.sh:176-194`). Option parsing is `eval`-free (`addon/run.sh:70-134`), driven by a 13-row `OPTION_TABLE` (`addon/run.sh:38-52`).

| Layer | Where it comes from | Applies to |
|---|---|---|
| 1. Options | Supervisor App options panel → `addon/config.yaml:108-126` (13 options) with schema `addon/config.yaml:128-143` (13 entries) | Shape A only |
| 2. Environment | Container environment, `env_file .env` (`docker-compose.yml:25`) | Shape B, and Shape A's non-option environment |
| 3. Defaults | `addon/run.sh:38-52` `OPTION_TABLE` default column | Both |

### 4.2 The three-place rule

**Rule.** A new engine setting exposed through the App must be added in **all three** places. `addon/run.sh:36-37` states that `scripts/validate_addon.sh` parses the shell `OPTION_TABLE` and compares it against `engine/wattmosaic_engine/config/settings.py`; `scripts/validate_addon.sh:356-408` implements the three enforcement points:

1. `addon/run.sh` `OPTION_TABLE` (`:38-52`) — the shell-level default and validation;
2. `engine/wattmosaic_engine/config/settings.py` `from_env` — the engine-level read;
3. `addon/config.yaml` `options` (`:108-126`) and `schema` (`:128-143`) — the Supervisor-level surface.

Adding a setting to only one or two of these fails `scripts/validate_addon.sh` (run with `--no-build` in CI at `.github/workflows/ci.yml:33`). The disaggregation capability introduces new engine settings; each one that is user-visible must satisfy this rule.

### 4.3 Secrets

| Secret | Source | Handling |
|---|---|---|
| Supervisor token | `SUPERVISOR_TOKEN`; `addon/run.sh` falls back to it when `ha_token` is empty, and switches to the Core proxy at `http://supervisor/core` | `addon/run.sh:216-229`; `addon/config.yaml:9` |
| Home Assistant API token | `ha_token` option → `WM_HA_TOKEN` | `addon/config.yaml:8-9`, `:110`; `addon/config.yaml:69` `homeassistant_api: true` |
| Engine API token | `api_token` option → `WM_API_TOKEN` | `addon/config.yaml:10`, `:111`; schema `password?` (`addon/config.yaml:133`) |

**Rule.** `hassio_api: false` (`addon/config.yaml:70`): the App has no Supervisor API access beyond the Core proxy it already uses. The Supervisor API grant is not part of the deployment surface.

**Rule.** Generate an API token with `python -c "import secrets; print(secrets.token_urlsafe(32))"` (`addon/config.yaml:132`).

**Rule.** Secrets are never printed. The entrypoint redacts them as `***set(N chars)` (`addon/run.sh:277-302`). `addon/run.sh:232-274` validates log level, numeric fields, port and token presence before the engine starts.

**Rule.** `--print-config` output is redacted by design. A redacted value such as `***set(64 chars)` is proof that the value was supplied, not a defect to be fixed by removing the redaction.

**Rule.** The `secrets` CI job (`.github/workflows/ci.yml:61`) and `tools/dev/check_secrets.py` gate the repository against committed secrets. Never place a token in the repository, in `addon/config.yaml`, or in a committed `.env`.

### 4.4 Configuration surface inventory

The 13 App options are declared at `addon/config.yaml:108-126` with the matching schema at `addon/config.yaml:128-143`. `addon/config.yaml:8-20` maps each option name to the exact environment variable the engine reads, and `addon/run.sh:38-52` carries the same 13 rows in its `OPTION_TABLE`.

| # | Option | Engine env var | Default (`addon/config.yaml:108-126`) | Schema (`addon/config.yaml:128-143`) |
|---|---|---|---|---|
| 1 | `ha_url` | `WM_HA_URL` | `http://homeassistant:8123` | `url` |
| 2 | `ha_token` | `WM_HA_TOKEN` (fallback: `SUPERVISOR_TOKEN`) | `""` | `password?` |
| 3 | `api_token` | `WM_API_TOKEN` | `""` | `password?` |
| 4 | `learning_enabled` | `WM_LEARNING_ENABLED` | `true` | `bool` |
| 5 | `stale_after_s` | `WM_STALE_AFTER_S` | `120` | `int(10,3600)` |
| 6 | `inference_interval_s` | `WM_INFERENCE_INTERVAL_S` | `5` | `float(1,600)` |
| 7 | `log_level` | `WM_LOG_LEVEL` | `INFO` | `list(DEBUG\|INFO\|WARNING\|ERROR)` |
| 8 | `log_json` | `WM_LOG_JSON` | `true` | `bool` |
| 9 | `database_path` | `WM_DATABASE_PATH` | `/data/wattmosaic.db` | `str` |
| 10 | `retention_days_raw` | `WM_RETENTION_DAYS_RAW` | `30` | `int(1,3650)` |
| 11 | `retention_days_aggregates` | `WM_RETENTION_DAYS_AGGREGATES` | `730` | `int(30,36500)` |
| 12 | `queue_max_size` | `WM_QUEUE_MAX_SIZE` | `5000` | `int(100,1000000)` |
| 13 | `auto_placement` | `WM_AUTO_PLACEMENT` | `false` | `bool` |

`addon/run.sh:232-274` validates the log level, the numeric options, the port and the token presence before the engine starts. The Compose shape supplies the same surface through `env_file .env` (`docker-compose.yml:25`) and sets the container-internal values directly (`docker-compose.yml:30-37`): `WM_DATABASE_PATH: /data/wattmosaic.db`, `WM_API_HOST: 127.0.0.1`, `WM_API_PORT: "8788"`, `WM_GATEWAY_PORT: "8787"`, `WM_GATEWAY_ENABLED: "true"`, `WM_DROP_PRIVILEGES: ${WM_DROP_PRIVILEGES:-true}`.

**Rule.** The Compose environment block (`docker-compose.yml:27-37`) deliberately overrides `.env`: those are container paths and ports, not user preferences. Do not move `WM_DATABASE_PATH` or the ports into `.env`.

**Rule.** `auto_placement` defaults to `false` and writes device configurations when enabled (`addon/config.yaml:121-126`). It requires two independent consents: the option itself plus `apply=true` in the Discover card's request. Enabling it in a deployment is a separate consent decision, not a deployment default.

**Rule.** The ingress port is `8787` (`addon/config.yaml:54`), the container exposes `8787` (`Dockerfile:80`), the Compose gateway port is `8787` (`docker-compose.yml:33`, `:43`), and the watchdog probes `/healthz` on that port (`addon/config.yaml:95`). Changing one without the others breaks the health path (`addon/gateway.py:398-408`).

### 4.5 Privilege and filesystem posture

`addon/run.sh:337-354` drops privileges via `setpriv` and performs the `/data` write test as `run_as_service` (`addon/run.sh:346-354`). The container runs as UID 10001 (`Dockerfile:68-70`).

**Rule.** The `/data` write test at `addon/run.sh:346-354` is the earliest hard failure in the startup sequence. A permissions error on `/data` means the App will not start; check the mapped path ownership (`addon/config.yaml:97-101`) before anything else.

The gateway supervisor loop is `addon/run.sh:361-374`; discovery is `addon/run.sh:380-387`; the engine is `exec`'d as PID 1 at `addon/run.sh:400-403`.

---

## 5. Database migrations

### 5.1 Engine mechanics

| Property | Value | Evidence |
|---|---|---|
| Latest schema version | `11` | `engine/wattmosaic_engine/storage/migrations.py:792` |
| Transaction discipline | `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` | `engine/wattmosaic_engine/storage/migrations.py:799-816` |
| When migrations run | `Database.__init__` | `engine/wattmosaic_engine/storage/database.py:90` |
| Journal mode | WAL | `engine/wattmosaic_engine/storage/database.py:92-99` |
| Busy timeout | `busy_timeout=10000` | `engine/wattmosaic_engine/storage/database.py:92-99` |
| Liveness check | `ping()` | `engine/wattmosaic_engine/storage/database.py:140` |
| Integrity check | `integrity_check()` | `engine/wattmosaic_engine/storage/database.py:151` |
| Checkpoint | `checkpoint()` | `engine/wattmosaic_engine/storage/database.py:169` |
| Version reporting | `stats()` → `schema_version`, `latest_schema_version` | `engine/wattmosaic_engine/storage/database.py:1365-1379` |

Migrations therefore run automatically at engine start, before the API or the workers come up.

### 5.2 Discipline

**Rule.** Migrations are forward-only, additive and idempotent. A migration must create new tables or add new columns; it must not drop, rename or rewrite existing ones, because the previous binary must remain able to read a migrated database for as long as the restore path is untested.

**Rule.** A migration must not rewrite historical evidence. Rows already written — measurements, inference results, audit entries — are the evidentiary record of the disaggregation system's behaviour and must not be updated or deleted by a schema change.

**Rule.** A migration that alters the meaning of a stored value is a destructive migration and requires a tested restore path before it ships. See §9.3.

### 5.3 Schema-version mismatch

On a schema-version mismatch the engine reports the installed and the expected versions through `stats()` (`engine/wattmosaic_engine/storage/database.py:1365-1379`). The operator reads `schema_version` and `latest_schema_version` and compares them.

| Observed | Meaning | Action |
|---|---|---|
| `schema_version == latest_schema_version` | Database is at the current schema | Continue with the deployment checklist |
| `schema_version < latest_schema_version` | Engine has not yet applied pending migrations | Confirm the engine started with write access to `/data`; migrations run in `Database.__init__` (`engine/wattmosaic_engine/storage/database.py:90`) |
| `schema_version > latest_schema_version` | Database was written by a **newer** engine than the one running | **Stop.** The running binary is older than the database. Do not start it against this database. Restore the backup (§9) or roll forward to a build whose `LATEST_VERSION` (`engine/wattmosaic_engine/storage/migrations.py:792`) is at least the database version |

**Rule.** Never start an older engine against a newer-schema database. `docs/deployment.md:358-381` states that a schema downgrade is not supported in place.

### 5.4 Pre-migration backup

**Rule.** A verified backup must exist **before** the engine starts on a database that is not already at the current schema version. Migrations run unattended inside `Database.__init__` (`engine/wattmosaic_engine/storage/database.py:90`); there is no point at which the operator can intervene between "engine started" and "migration applied". The backup therefore precedes the *start*, not the migration.

See §6 for the backup procedure and §9.3 for what a backup buys you once a migration has been applied.

### 5.5 Testing a migration against a prior-schema database

**Rule.** A migration must be tested against a prior-schema database built with **raw `sqlite3`**, not by instantiating `Database`. Constructing `Database` runs migrations in `__init__` (`engine/wattmosaic_engine/storage/database.py:90`), so an application-level fixture silently migrates the fixture and the test proves nothing.

`tests/unit/test_migrations.py:92-106` contains the raw-`sqlite3` prior-schema fixture pattern to copy. The fixture creates the prior schema directly with `sqlite3`, then the test exercises the migration path.

**Rule.** Any migration that the disaggregation capability introduces must ship with a test in that raw-`sqlite3` style, so the pre-migration data is demonstrably preserved.

---

## 6. Backup strategy

### 6.1 What to back up

| Item | Shape A (App) | Shape B (Compose) |
|---|---|---|
| Database file | on the mapped `data` path (`addon/config.yaml:97-101`) | `wattmosaic-data` volume (`docker-compose.yml:46`, `:81`), file at `/data/wattmosaic.db` (`docker-compose.yml:30`) |
| `-wal` and `-shm` sidecars | required if the DB is copied while running | required if the DB is copied while running |
| Checkpointed database | acceptable alternative | acceptable alternative |

**Rule.** A SQLite database in WAL mode (`engine/wattmosaic_engine/storage/database.py:92-99`) is only consistent if the `-wal` file travels with the database file. Either copy the DB **and** its `-wal`/`-shm` sidecars together, or checkpoint first (`engine/wattmosaic_engine/storage/database.py:169`) and copy only the database file. Copying only the `.db` while the engine is running yields a torn snapshot.

### 6.2 Backup mechanisms that exist

| Mechanism | Evidence | Notes |
|---|---|---|
| Supervisor backup, type `cold` | `addon/config.yaml:105` | A cold backup requires the add-on to be stopped; it is the Supervisor's own snapshot facility and is the operator's default for Shape A |
| Compose named volume | `docker-compose.yml:46`, `docker-compose.yml:81` | The volume is the unit to back up for Shape B |
| Documented restore procedure | `docs/deployment.md:319-393` | Existing repository guidance |
| Fresh-install verify table | `docs/installation.md:397-436` | Includes backup/restore verification |
| Local backup directory | `.gitignore:27-28` | `data/backups/` is git-ignored — backups are never committed |

### 6.3 Cadence and retention — MISSING

**Status: MISSING.** No repository file defines a backup cadence or a retention period. `docs/deployment.md:319-393` documents *how* to back up and restore; it does not say *how often* or *how many*.

**Proposal (OD-99) — not an existing repository fact.** For a single-node, single-writer deployment on the order of one household:

| Parameter | Proposed value | Rationale |
|---|---|---|
| Cadence | Daily, at a fixed local time, plus an ad-hoc snapshot before every version upgrade | The upgrade snapshot is the one that makes rollback possible (§9) |
| Retention | 7 daily + 4 weekly + 6 monthly | Keeps the pre-upgrade snapshot long enough to catch a slow-burn regression |
| Location | Off the host, on a different physical device or an encrypted off-site store | A backup on the same disk is not a backup |
| Verification | `PRAGMA integrity_check` + schema-version check on every restore (§6.4), and a full restore drill monthly | An unverified backup is an assumption |

**Rule.** Until a cadence is adopted, the operator must take a backup **before every deployment** regardless. That is the only cadence this procedure relies on.

### 6.4 Restore verification

After any restore, both checks must pass before the service is considered healthy:

| Check | Method | Passing result | Evidence for the check's existence |
|---|---|---|---|
| Structural integrity | `PRAGMA integrity_check` | `ok` | `engine/wattmosaic_engine/storage/database.py:151` (`integrity_check()`) |
| Schema version | compare `stats().schema_version` with `stats().latest_schema_version` (`engine/wattmosaic_engine/storage/database.py:1365-1379`) and with `LATEST_VERSION` (`engine/wattmosaic_engine/storage/migrations.py:792`) | equal, or the database is deliberately behind and the migration path is understood (§5.3) | — |
| Engine liveness | `ping()` | True | `engine/wattmosaic_engine/storage/database.py:140` |
| Service liveness | `GET /healthz` | HTTP 200 | `addon/gateway.py:398-408` |

**Rule.** A backup that has not been restored and verified is not a backup. Record the restore verification result for every pre-upgrade snapshot.

---

## 7. Deployment procedure (Shape A — canonical)

**Rule.** The steps below are ordered and copy-pasteable. Do not reorder them. Each step names where it runs.

### 7.1 Pre-flight checklist

| # | Check | Command | Expected | Where |
|---|---|---|---|---|
| P-1 | Working tree is clean and up to date | `git status --short` and `git log -1 --oneline` | no output from `git status --short` | Checkout |
| P-2 | Verification gates green | `python tools/dev/verify.py` | all 12 required checks pass (`tools/dev/verify.py:105-124`) | Checkout |
| P-3 | App manifest matches the engine | `scripts/validate_addon.sh --no-build` | exit 0 | Checkout |
| P-4 | Schemas not stale | `python tools/dev/export_schemas.py` then `git diff --exit-code data/schemas` | empty diff (`.github/workflows/ci.yml:26-29`) | Checkout |
| P-5 | Release checks | `python tools/dev/check_release.py --channel main` | exit 0 (`tools/dev/check_release.py:407-457`) | Checkout |
| P-6 | Five version sites agree | `python tools/dev/check_release.py` | one version string across `pyproject.toml:7`, `custom_components/wattmosaic/const.py:14`, `custom_components/wattmosaic/manifest.json:4`, `addon/config.yaml:23`, `engine/wattmosaic_engine/__init__.py:3` | Checkout |
| P-7 | CI green on the commit | GitHub Actions run for the commit | `test`, `privacy`, `secrets`, `integration-structure`, `hacs`, `release` all succeed (`.github/workflows/ci.yml:9`, `:37`, `:61`, `:78`, `:102`, `:117`) | GitHub |
| P-8 | **Backup taken and verified** | §6 procedure; then `PRAGMA integrity_check` | `ok` (see §6.4) | Host / Supervisor |
| P-9 | Rollback path confirmed | the previous image tag and the backup location are written down and reachable | both recorded in the deployment log | Operator |
| P-10 | Target confirmed | the intended version string and the intended deployment shape | recorded | Operator |
| P-11 | Image published | a prerelease exists for the version | release appears on the Releases page as a **prerelease** (`.github/workflows/release.yaml:387-408`) | GitHub |
| P-12 | Stale tag absent | the version's image does not already exist | `.github/workflows/release.yaml:222-232` would have refused the push | GitHub |

**Rule.** If P-8, P-9 or P-10 cannot be satisfied, **stop**. A deployment without a verified backup has no rollback path, and the disaggregation schema change is a forward-only migration.

### 7.2 Deploy

| # | Step | Command / action | Where | Expected |
|---|---|---|---|---|
| D-1 | Push the prepared commit | `git push origin main` | Checkout | The project owner has authorized pushing to `main`; CI publishes the prerelease automatically (`.github/workflows/release.yaml:44-52`) |
| D-2 | Release workflow completes | watch the `release.yaml` run | GitHub | `prepare` → `image` → `publish` → `prune` all succeed (`.github/workflows/release.yaml:66`, `:196`, `:312`, `:420`) |
| D-3 | Image and tag exist | the release for the new version is a prerelease; the image is pullable as `<image>:<version>` (`addon/config.yaml:38`, `:23`) | GitHub | tag present; manifest verified (`.github/workflows/release.yaml:304-310`) |
| D-4 | Stop the running App | Supervisor: stop the WattMosaic add-on | Supervisor | App stopped; cold backup can be taken (`addon/config.yaml:105`) |
| D-5 | Take the cold backup | Supervisor add-on backup, or the Shape B volume backup (§6.2) | Supervisor / Host | backup artefact exists |
| D-6 | Verify the backup | restore-to-temporary and run the §6.4 checks | Host | `integrity_check` → `ok`; schema version recorded |
| D-7 | Update the App to the new version | Supervisor: update the WattMosaic add-on | Supervisor | Supervisor pulls `<image>:<version>`; the version advanced so the update is offered |
| D-8 | Start the App | Supervisor: start the WattMosaic add-on | Supervisor | startup completes within the 30 s timeout (`addon/config.yaml:106`); the `/data` write test passes (`addon/run.sh:346-354`) |
| D-9 | Migrations applied | observe the startup log | Supervisor | migrations ran in `Database.__init__` (`engine/wattmosaic_engine/storage/database.py:90`); `schema_version == latest_schema_version` (`engine/wattmosaic_engine/storage/database.py:1365-1379`) |

### 7.3 Post-deploy checklist

| # | Check | Command / request | Expected |
|---|---|---|---|
| Q-1 | Container healthy | `docker inspect --format '{{.State.Health.Status}}' <container>` | `healthy` (`Dockerfile:86-87` → `/app/healthcheck.py`) |
| Q-2 | Gateway health | `GET /healthz` through ingress | HTTP 200 (`addon/gateway.py:398-408`) |
| Q-3 | Engine health | `GET /healthz` on the engine API | HTTP 200 (`engine/wattmosaic_engine/api/server.py:760-784`) |
| Q-4 | Schema version | `GET /api/v1/...` status payload | `schema_version == latest_schema_version` (`engine/wattmosaic_engine/storage/database.py:1365-1379`) |
| Q-5 | Workers up | status payload | writer, quality, inference and maintenance present (`engine/wattmosaic_engine/main.py:3169-3173`) |
| Q-6 | No restart loop | status payload `runtime_stats` | component restart counts at 0 (`engine/wattmosaic_engine/main.py:2995-3015`) |
| Q-7 | Smoke tests | §8 | all pass |
| Q-8 | Monitoring armed | §10 signals watched | dashboard or log filter in place |

**Rule.** The full smoke-test list in §8 must pass before the deployment is called successful. This document does not claim any deployment has succeeded; no deployment has been executed as part of writing it.

---

## 8. Smoke tests

### 8.1 Health

| ID | Check | Request / command | Expected result | Automatable |
|---|---|---|---|---|
| ST-01 | Gateway `/healthz` through ingress | `GET /healthz` on the ingress URL | HTTP 200 | Yes |
| ST-02 | Engine `/healthz` | `GET /healthz` on the engine API port | HTTP 200 (`engine/wattmosaic_engine/api/server.py:760-784`) | Yes |
| ST-03 | Engine API route table | `GET /api/v1/...` | the 24 `/api/v1` routes respond (`engine/wattmosaic_engine/api/server.py:760-784`, `API_PREFIX` `engine/wattmosaic_engine/api/server.py:57`) | Yes |
| ST-04 | Container health status | `docker inspect --format '{{.State.Health.Status}}' <container>` | `healthy` | Yes |
| ST-05 | Gateway degraded semantics | inspect `/healthz` when a component is degraded | 200 only for `ok` or `degraded` (`addon/gateway.py:398-408`); any other status is a failure | Yes |

**Rule.** `addon/healthcheck.py` probes `/healthz` only. A healthy container therefore says nothing about the disaggregation pipeline; ST-06 and later are the checks that matter for this capability.

### 8.2 Storage and workers

| ID | Check | Command / request | Expected result | Automatable |
|---|---|---|---|---|
| ST-06 | Database reachable | `ping()` via the status payload | True (`engine/wattmosaic_engine/storage/database.py:140`) | Yes |
| ST-07 | Schema version | `stats()` | `schema_version == latest_schema_version` (`engine/wattmosaic_engine/storage/database.py:1365-1379`, `engine/wattmosaic_engine/storage/migrations.py:792`) | Yes |
| ST-08 | Integrity | `integrity_check()` | `ok` (`engine/wattmosaic_engine/storage/database.py:151`) | Yes |
| ST-09 | Writer worker | status payload | writer present and consuming (`engine/wattmosaic_engine/main.py:3169-3173`) | Yes |
| ST-10 | Quality worker | status payload | quality present (`engine/wattmosaic_engine/main.py:3169-3173`) | Yes |
| ST-11 | Inference worker | status payload | inference present; cycle count increasing (§10) | Yes |
| ST-12 | Maintenance worker | status payload | maintenance present (`engine/wattmosaic_engine/main.py:3169-3173`) | Yes |
| ST-13 | No component restarts | `runtime_stats` | restart counts zero (`engine/wattmosaic_engine/main.py:2995-3015`) | Yes |
| ST-14 | Graceful shutdown path | Supervisor stop | shutdown completes inside the 30 s grace period (`docker-compose.yml:62`, `engine/wattmosaic_engine/main.py:803-841`) | Yes |

### 8.3 Ingestion

| ID | Check | Command / request | Expected result | Automatable |
|---|---|---|---|---|
| ST-15 | Reader ingestion | wait for the reader cadence and inspect the measurement store | new measurement rows appear for the configured readers | Partly — the wait is time-based |
| ST-16 | HA event ingestion | trigger a HA event and observe the queue | the event is queued and consumed (`engine/wattmosaic_engine/main.py:1500-1552`) | Partly — needs a HA trigger |
| ST-17 | Queue drains | status payload | the writer queue depth returns to zero after the burst (`engine/wattmosaic_engine/main.py:1500-1552`) | Yes |
| ST-18 | Unlinked events | status payload | `unlinked_events_ignored` behaves as expected for the installation (`engine/wattmosaic_engine/main.py:1500-1552`) | Yes |
| ST-19 | Discovery | Supervisor discovery list | `wattmosaic` discovered (`addon/config.yaml:80-81`, `addon/run.sh:380-387`) | Partly — needs Supervisor UI |

### 8.4 Disaggregation-specific

| ID | Check | Command / request | Expected result | Automatable |
|---|---|---|---|---|
| ST-20 | Topology initialized | status payload / API | topology candidate state is populated, not empty | Yes |
| ST-21 | Model initialized | status payload / API | the disaggregation model is initialized and reporting | Yes |
| ST-22 | End-to-end inference | allow a full inference cycle to complete | an inference result is produced and stored | Partly — needs elapsed time (cadence §9.1) |
| ST-23 | Reconciliation identity | evaluate the reconciliation identity against the produced result | `unmonitored_w + meter_disagreement_w == unknown_residual_w` (`engine/wattmosaic_engine/models/residual_power.py:8`, `engine/wattmosaic_engine/contracts.py:1712-1719`) | Yes |
| ST-24 | Audit pass counters | status payload | audit pass counters advancing (`engine/wattmosaic_engine/disaggregation/audit.py:340-380`, `:1195-1235`) | Yes |

**Rule.** ST-23 is the single strongest correctness signal for this capability. A failure there means the residual accounting does not close, and the deployment must be rolled back or the feature disabled — not shipped "with a known issue".

### 8.5 Frontend, logs and configuration

| ID | Check | Command / request | Expected result | Automatable |
|---|---|---|---|---|
| ST-25 | Frontend loads | open the App panel through ingress | the panel renders (`addon/config.yaml:55-57` `panel_admin: true`) | No — manual |
| ST-26 | Frontend navigation | click through the panel's routes | navigation works, no console errors | No — manual |
| ST-27 | Log scan for errors | scan the Supervisor / container logs for the restart window | no `ERROR`/`CRITICAL` entries attributable to the deployment | Yes |
| ST-28 | Startup warnings | inspect startup log | no credential, token or path warnings (`addon/run.sh:277-302` redaction means a token never appears) | Yes |
| ST-29 | Config redaction | run the config print path | tokens appear as `***set(N chars)` (`addon/run.sh:277-302`), never in clear | Yes |
| ST-30 | Addon validation | `scripts/validate_addon.sh --no-build` | exit 0; the three-place rule holds (`scripts/validate_addon.sh:356-408`) | Yes |

### 8.6 Automatable vs manual summary

**Fully automatable** (23): ST-01, ST-02, ST-03, ST-04, ST-05, ST-06, ST-07, ST-08, ST-09, ST-10, ST-11, ST-12, ST-13, ST-17, ST-18, ST-20, ST-21, ST-23, ST-24, ST-27, ST-28, ST-29, ST-30.
**Time-dependent** (3): ST-15, ST-16, ST-22.
**Manual** (4): ST-14 (partly), ST-19 (partly), ST-25, ST-26.

**Rule.** At least one manual check (ST-25 or ST-26) requires a human with a browser. An automated pipeline may execute the automatable set only; it must not report "smoke tests passed" without naming which checks ran.

---

## 9. Rollback procedure

### 9.1 Ordered steps

| # | Step | Command / action | Where | Expected |
|---|---|---|---|---|
| R-1 | Decide | compare the failure against §8 and §10; a reconciliation-identity failure (ST-23) or a migration failure is a rollback trigger | Operator | decision recorded |
| R-2 | Stop the failing service | Supervisor: stop the WattMosaic add-on; Compose: `docker compose stop engine` | Supervisor / Host | engine stopped; no further writes to the database |
| R-3 | Preserve the failing state | copy the database and its `-wal`/`-shm` sidecars to a separate directory before restoring | Host | failing-state snapshot exists for diagnosis |
| R-4 | Restore the previous image / version | Supervisor: install the previous App version; Compose: set `WM_IMAGE_TAG` to the previous tag (`docker-compose.yml:20`) and `docker compose up -d` | Supervisor / Host | previous version running |
| R-5 | Restore the database — **only if a migration was applied** | see §9.3 | Host | `schema_version` matches the previous binary's expectation |
| R-6 | Verify the restore | `PRAGMA integrity_check`; compare `stats().schema_version` with `LATEST_VERSION` (`engine/wattmosaic_engine/storage/migrations.py:792`); `ping()` | Host | `ok`; versions compared; `ping()` True |
| R-7 | Verify the service | `GET /healthz` (gateway and engine) | Supervisor / Host | HTTP 200 (`addon/gateway.py:398-408`) |
| R-8 | Verify data | confirm measurements and inference results from before the deployment are present | Host | expected rows present |
| R-9 | Record | write the failure, the trigger and the restored version into the deployment log | Operator | record complete |

### 9.2 Rollback without a migration

If the deployment applied **no** schema change, the previous binary can read the database as it stands. Restore the previous version (R-4), start it, and run the §8 health subset (ST-01 … ST-08). Restoring the database is unnecessary and would discard data written since the deployment; do not do it.

### 9.3 Rollback with a migration — the hard case

**Rule.** A schema downgrade is **not supported in place**. `docs/deployment.md:358-381` states this for the existing schema history, and the same rule holds for any migration the disaggregation capability introduces. The previous binary has no downgrade migration and will not be able to write a database whose schema it does not understand; at best it will refuse to start, at worst it will misinterpret columns.

Therefore, when the deployed release included a migration:

| Step | Action | Rationale |
|---|---|---|
| M-1 | Stop the engine before restoring | No writes may occur against the migrated schema (`engine/wattmosaic_engine/storage/database.py:90` runs migrations at start; a running engine would re-migrate) |
| M-2 | Restore the **pre-deployment** backup | The backup from P-8/D-5 is the only copy with the old schema |
| M-3 | Verify the restored schema version | it must equal the previous binary's `LATEST_VERSION` (`engine/wattmosaic_engine/storage/migrations.py:792`) |
| M-4 | Start the previous version | it must come up without attempting any migration |
| M-5 | Verify | §9.1 R-6 … R-8 |

**Rule.** The supported rollback for a migrated database is **restore-from-backup**, not in-place downgrade. There is no alternative and this document does not describe one.

**Rule.** A destructive migration must not be shipped without a tested restore path. "Tested" means: the pre-deployment backup has been restored onto a scratch copy and the previous binary has been started against it successfully. A migration whose rollback has never been rehearsed is not releasable.

**Rule.** Because the restore discards everything written after the backup, the backup must be taken as close to the deployment moment as possible (P-8) and its restore must be rehearsed (D-6). The window of lost data is the time between the backup and the restore.

### 9.4 Rollback triggers

| Trigger | Signal | Section |
|---|---|---|
| Reconciliation identity does not close | ST-23 fails | §8.4 |
| Migration failed or the schema version is ahead of the binary | §5.3 | §5.3 |
| Engine fails to start | `/data` write test fails (`addon/run.sh:346-354`) or the process exits | §4.5 |
| Sustained restart loop | component restart counts climb (`engine/wattmosaic_engine/main.py:2995-3015`) | §10 |
| Ingestion stalled | no new measurements while readers report healthy | §10.1 |
| Unbounded growth | DB/WAL size grows without bound | §10.4 |
| Data loss suspected | reconciliation counts do not match the underlying meter totals | §10.2 |

**Rule.** Roll back on a correctness failure, not on a cosmetic one. A cosmetic issue is recorded and fixed forward.

---

## 10. Post-deployment monitoring

### 10.1 First hours

| Signal | Where | Healthy | Unhealthy |
|---|---|---|---|
| Readers' ingestion rate | measurement store / status payload | steady rate consistent with the configured reader cadence | rate drops to zero while readers report online |
| Writer queue depth | status payload (`engine/wattmosaic_engine/main.py:1500-1552`) | returns to zero after each burst | grows monotonically |
| `unlinked_events_ignored` | status payload (`engine/wattmosaic_engine/main.py:1500-1552`) | stable or slowly varying | climbs rapidly after a deployment |
| Inference cycle count | status payload (`engine/wattmosaic_engine/main.py:1500-1552`) | increasing | flat |
| `last_successful_processing_at` | status payload (`engine/wattmosaic_engine/main.py:3014`) | advancing | frozen |
| Component restart counts | `runtime_stats` (`engine/wattmosaic_engine/main.py:2995-3015`) | zero | non-zero and rising |
| Runtime error list | `runtime_stats` (`engine/wattmosaic_engine/main.py:2995-3015`) | empty | entries present |
| DB and WAL size | host filesystem | stable between checkpoints | grows without bound |

### 10.2 First days

| Signal | Where | Why it matters |
|---|---|---|
| Reconciliation identity | inference result | `unmonitored_w + meter_disagreement_w == unknown_residual_w` (`engine/wattmosaic_engine/models/residual_power.py:8`, `engine/wattmosaic_engine/contracts.py:1712-1719`) — the residual accounting must close continuously, not only at deployment time |
| Residual baseline | inference result | a baseline that drifts steadily indicates the topology or the model has settled wrongly |
| Topology candidate/confirmation state | status payload / API | a topology stuck in candidate state means the disaggregation never became usable |
| Pending-transition deque | `engine/wattmosaic_engine/main.py:599` (deque, `maxlen=4096`) | bounded by design; a rise toward the bound means transitions are being produced faster than they are consumed |
| Audit pass counters | `engine/wattmosaic_engine/disaggregation/audit.py:340-380`, `:1195-1235` | advancing counters show the audit passes are actually running |
| `latest_inference_results` scan cost | `engine/wattmosaic_engine/storage/database.py:586-602` | unbounded scan — see §11 |

### 10.3 Monitoring method

| Method | Source | Use |
|---|---|---|
| `/healthz` | `addon/gateway.py:398-408` | liveness and readiness |
| `/metrics` | Prometheus endpoint (`engine/wattmosaic_engine/main.py:2940-2975`) | rate and counter series; it does not replace the status payload, which is where the disaggregation counters live (`engine/wattmosaic_engine/main.py:1500-1552`) |
| `runtime_stats` | `engine/wattmosaic_engine/main.py:2995-3015` | restarts and errors |
| Log scan | Supervisor / container logs | error lines and startup warnings |

### 10.4 Growth

**Rule.** Watch the database file and its `-wal` sidecar size daily for the first week after a migration. WAL grows between checkpoints (`engine/wattmosaic_engine/storage/database.py:169`); unbounded WAL growth is a checkpoint problem, and unbounded database growth is an audit-retention problem (§11).

---

## 11. Known operational risks

| ID | Risk | Evidence | Mitigation |
|---|---|---|---|
| R-01 | **Single-writer SQLite.** One engine owns the database; a second writer contends. `busy_timeout=10000` (`engine/wattmosaic_engine/storage/database.py:92-99`) absorbs short contention, not sustained contention | `engine/wattmosaic_engine/storage/database.py:92-99` | Never run two engines against one database. One App instance, one Compose `engine` service. A contention spike shows up as writer queue growth (§10.1) |
| R-02 | **Cadence mismatch.** Data arrives on a 15.7 s cadence while inference ticks every 5 s | `docs/algorithms.md:75-95` | An inference tick can legitimately find no new data. A flat cycle count with no error is not a fault; a flat `last_successful_processing_at` is (§10.1) |
| R-03 | **Push-on-change HA behaviour.** An idle reader may never publish, so no data is sent at all — silence is not a fault | Home Assistant push model; `addon/config.yaml:69` `homeassistant_api: true` | Do not treat "no new readings" as a broken deployment without first checking reader activity in HA |
| R-04 | **`two_second_assumption_supported = false`** is a standing finding of the algorithm, not a deployment defect | `docs/algorithms.md:75-95` | The deployment does not change it. Do not use sub-2 s residual behaviour as a release gate |
| R-05 | **Audit tables grow without a horizon.** The audit passes write continuously and nothing prunes them | `engine/wattmosaic_engine/disaggregation/audit.py:340-380`, `:1195-1235` | Track DB size (§10.4). Retention policy is an open decision — see `G-DEPLOY-03` |
| R-06 | **`latest_inference_results()` performs an unbounded scan** | `engine/wattmosaic_engine/storage/database.py:586-602` | Query latency grows with history. Avoid aggressive polling of that path from monitoring; a monitoring script that calls it every 5 s makes R-06 worse than the product does |
| R-07 | **Stale-tag guard.** The release refuses to overwrite an existing image tag | `.github/workflows/release.yaml:222-232`, `:234-285` | A repeated release attempt on the same version fails by design. Bump the version (§3.4); do not force it |
| R-08 | **Prerelease on every push to `main`.** Each push to `main` publishes a prerelease and `--latest=false` | `.github/workflows/release.yaml:44-52`, `:387-408` | Prereleases are the normal channel for this project. A version appearing on the Releases page does not mean it is the stable release; confirm the channel before promoting |
| R-09 | **Downgrade is not supported in place** | `docs/deployment.md:358-381` | The only supported rollback for a migrated database is restore-from-backup (§9.3) |
| R-10 | **The system is read-only against Home Assistant.** `homeassistant_api: true`, `hassio_api: false` | `addon/config.yaml:69`, `addon/config.yaml:70` | A deployment cannot break the home's devices, because it cannot write to them. The blast radius of a bad deployment is confined to the disaggregation outputs |
| R-11 | **The App offers updates only on a version change** | `addon/config.yaml:23`, `:38` | A code change without a version bump ships an image nobody is offered (§3.1) |
| R-12 | **Three-place rule violations** | `addon/run.sh:36-37`, `scripts/validate_addon.sh:356-408` | `scripts/validate_addon.sh --no-build` must pass before release (P-3). A missing option surfaces as a runtime default, not as a build error |

---

## 12. Deployment checklist

| Phase | # | Command | Expected | Failure action |
|---|---|---|---|---|
| **Pre-flight** | 1 | `git status --short` | empty | Commit or stash; do not deploy a dirty tree |
| | 2 | `python tools/dev/verify.py` | 12 required checks pass (`tools/dev/verify.py:105-124`) | Fix and re-run |
| | 3 | `scripts/validate_addon.sh --no-build` | exit 0 (`scripts/validate_addon.sh:356-408`) | Fix the three-place rule violation |
| | 4 | `python tools/dev/export_schemas.py` then `git diff --exit-code data/schemas` | empty diff (`.github/workflows/ci.yml:26-29`) | Regenerate and commit the schemas |
| | 5 | `python tools/dev/check_release.py --channel main` | exit 0 (`tools/dev/check_release.py:407-457`) | Fix the version sites |
| | 6 | CI run for the commit | all six jobs green (`.github/workflows/ci.yml:9`, `:37`, `:61`, `:78`, `:102`, `:117`) | Fix and re-run CI |
| | 7 | Cold backup / volume backup (§6.2) | backup artefact exists | Do not deploy |
| | 8 | Restore the backup to scratch and run `PRAGMA integrity_check` | `ok` (`engine/wattmosaic_engine/storage/database.py:151`) | Investigate the backup procedure |
| | 9 | Record previous version, previous image tag and backup location | all three recorded | Do not deploy |
| **Deploy** | 10 | `git push origin main` | push accepted | Resolve the push failure |
| | 11 | Watch the `release.yaml` run | `prepare`, `image`, `publish`, `prune` succeed (`.github/workflows/release.yaml:66`, `:196`, `:312`, `:420`) | Investigate the failed job; the stale-tag guard (R-07) requires a version bump, not a retry |
| | 12 | Supervisor: stop the add-on | stopped | Investigate |
| | 13 | Supervisor: update the add-on | the new version is offered and installed (`addon/config.yaml:23`, `:38`) | Check that the version advanced (§3.1) |
| | 14 | Supervisor: start the add-on | starts within 30 s (`addon/config.yaml:106`) | Check the `/data` write test (`addon/run.sh:346-354`) |
| **Smoke test** | 15 | `GET /healthz` (gateway + engine) | HTTP 200 (`addon/gateway.py:398-408`) | Roll back (§9.1 R-2) |
| | 16 | `GET /api/v1/...` | routes respond (`engine/wattmosaic_engine/api/server.py:760-784`) | Roll back |
| | 17 | Status payload | workers present (`engine/wattmosaic_engine/main.py:3169-3173`), restarts zero (`engine/wattmosaic_engine/main.py:2995-3015`) | Roll back |
| | 18 | Status payload | `schema_version == latest_schema_version` (`engine/wattmosaic_engine/storage/database.py:1365-1379`) | Roll back and restore (§9.3) |
| | 19 | Reconciliation identity | `unmonitored_w + meter_disagreement_w == unknown_residual_w` (`engine/wattmosaic_engine/models/residual_power.py:8`) | Roll back (§9.4) |
| | 20 | Log scan | no new `ERROR`/`CRITICAL` | Investigate; roll back if attributable to the deployment |
| | 21 | Frontend panel (manual) | renders and navigates | Fix forward if cosmetic; roll back if the panel is unusable |
| **Post-deploy** | 22 | `/metrics` scrape (`engine/wattmosaic_engine/main.py:2940-2975`) | series present | Investigate |
| | 23 | Status payload after one inference cycle | cycle count increasing; `last_successful_processing_at` advancing (`engine/wattmosaic_engine/main.py:3014`) | Investigate; roll back if stalled with no error |
| | 24 | Audit counters | advancing (`engine/wattmosaic_engine/disaggregation/audit.py:340-380`, `:1195-1235`) | Investigate |
| | 25 | DB and WAL size after 24 h | stable | Investigate growth (R-05, R-06) |
| | 26 | Record the deployed version and the backup location | recorded | Complete the record |

---

## 13. Gaps and open decisions

### 13.1 Locally-scoped gap IDs

**Rule.** The register of record for gap identifiers is `00-project-overview.md` §13.2, the single source of truth for them; `00-project-overview.md` §13.1 is the upstream source register for the `G21`–`G32` band. This document uses locally-scoped IDs only and mints no `G2x` IDs, because `03-…` has already collided on `G21`–`G24`. `G-DEPLOY-01` … `G-DEPLOY-05` are **registered** in the canonical gap register at `00-project-overview.md` §13.2; a locally-scoped ID is valid only together with its entry there. The table below is the upstream source for the gaps it lists.

| Local ID | Gap | Status | Proposed resolution |
|---|---|---|---|
| `G-DEPLOY-01` | No backup cadence or retention policy is defined anywhere in the repository | MISSING | Adopt the proposal in §6.3 (OD-99) |
| `G-DEPLOY-02` | The release's stale-tag guard means a re-release of the same version is impossible; the operator needs guidance on what to do when a release attempt is refused | PARTIAL — the guard exists (`.github/workflows/release.yaml:222-232`) but the operator remedy is undocumented here only | Documented in §3.3 and R-07: bump the version; never force |
| `G-DEPLOY-03` | The audit tables have no retention horizon, so the database grows without bound over long deployments | MISSING | Decide an audit-retention policy (§11 R-05); out of scope here |
| `G-DEPLOY-04` | `latest_inference_results()` scans without bound, so its latency grows with history | EXISTS (behaviour) — no index or pagination exists | Out of scope here; flag for the storage design |
| `G-DEPLOY-05` | There is no operator-facing rollback runbook for a migrated database; `docs/deployment.md:358-381` states only the constraint | MISSING | §9.3 of this document proposes the runbook; the orchestrator decides whether to fold it into `docs/deployment.md` |

### 13.2 Open decisions

`00-project-overview.md` holds `OD-01`…`OD-08`. This document's own decisions occupy the **`OD-99`–`OD-103`** band allocated to it by the canonical register in `13-open-decisions-and-assumptions.md` §5.2.

| ID | Decision | Assumption proceeded on | Consequence if wrong |
|---|---|---|---|
| `OD-99` | Backup cadence and retention period | The proposal in §6.3 is adopted: daily + pre-upgrade, 7 daily / 4 weekly / 6 monthly, off-host, with a monthly restore drill | The pre-upgrade backup — the one rollback depends on — may not exist when it is needed |
| `OD-100` | Whether the disaggregation capability ships in the same image as the current engine | **Assumption: yes.** One artefact, one image, no new packaging format, per `REQ-DEPLOY-001` (`addon/config.yaml:38` tagless image + `addon/config.yaml:23` version; `docker-compose.yml:20` single image tag) | If a separate image is required, `addon/config.yaml:38` and `docker-compose.yml:20` need a second reference, the release `image` job needs a second push target, and `G-DEPLOY-02` widens |
| `OD-101` | Whether the pre-migration backup is a cold Supervisor backup or an online copy of the DB plus `-wal`/`-shm` | Online copy of the DB plus its sidecars for Shape B; cold Supervisor backup for Shape A (`addon/config.yaml:105`) | An online copy taken incorrectly (missing `-wal`) produces a torn snapshot that fails the restore verification in §6.4 — which is exactly why the verification is mandatory |
| `OD-102` | Whether audit-table retention is added to the engine or handled operationally (external pruning) | Handled operationally for now; no engine change is in scope | Long-lived deployments grow the database without bound (§11 R-05) |
| `OD-103` | Whether the smoke-test automatable set (ST-01 … ST-24, ST-27 … ST-30) is wired into CI | Not wired; CI runs the six existing jobs (`.github/workflows/ci.yml:9`, `:37`, `:61`, `:78`, `:102`, `:117`) only | An operator-only smoke test is run by hand each time; the manual frontend checks stay manual |

---

## 14. Related documents

- [00 — Project overview](./00-project-overview.md)
- [01 — Requirements](./01-requirements.md)
- [02 — Architecture](./02-architecture.md)
- [03 — Measurement and electrical domain](./03-measurement-and-electrical-domain.md)
- [06 — Device and load model spec](./06-device-and-load-model-spec.md)
- [10 — UI and user-experience spec](./10-ui-and-user-experience-spec.md)
- [Deployment guide](../../deployment.md)
- [Installation guide](../../installation.md)
- [Testing guide](../../testing.md)
- [Risks register](../../risks.md)
- [Limitations](../../limitations_v2.md)
- [Algorithms](../../algorithms.md)
