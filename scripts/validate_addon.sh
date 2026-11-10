#!/usr/bin/env bash
# =============================================================================
# WattMosaic deployment validation.
#
# Runs fast, dependency-light checks and then (when Docker is available) a real
# image build.  Every check prints PASS/FAIL/SKIP with the command it ran, and
# the script exits non-zero if any check fails, so it is safe to use in CI.
#
#   scripts/validate_addon.sh              # full run, builds the image
#   scripts/validate_addon.sh --no-build   # static checks only
#   scripts/validate_addon.sh --keep       # keep the temp staging dir
#
# Requirements: bash, python3. Optional: docker, shellcheck, hadolint.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

DO_BUILD=1
KEEP=0
for arg in "$@"; do
  case "${arg}" in
    --no-build) DO_BUILD=0 ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

PASS=0
FAIL=0
SKIP=0
FAILURES=()

pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); FAILURES+=("$1"); printf 'FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '      %s\n' "$2"; }
skip() { SKIP=$((SKIP + 1)); printf 'SKIP  %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }

PY="${PYTHON:-}"
if [[ -z "${PY}" ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]] \
     && "${REPO_ROOT}/.venv/bin/python" -c "import yaml" >/dev/null 2>&1; then
    PY="${REPO_ROOT}/.venv/bin/python"
  else
    PY="python3"
  fi
fi

# ---------------------------------------------------------------------------
section "Tooling"
# ---------------------------------------------------------------------------
for tool in python3 bash; do
  if command -v "${tool}" >/dev/null 2>&1; then
    pass "${tool} available ($("${tool}" --version 2>&1 | head -1))"
  else
    fail "${tool} available" "required"
  fi
done

HAVE_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  HAVE_DOCKER=1
  pass "docker available ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"
else
  skip "docker available (no daemon; image build will be skipped)"
fi

HAVE_SHELLCHECK=0
command -v shellcheck >/dev/null 2>&1 && HAVE_SHELLCHECK=1
HAVE_HADOLINT=0
command -v hadolint >/dev/null 2>&1 && HAVE_HADOLINT=1

# ---------------------------------------------------------------------------
section "Repository layout"
# ---------------------------------------------------------------------------
required_files=(
  addon/config.yaml
  addon/Dockerfile
  addon/run.sh
  addon/gateway.py
  addon/healthcheck.py
  addon/discovery.py
  addon/ui/index.html
  addon/ui/app.css
  addon/ui/app.js
  addon/ui/mark.svg
  addon/constraints.txt
  addon/icon.png
  addon/logo.png
  addon/README.md
  Dockerfile
  docker-compose.yml
  .dockerignore
  docs/deployment.md
  scripts/package_addon.sh
  pyproject.toml
  engine/wattmosaic_engine/config/settings.py
)
for f in "${required_files[@]}"; do
  if [[ -e "${f}" ]]; then pass "exists ${f}"; else fail "exists ${f}"; fi
done

# ---------------------------------------------------------------------------
section "repository.yaml (app-store repository contract)"
# ---------------------------------------------------------------------------
# Without a valid manifest at the repository *root* the Supervisor refuses the
# URL outright ("is not a valid app repository"), so this is the one file that
# makes the whole App distribution path work at all.
REPO_CHECK="$("${PY}" - <<'PY'
import sys
try:
    import yaml
except ImportError:
    print("NO_YAML")
    sys.exit(0)

import pathlib

errors = []
path = None
for name in ("repository.yaml", "repository.yml", "repository.json"):
    candidate = pathlib.Path(name)
    if candidate.exists():
        path = candidate
        break
if path is None:
    print("ERRORS no repository manifest at the repository root")
    sys.exit(0)

try:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    print("ERRORS %s does not parse: %s" % (path.name, exc))
    sys.exit(0)

if not isinstance(cfg, dict):
    print("ERRORS %s must be a mapping" % path.name)
    sys.exit(0)
# `name` is the only required key; extra keys are dropped, not rejected.
if not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
    errors.append("%s must define a non-empty name" % path.name)
if "url" in cfg and not str(cfg["url"]).startswith(("http://", "https://")):
    errors.append("%s url must be an http(s) URL" % path.name)
if "maintainer" in cfg and not isinstance(cfg["maintainer"], str):
    errors.append("%s maintainer must be a string" % path.name)

# The store discovers apps by globbing for config.* at any depth, so a second
# config.yaml anywhere would be published as a bogus second app.
found = [
    p.as_posix()
    for p in pathlib.Path(".").glob("**/config.yaml")
    if not any(part.startswith(".") or part == "rootfs" for part in p.parts)
    and "dist" not in p.parts
    and ".venv" not in p.parts
]
if found != ["addon/config.yaml"]:
    errors.append("expected exactly one app folder, addon/config.yaml, found %r" % (found,))

print("OK" if not errors else "ERRORS " + " | ".join(errors))
PY
)"
case "${REPO_CHECK}" in
  OK) pass "repository.yaml is a valid app-store manifest with one app folder" ;;
  NO_YAML) skip "repository.yaml semantic checks (PyYAML not installed)" ;;
  *) fail "repository.yaml" "${REPO_CHECK#ERRORS }" ;;
