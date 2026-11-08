#!/usr/bin/env bash
# =============================================================================
# WattMosaic Engine — container entrypoint.
#
# Responsibilities (in order):
#   1. Read Home Assistant App options from /data/options.json (if present).
#   2. Map options -> the WM_* environment variables the engine reads.
#   3. Apply documented defaults; options win over the environment, which wins
#      over built-in defaults (so Docker .env keeps working unchanged).
#   4. Resolve the Home Assistant token (option -> env -> SUPERVISOR_TOKEN).
#   5. Prepare /data and drop privileges when started as root.
#   6. Start the small auth/ingress gateway on the ingress port, then exec the
#      engine in the foreground so it is PID 1 and receives SIGTERM directly.
#
# Secrets are never echoed. The only place a token may appear is the process
# environment, which the engine reads and never logs (EngineConfig.redacted()).
#
# Runs on bash 3.2+ (macOS) and bash 5 (the container), so no associative
# arrays and no ${var,,}: only constructs available in both.
#
# Usage:
#   run.sh                  normal start
#   run.sh --print-config   print the resolved, redacted plan and exit
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

readonly APP_DIR="/app"
readonly GATEWAY="${APP_DIR}/gateway.py"
readonly VENV_BIN="/opt/venv/bin/wattmosaic-engine"
readonly SERVICE_UID=10001
readonly SERVICE_GID=10001
readonly DEFAULT_HA_URL="http://homeassistant:8123"

# Option (config.yaml schema key) | environment variable the engine reads |
# default.  scripts/validate_addon.sh parses this table and compares it with
# engine/wattmosaic_engine/config/settings.py, so keep one entry per line.
readonly OPTION_TABLE="
ha_url|WM_HA_URL|${DEFAULT_HA_URL}
ha_token|WM_HA_TOKEN|
api_token|WM_API_TOKEN|
learning_enabled|WM_LEARNING_ENABLED|true
stale_after_s|WM_STALE_AFTER_S|120
inference_interval_s|WM_INFERENCE_INTERVAL_S|5
log_level|WM_LOG_LEVEL|INFO
log_json|WM_LOG_JSON|true
database_path|WM_DATABASE_PATH|/data/wattmosaic.db
retention_days_raw|WM_RETENTION_DAYS_RAW|30
retention_days_aggregates|WM_RETENTION_DAYS_AGGREGATES|730
queue_max_size|WM_QUEUE_MAX_SIZE|5000
"

OPTIONS_FILE="${WM_OPTIONS_FILE:-/data/options.json}"
PARSED_OPTIONS=""
PRINT_CONFIG=0
[[ "${1:-}" == "--print-config" ]] && PRINT_CONFIG=1

log() {
  printf '%s level=%s component=entrypoint msg="%s"\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${2:-info}" "$1"
}

die() {
  log "$1" error
  exit "${2:-1}"
}

cleanup() {
  [[ -n "${PARSED_OPTIONS}" && -f "${PARSED_OPTIONS}" ]] && rm -f "${PARSED_OPTIONS}"
  return 0
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Read options.json safely.
#
# The Supervisor writes JSON. We avoid `eval` entirely: Python encodes the
# object as NUL-separated key/value records and bash reads them with
# `read -d ''`, which preserves spaces, quotes, $, backslashes and newlines
# without any shell parsing. Values are never interpolated into code.
# ---------------------------------------------------------------------------
if [[ -f "${OPTIONS_FILE}" ]]; then
  command -v python3 >/dev/null 2>&1 \
    || die "python3 is required to read ${OPTIONS_FILE} but was not found"

  PARSED_OPTIONS="$(mktemp "${TMPDIR:-/tmp}/wm-options.XXXXXX")"

  if ! python3 - "${OPTIONS_FILE}" >"${PARSED_OPTIONS}" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError) as exc:
    sys.stderr.write("cannot read %s: %s\n" % (path, exc))
    raise SystemExit(3)

if not isinstance(data, dict):
    sys.stderr.write("%s must contain a JSON object\n" % path)
    raise SystemExit(3)

