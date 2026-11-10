"""Compute and write the next development version.

Why this exists
---------------
``main`` is now the development line: every push publishes a prerelease.  The
Supervisor only offers an App update when ``addon/config.yaml``'s ``version:``
*string* changes -- it clones one branch and compares version strings, it never
compares commits.  So a push that changes code but not the version would build a
new image, tag it with the old version, and the installed App would stay on old
code while reporting success.  That is the ``0.2.0-dev5`` defect again.

The bump therefore has to happen in CI, on the pushed tree, and it has to move
*all five* places that state a version or the release contract fails.

Numbering
---------
``X.Y.Z-dev.N`` -> ``X.Y.Z-dev.(N+1)``.

The counter must stay a **separate numeric identifier**.  SemVer 2.0.0 compares
an identifier containing letters lexically, so ``dev9 > dev10``; a glued counter
silently stops being offered at the tenth release.  ``-dev.10 > -dev.9`` is true
by the numeric rule, so the dot form increments forever.

A *stable* version is refused rather than bumped.  ``X.Y.Z`` -> ``X.Y.Z-dev.1``
would sort **below** the release it came from, so no updater would ever offer
it; and ``X.Y.(Z+1)-dev.1`` would invent a patch bump the author did not ask
for.  Moving the core version is a human decision, so it is left to a human.

Run:
    python tools/dev/bump_version.py --next          # print the next version
    python tools/dev/bump_version.py --write         # rewrite the five fields
    python tools/dev/bump_version.py --write --notes-file body.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MANIFEST = ROOT / "custom_components" / "wattmosaic" / "manifest.json"
CONST = ROOT / "custom_components" / "wattmosaic" / "const.py"
ADDON = ROOT / "addon" / "config.yaml"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_INIT = ROOT / "engine" / "wattmosaic_engine" / "__init__.py"
NOTES = ROOT / "RELEASE_NOTES.md"

SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

#: A prerelease counter that is its own numeric identifier, e.g. ``dev.7``.
DEV_COUNTER = re.compile(r"^dev\.(?P<counter>\d+)$")


class BumpError(Exception):
    """A version that cannot be incremented automatically."""


def read_versions(root: Path = ROOT) -> dict[str, str | None]:
    """Every place a version is stated, in reporting order."""
    manifest_path = root / MANIFEST.relative_to(ROOT)
    const_path = root / CONST.relative_to(ROOT)
    addon_path = root / ADDON.relative_to(ROOT)
    pyproject_path = root / PYPROJECT.relative_to(ROOT)
    package_path = root / PACKAGE_INIT.relative_to(ROOT)

    versions: dict[str, str | None] = {}

    try:
        data = json.loads(manifest_path.read_text())
        versions["manifest.json"] = data.get("version")
    except (FileNotFoundError, json.JSONDecodeError):
        versions["manifest.json"] = None

    def _grab(path: Path, pattern: str) -> str | None:
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return None
        match = re.search(pattern, raw, re.MULTILINE)
        return match.group(1) if match else None

    versions["const.py"] = _grab(const_path, r'^VERSION\s*=\s*["\']([^"\']+)["\']')
    versions["addon/config.yaml"] = _grab(
        addon_path, r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$'
    )
    versions["pyproject.toml"] = _grab(
        pyproject_path, r'^version\s*=\s*["\']([^"\']+)["\']'
    )
    versions["engine __init__.py"] = _grab(
        package_path, r'^__version__\s*=\s*["\']([^"\']+)["\']'
    )
    return versions


def current_version(root: Path = ROOT) -> str:
    """The canonical version, refusing to guess if the fields disagree."""
    versions = read_versions(root)
    present = {name: value for name, value in versions.items() if value}
    if not present:
        raise BumpError("no version field could be read")
    distinct = set(present.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{name}={value}" for name, value in present.items())
        raise BumpError(f"version fields disagree, refusing to bump: {detail}")
    version = versions["manifest.json"]
    assert version is not None
    return version


def next_version(current: str) -> str:
    """The next development version.

    Only a ``-dev.N`` prerelease can be incremented automatically; anything else
    is a human decision, and guessing would produce a version that sorts below
    the one it replaces.
    """
    match = SEMVER.match(current)
    if match is None:
        raise BumpError(f"{current!r} is not valid SemVer 2.0.0")

    pre = match.group("pre")
    if pre is None:
        raise BumpError(
            f"{current!r} is a stable version. An automatic bump would have to "
            "invent a core version, and 'X.Y.Z-dev.1' sorts BELOW 'X.Y.Z' so no "
            "updater would ever offer it. Set the core version by hand "
            "(e.g. 0.2.2-dev.1) and push again."
        )

    counter = DEV_COUNTER.match(pre)
    if counter is None:
        raise BumpError(
            f"{current!r} does not end in a '-dev.N' counter, so it cannot be "
            "incremented automatically. Use the dot form (e.g. 0.2.1-dev.1); a "
            "glued counter like 'dev9' sorts above 'dev10' and stops being "
            "offered."
        )

    major, minor, patch = (
        match.group("major"),
        match.group("minor"),
        match.group("patch"),
    )
    # Build metadata is ignored for precedence but is still part of the version
    # string, so dropping it would silently change what a build claims to be.
    build = match.group("build")
    suffix = f"+{build}" if build else ""
    return f"{major}.{minor}.{patch}-dev.{int(counter.group('counter')) + 1}{suffix}"


def _sub(path: Path, pattern: str, replacement: str) -> None:
    raw = path.read_text()
    updated, count = re.subn(pattern, replacement, raw, count=1, flags=re.MULTILINE)
    if count != 1:
        raise BumpError(f"{path.relative_to(ROOT)} did not match {pattern!r}")
    path.write_text(updated)


def write_versions(new_version: str, root: Path = ROOT) -> list[Path]:
    """Rewrite every version field, returning the files that changed.

    Each pattern is anchored to the exact line the field lives on, so a version
    string appearing elsewhere (a changelog, a comment) is never touched.
    """
    if SEMVER.match(new_version) is None:
        raise BumpError(f"{new_version!r} is not valid SemVer 2.0.0")

    manifest_path = root / MANIFEST.relative_to(ROOT)
    const_path = root / CONST.relative_to(ROOT)
    addon_path = root / ADDON.relative_to(ROOT)
    pyproject_path = root / PYPROJECT.relative_to(ROOT)
    package_path = root / PACKAGE_INIT.relative_to(ROOT)

    data = json.loads(manifest_path.read_text())
    data["version"] = new_version
    manifest_path.write_text(json.dumps(data, indent=2) + "\n")

    _sub(const_path, r'^(VERSION\s*=\s*)["\'][^"\']+["\']', rf'\g<1>"{new_version}"')
    _sub(addon_path, r'^(version:\s*)["\']?[^"\'\s]+["\']?\s*$', rf'\g<1>"{new_version}"')
    _sub(pyproject_path, r'^(version\s*=\s*)["\'][^"\']+["\']', rf'\g<1>"{new_version}"')
    _sub(
        package_path,
        r'^(__version__\s*=\s*)["\'][^"\']+["\']',
        rf'\g<1>"{new_version}"',
    )

    return [manifest_path, const_path, addon_path, pyproject_path, package_path]


def append_notes(
    version: str,
    body: str,
    *,
    root: Path = ROOT,
    channel: str = "development channel",
    today: date | None = None,
) -> Path:
    """Insert a ``## v<version>`` section above the newest existing one.

    ``check_release.py`` refuses to tag a version the notes do not document, and
    an automatically incremented version cannot have been written by hand.  The
    body is derived from the commits since the last tag, so the section says
    what actually changed rather than restating the version.
    """
    notes_path = root / NOTES.relative_to(ROOT)
    raw = notes_path.read_text()
    heading = re.compile(r"^##\s+v?\d+\.\d+\.\d+", re.MULTILINE)
    match = heading.search(raw)
    if match is None:
        raise BumpError("RELEASE_NOTES.md has no existing '## vX.Y.Z' section")

    stamp = (today or date.today()).isoformat()
    section = f"## v{version} — {stamp} ({channel})\n\n{body.strip()}\n\n"

    updated = raw[: match.start()] + section + raw[match.start() :]
    notes_path.write_text(updated)
    return notes_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--next", action="store_true", help="Print the next version.")
    parser.add_argument("--write", action="store_true", help="Rewrite the version fields.")
    parser.add_argument(
        "--notes-file",
        default=None,
        metavar="PATH",
        help="Insert this file's contents as the new RELEASE_NOTES.md section.",
    )
    args = parser.parse_args(argv)

    if not (args.next or args.write):
        parser.error("one of --next or --write is required")

    try:
        current = current_version()
        target = next_version(current)
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.next and not args.write:
        print(target)
        return 0

    try:
        write_versions(target)
        if args.notes_file:
            append_notes(target, Path(args.notes_file).read_text())
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{current} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