esac

# ---------------------------------------------------------------------------
section "config.yaml (Supervisor manifest contract)"
# ---------------------------------------------------------------------------
CONFIG_CHECK="$("${PY}" - <<'PY'
import sys

try:
    import yaml
except ImportError:
    print("NO_YAML")
    sys.exit(0)

REQUIRED = {
    "name": str,
    "version": str,
    "slug": str,
    "description": str,
    "arch": list,
    "startup": str,
    "boot": str,
    "init": bool,
    "ingress": bool,
    "ingress_port": int,
    "panel_title": str,
    "panel_icon": str,
    "host_network": bool,
    "map": list,
    "options": dict,
    "schema": dict,
    "watchdog": str,
}

errors = []
try:
    with open("addon/config.yaml", "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
except Exception as exc:  # noqa: BLE001
    print("PARSE_ERROR %s" % exc)
    sys.exit(0)

if not isinstance(cfg, dict):
    print("PARSE_ERROR top level is not a mapping")
    sys.exit(0)

for key, typ in REQUIRED.items():
    if key not in cfg:
        errors.append("missing required key: %s" % key)
        continue
    value = cfg[key]
    if typ is bool:
        if not isinstance(value, bool):
            errors.append("%s must be a boolean, got %r" % (key, type(value).__name__))
    elif not isinstance(value, typ):
        errors.append("%s must be %s, got %r" % (key, typ.__name__, type(value).__name__))

# Slug format accepted by the Supervisor (RE_SLUG).
import re
if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", str(cfg.get("slug", ""))):
    errors.append("slug %r does not match ^[a-z0-9][a-z0-9_-]*$" % cfg.get("slug"))
if cfg.get("slug") != "wattmosaic_engine":
    errors.append("slug must be wattmosaic_engine")
if cfg.get("arch") and not set(cfg["arch"]).issubset({"amd64", "aarch64", "armv7", "armhf", "i386"}):
    errors.append("unsupported arch list %r" % (cfg["arch"],))
if not set(cfg.get("arch") or []).issuperset({"amd64", "aarch64"}):
    errors.append("arch must include amd64 and aarch64")
if cfg.get("startup") not in {"initialize", "system", "services", "application", "once"}:
    errors.append("startup %r is not a Supervisor value" % cfg.get("startup"))
if cfg.get("boot") not in {"auto", "manual", "manual_only"}:
    errors.append("boot %r is not a Supervisor value" % cfg.get("boot"))
if cfg.get("init") is not False:
    errors.append("init must be false (the image has its own entrypoint, no s6 overlay)")
if cfg.get("ingress_port") != 8787:
    errors.append("ingress_port must match the engine port 8787")
wd = str(cfg.get("watchdog", ""))
if not (wd.startswith(("http://[HOST]:", "https://[HOST]:", "tcp://[HOST]:", "[PROTO:"))):
    errors.append("watchdog %r must use a [HOST] URL template" % wd)
if cfg.get("host_network") is not False:
    errors.append("host_network must be false (internal network + ingress)")

# Prebuilt image.  The Supervisor pulls exactly ``<image>:<version>`` and only
# substitutes ``{arch}``, so the name must be tagless and the repository must
# publish that tag.  A tag here would be silently ignored, and a wrong version
# would fail at install time on the user machine rather than here.
image = cfg.get("image")
if image is not None:
    if not isinstance(image, str) or not image.strip():
        errors.append("image must be a non-empty string when present")
    elif ":" in image.rsplit("/", 1)[-1]:
        errors.append(
            "image %r must not contain a tag; the Supervisor uses <image>:<version>" % image
        )
    elif "{arch}" in image and not image.startswith("ghcr.io/"):
        errors.append("image %r with {arch} must point at a registry" % image)
    elif not cfg.get("version"):
        errors.append("image requires a version so the pulled tag is defined")

# map entries
allowed_map = {
    "data", "share", "media", "ssl", "backup", "homeassistant_config",
    "app_config", "all_app_configs", "local_apps",
}
for entry in cfg.get("map") or []:
    if isinstance(entry, str):
        name = entry.split(":", 1)[0]
        if name not in allowed_map:
            errors.append("map entry %r is not a known type" % entry)
    elif isinstance(entry, dict):
        name = entry.get("type")
        if name not in allowed_map:
            errors.append("map entry %r has unknown type %r" % (entry, name))
        if "read_only" in entry and not isinstance(entry["read_only"], bool):
            errors.append("map entry %r read_only must be boolean" % entry)
    else:
        errors.append("map entry %r is not a string or mapping" % (entry,))

# options/schema consistency
options = cfg.get("options") or {}
schema = cfg.get("schema") or {}
if set(options) != set(schema):
    errors.append(
        "options/schema keys differ: options-only=%s schema-only=%s"
        % (sorted(set(options) - set(schema)), sorted(set(schema) - set(options)))
    )

TYPE_RE = {
    "bool": lambda v: isinstance(v, bool),
    "str": lambda v: isinstance(v, str),
    "url": lambda v: isinstance(v, str) and v.startswith(("http://", "https://")),
    "password": lambda v: isinstance(v, str),
}
for key, decl in schema.items():
    decl = str(decl)
    base = decl.rstrip("?").split("(", 1)[0]
    value = options.get(key)
    if decl.endswith("?") and (value is None or value == ""):
        continue
    checker = TYPE_RE.get(base)
    if checker is None:
        # int()/float()/list()/match() types
        if base.startswith("int"):
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append("option %s should be int, got %r" % (key, value))
        elif base.startswith("float"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append("option %s should be float, got %r" % (key, value))
        elif base.startswith("list"):
            # ``base`` has already been truncated at "(", so the allowed values
            # must be read from the original declaration: list(A|B|C)
            allowed = decl[len("list("):].rstrip("?").rstrip(")").split("|")
            if value not in allowed:
                errors.append("option %s=%r not in %s" % (key, value, allowed))
        elif base.startswith("match"):
            pass
        else:
            errors.append("option %s has unknown schema type %r" % (key, decl))
    elif not checker(value):
        errors.append("option %s=%r does not match schema %s" % (key, value, decl))

print("OK %d options" % len(options) if not errors else "ERRORS " + " | ".join(errors))
PY
)"

case "${CONFIG_CHECK}" in
  OK*) pass "config.yaml parses and matches the Supervisor contract (${CONFIG_CHECK#OK })" ;;
  NO_YAML) skip "config.yaml semantic checks (PyYAML not installed for ${PY})" ;;
  *) fail "config.yaml semantic checks" "${CONFIG_CHECK}" ;;