out = sys.stdout.buffer
for key, value in data.items():
    if not isinstance(key, str):
        continue
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = ""
    elif isinstance(value, float):
        text = repr(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        sys.stderr.write("ignoring non-scalar option %r\n" % key)
        continue
    out.write(key.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
out.flush()
PY
  then
    die "failed to parse ${OPTIONS_FILE}" 2
  fi

  OPTION_COUNT="$(python3 - "${PARSED_OPTIONS}" <<'PY'
import sys
raw = open(sys.argv[1], "rb").read()
print(raw.count(b"\0") // 2)
PY
)"
  log "read ${OPTIONS_FILE} (${OPTION_COUNT} options)"
else
  log "no ${OPTIONS_FILE}; using environment variables and defaults"
fi

# option_present KEY -> exit 0 when the key was supplied in options.json
option_present() {
  [[ -n "${PARSED_OPTIONS}" ]] || return 1
  local want="$1" key value
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    if [[ "${key}" == "${want}" ]]; then
      return 0
    fi
  done <"${PARSED_OPTIONS}"
  return 1
}

# option_value KEY -> prints the value (only call after option_present).
option_value() {
  local want="$1" key value
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    if [[ "${key}" == "${want}" ]]; then
      printf '%s' "${value}"
      return 0
    fi
  done <"${PARSED_OPTIONS}"
  return 1
}

# ---------------------------------------------------------------------------
# 2/3. Resolve every mapped option with precedence:
#        options.json  >  existing environment  >  built-in default
# ---------------------------------------------------------------------------
HA_URL_FROM_OPTIONS=0
option_present ha_url && HA_URL_FROM_OPTIONS=1

HA_URL_FROM_ENV=0
if [[ -n "${WM_HA_URL:-}" || -n "${HA_URL:-}" || -n "${HA_HOST:-}" ]]; then
  HA_URL_FROM_ENV=1
fi

while IFS='|' read -r opt env_name default_value; do
  [[ -n "${opt}" ]] || continue
  if option_present "${opt}"; then
    value="$(option_value "${opt}")"
  elif [[ -n "${!env_name:-}" ]]; then
    value="${!env_name}"
  else
    value="${default_value}"
  fi
  printf -v "${env_name}" '%s' "${value}"
  # ``${env_name?}`` (rather than ``${env_name}``) keeps shellcheck quiet about
  # SC2163 while still exporting the variable *named by* ``env_name``.  It also
  # fails loudly if the name were ever empty, which would otherwise silently
  # export nothing.  ``declare -g`` is not an option: macOS ships bash 3.2.
  export "${env_name?}"
  unset value
done <<EOF
$(printf '%s\n' "${OPTION_TABLE}" | grep -v '^[[:space:]]*$')
EOF

# Home Assistant connection aliases accepted by the engine (HA_URL / HA_HOST)
# are honoured for standalone deployments that copied .env.example unchanged.
if [[ "${HA_URL_FROM_OPTIONS}" -eq 0 && "${HA_URL_FROM_ENV}" -eq 1 ]]; then
  if [[ -n "${HA_URL:-}" ]]; then
    WM_HA_URL="${HA_URL}"
  elif [[ -n "${HA_HOST:-}" ]]; then
    WM_HA_URL="${HA_HOST}"
  fi
  export WM_HA_URL
fi

# --- token resolution ------------------------------------------------------
# Direct access to http://homeassistant:8123 needs a *user* long-lived token.
# SUPERVISOR_TOKEN is the Supervisor's own credential and is only accepted by
# the Core proxy (http://supervisor/core), so when we fall back to it we also
# switch the URL to the proxy. This requires `homeassistant_api: true`.
#
# The App always supplies ha_url (its default is http://homeassistant:8123), so
# the trigger is "the effective URL is still the default", not "no option was
# given" — otherwise the fallback could never fire in App mode.
if [[ -z "${WM_HA_TOKEN}" ]]; then
  if [[ -n "${HA_TOKEN:-}" ]]; then
    WM_HA_TOKEN="${HA_TOKEN}"
  elif [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
    WM_HA_TOKEN="${SUPERVISOR_TOKEN}"
    if [[ "${WM_HA_URL}" == "${DEFAULT_HA_URL}" ]]; then
      WM_HA_URL="http://supervisor/core"
      log "no ha_token option: using SUPERVISOR_TOKEN via the Core proxy"
    else
      log "no ha_token option: using SUPERVISOR_TOKEN with the configured ha_url (only works if it points at the Core proxy)" warn
    fi
  fi
  export WM_HA_TOKEN
fi

# --- normalise / validate --------------------------------------------------
WM_LOG_LEVEL="$(printf '%s' "${WM_LOG_LEVEL}" | tr '[:lower:]' '[:upper:]')"
export WM_LOG_LEVEL
case "${WM_LOG_LEVEL}" in
  DEBUG|INFO|WARNING|ERROR) ;;
  *) die "log_level must be one of DEBUG, INFO, WARNING, ERROR (got '${WM_LOG_LEVEL}')" 2 ;;
esac

for numeric in WM_STALE_AFTER_S WM_INFERENCE_INTERVAL_S WM_RETENTION_DAYS_RAW \
               WM_RETENTION_DAYS_AGGREGATES WM_QUEUE_MAX_SIZE; do
  if ! printf '%s' "${!numeric}" | grep -Eq '^[0-9]+([.][0-9]+)?$'; then
    die "${numeric} must be numeric (got '${!numeric}')" 2
  fi
done

# The engine binds to loopback behind the gateway; the gateway owns the ingress
# port. They must never share a socket.
WM_API_HOST="${WM_API_HOST:-127.0.0.1}"
WM_API_PORT="${WM_API_PORT:-8788}"
WM_GATEWAY_PORT="${WM_GATEWAY_PORT:-8787}"
WM_GATEWAY_ENABLED="${WM_GATEWAY_ENABLED:-true}"
WM_GATEWAY_TRUSTED_INGRESS_IPS="${WM_GATEWAY_TRUSTED_INGRESS_IPS:-172.30.32.2}"
WM_DROP_PRIVILEGES="${WM_DROP_PRIVILEGES:-true}"
# App-install discovery (addon/discovery.py).  Defaults to on; outside the
# Supervisor the publisher finds no SUPERVISOR_TOKEN and exits quietly.
WM_DISCOVERY_ENABLED="${WM_DISCOVERY_ENABLED:-true}"
export WM_API_HOST WM_API_PORT WM_GATEWAY_PORT WM_GATEWAY_ENABLED \
       WM_GATEWAY_TRUSTED_INGRESS_IPS WM_DROP_PRIVILEGES WM_DISCOVERY_ENABLED

if [[ "${WM_GATEWAY_ENABLED}" == "true" && "${WM_API_PORT}" == "${WM_GATEWAY_PORT}" ]]; then
  die "WM_API_PORT and WM_GATEWAY_PORT must differ (both ${WM_API_PORT})" 2
fi

if [[ -z "${WM_HA_TOKEN}" ]]; then
  die "no Home Assistant token: set the 'ha_token' option, HA_TOKEN/WM_HA_TOKEN, or grant SUPERVISOR_TOKEN" 2
fi

if [[ -z "${WM_HA_URL}" ]]; then
  die "no Home Assistant URL: set the 'ha_url' option, or WM_HA_URL / HA_URL / HA_HOST" 2
fi

if [[ -z "${WM_API_TOKEN}" ]]; then
  log "api_token is empty: the API is unauthenticated for loopback and the Supervisor network only" warn
fi

# --- redacted plan ---------------------------------------------------------
redact() {
  if [[ -n "$1" ]]; then
    printf '***set(%d chars)' "${#1}"
  else
    printf '(empty)'
  fi
}

if [[ "${PRINT_CONFIG}" == "1" ]]; then
  echo "# WattMosaic resolved configuration (secrets redacted)"
  printf '%s\n' "${OPTION_TABLE}" | grep -v '^[[:space:]]*$' | sort | \
  while IFS='|' read -r opt env_name default_value; do
    case "${env_name}" in
      WM_HA_TOKEN|WM_API_TOKEN) shown="$(redact "${!env_name}")" ;;
      *) shown="${!env_name}" ;;
    esac
    printf '%-28s = %s\n' "${env_name}" "${shown}"
  done
  for extra in WM_HA_VERIFY_SSL WM_API_HOST WM_API_PORT WM_GATEWAY_PORT \
               WM_GATEWAY_ENABLED WM_GATEWAY_TRUSTED_INGRESS_IPS WM_DROP_PRIVILEGES \
               WM_DISCOVERY_ENABLED WM_DISCOVERY_HOST WM_DISCOVERY_PORT \
               WM_DISCOVERY_SERVICE; do
    printf '%-28s = %s\n' "${extra}" "${!extra:-<unset>}"
  done
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. Prepare persistent storage.
# ---------------------------------------------------------------------------
DB_DIR="$(dirname "${WM_DATABASE_PATH}")"
if [[ ! -d "${DB_DIR}" ]]; then
  mkdir -p "${DB_DIR}" 2>/dev/null || true
