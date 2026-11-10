"""Print the image revision from ``docker buildx imagetools inspect`` JSON.

Used by ``.github/workflows/release.yaml`` to answer one question: *which
commit was the published image built from?*  That decides whether a push has
already been published, so getting it wrong is not cosmetic.

Why this is a file and not an inline ``python -c``
-------------------------------------------------
The workflow originally read the label with a one-liner:

    docker buildx imagetools inspect "$tag" --format '{{json .Image}}' \
      | python -c '... (json.load(sys.stdin).get("config") or {}).get("Labels", {}) ...'

``--format '{{json .Image}}'`` does **not** return a flat image object.  On a
multi-arch manifest list it returns an object keyed by platform::

    {"linux/amd64": {"config": {"Labels": {...}}}, "linux/arm64": {...}}

So ``.config.Labels`` at the top level was always empty, the shell captured an
empty string, and the stale-tag guard took its "already published from this
commit" branch unconditionally.  It reported success for an image built from a
*different* commit.  A check that fails open is worse than no check: it asserts
the tree is fine.

The one-liner was also unindentable inside a YAML block scalar, so it could not
be exercised outside CI.  This script takes the JSON on stdin and prints one
revision, which makes it testable against a recorded fixture.

Exit status
-----------
0  one revision printed (possibly empty when the image carries no label)
1  the manifest list is internally inconsistent
2  the input is not the expected JSON shape

An empty revision is deliberately *not* an error here: the caller decides, and
the caller must treat empty as "cannot tell" rather than "matches".

Usage::

    docker buildx imagetools inspect "$tag" --format '{{json .Image}}' \\
        | python tools/dev/image_revision.py
"""

from __future__ import annotations

import json
import sys

LABEL = "org.opencontainers.image.revision"

#: Key under which ``imagetools`` nests a single-platform result.
_PLATFORM_HINTS = ("config", "architecture", "os")


def image_entries(data: object) -> list[dict]:
    """Return the per-image objects from either response shape.

    ``imagetools`` returns a flat object for a single-platform tag and a
    platform-keyed object for a manifest list.  Both appear in practice.
    """
    if not isinstance(data, dict):
        return []
    if any(key in data for key in _PLATFORM_HINTS):
        return [data]
    return [value for value in data.values() if isinstance(value, dict)]


def revisions(data: object) -> set[str]:
    """Every distinct non-empty revision label in the response."""
    found: set[str] = set()
    for entry in image_entries(data):
        config = entry.get("config") or {}
        labels = config.get("Labels") or {}
        revision = labels.get(LABEL)
        if isinstance(revision, str) and revision:
            found.add(revision)
    return found


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not image_entries(data):
        print(
            "error: no image entries found; expected either a flat image object "
            "or a platform-keyed manifest list",
            file=sys.stderr,
        )
        return 2

    found = revisions(data)
    if len(found) > 1:
        # Architectures built from different commits mean the manifest list is
        # inconsistent.  There is no safe revision to report, and guessing one
        # would hide the defect.
        print(
            f"error: manifest list carries inconsistent {LABEL} values: "
            f"{sorted(found)}",
            file=sys.stderr,
        )
        return 1

    print(next(iter(found)) if found else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