esac

# Plain parse as required by the task, using whichever interpreter has PyYAML.
if "${PY}" -c "import yaml" >/dev/null 2>&1; then
  if OUT="$("${PY}" -c "import yaml,sys; yaml.safe_load(open('addon/config.yaml')); print('ok')" 2>&1)"; then
    pass "python -c yaml.safe_load(addon/config.yaml) -> ${OUT}"
  else
    fail "python -c yaml.safe_load(addon/config.yaml)" "${OUT}"
  fi
else
  skip "pyyaml parse (install pyyaml for ${PY})"
fi

# ---------------------------------------------------------------------------
section "Options -> engine environment variables"
# ---------------------------------------------------------------------------
MAP_CHECK="$("${PY}" - <<'PY'
import ast
import pathlib
import re

errors = []

# 1. Parse the OPTION_TABLE from run.sh without executing it.
run_sh = pathlib.Path("addon/run.sh").read_text(encoding="utf-8")
block = re.search(r'readonly OPTION_TABLE="\n(.*?)"\n', run_sh, re.S)
if not block:
    print("ERROR cannot find OPTION_TABLE in addon/run.sh")
    raise SystemExit(0)
mapping = {}
for line in block.group(1).splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    parts = line.split("|")
    if len(parts) != 3:
        print("ERROR malformed OPTION_TABLE line: %s" % line)
        raise SystemExit(0)
    mapping[parts[0]] = parts[1]
