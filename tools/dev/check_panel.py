"""Static checks for the Home Assistant ingress panel (``addon/ui``).

The panel is served by the gateway under a strict Content-Security-Policy and
must work with no network access beyond its own origin.  A hand-written panel
breaks inside the ingress frame in three predictable ways:

* it references an external origin, which the CSP blocks and the offline
  container cannot reach;
* it uses an inline ``<script>``/``<style>`` block or an ``on*`` attribute,
  which ``script-src 'self'`` refuses to run;
* it names an asset that was never shipped, so the shell half-loads.

It also gates the retired "dedicated CT" vocabulary.  ADR-0029 removed the CT
role, the monitoring level and the attribution field, so the panel must not
offer them.  One token survives on purpose: ``STRINGS.dedicated_device`` is an
explicit legacy alias whose *value* is ``Branch CT``.  Without it the
``titleCase`` fallback would render a stored legacy role as "Dedicated Device" —
a different wrong label rather than the branch label.  Check 7 allowlists
exactly that line and fails on every other match, so deleting the alias and
reintroducing the word are both caught.

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
REQUIRED = ("index.html", "app.css", "matrix.css", "main.js", "mark.svg")

#: The SVG namespace is a name, not a request.  Everything else that looks like
#: an absolute URL is an external reference the runtime cannot satisfy.
ALLOWED_URLS = ("http://www.w3.org/2000/svg",)

#: Elements whose open/close counts must balance in the static shell.  The
#: dynamic views are built by the ES modules and are not parsed here.
BALANCED_TAGS = ("html", "head", "body", "header", "nav", "main", "footer")

#: ADR-0029 retired the "dedicated CT" role, its monitoring level and its
#: attribution field.  The panel must not print the word, so AC-1 is a static
#: scan rather than a review checklist.
RETIRED_VOCABULARY = re.compile(r"dedicat", re.I)

#: The single allowlisted occurrence: the legacy alias key in ``STRINGS``, whose
#: value renders "Branch CT".  It is matched by *content*, not by line number, so
#: the allowlist cannot drift onto an unrelated line when code is inserted above
#: it — and editing the value back to "Dedicated Device" stops matching, which
#: is the regression that matters.  ``titleCase`` would render a stored legacy
#: role as "Dedicated Device" without this key.
LEGACY_ALIAS = re.compile(r"^\s*dedicated_device:\s*'Branch CT',\s*$", re.MULTILINE)


def retired_vocabulary_errors(name: str, text: str) -> list[str]:
    """Lines in ``text`` that name the retired concept, minus the alias line."""
    allowed = {
        text.count("\n", 0, match.start()) + 1 for match in LEGACY_ALIAS.finditer(text)
    }
    return [
        f"{name}:{lineno} still names the retired dedicated CT concept"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if RETIRED_VOCABULARY.search(line) and lineno not in allowed
    ]


# --------------------------------------------------------------------------- #
# AC-7: the badge text on a legacy payload
# --------------------------------------------------------------------------- #

#: The panel is shipped as ES modules; the repo has no JS
#: runner in the static-check path, so AC-7 ("the panel renders no dedicated text even on legacy data")
#: is checked by *replaying* the panel's own label lookup rather than by a DOM
#: render.  The table is parsed out of the shipped file, so this cannot pass
#: against a copy of the strings that has drifted from what the browser loads.
STRINGS_BLOCK = re.compile(r"const STRINGS = \{(.*?)\n\};", re.DOTALL)
STRINGS_ENTRY = re.compile(r"^\s*([a-z0-9_]+):\s*'([^']*)',?$", re.MULTILINE)

#: The payload shapes an older engine can still send.  ``dedicated_device`` is
#: the retired CT role; it is the shape that used to print "Dedicated Device"
#: through the ``titleCase`` fallback.
LEGACY_BADGE_PAYLOADS = ("dedicated_device",)


def panel_strings(script: str) -> dict[str, str]:
    """The panel's ``STRINGS`` table, parsed from the shipped ``core.js``."""
    block = STRINGS_BLOCK.search(script)
    if block is None:
        return {}
    return dict(STRINGS_ENTRY.findall(block.group(1)))


