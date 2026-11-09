"""Validate the release contract for a given git ref.

No network and no Home Assistant required. Runs in CI and locally.

Why this exists
---------------
Home Assistant, HACS and the built-in Marketplace all resolve "what version is
installed" from **git metadata**, not from a file in the tree:

* HACS reads the repository's *tags* and *GitHub Releases*.  When neither
  exists it falls back to the branch head and shows a raw commit SHA.
* The Marketplace publishes a ``version`` derived from the same source.

This repository shipped fifteen versions (``0.1.0`` through ``0.2.0-dev9``),
each with a correct ``manifest.json`` version and a published container image,
but **no git tag and no GitHub Release at all**.  The code was right and the
metadata was missing, so every install reported something like ``dev`` or a
40-character SHA instead of ``0.2.0-dev9``.

The checks below are the machine-enforceable half of the release contract. They
do not create tags (that needs the network and a write); they prove that *if* a
tag were created at this commit, it would describe the tree honestly.

Rules
-----
For a **release ref** (``vX.Y.Z`` or ``vX.Y.Z-suffix``):

1. the tag must be valid SemVer 2.0.0 with a leading ``v``;
2. ``manifest.json``, ``const.VERSION``, ``addon/config.yaml`` and
   ``pyproject.toml`` must all state that exact version;
3. ``RELEASE_NOTES.md`` must contain a ``## vX.Y.Z`` section for it;
4. a ``-suffix`` tag (dev/rc) must NOT be published as a stable GitHub Release,
   and a plain ``X.Y.Z`` tag must.

For an **ordinary commit** (no tag), the same version fields must at least agree
with each other, and ``RELEASE_NOTES.md`` must document the current version.

Run:
    python tools/dev/check_release.py                 # check the working tree
    python tools/dev/check_release.py --tag v0.2.0    # check a candidate tag
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "custom_components" / "wattmosaic" / "manifest.json"
CONST = ROOT / "custom_components" / "wattmosaic" / "const.py"
ADDON = ROOT / "addon" / "config.yaml"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_INIT = ROOT / "engine" / "wattmosaic_engine" / "__init__.py"
NOTES = ROOT / "RELEASE_NOTES.md"

#: SemVer 2.0.0 without the leading ``v``.  The ``v`` is a git-tag convention,
#: not part of the version, so it is stripped before comparison.
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

TAG = re.compile(r"^v(?P<version>.+)$")

ERRORS: list[str] = []
NOTES_OUT: list[str] = []


def error(message: str) -> None:
    ERRORS.append(message)


def note(message: str) -> None:
    NOTES_OUT.append(message)


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        error(f"missing {path.relative_to(ROOT)}")
        return None


def manifest_version() -> str | None:
    raw = _read(MANIFEST)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        error(f"manifest.json is not valid JSON: {exc}")
        return None
    version = data.get("version")
    if not isinstance(version, str):
        error("manifest.json has no string 'version'")
        return None
    return version


def const_version() -> str | None:
    raw = _read(CONST)
    if raw is None:
        return None
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
    if not match:
        error("const.py has no VERSION assignment")
        return None
    return match.group(1)


def addon_version() -> str | None:
    raw = _read(ADDON)
    if raw is None:
        return None
    match = re.search(r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$', raw, re.MULTILINE)
    if not match:
        error("addon/config.yaml has no version")
        return None
    return match.group(1)


def pyproject_version() -> str | None:
    raw = _read(PYPROJECT)
    if raw is None:
        return None
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
    if not match:
        error("pyproject.toml has no version")
        return None
    return match.group(1)


def package_version() -> str | None:
    """``engine/wattmosaic_engine/__init__.py``'s ``__version__``.

    Nothing reads it at runtime, which is exactly how it drifted three releases
    behind without anyone noticing.  It is still shipped, so it must not lie.
    """
    raw = _read(PACKAGE_INIT)
    if raw is None:
        return None
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', raw, re.MULTILINE)
    if not match:
        error("engine/wattmosaic_engine/__init__.py has no __version__")
        return None
    return match.group(1)


def release_notes_versions() -> set[str]:
    raw = _read(NOTES)
    if raw is None:
        return set()
    return {
        m.group(1)
        for m in re.finditer(r"^##\s+v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", raw, re.MULTILINE)
    }


def check_version_fields() -> str | None:
    """Every place that states a version must state the same one."""
    versions = {
        "manifest.json": manifest_version(),
        "const.py": const_version(),
        "addon/config.yaml": addon_version(),
        "pyproject.toml": pyproject_version(),
        "engine __init__.py": package_version(),
    }
    present = {name: value for name, value in versions.items() if value is not None}
    if not present:
        return None

    distinct = set(present.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{name}={value}" for name, value in present.items())
        error(f"version fields disagree: {detail}")

    for name, value in present.items():
        if value and not SEMVER.match(value):
            error(f"{name} version {value!r} is not valid SemVer 2.0.0")

    # The manifest is what Home Assistant reads, so it wins as the canonical
    # version when reporting.
    return versions["manifest.json"]


def check_tag(tag: str) -> None:
    """A candidate tag must describe this exact tree."""
    match = TAG.match(tag)
    if not match:
        error(f"tag {tag!r} must start with 'v' (e.g. v0.2.0 or v0.2.0-dev1)")
        return
    tag_version = match.group("version")
    semver = SEMVER.match(tag_version)
    if not semver:
        error(f"tag {tag!r} does not carry a valid SemVer version ({tag_version!r})")
        return

    current = check_version_fields()
    if current is not None and current != tag_version:
        error(
            f"tag {tag!r} says {tag_version} but the tree says {current}. "
            "A tag must point at a commit whose version fields already match it; "
            "otherwise the release advertises a version the code does not contain."
        )

    documented = release_notes_versions()
    if tag_version not in documented:
        error(
            f"RELEASE_NOTES.md has no '## v{tag_version}' section. "
            "Every released version must be documented before it is tagged."
        )

    if semver.group("pre"):
        note(f"{tag_version} is a prerelease; it must be tagged, not released as 'latest'")
    else:
        note(f"{tag_version} is a stable version; it may be published as 'latest'")


def notes_section(version: str) -> str | None:
    """Return the body of the ``## v<version>`` section, or None.

    The GitHub Release body is taken from here rather than retyped, so the
    release page and the repository's own history cannot disagree.
    """
    try:
        raw = NOTES.read_text()
    except FileNotFoundError:
        return None
    lines = raw.splitlines()
    heading = re.compile(r"^##\s+v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")
    start = None
    for index, line in enumerate(lines):
        match = heading.match(line)
        if match and match.group(1) == version:
            start = index
            break
    if start is None:
        return None

    body: list[str] = []
    for line in lines[start + 1 :]:
        if heading.match(line):
            break
        body.append(line)

    # A horizontal rule between sections belongs to neither of them.
    while body and body[-1].strip() in ("", "---"):
        body.pop()
    return "\n".join(body).strip() + "\n"


def check_channel(channel: str) -> None:
    """A branch may only carry a version of its own kind.

    ``main`` is the stable channel and ``dev`` is the development channel.  A
    prerelease published from ``main`` would become the version stable users are
    offered; a stable version published from ``dev`` would do the same while
    carrying untested code.  The tag is derived from the version string, so the
    version string is what has to be checked.
    """
    current = manifest_version()
    if current is None:
        return
    semver = SEMVER.match(current)
    if semver is None:
        return  # already reported by check_version_fields

    is_prerelease = bool(semver.group("pre"))
    if channel == "main" and is_prerelease:
        error(
            f"'{channel}' is the stable channel but the tree version {current!r} is a "
            "prerelease. Stable users would be offered it as an update. Release "
            "prereleases from 'dev'."
        )
    elif channel == "dev" and not is_prerelease:
        error(
            f"'{channel}' is the development channel but the tree version {current!r} "
            "is a stable version. Publishing it from 'dev' would offer untested code "
            "to stable users as 'latest'. Add a '-devN' suffix, or release from 'main'."
        )
    elif channel == "dev":
        note(f"{current} is a prerelease, as required on the dev channel")
    else:
        note(f"{current} is stable, as required on the main channel")


def check_notes_cover_current() -> None:
    current = manifest_version()
    if current is None:
        return
    documented = release_notes_versions()
    if current not in documented:
        error(
            f"RELEASE_NOTES.md does not document the current version {current}. "
            "A version bump without an entry leaves the release history wrong."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        default=None,
        help="Validate a candidate release tag (e.g. v0.2.0) against this tree.",
    )
    parser.add_argument(
        "--channel",
        choices=("main", "dev"),
        default=None,
        help="Validate that this branch may carry the current version.",
    )
    parser.add_argument(
        "--notes-for",
        default=None,
        metavar="VERSION",
        help="Print the RELEASE_NOTES.md body for VERSION (used as the release body).",
    )
    args = parser.parse_args(argv)

    if args.notes_for:
        section = notes_section(args.notes_for)
        if section is None:
            print(
                f"error: RELEASE_NOTES.md has no section for {args.notes_for}",
                file=sys.stderr,
            )
            return 1
        sys.stdout.write(section)
        return 0

    current = check_version_fields()
    check_notes_cover_current()
    if args.tag:
        check_tag(args.tag)
    if args.channel:
        check_channel(args.channel)

    if current is not None:
        print(f"version: {current}")
    if args.tag:
        print(f"candidate tag: {args.tag}")
    if args.channel:
        print(f"channel: {args.channel}")
    for message in NOTES_OUT:
        print(f"note: {message}")
    if ERRORS:
        for message in ERRORS:
            print(f"error: {message}", file=sys.stderr)
        return 1
    print("release checks: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