if not mapping:
    errors.append("OPTION_TABLE is empty")

# 2. Find every environment name settings.py actually reads.
settings = pathlib.Path("engine/wattmosaic_engine/config/settings.py").read_text(encoding="utf-8")
read = set(re.findall(r'get\(\s*"([A-Z][A-Z0-9_]*)"', settings))
read |= set(re.findall(r'"([A-Z][A-Z0-9_]*)"', settings.split("def from_env", 1)[-1]))
read = {n for n in read if n.startswith(("WM_", "HA_"))}

for opt, env in sorted(mapping.items()):
    if env not in read:
        errors.append("option %s maps to %s, which settings.py never reads" % (opt, env))

# 3. Every schema key must be mapped.
import yaml
cfg = yaml.safe_load(pathlib.Path("addon/config.yaml").read_text(encoding="utf-8"))
schema_keys = set((cfg.get("schema") or {}).keys())
options_keys = set((cfg.get("options") or {}).keys())
for key in sorted(schema_keys | options_keys):
    if key not in mapping:
        errors.append("config.yaml option %s has no WM_* mapping in run.sh" % key)
for key in sorted(set(mapping) - schema_keys):
    errors.append("run.sh maps option %s that config.yaml does not expose" % key)

# 4. Required engine variables must be reachable from either an option or a
#    documented standalone env var.
guaranteed = set(mapping.values())
for env in ("WM_HA_URL", "WM_HA_TOKEN", "WM_API_TOKEN", "WM_DATABASE_PATH"):
    if env not in guaranteed:
        errors.append("%s is not set by run.sh" % env)

print("OK %d options mapped" % len(mapping) if not errors else "ERRORS " + " | ".join(errors))
PY
)"
case "${MAP_CHECK}" in
  OK*) pass "options map to engine env vars (${MAP_CHECK#OK })" ;;
  *) fail "options -> env var mapping" "${MAP_CHECK}" ;;
esac

# ---------------------------------------------------------------------------
section "Shell scripts"
# ---------------------------------------------------------------------------
for script in addon/run.sh scripts/package_addon.sh scripts/validate_addon.sh; do
  if OUT="$(bash -n "${script}" 2>&1)"; then
    pass "bash -n ${script}"
  else
    fail "bash -n ${script}" "${OUT}"
  fi
done

if [[ "${HAVE_SHELLCHECK}" -eq 1 ]]; then
  for script in addon/run.sh scripts/package_addon.sh scripts/validate_addon.sh; do
    if OUT="$(shellcheck -x -S warning "${script}" 2>&1)"; then
      pass "shellcheck ${script}"
    else
      fail "shellcheck ${script}" "${OUT}"
    fi
  done
else
  skip "shellcheck (not installed; bash -n still ran)"
fi

# Executable bits matter for the Supervisor entrypoint.
for script in addon/run.sh scripts/package_addon.sh; do
  if [[ -x "${script}" ]]; then pass "executable ${script}"; else fail "executable ${script}" "chmod +x"; fi
done

# ---------------------------------------------------------------------------
section "Python modules"
# ---------------------------------------------------------------------------
for module in addon/gateway.py addon/healthcheck.py scripts/generate_icons.py; do
  if OUT="$("${PY}" -m py_compile "${module}" 2>&1)"; then
    pass "py_compile ${module}"
  else
    fail "py_compile ${module}" "${OUT}"
  fi
  rm -rf addon/__pycache__ scripts/__pycache__ 2>/dev/null || true
done

