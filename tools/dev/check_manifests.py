"""Static validation of integration manifest, services and add-on config.

No Home Assistant required. Runs in CI.

Run: python tools/dev/check_manifests.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ERRORS: list[str] = []

#: SemVer 2.0.0.  Shared shape with tools/dev/check_release.py; kept separate so
#: this validator has no import dependency on the release checker.
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def check_integration() -> None:
    base = ROOT / "custom_components" / "wattmosaic"
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        ERRORS.append("missing custom_components/wattmosaic/manifest.json")
        return
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        ERRORS.append(f"manifest.json is not valid JSON: {exc}")
        return

    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners", "iot_class"):
        if key not in manifest:
            ERRORS.append(f"manifest.json missing required key: {key}")
    if manifest.get("domain") != "wattmosaic":
        ERRORS.append("manifest domain must be 'wattmosaic'")
    if manifest.get("config_flow") is not True:
        ERRORS.append("manifest config_flow must be true")
    # SemVer 2.0.0, not "two dots".  Counting dots rejected every legitimate
    # dotted prerelease (``0.2.1-dev.1``) while accepting malformed versions the
    # grammar rejects -- the check has to be the grammar, because the version is
    # also a git tag and a container tag.
    if not SEMVER.match(str(manifest.get("version", ""))):
        ERRORS.append(
            "manifest version must be SemVer 2.0.0 (x.y.z, optionally with a "
            "-prerelease and +build suffix)"
        )
    if "requirements" in manifest and "aiohttp" in json.dumps(manifest.get("requirements", [])):
        ERRORS.append("must not declare aiohttp as a requirement (HA provides it)")

    for required in ("__init__.py", "const.py", "config_flow.py", "coordinator.py"):
        if not (base / required).exists():
            ERRORS.append(f"missing custom_components/wattmosaic/{required}")

    services_path = base / "services.yaml"
    if services_path.exists():
        try:
            services = yaml.safe_load(services_path.read_text())
        except yaml.YAMLError as exc:
            ERRORS.append(f"services.yaml invalid: {exc}")
        else:
            expected = {
                "start_learning", "pause_learning", "resume_learning", "accept_sample",
                "reject_sample", "rebuild_model", "freeze_model", "unfreeze_model",
                "reset_device_model", "rebuild_topology", "run_diagnostics", "export_device_data",
            }
            missing = expected - set(services or {})
            if missing:
                ERRORS.append(f"services.yaml missing: {sorted(missing)}")
            for name in ("reset_device_model", "rebuild_topology", "reject_sample"):
                svc = (services or {}).get(name, {})
                fields = svc.get("fields", {}) if isinstance(svc, dict) else {}
                if "confirm" not in fields:
                    ERRORS.append(f"destructive service '{name}' must declare a 'confirm' field")

    strings = base / "strings.json"
    if strings.exists():
        try:
            json.loads(strings.read_text())
        except json.JSONDecodeError as exc:
            ERRORS.append(f"strings.json invalid: {exc}")


def check_addon() -> None:
    path = ROOT / "addon" / "config.yaml"
    if not path.exists():
        ERRORS.append("missing addon/config.yaml")
        return
    try:
        config = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        ERRORS.append(f"addon/config.yaml invalid: {exc}")
        return
    for key in ("name", "slug", "version", "description", "arch", "startup", "boot", "options", "schema"):
        if key not in config:
            ERRORS.append(f"addon/config.yaml missing: {key}")
    if config.get("slug") != "wattmosaic_engine":
        ERRORS.append("addon slug should be 'wattmosaic_engine'")
    for arch in config.get("arch", []):
        if arch not in ("amd64", "aarch64", "armv7", "armhf", "i386"):
            ERRORS.append(f"unsupported arch: {arch}")
    if not isinstance(config.get("options"), dict):
        ERRORS.append("addon options must be a mapping")
    if not isinstance(config.get("schema"), dict):
        ERRORS.append("addon schema must be a mapping")
    options = config.get("options") or {}
    schema = config.get("schema") or {}
    missing_schema = set(options) - set(schema)
    if missing_schema:
        ERRORS.append(f"addon options without schema: {sorted(missing_schema)}")

    for fname in ("Dockerfile", "run.sh", "README.md"):
        if not (ROOT / "addon" / fname).exists():
            ERRORS.append(f"missing addon/{fname}")


def check_documented_app_names() -> None:
    """Keep the published hostname/container/volume names honest.

    The Supervisor derives all three from the repository URL and the app slug:

        repository id = sha1(lowercased URL)[:8]
        app slug      = f"{repo_id}_{slug}"
        hostname      = app slug with "_" -> "-"
        container     = f"app_{app slug}"

    Every one of these was wrong in the docs at some point (the community repo's
    hash ``a0d7b954`` was pasted in, and the pre-2026.08 ``addon_`` container
    prefix was left behind).  Rather than hard-code the strings, check that the
    docs agree with the values derived from the repository URL, so a rename or a
    URL change cannot silently invalidate the instructions again.
    """
    import hashlib

    repo_yaml = ROOT / "repository.yaml"
    cfg_path = ROOT / "addon" / "config.yaml"
    if not repo_yaml.exists() or not cfg_path.exists():
        return
    try:
        repo = yaml.safe_load(repo_yaml.read_text()) or {}
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as exc:
        ERRORS.append(f"cannot derive app names: {exc}")
        return

    url = str(repo.get("url") or "").strip().rstrip("/")
    slug = str(cfg.get("slug") or "")
    if not url or not slug:
        ERRORS.append("repository.yaml needs a url and addon/config.yaml a slug")
        return

    repo_id = hashlib.sha1(url.lower().encode()).hexdigest()[:8]
    app_slug = f"{repo_id}_{slug}"
    hostname = app_slug.replace("_", "-")
    container = f"app_{app_slug}"
    local_container = f"app_local_{slug}"

    # The Community Apps repository hash must never appear: it is a different
    # repository, so it resolves to a different app and refuses connections.
    community_id = hashlib.sha1(b"https://github.com/hassio-addons/repository").hexdigest()[:8]
    if community_id != repo_id:
        for doc in (ROOT / "docs").glob("*.md"):
            if community_id in doc.read_text(encoding="utf-8"):
                ERRORS.append(
                    f"{doc.name} uses {community_id}, the Community Apps repository id, "
                    f"but this repository's id is {repo_id}"
                )

    # Old container prefixes break every documented `docker exec`/`docker logs`.
    for doc in [ROOT / "addon" / "README.md", *(ROOT / "docs").glob("*.md")]:
        text = doc.read_text(encoding="utf-8")
        if f"addon_{app_slug}" in text or "addon_local_" in text or "addon_" + community_id in text:
            ERRORS.append(f"{doc.name} uses a pre-2026.08 'addon_' container name; use 'app_'")

    # The documented hostname must be the derived one for at least the store
    # install, which is the path the README tells users to take.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    if hostname not in install and hostname not in readme:
        ERRORS.append(
            f"docs do not mention the derived store hostname {hostname!r} "
            f"(repository id {repo_id}); store installs cannot connect without it"
        )

    # `local-` is documented as the *local copy* hostname only.
    for doc in (ROOT / "docs" / "installation.md", ROOT / "docs" / "deployment.md", ROOT / "addon" / "README.md"):
        text = doc.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "local-wattmosaic-engine" in line and "local copy" not in line and "Local copy" not in line \
                    and "local-wattmosaic-engine:8787" not in line:
                ERRORS.append(f"{doc.name}: 'local-' hostname is only valid for a local copy install")
                break

    # The container name documented for host-side debugging.
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    if container not in deployment and local_container not in deployment:
        ERRORS.append(
            f"docs/deployment.md uses neither the store container {container!r} "
            f"nor the local-copy container {local_container!r}"
        )


def check_packaging() -> None:
    for fname in ("pyproject.toml", "README.md", "LICENSE", ".gitignore", ".env.example", "docker-compose.yml"):
        if not (ROOT / fname).exists():
            ERRORS.append(f"missing root {fname}")
    env_example = ROOT / ".env.example"
    if env_example.exists():
        text = env_example.read_text()
        if "HA_TOKEN=" not in text:
            ERRORS.append(".env.example must document HA_TOKEN")
        for line in text.splitlines():
            if line.startswith("HA_TOKEN=") and "replace" not in line.lower() and line.strip() != "HA_TOKEN=":
                ERRORS.append(".env.example must not contain a real token")
    gitignore = ROOT / ".gitignore"
    if gitignore.exists():
        text = gitignore.read_text()
        if ".env" not in text:
            ERRORS.append(".gitignore must ignore .env")


def main() -> int:
    check_integration()
    check_addon()
    check_packaging()
    check_documented_app_names()
    if ERRORS:
        print("manifest validation FAILED:", file=sys.stderr)
        for e in ERRORS:
            print("  - " + e, file=sys.stderr)
        return 1
    print("manifest validation: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
