# syntax=docker/dockerfile:1.7
#
# WattMosaic Engine — Home Assistant App image.
#
# Build context: a *self-contained* app directory produced by
#   scripts/package_addon.sh
# i.e. it contains Dockerfile, run.sh, gateway.py, healthcheck.py, config.yaml,
# constraints.txt, pyproject.toml, README.md and engine/.  This mirrors exactly
# what the Supervisor mounts at /addon when it builds a local app — the
# Supervisor always uses the app folder as the build context, so the Python
# package has to be staged into it first.
#
# The standalone, generic-Docker variant lives in ../Dockerfile and is kept in
# sync with the runtime stages below.  scripts/validate_addon.sh checks both.
#
# Base image is pinned by tag; override PYTHON_IMAGE with a digest
# (python:3.12-slim-bookworm@sha256:...) in build.yaml or --build-arg for a
# byte-reproducible build.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --------------------------------------------------------------------------- #
# Stage 1 — build a virtualenv with the engine and its pinned dependencies
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# Pinned build tooling (versions verified against pypi.org).
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade \
        "pip==25.0.1" "setuptools==80.9.0" "wheel==0.45.1"

COPY constraints.txt ./
COPY pyproject.toml README.md ./
COPY engine ./engine

# Install the package (which pulls in every runtime dependency) constrained by
# constraints.txt.  Wheels only: the final image must not need a compiler, and
# the build fails loudly if an sdist would be required.
RUN /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: \
        -c constraints.txt . \
 && /opt/venv/bin/pip check

# --------------------------------------------------------------------------- #
# Stage 2 — minimal runtime image
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS runtime

# OCI labels (the Supervisor adds its own app labels on top).
LABEL org.opencontainers.image.title="WattMosaic Engine" \
      org.opencontainers.image.description="Multi-CT load disaggregation engine for WattMosaic" \
      org.opencontainers.image.version="0.1.2" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/asxzy/wattmosaic"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}" \
    WM_HA_URL="http://homeassistant:8123" \
    WM_DATABASE_PATH="/data/wattmosaic.db" \
    WM_API_HOST="127.0.0.1" \
    WM_API_PORT="8788" \
    WM_GATEWAY_PORT="8787" \
    WM_GATEWAY_ENABLED="true" \
    WM_LOG_LEVEL="INFO"

# setpriv (util-linux) is already present in the slim image; no apt layer is
# needed, so the final image has no package manager cache and no build tools.
RUN groupadd --gid 10001 wattmosaic \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin wattmosaic \
 && install -d -o 10001 -g 10001 -m 0750 /data /app

COPY --from=builder /opt/venv /opt/venv
COPY run.sh gateway.py healthcheck.py discovery.py /app/

RUN chmod 0755 /app/run.sh /app/gateway.py /app/healthcheck.py /app/discovery.py

WORKDIR /app
VOLUME ["/data"]
EXPOSE 8787

# The image entrypoint starts as root so run.sh can fix ownership of /data
# (created as root by the Supervisor and by Docker named volumes with
# uid 0) and then drop to UID 10001 with no_new_privs and all capabilities
# dropped.  WM_DROP_PRIVILEGES=false is an escape hatch for volume setups
# where the mount is already owned by the service user.
#
# start-period covers SQLite migrations + the first HA connection attempt. The
# Supervisor only waits 120 s for an app to leave STARTUP, so a terminal health
# state must be reachable well inside that window: 60 s + 3 x (5 s + probe) is
# under 100 s, whereas the old 90 s + 3 x 30 s first reported at ~180 s.
HEALTHCHECK --interval=5s --timeout=8s --start-period=60s --retries=3 \
    CMD ["python3", "/app/healthcheck.py"]

ENTRYPOINT ["/app/run.sh"]
CMD []