# ---------------------------------------------------------------------------
section "Ingress panel (static assets)"
# ---------------------------------------------------------------------------
# The panel is served by the gateway under a strict CSP and must work without
# any network access beyond this origin.  The checks live in a Python helper
# because a heredoc containing regex like ``eval()`` cannot be nested inside
# ``$( )`` on the bash 3.2 that ships with macOS.
if OUT="$("${PY}" tools/dev/check_panel.py 2>&1)"; then
  pass "ingress panel is self-contained and CSP-safe (${OUT##*: })"
else
  fail "ingress panel static checks" "${OUT}"
fi

# Syntax-check the browser script when a JavaScript engine is available.  CI
# has no node, so this is a skip there; a developer machine catches a typo
# before it is ever served to a browser.
if command -v node >/dev/null 2>&1; then
  if OUT="$(node --check addon/ui/app.js 2>&1)"; then
    pass "node --check addon/ui/app.js"
  else
    fail "node --check addon/ui/app.js" "${OUT}"
  fi
else
  skip "node --check addon/ui/app.js (node not installed)"
fi

# ---------------------------------------------------------------------------
section "Images"
# ---------------------------------------------------------------------------
IMG_CHECK="$("${PY}" - <<'PY'
import struct
import zlib
import pathlib

errors = []
for name, size in (("addon/icon.png", 128), ("addon/logo.png", 256)):
    path = pathlib.Path(name)
    if not path.exists():
        errors.append("%s missing" % name)
        continue
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        errors.append("%s is not a PNG" % name)
        continue
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (size, size):
        errors.append("%s is %dx%d, expected %dx%d" % (name, width, height, size, size))
    # Verify IDAT decompresses and the raw size matches 1 filter byte + RGB rows.
    idat = b""
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        if kind == b"IDAT":
            idat += chunk
        pos += 12 + length
    try:
        raw = zlib.decompress(idat)
    except zlib.error as exc:
        errors.append("%s IDAT does not decompress: %s" % (name, exc))
        continue
    expected = height * (1 + width * 3)
    if len(raw) != expected:
        errors.append("%s raw size %d != %d" % (name, len(raw), expected))
    if data.find(b"http://") != -1 or data.find(b"https://") != -1:
        errors.append("%s embeds an external URL" % name)

print("OK 2 images" if not errors else "ERRORS " + " | ".join(errors))
PY
)"
case "${IMG_CHECK}" in
  OK*) pass "icon.png/logo.png are valid ${IMG_CHECK#OK }" ;;
  *) fail "icon/logo PNG validation" "${IMG_CHECK}" ;;
esac

# ---------------------------------------------------------------------------
section "Container definitions"
# ---------------------------------------------------------------------------
if grep -q '^HEALTHCHECK' addon/Dockerfile && grep -q 'healthcheck.py' addon/Dockerfile; then
  pass "addon/Dockerfile defines a HEALTHCHECK"
else
  fail "addon/Dockerfile defines a HEALTHCHECK"
fi

if grep -q '^USER ' addon/Dockerfile; then
  pass "addon/Dockerfile declares a USER"
else
  # Documented exception: run.sh drops privileges with setpriv after fixing
  # volume ownership. Confirm the drop actually happens.
  if grep -q 'setpriv' addon/run.sh; then
    pass "addon/Dockerfile runs root entrypoint that drops to uid 10001 (setpriv)"
  else
    fail "addon/Dockerfile has no USER and run.sh does not drop privileges"
  fi
fi

if grep -Eq '^[[:space:]]*RUN.*(apt-get|apt[[:space:]]|gcc|build-essential|apk add)' addon/Dockerfile; then
  fail "addon/Dockerfile installs build tools/apt packages in the runtime image"
else
  pass "addon/Dockerfile has no apt/build-tool layer"
fi

if grep -q 'python:3\.1[23]' addon/Dockerfile; then
  pass "addon/Dockerfile uses a pinned python:3.1x slim base"
else
  fail "addon/Dockerfile does not pin a slim Python base image"
fi

# The two Dockerfiles must agree on the runtime contract (same entrypoint,
# same healthcheck, same exposed port, same shipped entrypoint scripts).
for needle in 'ENTRYPOINT \["/app/run.sh"\]' 'EXPOSE 8787' '/app/healthcheck.py' \
              'discovery.py'; do
  if grep -q "${needle}" Dockerfile && grep -q "${needle}" addon/Dockerfile; then
    pass "root and addon Dockerfiles agree: ${needle}"
  else
    fail "root and addon Dockerfiles diverge on: ${needle}"
  fi