fi
[[ -d "${DB_DIR}" ]] || die "database directory ${DB_DIR} does not exist and cannot be created" 2

AM_ROOT=0
if [[ "$(id -u)" -eq 0 ]]; then AM_ROOT=1; fi

if [[ "${AM_ROOT}" -eq 1 && "${WM_DROP_PRIVILEGES}" == "true" ]]; then
  # /data is created by the Supervisor as root and survives upgrades. Fix only
  # the paths we use; a blanket recursive chown would be slow on large DBs.
  chown "${SERVICE_UID}:${SERVICE_GID}" "${DB_DIR}" 2>/dev/null || true
  for f in "${WM_DATABASE_PATH}" "${WM_DATABASE_PATH}-wal" "${WM_DATABASE_PATH}-shm"; do
    if [[ -e "${f}" ]]; then
      chown "${SERVICE_UID}:${SERVICE_GID}" "${f}" 2>/dev/null || true
    fi
  done
fi

DROP_PRIVS=0
if [[ "${AM_ROOT}" -eq 1 && "${WM_DROP_PRIVILEGES}" == "true" ]]; then
  if command -v setpriv >/dev/null 2>&1; then
    DROP_PRIVS=1
  else
    die "setpriv not found; set WM_DROP_PRIVILEGES=false to run as root" 2
  fi
