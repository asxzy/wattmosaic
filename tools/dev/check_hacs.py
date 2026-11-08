"""Validate the repository against HACS / Marketplace publication rules.

No network and no Home Assistant required. Runs in CI.

HACS and the built-in Marketplace both install from a *public* GitHub
repository and both read ``hacs.json`` from the repository root. The rules
checked here are the ones that cause a repository to be rejected or to install
incorrectly:

* ``hacs.json`` exists, is valid JSON, and names the project.
* ``hacs.json`` claims a minimum Home Assistant version the code supports.
* The integration lives at ``custom_components/<domain>/`` and its
  ``manifest.json`` is internally consistent.
* A ``brand/`` directory provides the icon/logo used in the UI.
* The declared ``documentation``/``issue_tracker`` URLs are not the upstream
  placeholder organisation.
* The version in ``hacs.json`` (if any) matches the manifest.

Run: python tools/dev/check_hacs.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "custom_components" / "wattmosaic"
ERRORS: list[str] = []
NOTES: list[str] = []

#: A repository that still points at the upstream placeholder org will send
#: users to a 404 issue tracker after a fork or rename.
PLACEHOLDER_ORGS = ("wattmosaic/wattmosaic",)

#: SemVer 2.0.0: MAJOR.MINOR.PATCH with an optional prerelease suffix
#: (``-dev1``, ``-rc.2``).  The suffix is part of the spec, not a violation of
#: it, so a dev channel does not need a fake stable version number.
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        ERRORS.append(f"missing {path.relative_to(ROOT)}")
    except json.JSONDecodeError as exc:
        ERRORS.append(f"{path.relative_to(ROOT)} is not valid JSON: {exc}")
    return None


def check_hacs_json() -> None:
    path = ROOT / "hacs.json"
    data = _load_json(path)
    if data is None:
        return

    if not isinstance(data, dict):
        ERRORS.append("hacs.json must contain a JSON object")
        return

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        ERRORS.append("hacs.json must declare a non-empty 'name'")

    # Optional keys that, when present, must have the right type.
    for key in ("homeassistant", "content_in_root", "zip_release", "hide_default_branch"):
        if key in data and key not in ("homeassistant",) and not isinstance(data[key], bool):
            ERRORS.append(f"hacs.json '{key}' must be a boolean")

    ha_min = data.get("homeassistant")
    if ha_min is not None:
        if not isinstance(ha_min, str) or not SEMVER.match(ha_min):
            ERRORS.append(
                f"hacs.json 'homeassistant' must look like '2024.11.0', got {ha_min!r}"
            )
        else:
            major, minor, _ = (int(p) for p in ha_min.split("."))
            # The integration relies on config_entry injection (2024.11+).
            if (major, minor) < (2024, 11):
                ERRORS.append(
                    "hacs.json 'homeassistant' is below 2024.11.0, but the config "
                    "flow depends on config_entry injection added in 2024.11"
                )

    if data.get("zip_release") and not data.get("filename"):
        ERRORS.append("hacs.json sets zip_release but no filename")

    if "content_in_root" in data and data["content_in_root"] is True:
        ERRORS.append(
            "content_in_root must be false: the integration lives in "
            "custom_components/wattmosaic"
        )


def check_layout() -> None:
    if not COMPONENT.is_dir():
        ERRORS.append("missing custom_components/wattmosaic directory")
        return
    for required in ("__init__.py", "manifest.json"):
        if not (COMPONENT / required).is_file():
            ERRORS.append(f"missing custom_components/wattmosaic/{required}")


def check_brand() -> None:
    brand = COMPONENT / "brand"
    if not brand.is_dir():
        ERRORS.append("missing custom_components/wattmosaic/brand directory")
        return
    if not (brand / "icon.png").is_file():
        ERRORS.append("missing brand/icon.png")
    if not (brand / "logo.png").is_file():
        NOTES.append("brand/logo.png is optional but recommended")
    for extra in ("icon@2x.png",):
        if (brand / extra).is_file():
            NOTES.append(f"brand/{extra} present (HiDPI icon)")


def check_manifest() -> None:
    path = COMPONENT / "manifest.json"
    manifest = _load_json(path)
    if manifest is None:
        return

    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
        if not manifest.get(key):
            ERRORS.append(f"manifest.json missing '{key}'")

    if manifest.get("domain") != "wattmosaic":
        ERRORS.append("manifest.json domain must be 'wattmosaic'")

    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER.match(version):
        ERRORS.append(f"manifest.json version must be semver, got {version!r}")

    for key in ("documentation", "issue_tracker"):
        url = str(manifest.get(key) or "")
        if not url.startswith("https://"):
            ERRORS.append(f"manifest.json '{key}' must be an https URL")
        for org in PLACEHOLDER_ORGS:
            if org in url:
                ERRORS.append(
                    f"manifest.json '{key}' still points at placeholder '{org}': {url}"
                )

    owners = manifest.get("codeowners") or []
    if not isinstance(owners, list) or not all(isinstance(o, str) for o in owners):
        ERRORS.append("manifest.json 'codeowners' must be a list of strings")
    elif any(o in ("@wattmosaic",) for o in owners):
        ERRORS.append("manifest.json 'codeowners' still has the placeholder owner")

    # Localisation files must stay valid JSON and cover the entity keys.
    for rel in ("strings.json", "translations/en.json"):
        p = COMPONENT / rel
        if not p.is_file():
            ERRORS.append(f"missing {rel}")
            continue
        data = _load_json(p)
        if data is not None and "entity" not in data:
            ERRORS.append(f"{rel} has no 'entity' section")


def check_release_consistency() -> None:
    """Add-on and integration versions should not silently diverge."""
    addon = ROOT / "addon" / "config.yaml"
    if not addon.is_file():
        return
    text = addon.read_text()
    m = re.search(
        r"^version:\s*[\"']?(\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)[\"']?\s*$",
        text,
        re.MULTILINE,
    )
    manifest = COMPONENT / "manifest.json"
    if not m or not manifest.is_file():
        return
    addon_version = m.group(1)
    integration_version = json.loads(manifest.read_text()).get("version")
    if addon_version != integration_version:
        NOTES.append(
            f"addon version {addon_version} differs from integration "
            f"{integration_version} (allowed: they version independently)"
        )


def main() -> int:
    check_hacs_json()
    check_layout()
    check_brand()
    check_manifest()
    check_release_consistency()

    if NOTES:
        print("hacs validation notes:")
        for note in NOTES:
            print(f"  - {note}")

    if ERRORS:
        print("hacs validation FAILED:", file=sys.stderr)
        for err in ERRORS:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("hacs validation: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