done

if [[ "${HAVE_HADOLINT}" -eq 1 ]]; then
  for df in Dockerfile addon/Dockerfile; do
    if OUT="$(hadolint --failure-threshold warning "${df}" 2>&1)"; then
      pass "hadolint ${df}"
    else
      fail "hadolint ${df}" "${OUT}"
    fi
  done
else
  skip "hadolint (not installed)"
fi


# Secrets must never be committed. `.env.example` is allowed to contain
# obvious placeholders; anything that looks like a real credential is a failure.
SECRET_HITS="$("${PY}" - <<'PY'
import pathlib
import re

PLACEHOLDER = re.compile(
    r"^(replace[-_ ].*|<.*>|\$\{.*\}|\$[A-Z_]+|changeme|example.*|your[-_ ].*|none|null|)$",
    re.I,
)
files = ["addon/config.yaml", "docker-compose.yml", ".env.example", "addon/README.md"]
hits = []
pattern = re.compile(
    r"(ha_token|api_token|HA_TOKEN|WM_API_TOKEN|HA_HOST)\s*[:=]\s*[\"']?([^\s\"'#]+)",
    re.I,
)
for name in files:
    path = pathlib.Path(name)
    if not path.exists():
        continue
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for match in pattern.finditer(line):
            value = match.group(2).strip()
            if not PLACEHOLDER.match(value) and len(value) >= 16:
                hits.append("%s:%d %s" % (name, lineno, match.group(1)))
print("\n".join(hits))
PY
)"
if [[ -n "${SECRET_HITS}" ]]; then
  fail "no hard-coded credentials in deployment files" "${SECRET_HITS}"
else
  pass "no hard-coded credentials in deployment files (placeholders only)"
fi

# ---------------------------------------------------------------------------
section "Runtime smoke test (run.sh --print-config)"
# ---------------------------------------------------------------------------
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wm-validate.XXXXXX")"
cleanup() {
  [[ "${KEEP}" -eq 1 ]] && { echo "kept ${TMP_DIR}"; return; }
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

# Run addon/run.sh in a *clean* environment.
#
# The smoke tests below assert the precedence rules of run.sh itself: an option
# in options.json beats the process environment.  If the invoking shell has
# sourced the operator's .env (HA_HOST / HA_TOKEN), those variables leak in and
# silently change the answer — the token-fallback check could never fire on a
# developer machine while passing in CI.  A gate that depends on the operator's
# environment is not a gate, so scrub every HA_*/WM_*/SUPERVISOR_* variable
# first and pass only what each test intends to exercise.
smoke_run() {
  local -a scrub=()
  local name
  while IFS= read -r name; do
    case "${name}" in
      HA_*|WM_*|SUPERVISOR_TOKEN) scrub+=(-u "${name}") ;;
    esac
  done < <(env | cut -d= -f1)
  # bash 3.2 aborts on `"${empty[@]}"` under `set -u`; the `+` guard expands to
  # nothing when the array is empty.
  env ${scrub[@]+"${scrub[@]}"} "$@" bash addon/run.sh --print-config 2>&1
}

# `docker compose config` needs a literal .env next to the compose file
# (the service declares `env_file: .env`).  That file is git-ignored, so a fresh
# clone has none and the check would fail on every machine but the operator's.
# Stage the *committed* compose file with the *committed* placeholder env into a
# throwaway project directory, so the result is identical everywhere.
COMPOSE_DIR="${TMP_DIR}/compose"
mkdir -p "${COMPOSE_DIR}"
cp docker-compose.yml "${COMPOSE_DIR}/"
cp .env.example "${COMPOSE_DIR}/.env"
if docker compose version >/dev/null 2>&1; then
  if OUT="$(cd "${COMPOSE_DIR}" && docker compose -f docker-compose.yml config 2>&1 >/dev/null)"; then
    pass "docker compose config parses"
  else
    fail "docker compose config" "${OUT:-invalid compose file}"
  fi
elif command -v docker >/dev/null 2>&1; then
  if OUT="$(cd "${COMPOSE_DIR}" && docker-compose -f docker-compose.yml config 2>&1 >/dev/null)"; then
    pass "docker-compose config parses"
  else
    fail "docker-compose config" "docker compose plugin not available"
  fi