def _title_case(text: str) -> str:
    """A Python port of the panel's ``titleCase`` (the fallback ``label`` uses)."""
    spaced = re.sub(r"[_-]+", " ", str(text))
    return re.sub(r"\b\w", lambda match: match.group(0).upper(), spaced)


def render_label(value: str, strings: dict[str, str]) -> str:
    """``const label = (value) => STRINGS[value] || titleCase(value || 'unknown')``."""
    return strings.get(value) or _title_case(value or "unknown")


def render_smoke_errors(script: str) -> list[str]:
    """Replay the badge label for a legacy payload and reject retired text.

    This is the AC-7 smoke check.  It asserts the rendered text *is* ``Branch
    CT`` for the retired role, and that no legacy payload renders the word in
    any casing.  It also proves the replay is not vacuous by feeding an unknown
    value through the same fallback the panel uses.
    """
    strings = panel_strings(script)
    if not strings:
        return ["core.js: the STRINGS table could not be parsed for the AC-7 check"]

    errors: list[str] = []
    for ct_type in LEGACY_BADGE_PAYLOADS:
        rendered = render_label(ct_type, strings)
        if "dedicat" in rendered.lower():
            errors.append(
                f"panel renders a legacy {ct_type!r} channel as {rendered!r}; "
                "the badge must read 'Branch CT'"
            )
        elif rendered != "Branch CT":
            errors.append(
                f"panel renders a legacy {ct_type!r} channel as {rendered!r}, "
                "not 'Branch CT'"
            )
    if render_label("new_role", strings) != "New Role":
        errors.append(
            "panel: the titleCase replay is broken, so the AC-7 check proves nothing"
        )
    return errors


def check(root: Path) -> list[str]:
    errors: list[str] = []

    missing = [name for name in REQUIRED if not (root / name).is_file()]
    for name in missing:
        errors.append(f"missing addon/ui/{name}")
    if missing:
        return errors

    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "app.css").read_text(encoding="utf-8")
    svg = (root / "mark.svg").read_text(encoding="utf-8")
    # Every shipped ES module is scanned; the STRINGS table (checked below) is
    # parsed from core.js, where the shared vocabulary now lives.
    modules = sorted(p for p in root.glob("*.js") if p.is_file())
    js = "\n".join(p.read_text(encoding="utf-8") for p in modules)
    core_js = (root / "core.js").read_text(encoding="utf-8")

    # 1. No external origins anywhere.
    texts = [("index.html", html), ("app.css", css),
             ("matrix.css", (root / "matrix.css").read_text(encoding="utf-8")),
             ("mark.svg", svg)] + [(p.name, p.read_text(encoding="utf-8")) for p in modules]
    for name, text in texts:
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
        errors.append("panel uses eval()")
    if re.search(r"\bnew\s+Function\s*\(", js):
        errors.append("panel uses new Function()")
    if re.search(r"""fetch\(\s*[`'"]https?://""", js):
        errors.append("panel fetches an absolute URL")
    if re.search(r"\bdocument\.write\s*\(", js):
        errors.append("panel uses document.write()")

    # 7. No retired "dedicated CT" vocabulary.  AC-1 is about what the panel
    #    shows, and the word can only reach it from a shipped asset, so every
    #    file is scanned; the ``STRINGS.dedicated_device`` alias is the one
    #    allowlisted line and it renders "Branch CT".
    for name, text in texts:
        errors.extend(retired_vocabulary_errors(name, text))

    # 8. AC-7: a legacy payload must still render the branch label.
    errors.extend(render_smoke_errors(core_js))

    # 9. Every relative ES-module import must name a shipped file.  A dangling
    #    import would make the panel half-load under ingress with a console error.
    for p in modules:
        text = p.read_text(encoding="utf-8")
        for spec in re.findall(r"(?:import|export)[^;'\n]*?from\s*['\"]([^'\"]+)['\"]", text):
            if spec.startswith("."):
                target = (p.parent / spec).resolve()
                if not target.is_file():
                    errors.append(f"{p.name} imports missing module {spec}")

    return errors


def main() -> int:
    errors = check(UI)
    if errors:
        print("panel check failed:", file=sys.stderr)
        for error in errors:
            print("  " + error, file=sys.stderr)
        return 1
    print(f"panel check: {len(REQUIRED)} required assets, {len(list(UI.glob('*.js')))} modules, self-contained and CSP-safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
