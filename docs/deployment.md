# Deployment

Operational guide for running the WattMosaic Engine. Three supported shapes, all
using the same image contents and the same database migrations:

| Shape | Use when | Section |
| --- | --- | --- |
| **A. Home Assistant OS App** | HA OS, recommended | [A](#a-home-assistant-os-app) |
| **B. docker compose** | Home Assistant Container, or a separate Docker host | [B](#b-docker-compose) |
| **C. Plain `docker run`** | Minimal hosts, orchestration elsewhere | [C](#c-plain-docker-run) |

User-facing install steps belong in [`installation.md`](installation.md); this
document covers container internals, storage, health, upgrade and recovery.
The engine speaks **contract 3.0.0**: the engine and integration must be upgraded
together, migrations are forward-only, and there are no down-migrations (DEC-02).

---

## 1. What runs inside the container

```text
  :8787  gateway.py            aiohttp reverse proxy
             ├── /healthz        unauthenticated liveness (no data)
             ├── /              panel for Supervisor ingress, JSON otherwise
             ├── /ui/{asset}     panel stylesheet, script and mark (allowlisted)
             └── /api/v1/*       proxied, X-API-Token injected for ingress peers
  :8788  engine (loopback)     WattMosaic /api/v1 (contract 3.0.0), token-protected
             ├── Home Assistant WebSocket ingestion (read-only)
             ├── three-loop runtime: fast / event / background
             ├── joint delta + absolute inference, groups, residual, matrix
             └── SQLite (WAL) at WM_DATABASE_PATH
  /data  persistent volume     database, WAL, -shm, pre-migration backups
  /app   code                  run.sh, gateway.py, healthcheck.py, ui/
  /opt/venv                    engine + pinned dependencies, no build tools
```

Why two ports? The engine's API is token-protected. The Supervisor watchdog
cannot send custom headers, and an ingress user should not have to paste a token.
The gateway solves both while keeping the token requirement on the actual API.
Direct (non-ingress) callers still need `X-API-Token`.

Process model:

- `run.sh` is the ENTRYPOINT and starts as root **only** to fix `/data`
  ownership, then `exec`s the engine as UID 10001 with `no_new_privs` and all
  capabilities dropped (via `setpriv`).
- The engine is PID 1, so `docker stop` / Supervisor stop delivers `SIGTERM`
  directly: SQLite checkpoints the WAL and the HTTP server closes cleanly.
- The gateway is a supervised child process; it exits if the engine has been
  unreachable, and the runtime tears it down with the engine.

Set `WM_DROP_PRIVILEGES=false` only when the data volume is already owned by
UID 10001 and you want the whole container to run unprivileged.

---

## 2. Configuration

Configuration is entirely environment-driven. On Home Assistant OS the App
options UI is mapped to these variables by `addon/run.sh`.

### 2.1 Precedence

```text
/data/options.json (App options)  >  container environment  >  built-in default
```

An explicitly set environment variable is never silently overwritten — except by
an App option, which is the user's own configured intent.

### 2.2 Variables the engine reads

Source of truth: `engine/wattmosaic_engine/config/settings.py`.

| Variable | Default | Notes |
| --- | --- | --- |
| `WM_HA_URL` (alias `HA_URL`, `HA_HOST`) | — | `http://host:8123`; a bare host gets `http://` and `:8123`. |
| `WM_HA_TOKEN` (alias `HA_TOKEN`) | — | Long-lived **user** token. Falls back to `SUPERVISOR_TOKEN` in App mode. |
| `WM_HA_VERIFY_SSL` | `true` | Only disable for a trusted self-signed local instance. |
| `WM_API_HOST` | `127.0.0.1` (image) | Keep on loopback so only the gateway is reachable. |
| `WM_API_PORT` | `8788` (image) | Engine port. Must differ from the gateway port. |
| `WM_API_TOKEN` | *(empty)* | When set, every `/api/v1` request needs `X-API-Token`. |
| `WM_DATABASE_PATH` | `/data/wattmosaic.db` | Must be inside the writable volume. |
| `WM_RETENTION_DAYS_RAW` | `30` | Raw retention; must exceed your longest training horizon. |
| `WM_RETENTION_DAYS_AGGREGATES` | `730` | Aggregate retention. |
| `WM_LEARNING_ENABLED` | `true` | Inference continues when false. |
| `WM_STALE_AFTER_S` | `120` | Staleness threshold; typical CT cadence ~15.7 s. |
| `WM_INFERENCE_INTERVAL_S` | `5` | **Deprecated and ignored** (accepted with a startup warning). Inference runs per event group, not on a timer. |
| `WM_EVENT_QUEUE_MAX` | `1000` | Max ready event groups; overflow drops oldest. |
| `WM_STEADY_HEARTBEAT_S` | `60` | Staleness-only heartbeat period; never a full solve. |
| `WM_UNKNOWN_PENALTY_SCALE` | `1.0` | Unknown-load penalty multiplier in the joint solve. |
| `WM_QUEUE_MAX_SIZE` | `5000` | Bounded ingestion queue. |
| `WM_LOG_LEVEL` | `INFO` | Normalised to upper case by `run.sh`. |
| `WM_LOG_JSON` | `true` | `false` for human-readable logs. |

### 2.3 Container-only variables

| Variable | Default | Notes |
| --- | --- | --- |
| `WM_GATEWAY_ENABLED` | `true` | Set `false` to expose the engine API directly (then `WM_API_PORT` must be 8787 and published). |
| `WM_GATEWAY_PORT` | `8787` | Must match `ingress_port` in `addon/config.yaml`. |
| `WM_GATEWAY_BIND` | `0.0.0.0` | Inside the container only. |
| `WM_GATEWAY_TRUSTED_INGRESS_IPS` | `172.30.32.2` | Supervisor address(es), comma-separated. |
| `WM_DROP_PRIVILEGES` | `true` | `run.sh` drops from root to UID 10001. |
| `WM_OPTIONS_FILE` | `/data/options.json` | Override for testing only. |

`HA_URL`/`HA_HOST` are accepted so a `.env` copied from `.env.example` works
unchanged.

### 2.4 Token strategy on Home Assistant OS

| `ha_token` option | `homeassistant_api` | Effective URL | Effective token |
| --- | --- | --- | --- |
| set | any | `ha_url` | your user token |
| empty | `true` | `http://supervisor/core` | `SUPERVISOR_TOKEN` |
| empty | `false` | `ha_url` | — (start refuses) |

`SUPERVISOR_TOKEN` only works against the Supervisor proxy, which is why
`run.sh` rewrites the URL when it falls back. A manually pasted user token in
`ha_token` is the least-privilege option: create a dedicated read-only HA user and
a long-lived token for it.

---

## 3. A. Home Assistant OS App

### 3.1 Build context (important)

There are two supported ways to get the App onto a box, and they use different
Docker build contexts. Mixing them up is the usual cause of a failed install.

**1. App store (default).** `repository.yaml` registers the repository; the
store finds the app folder `addon/` by scanning for `config.yaml`.
`addon/config.yaml` sets `image: "ghcr.io/asxzy/wattmosaic-engine"`, so the
Supervisor **pulls** the multi-arch image published by
`.github/workflows/release.yaml`. Nothing is built on the HA host.

**2. Local copy (offline / no GHCR).** The Supervisor's Docker build context is
the app folder itself, so a locally built app must contain the engine sources.
Stage that folder first:

```bash
scripts/package_addon.sh                 # -> dist/addon/wattmosaic_engine/
scripts/package_addon.sh --install root@homeassistant
```

Staging **removes** the `image:` line, because a self-contained folder must be
built locally rather than pull a prebuilt image. Copying `addon/` on its own does
not work: it has no `engine/`, so a local build fails at `COPY engine ./engine`.

Then **Settings → Apps → App store → ⋮ → Check for updates** → *WattMosaic
Engine* → **Install**.

### 3.2 Manifest decisions

`addon/config.yaml` is deliberately explicit: `image` is the tagless prebuilt
multi-arch image, `init: false` (run.sh is the init), `startup: application`,
`boot: auto`, `ingress: true` port 8787, `host_network: false`,
`homeassistant_api: true`, `map: data:rw, share:rw`, `backup: cold` (so the SQLite
WAL is copied consistently), `watchdog` on `/healthz`, and `ports: 8787/tcp: null`.

### 3.3 Integration endpoint

From the Home Assistant integration, use the container host name
`http://<hostname>:8787`. The Supervisor names an installed app
`{repository}_{slug}` and registers that hostname with `_` replaced by `-`
(e.g. `f48de52e-wattmosaic-engine` or `local-wattmosaic-engine`). The app's own
page shows **Hostname** under its info tab — that value is authoritative. Do not
use `127.0.0.1` or the host IP: the app's port is not published.

### 3.4 Storage

- `/data` survives app updates, restarts and reboots, and is included in HA
  backups (cold).
- `/share` is available read-write for manual exports, e.g.
  `/api/v1/export/{device_id}` piped to `/share/wattmosaic/`.

---

## 4. B. docker compose

```bash
cp .env.example .env
$EDITOR .env          # HA_URL (or HA_HOST), HA_TOKEN, WM_API_TOKEN
docker compose up -d --build
docker compose logs -f wattmosaic
```

`.env` is the only place secrets live; it is git-ignored and excluded from the
image build context. The compose file applies `restart: unless-stopped`,
`init: true`, `stop_grace_period: 30s`, `read_only: true` root filesystem with a
single writable `/data` volume, `cap_drop: ALL`, `no-new-privileges`, a `tmpfs`
`/tmp`, the same `/app/healthcheck.py` healthcheck, a 1 GB memory limit and
bounded JSON logs. Only port `8787` is published; the engine's `8788` stays on
container loopback.

```bash
curl -s http://localhost:8787/healthz                       # ok
curl -s -H "X-API-Token: $WM_API_TOKEN" \
     http://localhost:8787/api/v1/health | jq
```

### Updating

```bash
git pull
docker compose build --pull
docker compose up -d
docker compose ps          # wait for "healthy"
```

Migrations run automatically at start, forward-only; an automatic pre-migration
backup is taken first (§7).

---

## 5. C. Plain docker run

```bash
docker build -t wattmosaic-engine:<version> .
docker run -d --name wattmosaic \
  --restart unless-stopped \
  --env-file .env \
  -e WM_DATABASE_PATH=/data/wattmosaic.db \
  -v wattmosaic-data:/data \
  -p 127.0.0.1:8787:8787 \
  --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges \
  wattmosaic-engine:<version>
```

Binding to `127.0.0.1` is the safe default; put a TLS reverse proxy in front for
remote access.

---

## 6. Health checks

Three independent signals:

| Check | Endpoint | Auth | Healthy when |
| --- | --- | --- | --- |
| Gateway liveness | `GET /healthz` | none | `200 ok`; it reached `/api/v1/health` |
| Engine health | `GET /api/v1/health` | `X-API-Token` | `status` is `ok` or `degraded`, `contract_version` is `3.0.0` |
| Docker HEALTHCHECK | `/app/healthcheck.py` (both) | token from env | exit 0 |

`degraded` is intentionally treated as healthy by the container healthcheck: it
usually means HA is reconnecting or the runtime is not yet settled, and
restarting would not fix either. `error` and hard failures fail the check.
Supervisor watchdog uses `/healthz` only, because it cannot send headers.

Contract compatibility is visible on every response:

```bash
curl -sI -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/health \
  | grep -i x-wattmosaic-contract-version    # X-WattMosaic-Contract-Version: 3.0.0
```

---

## 7. Persistence, backup and rollback

### What must persist

Only `/data` — the SQLite database (`wattmosaic.db`, `-wal`, `-shm`) and the
pre-migration backups. Models, learning observations, topology/mapping/model
versions, estimates and energy aggregates all live in that database.

### Automatic pre-migration backup (DEC-02)

Before applying any migration numbered **13 or above**, the engine writes a
backup file beside the database **before the first destructive statement**:

```text
/data/wattmosaic.db.pre-v025.<UTC-timestamp>.bak
```

The file is made after a WAL checkpoint and includes any surviving `-wal`/`-shm`
sidecars, so it is self-contained. The newest three backups are kept (older ones
are rotated away). A fresh or unversioned database produces no backup — there is
nothing to roll back to. **If a backup copy fails, startup aborts**: running a
destructive migration without a rollback point is worse than not starting.

### Manual backup

**Consistent, while running** (SQLite online backup):

```bash
docker exec app_local_wattmosaic_engine \
  python -c "import sqlite3;s=sqlite3.connect('/data/wattmosaic.db');d=sqlite3.connect('/data/wbm.db');s.backup(d)"
docker cp app_local_wattmosaic_engine:/data/wbm.db ./wattmosaic-$(date +%F).db

# compose / docker run
docker compose exec engine \
  python -c "import sqlite3;s=sqlite3.connect('/data/wattmosaic.db');d=sqlite3.connect('/data/wbm.db');s.backup(d)"
docker compose cp engine:/data/wbm.db ./wattmosaic-$(date +%F).db
```

**Cold copy**: stop the container, copy the volume, start it. This is what the
Supervisor does with `backup: cold`.

### Rollback (DEC-02)

There are **no down-migrations**. Rollback is: reinstall the previous engine
(and integration) version, then restore the matching backup.

1. Stop the container/app.
2. Replace `/data/wattmosaic.db` with the pre-migration backup
   (`wattmosaic.db.pre-vNNN.<timestamp>.bak`) and remove any stale
   `wattmosaic.db-wal` / `wattmosaic.db-shm` (they belong to the new file).
3. Start the **previous** engine version. The engine runs `PRAGMA
   integrity_check` and refuses corrupt data rather than silently discarding it.
4. Verify with `/api/v1/status` and contract `3.0.0` / the previous contract on
   `/api/v1/health`, depending on the version restored.

Never run an older engine against a newer-schema database, and never restore a
newer database into an older engine: migrations are forward-only. If a migration
fails mid-apply, `user_version` is left unchanged and the database is still usable
by the previous version; restore the backup before retrying.

### Legacy tables

Migration v025 renames legacy tables to `<name>_legacy` and makes them read-only
with triggers. No table is dropped, so historical data remains available for one
release. Migrations 1–24 read the legacy table and column names as SQL data while
upgrading historical databases; those strings are not live code paths.

### Retention

`WM_RETENTION_DAYS_RAW` bounds L1 growth; L2 evidence and L4 version history are
operator-bounded (unset by default, so retained). Pruning never deletes model
versions or versioned evidence that a provenance reference may still cite.

```bash
docker exec app_local_wattmosaic_engine ls -lh /data
```

---

## 8. Upgrades

### App

1. Re-run `scripts/package_addon.sh --install …` (or copy the new folder).
2. **Check for updates** → **Update**. The `version` in `config.yaml` is the
   update trigger; bump it for every release.
3. Migrations run at start (with automatic backup). Watch the log for
   `database_ready schema_version=N` and the `pre-migration backup ... written`
   line.
4. Rollback: re-install the previous version **and restore the matching database
   backup** (§7). Do not downgrade the DB in place.

### Docker

```bash
docker compose build --pull
docker compose up -d
```

Tag images by version and keep the previous tag for rollback.

### Version compatibility

The engine reports `contract_version` `3.0.0` on every response. Contract 3.0 is
a **breaking** cutover: engine and integration must be upgraded together. An
integration that refuses a contract mismatch is behaving correctly. Removed at
the cutover: the legacy contract models, `/mapping` (now `/matrix`), v2 forms,
and the legacy HA topology-confirm endpoint.

### Releasing

A release is a **git tag** plus a **GitHub Release**. Home Assistant and the
Marketplace resolve the displayed version from those, not a file in the tree;
with neither they show a raw commit SHA. `check_release.py` verifies the machine-
enforceable half: all five version fields agree, `RELEASE_NOTES.md` documents the
current version, and a suffix tag is not published as stable. Run it before
tagging:

```bash
python tools/dev/check_release.py
```

Version channels: `main` carries `X.Y.Z-dev.N` prereleases (the Marketplace
ignores prereleases unless beta is enabled, so stable users are unaffected);
`stable` carries plain `X.Y.Z`. The release workflow bumps the counter in all
five version files, builds the multi-arch image, tags the commit and publishes
the Release, and prunes old development Releases (never stable ones). Keep the
prerelease counter a separate numeric identifier (`-dev.2`, not `-dev2`). Tags
are immutable: to correct a release, delete and recreate it deliberately.

The five version fields: `custom_components/wattmosaic/manifest.json` (`version`,
canonical), `custom_components/wattmosaic/const.py` (`VERSION`),
`addon/config.yaml` (`version`), `pyproject.toml` (`version`), and
`engine/wattmosaic_engine/__init__.py` (`__version__`).

---

## 9. Uninstall

1. Remove the integration: **Settings → Devices & Services → WattMosaic → Delete**.
   This removes entities and the config entry only.
2. Stop and uninstall the app (or `docker compose down`).
3. The data volume is intentionally preserved. Delete it explicitly to discard
   history and learned models (App: uninstall with "delete data"; Docker:
   `docker volume rm wattmosaic-data`).

---

## 10. Security notes

| Topic | Measure |
| --- | --- |
| Token handling | Secrets only in App options / env. `run.sh` never echoes them; `EngineConfig.redacted()` keeps them out of logs, diagnostics and exports. |
| API exposure | Engine API on `127.0.0.1:8788`, token-protected. Only `8787` is reachable, and only the gateway. |
| Ingress | Home Assistant authenticates the user; ingress is trusted only from `WM_GATEWAY_TRUSTED_INGRESS_IPS` and only with the Supervisor's `X-Remote-User-Id` header. |
| Panel assets | A fixed allowlist of static files, served only to ingress peers (no path traversal, no external origin, no inline script). All engine-provided strings are inserted as text. |
| `api_token` | Compared with `hmac.compare_digest`; generate with `secrets.token_urlsafe(32)`. |
| Least privilege | All capabilities dropped, `no_new_privs`, engine runs as UID 10001, read-only root filesystem in compose. |
| No actuation | The engine has no service-call code path; it can only read states and registries. |
| Network | Never publish `8787` on `0.0.0.0` without TLS and a token. |

---

## 11. Troubleshooting

See [`docs/troubleshooting.md`](troubleshooting.md) for a symptom-first guide.
Common deployment failures: missing HA token/URL, `WM_API_TOKEN` mismatch (401),
watchdog restarts (`/healthz` 503), corrupt database after a bad restore, and a
contract mismatch between engine and integration.

---

## 12. Reproducible builds

- Base image: `python:3.12-slim-bookworm` (pin a digest via `--build-arg` for a
  byte-reproducible base).
- Dependencies: `addon/constraints.txt`, a full `==` pin set from `pyproject.toml`;
  builds use `--only-binary=:all:` so an accidental source distribution fails
  rather than needing a compiler.
- Build tooling is pinned in the builder stage and never reaches the runtime
  image. The runtime image has no package manager cache, compiler or build
  headers; `pip check` runs at build time.
- `scripts/validate_addon.sh --no-build` runs the static checks, including
  `tools/dev/check_panel.py` (no external origin, no inline script/style, no
  `on*` handler, no `eval`), before a real image is built.

```bash
scripts/validate_addon.sh            # static checks + real docker build
scripts/validate_addon.sh --no-build  # static checks only (CI lint stage)
```

---

## 13. Unified workflow (build → migrate → smoke → observe)

```bash
scripts/run_workflow.sh [BASE_URL]     # defaults to http://127.0.0.1:8787
```

Stages, stopping at the first failure:

| Stage | What it does |
| --- | --- |
| **build** | Stages a self-contained App with `scripts/package_addon.sh --check`. Skip with `WM_SKIP_BUILD=1`. |
| **migrate** | Applies pending forward-only migrations (with automatic backup) and asserts schema `>=` the latest. Skip with `WM_SKIP_MIGRATE=1`. |
| **smoke** | Runs `scripts/smoke_test.sh BASE_URL`; any failed check exits non-zero. |
| **observe** | Polls `/api/v1/health` `WM_OBSERVE_COUNT` times; fails if the engine leaves `ok`/`degraded`. |

### Post-deploy smoke test

`scripts/smoke_test.sh` is distinct from the App watchdog probe (which only polls
`/healthz`): it exercises the whole post-deploy surface and exits NON-ZERO if any
check fails. Every check runs even after a failure, so one invocation reports all
problems.

```bash
scripts/smoke_test.sh [BASE_URL]
```

| Check | Endpoint / source | Pass condition |
| --- | --- | --- |
| `health` | `/api/v1/health` | `status` is `ok` or `degraded` |
| `status` | `/api/v1/status` | JSON with a `status` field |
| `panel` | `/` (gateway ingress) | index looks like the WattMosaic panel |
| `migrations` | `PRAGMA user_version` vs latest (fallback `/api/v1/diagnostics`) | local DB is at the latest schema |
| `residuals` | `/api/v1/residuals` | reachable, JSON object |
| `matrix` | `/api/v1/matrix` | reachable, JSON object (replaced the old `/mapping`) |
| `topology` | `/api/v1/topology/v3` | reachable, JSON object |
| `devices` | `/api/v1/devices` | reachable, JSON list/object |
| `metrics` | `/api/v1/metrics` | scrape contains `wattmosaic_` series |

The script needs only `curl` and the repo venv's python. A `curl` HTTP error
(≥ 400) counts as a FAIL, never a silent success.