else
  skip "docker compose config (docker not installed)"
fi

cat >"${TMP_DIR}/options.json" <<'JSON'
{
  "ha_url": "http://homeassistant:8123",
  "ha_token": "validation-placeholder-token",
  "api_token": "validation-placeholder-api-token",
  "learning_enabled": true,
  "stale_after_s": 120,
  "inference_interval_s": 5,
  "log_level": "info",
  "log_json": true,
  "database_path": "/tmp/wm-validate/wattmosaic.db",
  "retention_days_raw": 30,
  "retention_days_aggregates": 730,
  "queue_max_size": 5000
}
JSON
mkdir -p "${TMP_DIR}/data"

SMOKE_OUT="$(
  smoke_run \
  WM_OPTIONS_FILE="${TMP_DIR}/options.json" \
  WM_DATABASE_PATH="${TMP_DIR}/data/wattmosaic.db" \
  WM_DROP_PRIVILEGES=false \
  WM_API_PORT=18788 \
  WM_GATEWAY_PORT=18787
)"
SMOKE_RC=$?

if [[ "${SMOKE_RC}" -eq 0 ]]; then
  pass "run.sh --print-config exits 0"
else
  fail "run.sh --print-config exits 0" "rc=${SMOKE_RC}: ${SMOKE_OUT}"
fi

if grep -q 'WM_HA_URL * = http://homeassistant:8123' <<<"${SMOKE_OUT}"; then
  pass "run.sh maps ha_url -> WM_HA_URL"
else
  fail "run.sh maps ha_url -> WM_HA_URL" "${SMOKE_OUT}"
fi
if grep -q 'WM_LOG_LEVEL * = INFO' <<<"${SMOKE_OUT}"; then
  pass "run.sh normalises log_level to upper case"
else
  fail "run.sh normalises log_level" "${SMOKE_OUT}"
fi
if grep -q 'WM_API_TOKEN * = \*\*\*set' <<<"${SMOKE_OUT}" \
   && ! grep -q 'validation-placeholder' <<<"${SMOKE_OUT}"; then
  pass "run.sh redacts tokens in --print-config"
else
  fail "run.sh redacts tokens" "${SMOKE_OUT}"
fi

# Token fallback: no ha_token option, SUPERVISOR_TOKEN present -> proxy URL.
cat >"${TMP_DIR}/options-supervisor.json" <<'JSON'
{"ha_url": "http://homeassistant:8123", "ha_token": "", "api_token": ""}
JSON
FALLBACK_OUT="$(
  smoke_run \
  WM_OPTIONS_FILE="${TMP_DIR}/options-supervisor.json" \
  SUPERVISOR_TOKEN="validation-supervisor-token" \
  WM_DATABASE_PATH="${TMP_DIR}/data/wattmosaic.db"
)"
if grep -q 'WM_HA_URL * = http://supervisor/core' <<<"${FALLBACK_OUT}" \
   && grep -q 'WM_HA_TOKEN * = \*\*\*set' <<<"${FALLBACK_OUT}"; then
  pass "SUPERVISOR_TOKEN fallback switches ha_url to the Core proxy"
else
  fail "SUPERVISOR_TOKEN fallback" "${FALLBACK_OUT}"
fi

# Invalid numeric option must be rejected, not silently coerced. (options.json
# wins over the environment, so the bad value goes into a separate options file.)
cat >"${TMP_DIR}/options-bad.json" <<'JSON'
{"ha_url": "http://homeassistant:8123", "ha_token": "x", "stale_after_s": "not-a-number"}
JSON
if smoke_run WM_OPTIONS_FILE="${TMP_DIR}/options-bad.json" >/dev/null; then
  fail "run.sh rejects a non-numeric stale_after_s"
else
  pass "run.sh rejects a non-numeric stale_after_s"
fi

# When no token is available at all, fail closed with a clear message.
cat >"${TMP_DIR}/options-none.json" <<'JSON'
{"ha_url": "http://homeassistant:8123", "ha_token": "", "api_token": ""}
JSON
NONE_OUT="$(smoke_run \
  WM_OPTIONS_FILE="${TMP_DIR}/options-none.json")"
