"""Static checks for the Home Assistant ingress panel (``addon/ui``).

The panel is served by the gateway under a strict Content-Security-Policy and
must work with no network access beyond its own origin.  A hand-written panel
breaks inside the ingress frame in three predictable ways:

* it references an external origin, which the CSP blocks and the offline
  container cannot reach;
* it uses an inline ``<script>``/``<style>`` block or an ``on*`` attribute,
  which ``script-src 'self'`` refuses to run;
* it names an asset that was never shipped, so the shell half-loads.

These checks are static on purpose: they need no browser and no Docker, so they
run in the same fast path as the rest of ``scripts/validate_addon.sh``.  The
gateway-side behaviour (ingress gating, headers, asset content types, traversal
refusal) is covered by ``tests/unit/test_panel.py``.

Run: python tools/dev/check_panel.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "addon" / "ui"
REQUIRED = ("index.html", "app.css", "app.js", "mark.svg")

#: The SVG namespace is a name, not a request.  Everything else that looks like
#: an absolute URL is an external reference the runtime cannot satisfy.
ALLOWED_URLS = ("http://www.w3.org/2000/svg",)

#: Elements whose open/close counts must balance in the static shell.  The
#: dynamic views are built by ``app.js`` and are not parsed here.
BALANCED_TAGS = ("html", "head", "body", "header", "nav", "main", "footer")


def check(root: Path) -> list[str]:
    errors: list[str] = []

    missing = [name for name in REQUIRED if not (root / name).is_file()]
    for name in missing:
        errors.append(f"missing addon/ui/{name}")
    if missing:
        return errors

    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "app.css").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    svg = (root / "mark.svg").read_text(encoding="utf-8")

    # 1. No external origins anywhere.
    for name, text in (
        ("index.html", html),
        ("app.css", css),
        ("app.js", js),
        ("mark.svg", svg),
    ):
        stripped = text
        for allowed in ALLOWED_URLS:
            stripped = stripped.replace(allowed, "")
        if "http://" in stripped or "https://" in stripped:
            errors.append(f"{name} references an external URL")

    # 2. No inline event handlers (CSP script-src 'self' blocks them).
    for match in re.finditer(r"\son[a-z]+\s*=", html, re.I):
        snippet = html[match.start() : match.start() + 32]
        errors.append(f"index.html has an inline event handler near {snippet!r}")

    # 3. No inline <script>/<style> blocks in the shell.
    if re.search(r"<script(?![^>]*\bsrc=)", html, re.I):
        errors.append("index.html has an inline <script> block")
    if re.search(r"<style\b", html, re.I):
        errors.append("index.html has an inline <style> block")

    # 4. Every relative asset the shell names is actually shipped.  Absolute
    #    URLs are already reported by check 1, so skip them here.  The shell is
    #    served from ``/`` while the files live in ``addon/ui``, hence the
    #    ``ui/`` prefix mapping back onto the UI directory's parent.
    for path in re.findall(r"""(?:src|href)\s*=\s*["']([^"'#]+)["']""", html):
        if path.startswith(("http:", "https:", "data:", "#", "mailto:")):
            continue
        target = root / path[len("ui/") :] if path.startswith("ui/") else root / path
        if ".." in path or not target.is_file():
            errors.append(f"index.html references missing asset {path}")

    # 5. Balanced tags for the elements the static shell uses.
    for tag in BALANCED_TAGS:
        opens = len(re.findall(rf"<{tag}\b", html, re.I))
        closes = len(re.findall(rf"</{tag}>", html, re.I))
        if opens != closes:
            errors.append(f"index.html has {opens} <{tag}> and {closes} </{tag}>")

    # 6. The script must not build code at runtime.  Inline handlers are
    #    already gone, but eval/new Function would reintroduce the same hole.
    if re.search(r"\beval\s*\(", js):
        errors.append("app.js uses eval()")
    if re.search(r"\bnew\s+Function\s*\(", js):
        errors.append("app.js uses new Function()")
    if re.search(r"""fetch\(\s*[`'"]https?://""", js):
        errors.append("app.js fetches an absolute URL")
    if re.search(r"\bdocument\.write\s*\(", js):
        errors.append("app.js uses document.write()")

    return errors


def main() -> int:
    errors = check(UI)
    if errors:
        print("panel check failed:", file=sys.stderr)
        for error in errors:
            print("  " + error, file=sys.stderr)
        return 1
    print(f"panel check: {len(REQUIRED)} assets, self-contained and CSP-safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
