# Deployment

Operational guide for running the WattMosaic Engine. Three supported shapes, all
using the same image contents and the same database migrations:

| Shape | Use when | Section |
| --- | --- | --- |
| **A. Home Assistant OS App** | HA OS, recommended | [A](#a-home-assistant-os-app) |
| **B. docker compose** | Home Assistant Container, or a separate Docker host | [B](#b-docker-compose) |
| **C. Plain `docker run`** | Minimal hosts, orchestration elsewhere | [C](#c-plain-docker-run) |

User-facing install steps with screenshots belong in
[`installation.md`](installation.md); this document covers the container
internals, storage, health, upgrade and recovery procedures.

---

## 1. What runs inside the container

```text
  :8787  gateway.py            aiohttp reverse proxy
            ├── /healthz        unauthenticated liveness (no data)
            ├── /              panel for Supervisor ingress, JSON otherwise
            ├── /ui/{asset}     panel stylesheet, script and mark (allowlisted)
            └── /api/v1/*       proxied, X-API-Token injected for ingress peers
  :8788  engine (loopback)     WattMosaic /api/v1, requires X-API-Token
            ├── Home Assistant WebSocket ingestion (read-only)
            ├── SQLite (WAL) at WM_DATABASE_PATH
            └── inference / learning loops
  /data  persistent volume     database, WAL, -shm
  /app   code                  run.sh, gateway.py, healthcheck.py, ui/
  /opt/venv                    engine + pinned dependencies, no build tools
```

Why two ports? The engine's API is token-protected. Home Assistant's Supervisor
watchdog cannot send custom headers, and an ingress user should not have to paste a
token. The gateway solves both while keeping the token requirement on the actual
API. Direct (non-ingress) callers still need `X-API-Token`.

Process model:

- `run.sh` is the ENTRYPOINT and starts as root **only** to fix `/data`
  ownership, then `exec`s the engine as UID 10001 with `no_new_privs` and all
  capabilities dropped (via `setpriv`).
- The engine is PID 1, so `docker stop` / Supervisor stop delivers `SIGTERM`
  directly to it: SQLite checkpoints the WAL and the HTTP server closes cleanly.
- The gateway is a supervised child process. It exits if the engine has been
  unreachable, and the container runtime tears it down with the engine.

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

An explicitly set environment variable is therefore never silently overwritten —
except by an App option, which is the user's own configured intent.

### 2.2 Variables the engine reads

Source of truth: `engine/wattmosaic_engine/config/settings.py`.

| Variable | Default | Notes |
| --- | --- | --- |
| `WM_HA_URL` (alias `HA_URL`, `HA_HOST`) | — | `http://host:8123`; a bare host gets `http://` and `:8123`. |
| `WM_HA_TOKEN` (alias `HA_TOKEN`, `HA_LONG_LIVED_TOKEN`) | — | Long-lived **user** token. Falls back to `SUPERVISOR_TOKEN` in App mode. |
| `WM_HA_VERIFY_SSL` | `true` | Only disable for a trusted self-signed local instance. |
| `WM_API_HOST` | `127.0.0.1` (image) | Keep on loopback so only the gateway is reachable. |
| `WM_API_PORT` | `8788` (image) | Engine port. Must differ from the gateway port. |
| `WM_API_TOKEN` | *(empty)* | When set, every `/api/v1` request needs `X-API-Token`. |
| `WM_DATABASE_PATH` | `/data/wattmosaic.db` | Must be inside the writable volume. |
| `WM_RETENTION_DAYS_RAW` | `30` | Raw retention; must exceed your longest training horizon. |
| `WM_RETENTION_DAYS_AGGREGATES` | `730` | Aggregate retention. |
| `WM_LEARNING_ENABLED` | `true` | Inference continues when false. |
| `WM_STALE_AFTER_S` | `120` | Staleness threshold; typical CT cadence for a polling multi-channel monitor is ~15.7 s. |
| `WM_INFERENCE_INTERVAL_S` | `5` | Inference cadence. |
| `WM_QUEUE_MAX_SIZE` | `5000` | Bounded queue; overflow drops the oldest items and increments a metric. |
| `WM_LOG_LEVEL` | `INFO` | Normalised to upper case by `run.sh`. |
| `WM_LOG_JSON` | `true` | `false` for human-readable logs. |

### 2.3 Container-only variables

| Variable | Default | Notes |
| --- | --- | --- |
| `WM_GATEWAY_ENABLED` | `true` | Set `false` to expose the engine API directly (then `WM_API_PORT` must be 8787 and you must publish it). |
| `WM_GATEWAY_PORT` | `8787` | Must match `ingress_port` in `addon/config.yaml`. |
| `WM_GATEWAY_BIND` | `0.0.0.0` | Inside the container only. |
| `WM_GATEWAY_TRUSTED_INGRESS_IPS` | `172.30.32.2` | Supervisor address(es), comma-separated. Ingress requests from these peers get the token injected. |
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

**1. App store (default).** `repository.yaml` at the repository root registers
the repository; the store finds the app folder `addon/` by scanning for
`config.yaml`. `addon/config.yaml` sets

```yaml
image: "ghcr.io/asxzy/wattmosaic-engine"
```

so the Supervisor **pulls** `ghcr.io/asxzy/wattmosaic-engine:<version>` — a
multi-arch manifest list for `linux/amd64` and `linux/aarch64`, published by
`.github/workflows/release.yaml`. Nothing is built on the HA host, and the
app folder never needs to be self-contained. The tag is always the `version:`
field in `addon/config.yaml`; bump the version and push, and the workflow
publishes the matching tag.

**2. Local copy (offline / no GHCR).** The Supervisor's Docker build context is
the app folder itself, so a locally built app must contain the engine sources.
Stage that folder first:

```bash
scripts/package_addon.sh                 # -> dist/addon/wattmosaic_engine/
scripts/package_addon.sh --install root@homeassistant
```

The staged directory contains `config.yaml`, `Dockerfile`, `run.sh`,
`gateway.py`, `healthcheck.py`, `constraints.txt`, `icon.png`, `logo.png`,
`README.md`, `pyproject.toml`, `LICENSE` and `engine/`. Staging **removes** the
`image:` line, because a self-contained folder must be built locally rather than
pull a prebuilt image.

Copying `addon/` on its own does not work: it has no `engine/`, so a local build
fails at the `COPY engine ./engine` step.

Then **Settings → Apps → App store → ⋮ → Check for updates** → *WattMosaic
Engine* → **Install**.

### 3.2 Manifest decisions

`addon/config.yaml` is deliberately explicit:

- `image: ghcr.io/asxzy/wattmosaic-engine` — prebuilt multi-arch image, tagless
  (the Supervisor supplies `<version>`); only `{arch}` would be substituted.
- `init: false` — no s6 overlay; `run.sh` is the init for its two processes.
- `startup: application`, `boot: auto` — start after Home Assistant Core, on boot.
- `ingress: true`, `ingress_port: 8787` — sidebar panel, HA-authenticated.
- `host_network: false` — internal Docker network only.
- `homeassistant_api: true` — enables the `SUPERVISOR_TOKEN` Core proxy path.
- `map: data:rw, share:rw` — `/data` for the database, `/share` for exports.
- `backup: cold` — SQLite WAL is copied consistently.
- `watchdog: http://[HOST]:[PORT:8787]/healthz`.
- `ports: 8787/tcp: null` — not published unless the user enables it.

### 3.3 Integration endpoint

From the Home Assistant integration, use the container host name:

```text
http://<hostname>:8787
```

The Supervisor names an installed app `{repository}_{slug}` and registers that
hostname with `_` replaced by `-`:

| Install shape | Repository ID | Hostname |
| --- | --- | --- |
| App store (`https://github.com/asxzy/wattmosaic`) | `f48de52e` | `f48de52e-wattmosaic-engine` |
| Local copy (`/local_apps/wattmosaic_engine`, or `/addons/` before 2026.11) | `local` | `local-wattmosaic-engine` |

The repository ID is the first 8 hex characters of the SHA-1 of the lowercased
repository URL, so a URL typed with `.git` or a trailing slash yields a
*different* ID and a different hostname. Do not compute it by hand — the app's
own page shows **Hostname** under its info tab, and that value is authoritative.

Do not use `127.0.0.1` or the host IP: the app's port is not published.

### 3.4 Storage

- `/data` → host path `/mnt/data/supervisor/apps/data/<repo>_wattmosaic_engine`
  (the Supervisor renamed `addons/` to `apps/` in 2026.11; older releases use
  `addons/data/`).
- Survives app updates, restarts and reboots.
- Included in Home Assistant backups (cold).
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

`.env` is the only place secrets live. It is git-ignored and `.dockerignore`
keeps it out of the image build context.

The compose file applies:

- `restart: unless-stopped`, `init: true`, `stop_grace_period: 30s`;
- `read_only: true` root filesystem with a single writable volume `/data`;
- `cap_drop: ALL`, `security_opt: no-new-privileges`;
- a `tmpfs` `/tmp` (the app needs no other writable path);
- a healthcheck using the same `/app/healthcheck.py` as the image;
- a 1 GB memory limit and bounded JSON logs.

Override the published port/interface:

```bash
WM_BIND_ADDRESS=192.168.1.10 docker compose up -d
```

Only port `8787` is published. The engine's `8788` stays on container loopback.

Verify:

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

Migrations run automatically at start. Forward-only: take a backup first.

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

Binding the published port to `127.0.0.1` is the safe default; put a TLS reverse
proxy in front for remote access.

---

## 6. Health checks

Three independent signals:

| Check | Endpoint | Auth | Healthy when |
| --- | --- | --- | --- |
| Gateway liveness | `GET /healthz` | none | `200 ok`; it has just reached `/api/v1/health` |
| Engine health | `GET /api/v1/health` | `X-API-Token` | `status` is `ok` or `degraded` |
| Docker HEALTHCHECK | `/app/healthcheck.py` (both of the above) | token from env | exit 0 |

`degraded` is intentionally treated as healthy by the container healthcheck: it
usually means HA is reconnecting or the inference backend is not yet installed,
and restarting the container would not fix either — it would only lose the
connection and re-enter the loop. `error` and hard failures (401, connection
refused, non-JSON) fail the check.

Supervisor watchdog uses `/healthz` only, because it cannot send headers.

Inspect:

```bash
docker inspect --format '{{json .State.Health}}' wattmosaic-engine | jq
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/status | jq
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/diagnostics | jq
```

Contract compatibility is visible on every response:

```bash
curl -sI -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/health \
  | grep -i x-wattmosaic-contract-version
```

---

## 7. Persistence, backup and restore

### What must persist

Only `/data` — the SQLite database (`wattmosaic.db`, `-wal`, `-shm`). Nothing
else in the container is stateful. Models, learning samples, topology versions and
energy aggregates all live in that database.

### Backup

**Consistent, while running** (SQLite online backup):

```bash
# App
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

### Restore

1. Stop the container/app.
2. Replace `/data/wattmosaic.db` with the backup and remove any stale
   `wattmosaic.db-wal` / `-shm` (they belong to the old file).
3. Start the container. The engine runs `PRAGMA integrity_check` and refuses to
   serve corrupt data rather than silently discarding it.
4. Verify with `/api/v1/status` and `/api/v1/health`.

Never restore a database into an **older** engine than the one that wrote it:
migrations are forward-only.

### Retention

`WM_RETENTION_DAYS_RAW` / `WM_RETENTION_DAYS_AGGREGATES` bound growth. Pruning
never deletes `device_model_versions` or `learning_samples`. Monitor size:

```bash
docker exec app_local_wattmosaic_engine ls -lh /data
```

Rule of thumb: ~15 s CT cadence across ~20 channels is a few hundred MB per month
of raw samples; the default 30 days keeps that bounded.

---

## 8. Upgrades

### App

1. Re-run `scripts/package_addon.sh --install …` (or copy the new folder).
2. **Check for updates** → **Update**. The `version` in `config.yaml` is the
   update trigger; bump it for every release.
3. Migrations run at start. Watch the log for `database_ready schema_version=N`.
4. Rollback: re-install the previous version **and restore the matching database
   backup**. Do not downgrade the DB in place.

### Docker

```bash
docker compose build --pull
docker compose up -d
```

Tag images by version (`wattmosaic-engine:<version>`) and keep the previous tag for
rollback.

### Version compatibility

The engine reports `contract_version` (`1.4.0`) on every response. The
integration refuses to run against an incompatible contract rather than
misinterpreting data. Upgrade the integration and app together when the contract
version changes.

### Releasing

A release is a **git tag** plus a **GitHub Release**. Home Assistant, HACS and
the Marketplace resolve the displayed version from those two things, not from a
file in the tree: with no tag and no Release they fall back to the branch head
and show a raw commit SHA. That is the state this repository was in until
`0.2.1-dev.1`, and it is why an installed integration could report `eec273d`
while every version field correctly said `0.2.0-dev9`.

#### Channels

| Branch | Carries | Published as | Who sees it |
| --- | --- | --- | --- |
| `main` | `X.Y.Z-dev.N` | prerelease | development installs only |
| `stable` | `X.Y.Z` | latest | everyone |

`main` is the **development line**: every push publishes a prerelease. That is
safe rather than trusting, because the Marketplace ignores prereleases unless
the beta toggle is on — `show_beta` defaults to false, and the version offered
to a stable user is only ever the newest release with `prerelease=false`.

#### What a push to `main` does

You do not bump the version by hand. One workflow
(`.github/workflows/release.yaml`) does all of it, in order, in a single run:

1. **Bumps** the counter (`X.Y.Z-dev.N` → `X.Y.Z-dev.(N+1)`) in all five files
   and commits it with `[wm-auto-version]` in the message.
2. **Builds** `ghcr.io/asxzy/wattmosaic-engine:<version>` as a multi-arch
   manifest list.
3. **Tags** that commit and publishes the GitHub Release.
4. **Prunes** old development Releases, keeping the newest 10.

All four stages are one workflow on purpose. A push made with the automatic
`GITHUB_TOKEN` does not trigger another workflow, so splitting them into chained
push-triggered workflows would build the image from the *pre-bump* tree and tag
a version no artifact carries.

The bump is committed because the Supervisor offers an App update only when
`addon/config.yaml`'s `version:` **string** changes — it compares versions, never
commits. Without the commit the image would be published and never offered.

Pruning exists because the Marketplace reads a single unpaginated page of 30
Releases. If prereleases filled it, the newest stable Release would fall out,
the offered version would become a commit SHA, and the installer would fall back
to the default branch — offering a stable user the development line. Stable
Releases are never pruned.

#### What a push to `stable` does

The committed version **is** the release: no counter is incremented. Use this
for a deliberate stable release. The five fields are still checked for
agreement, and `RELEASE_NOTES.md` must already document the version.

| File | Field |
| --- | --- |
| `custom_components/wattmosaic/manifest.json` | `version` (canonical — what Home Assistant reads) |
| `custom_components/wattmosaic/const.py` | `VERSION` |
| `addon/config.yaml` | `version` (the App image tag) |
| `pyproject.toml` | `version` |
| `engine/wattmosaic_engine/__init__.py` | `__version__` |

Run `python tools/dev/check_release.py`; it fails if any of the five disagree, if
the notes do not cover the version, or if the branch carries a version of the
wrong kind.

#### Numbering

Keep the counter a **separate numeric identifier**: `-dev.2`, `-dev.3`. The glued
form (`-dev2`) is rejected by `check_release.py`. Note that the original reason
recorded here — that SemVer compares `dev10` below `dev9` — is **wrong for this
installer**: the Marketplace compares with AwesomeVersion, which parses `dev9`
as SemVer and orders it numerically (`v0.2.0-dev10 > v0.2.0-dev9` is `True`).
The dot form is kept because it is unambiguous and already published, not
because the glued form is broken here.

A human may still move the core version (for example `0.2.2-dev.1`); CI honours
a version that is not already tagged rather than skipping the number chosen.

Tags are immutable. To correct a release, delete and recreate the tag and
Release deliberately — never rely on a republish to change what an existing tag
means, because users may already have pulled it.

---

## 9. Uninstall

1. Remove the integration: **Settings → Devices & Services → WattMosaic → Delete**.
   This removes entities and the config entry only.
2. Stop and uninstall the app (or `docker compose down`).
3. The data volume is intentionally preserved. Delete it explicitly if you want
   to discard history and learned models:
   - App: uninstall with "delete data", or remove
     `/mnt/data/supervisor/apps/data/local_wattmosaic_engine` via SSH.
   - Docker: `docker volume rm wattmosaic-data`.

---

## 10. Security notes

| Topic | Measure |
| --- | --- |
| Token handling | Secrets only in App options / env. `run.sh` never echoes them (`--print-config` prints `***set(N chars)`). `EngineConfig.redacted()` keeps them out of logs, diagnostics and exports. |
| API exposure | Engine API on `127.0.0.1:8788`, token-protected. Only `8787` is reachable, and only the gateway. |
| Ingress | Home Assistant authenticates the user; `panel_admin: true` limits the sidebar panel to admins. Ingress requests are trusted **only** from `WM_GATEWAY_TRUSTED_INGRESS_IPS` *and* only when the Supervisor's `X-Remote-User-Id` header is present. |
| Panel assets | `index.html`, `app.css`, `app.js` and `mark.svg` ship inside the image and are served only to ingress peers, from a fixed allowlist (no path traversal, direct clients get `401`). The CSP is `script-src 'self'` with no inline script; `style-src` allows inline attributes only for meter widths and dial geometry. All engine-provided strings are inserted as text, never markup. |
| `api_token` | Compared with `hmac.compare_digest`. Empty is warned about at start. Generate with `secrets.token_urlsafe(32)`. |
| `SUPERVISOR_TOKEN` | Administrator-grade for the Core proxy. Prefer a dedicated read-only user token in `ha_token` when policy allows. |
| Least privilege | All capabilities dropped, `no_new_privs`, engine runs as UID 10001, read-only root filesystem in compose. |
| No actuation | The engine has no service-call code path; it can only read states and registries. |
| Logs | Access logs disabled for the gateway and engine (`access_log=None`) to avoid leaking query strings. |
| Network | Never publish `8787` on `0.0.0.0` without TLS and a token; prefer a VPN or reverse proxy. |

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Container exits immediately, log: `no Home Assistant token` | `ha_token` empty and no `SUPERVISOR_TOKEN` | Set `ha_token`, or keep it empty with `homeassistant_api: true` in App mode. |
| `no Home Assistant URL` | Standalone run without `WM_HA_URL`/`HA_URL`/`HA_HOST` | Set one in `.env`. |
| `failed to parse /data/options.json` | Corrupt options file | Re-save the app configuration; the Supervisor rewrites it. |
| `WM_STALE_AFTER_S must be numeric` | Non-numeric option | Fix the App option in the UI. |
| `database directory … is not writable` | Volume owned by a different UID | Remove/recreate the volume, or set `WM_DROP_PRIVILEGES=false`. |
| `setpriv not found` | Image tampered with | Rebuild; `setpriv` ships in `python:3.12-slim-bookworm`. |
| Healthcheck fails with `401` | `WM_API_TOKEN` mismatch between container and caller | Make the integration's token match. |
| Watchdog restarts the app repeatedly | `/healthz` returns 503: engine not answering | Check engine logs; `/healthz` only turns 200 when `/api/v1/health` answers. |
| Ingress panel is blank | CSP blocked an asset, or the gateway/image is missing `ui/` | Check the browser console; the panel loads `/ui/app.css` and `/ui/app.js` as separate files, so a `404` or a CSP `script-src` violation points at a stale image. The API is unaffected — `curl -H "X-API-Token: …" …/api/v1/health` still answers. |
| Integration gets 401 | Token not set or wrong | Set `api_token` and enter the same value in the integration. |
| Integration cannot connect | Wrong URL | Use `http://<hostname>:8787` — copy **Hostname** from the app's info tab, not `localhost` or a hand-computed hash. |
| `status: degraded`, `ha_connected: false` | HA unreachable or bad token | Check `ha_url`, token validity, and the log for `HA connection error`. |
| `inference backend not installed` in `details` | Engine build predates the inference modules | Rebuild from a complete checkout. |
| Database grows unexpectedly | Raw retention too high | Lower `WM_RETENTION_DAYS_RAW`; pruning runs hourly. |
| `integrity_check` fails at start | Corrupt database or bad restore | Restore a backup; do not delete data before copying it aside. |

### Collecting a diagnostic bundle

```bash
docker exec app_local_wattmosaic_engine python -c "import sqlite3,json;\
c=sqlite3.connect('/data/wattmosaic.db');print(c.execute('PRAGMA integrity_check').fetchone())"
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/healthz
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/health | jq
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/diagnostics | jq > diagnostics.json
docker logs --tail 200 app_local_wattmosaic_engine 2>&1 | tail -200
```

Diagnostics never include tokens; verify before sharing.

---

## 12. Reproducible builds

- Base image: `python:3.12-slim-bookworm`. Pin a digest
  (`python:3.12-slim-bookworm@sha256:…`) via `--build-arg PYTHON_IMAGE=…` (or
  `build.yaml`) for a byte-reproducible base.
- Dependencies: `addon/constraints.txt`, a full `==` pin set generated from
  `pyproject.toml` (see the header comment for the exact command). Builds use
  `--only-binary=:all:`, so an accidental source distribution fails the build
  instead of silently needing a compiler.
- Build tooling is pinned in the builder stage (`pip`, `setuptools`, `wheel`) and
  never reaches the runtime image.
- The runtime image contains no package manager cache, no compiler and no build
  headers; `pip check` runs at build time.
- `scripts/validate_addon.sh` rebuilds the image and asserts the entrypoint,
  healthcheck, dependencies, service user and the staged `ui/` panel. Its static
  stage runs `tools/dev/check_panel.py`, which proves the panel has no external
  origin, no inline script/style, no `on*` handler and no missing or `eval`-based
  code, so a broken shell fails before the image is built.

```bash
scripts/validate_addon.sh            # static checks + real docker build
scripts/validate_addon.sh --no-build  # static checks only (CI lint stage)
```