fi

# Wrap commands that must run as the unprivileged service user.
run_as_service() {
  if [[ "${DROP_PRIVS}" -eq 1 ]]; then
    setpriv --reuid="${SERVICE_UID}" --regid="${SERVICE_GID}" --clear-groups \
            --inh-caps=-all --ambient-caps=-all --no-new-privs -- "$@"
  else
    "$@"
  fi
}

# The write test must run as the user the engine will actually run as. Testing
# as root passes on a volume the service user cannot write, and the engine then
# dies at startup opening the database.
SERVICE_UID_SHOWN="$(id -u)"
[[ "${DROP_PRIVS}" -eq 1 ]] && SERVICE_UID_SHOWN="${SERVICE_UID}"
if ! run_as_service touch "${DB_DIR}/.wm-write-test" 2>/dev/null; then
  die "${DB_DIR} is not writable by uid ${SERVICE_UID_SHOWN}; fix volume ownership or set WM_DROP_PRIVILEGES=false" 2
fi
run_as_service rm -f "${DB_DIR}/.wm-write-test" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5/6. Start the gateway, then exec the engine so it is PID 1.
# ---------------------------------------------------------------------------
log "starting engine db=${WM_DATABASE_PATH} upstream=${WM_API_HOST}:${WM_API_PORT} gateway=${WM_GATEWAY_ENABLED}:${WM_GATEWAY_PORT} uid=$(id -u)"

if [[ "${WM_GATEWAY_ENABLED}" == "true" ]]; then
  (
    while :; do
      run_as_service python3 "${GATEWAY}" || true
      log "gateway exited; restarting in 2s" warn
      sleep 2
    done
  ) &
  # The gateway is a child of the engine after the exec below. When the engine
  # exits (SIGTERM during `docker stop`, or a crash) the runtime tears down the
  # remaining processes, and the gateway exits by itself once the engine has
  # been unreachable for a while.
  sleep 0.3
fi

# Announce this App to Home Assistant so the integration can be added from a
# confirmation screen instead of a hostname typed by hand. Publishing is
# best-effort and never blocks startup: outside the Supervisor there is nothing
# to announce, and the App must still boot.
if [[ "${WM_DISCOVERY_ENABLED}" != "false" && -x "${APP_DIR}/discovery.py" ]]; then
  (
    # Wait for the gateway so the announced endpoint is already answering when
    # the user accepts the discovery.
    run_as_service python3 "${APP_DIR}/discovery.py" || \
      log "discovery publisher failed; the integration can still be added manually" warn
  ) &
fi

if [[ -x "${VENV_BIN}" ]]; then
  ENGINE_CMD=("${VENV_BIN}")
elif command -v wattmosaic-engine >/dev/null 2>&1; then
  ENGINE_CMD=("$(command -v wattmosaic-engine)")
else
  ENGINE_CMD=(python3 -m wattmosaic_engine.main)
fi

if [[ "${DROP_PRIVS}" -eq 1 ]]; then
  # exec: the engine becomes PID 1 and receives SIGTERM/SIGINT directly, so the
  # SQLite WAL is checkpointed and the API closes cleanly on `docker stop`.
  exec setpriv --reuid="${SERVICE_UID}" --regid="${SERVICE_GID}" --clear-groups \
               --inh-caps=-all --ambient-caps=-all --no-new-privs -- "${ENGINE_CMD[@]}"
else
  exec "${ENGINE_CMD[@]}"
fi