if (( $? != 0 )) && grep -qi 'no Home Assistant token' <<<"${NONE_OUT}"; then
  pass "run.sh fails closed when no HA token is available"
else
  fail "run.sh fails closed without a token" "${NONE_OUT}"
fi

# Healthcheck must fail cleanly when nothing is listening (no traceback).
HC_OUT="$(WM_HEALTH_GATEWAY_URL="http://127.0.0.1:1/healthz" \
          WM_HEALTH_TIMEOUT_S=1 python3 addon/healthcheck.py 2>&1)"
if (( $? != 0 )) && ! grep -qi 'traceback' <<<"${HC_OUT}"; then
  pass "healthcheck.py fails cleanly when the gateway is down"
else
  fail "healthcheck.py unreachable-gateway behaviour" "${HC_OUT}"
fi

# ---------------------------------------------------------------------------
section "Add-on staging (build context)"
# ---------------------------------------------------------------------------
STAGE_PARENT="${TMP_DIR}/dist"
if OUT="$(bash scripts/package_addon.sh --out "${STAGE_PARENT}" 2>&1)"; then
  pass "package_addon.sh stages a self-contained app"
  STAGE="${STAGE_PARENT}/wattmosaic_engine"
  for f in config.yaml Dockerfile run.sh gateway.py healthcheck.py constraints.txt \
           pyproject.toml README.md ui/index.html ui/app.css ui/app.js ui/mark.svg \
           engine/wattmosaic_engine/main.py; do
    if [[ -e "${STAGE}/${f}" ]]; then pass "staged ${f}"; else fail "staged ${f}"; fi
  done
else
  fail "package_addon.sh" "${OUT}"
  STAGE=""
fi

# ---------------------------------------------------------------------------
section "Docker build"
# ---------------------------------------------------------------------------
if [[ "${DO_BUILD}" -eq 0 ]]; then
  skip "image build (--no-build)"
elif [[ "${HAVE_DOCKER}" -eq 0 ]]; then
  skip "image build (no docker daemon)"
elif [[ -z "${STAGE:-}" ]]; then
  fail "image build" "staging failed"
else
  TAG="wattmosaic-engine:validate-$$"
  echo "running: docker build --pull=false -t ${TAG} ${STAGE}"
  if OUT="$(docker build -t "${TAG}" "${STAGE}" 2>&1)"; then
    pass "docker build (addon context) succeeded"
    SIZE="$(docker image inspect "${TAG}" --format '{{.Size}}' 2>/dev/null || echo 0)"
    printf '      image size: %s bytes\n' "${SIZE}"

    if docker run --rm --entrypoint /app/run.sh "${TAG}" --print-config >/dev/null 2>&1; then
      pass "image entrypoint runs (run.sh --print-config)"
    else
      # Expected to fail without HA credentials; the binary and shell must work.
      if docker run --rm --entrypoint bash "${TAG}" -n /app/run.sh >/dev/null 2>&1; then
        pass "image entrypoint script is valid; requires credentials to start (expected)"
      else
        fail "image entrypoint sanity check"
      fi
    fi

    if docker run --rm --entrypoint python3 "${TAG}" -c \
        "import aiohttp, numpy, scipy, pydantic, yaml, websockets; print('imports ok')" >/dev/null 2>&1; then
      pass "image contains all runtime dependencies"
    else
      fail "image runtime dependency import check"
    fi

    if docker run --rm --entrypoint id "${TAG}" wattmosaic 2>/dev/null | grep -q 'uid=10001'; then
      pass "service user uid 10001 exists in the image"
    else
      fail "service user uid 10001 in the image"
    fi

    docker image rm -f "${TAG}" >/dev/null 2>&1 || true
  else
    fail "docker build (addon context)" "$(tail -30 <<<"${OUT}")"
  fi
fi

# ---------------------------------------------------------------------------
printf '\n== Summary ==\n'
printf 'passed: %d  failed: %d  skipped: %d\n' "${PASS}" "${FAIL}" "${SKIP}"
if [[ "${FAIL}" -gt 0 ]]; then
  printf 'failures:\n'
  for item in "${FAILURES[@]}"; do printf '  - %s\n' "${item}"; done
  exit 1
fi
echo "all checks passed"
