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
   and a plain ``X.Y.Z`` tag must;
5. a prerelease counter must be a **separate numeric identifier**
   (``-dev.10``), never glued to the word (``-dev10``).

Rule 5 is not style.  SemVer 2.0.0 compares a prerelease identifier that
contains letters **lexically**, so ``0.2.0-dev9 > 0.2.0-dev10``.  A channel
numbered ``dev1, dev2, ... dev10`` therefore *disappears* at ``dev10``: the
updater offers ``dev9`` as the newest release and the last build is never
installed.  Splitting the counter into its own numeric identifier makes
``0.2.1-dev.10 > 0.2.1-dev.9`` true by the same rule.

Escaping an already-published glued series also needs a **new core version**:
``0.2.1-dev.10`` still sorts below ``0.2.0-dev9``, because the comparison reaches
the identifier ``"dev"`` vs ``"dev9"`` first.  That is why this repository moved
to ``0.2.1-dev.1`` rather than ``0.2.0-dev.10``.

For an **ordinary commit** (no tag), the same version fields must at least agree
with each other, and ``RELEASE_NOTES.md`` must document the current version.

Channels
--------
``main`` is the development line and carries ``X.Y.Z-dev.N``; ``stable`` carries
``X.Y.Z``.  See :func:`check_channel` for why the development line is ``main``
and why that cannot reach a stable user.

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

    # Checked once on the canonical version; every field must equal it anyway.
    if versions["manifest.json"]:
        check_prerelease_numbering(versions["manifest.json"])

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

    check_prerelease_numbering(tag_version)

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

    ``main`` is the **development** line: every push publishes a prerelease, so
    the version there must carry a ``-dev.N`` suffix.  ``stable`` is the frozen
    stable line and may only carry a plain ``X.Y.Z``.

    Why ``main`` is the development line
    ------------------------------------
    The Supervisor updates an App only when ``addon/config.yaml``'s ``version:``
    changes, and it reads that file from the branch the repository URL pins.  A
    separate ``dev`` branch therefore meant every improvement had to be
    cherry-picked across two long-lived branches before the App could see it.
    Making ``main`` the development line removes that fork: one branch, one
    place to merge, and the App follows it directly.

    Stable users are not affected by this, and that is not a matter of trust:
    the Marketplace ignores prereleases unless the beta toggle is enabled
    (``show_beta`` defaults to false) and only ever names the newest
    *non*-prerelease Release as ``last_version``.  So a stable install stays on
    the last stable tag no matter how many ``-dev.N`` releases ``main``
    publishes.  A prerelease on ``main`` therefore cannot reach a stable user,
    which is why it is allowed here and required on ``stable``.

    The tag is derived from the version string, so the version string is what
    has to be checked.
    """
    current = manifest_version()
    if current is None:
        return
    semver = SEMVER.match(current)
    if semver is None:
        return  # already reported by check_version_fields

    is_prerelease = bool(semver.group("pre"))
    if channel == "main" and not is_prerelease:
        error(
            f"'{channel}' is the development channel but the tree version {current!r} "
            "is a stable version. A stable version published from main would be "
            "offered to every stable user as 'latest'. Add a '-dev.N' suffix, or "
            "release stable versions from the 'stable' branch."
        )
    elif channel == "stable" and is_prerelease:
        error(
            f"'{channel}' is the stable channel but the tree version {current!r} is a "
            "prerelease. Stable users would be offered it as an update. Release "
            "prereleases from 'main'."
        )
    elif channel == "main":
        note(f"{current} is a prerelease, as required on the development channel (main)")
    else:
        note(f"{current} is stable, as required on the stable channel")


def check_prerelease_numbering(version: str) -> None:
    """A prerelease counter must not be glued to the word once it is two digits.

    SemVer 2.0.0 compares an identifier containing letters **lexically**:

        "dev10" < "dev9"        (true, and surprising)
        "dev9"  > "dev8"        (also true -- single digits appear fine)

    So a channel numbered ``-dev1, -dev2, ... -dev9`` looks healthy and then
    *disappears* at ``-dev10``: every updater keeps offering ``-dev9`` as the
    newest release, the new build is never installed, and nothing is logged.
    The tag, the Release and the image all exist; the update entity just says
    "up to date".

    Only a counter of two or more digits is already broken, so only that is an
    error.  A single-digit glued counter is still ordered correctly today, but
    it is a trap that springs at the tenth release, so it is reported as a note
    rather than silently accepted.

    The fix is the standard SemVer form: make the counter its own numeric
    identifier (``-dev.10``).  Numeric identifiers compare numerically, so it
    increments forever.  Note that changing only the separator is not enough to
    overtake an existing glued release -- ``-dev.10`` still compares below
    ``-dev9`` because ``"dev" < "dev9"`` -- so the *core* version must move too
    when escaping an already-published glued series.
    """
    match = SEMVER.match(version)
    if match is None or not match.group("pre"):
        return

    for identifier in match.group("pre").split("."):
        if identifier.isdigit():
            continue
        glued = re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*?)(\d+)", identifier)
        if glued is None:
            continue
        stem, counter = glued.group(1), glued.group(2)
        if len(counter) >= 2:
            error(
                f"version {version!r} glues a {len(counter)}-digit counter to the "
                f"word ({identifier!r}). SemVer compares an identifier containing "
                f"letters lexically, so '{identifier}' sorts BELOW "
                f"'{stem}{int(counter) - 1}' and the updater would never offer it. "
                f"Write the counter as its own numeric identifier: '{stem}.{counter}' "
                "(and move the core version past the last published glued release)."
            )
            return
        note(
            f"version {version!r} uses the glued counter {identifier!r}. It orders "
            f"correctly at one digit, but at '{stem}10' it would sort below "
            f"'{stem}9'. Prefer '{stem}.{counter}' for the next release."
        )
        return


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
        choices=("main", "stable"),
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
