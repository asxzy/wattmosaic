"""Real-browser regression test for the Matrix tab click (matrix-tab-e2e).

The Matrix tab is the last link in the top navigation. Between roughly 761px
and 860px of viewport width the nav used to scroll horizontally; with six
sections the last link was clipped with no visible affordance, so a user click
at its apparent position landed on the element behind it and never changed the
hash. This test drives a real Chromium via Playwright at that exact viewport,
clicks the tab without auto-scrolling it into view, and asserts that the Matrix
view renders.

Playwright and a downloaded Chromium are development-only tools; the panel
itself needs neither at runtime. The test skips when they are unavailable so a
plain unit-test run stays green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "addon" / "ui"

# Playwright's chromium.executablePath() points at the revision the installed
# playwright expects, which is frequently not present on developer machines.
# Fall back to any downloaded Chromium in the standard ms-playwright cache.
_BROWSER_GLOBS = (
    "Library/Caches/ms-playwright/chromium-*/chrome-mac-*/"
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "Library/Caches/ms-playwright/chromium_headless_shell-*/"
    "chrome-headless-shell-*/chrome-headless-shell",
    ".cache/ms-playwright/chromium-*/chrome-linux/chrome",
)


def _playwright_and_browser():
    """Return (node, playwright_dir, executable_path) or None if unavailable."""
    node = shutil.which("node")
    if not node:
        return None
    probe = subprocess.run(
        [node, "-e", "console.log(require.resolve('playwright'))"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None
    resolved = probe.stdout.strip()
    playwright_dir = str(Path(resolved).parents[1])  # .../node_modules/playwright -> .../node_modules
    home = Path.home()
    exe: str | None = None
    for pattern in _BROWSER_GLOBS:
        matches = sorted(home.glob(pattern))
        if matches:
            exe = str(matches[-1])
            break
    if not exe:
        return None
    return node, playwright_dir, exe


_PW = _playwright_and_browser()

# Mock API payloads so rendering is deterministic and needs no live engine.
_API = {
    "/api/v1/health": {"status": "ok", "details": {"topology": {}}},
    "/api/v1/status": {"inference": {"backend": "WattMosaicBackend"}, "topology_version": 3},
    "/api/v1/snapshot": {"results": []},
    "/api/v1/devices": [],
    "/api/v1/mapping": {
        "coverage": {"counts": {"cell_count": 0}},
        "topology_version": 3,
        "cells": [],
    },
}


def test_matrix_tab_click_renders_view() -> None:
    """Clicking the Matrix tab at the clipped width must render the Matrix view."""
    if _PW is None:
        pytest.skip("Playwright / Chromium not available; browser e2e is optional")
    node, playwright_dir, exe = _PW

    index_html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    # Inline the real CSS/JS so the page needs no static-file server. The app
    # fetches only same-origin /api/v1/* paths, which Playwright intercepts.
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    app_js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    html = (
        index_html.replace('<link rel="stylesheet" href="ui/app.css">', f"<style>{css}</style>")
        .replace('<script src="ui/app.js" defer></script>', f"<script>{app_js}</script>")
    )

    script = r"""
const { chromium } = require('playwright');
const [exe, htmlB64, apiB64] = process.argv.slice(1);
const html = Buffer.from(htmlB64, 'base64').toString('utf8');
const api = JSON.parse(Buffer.from(apiB64, 'base64').toString('utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: exe, headless: true, args: ['--no-sandbox'] });
  // The width that used to clip the last nav link.
  const page = await browser.newPage({ viewport: { width: 780, height: 800 } });
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  await page.route('**/api/v1/**', route => {
    const key = new URL(route.request().url()).pathname;
    route.fulfill({ status: 200, contentType: 'application/json',
                    body: JSON.stringify(api[key] || {}) });
  });
  // Fulfill the shell from a stable http origin so API fetches stay same-origin.
  await page.route('http://panel.test/', route =>
    route.fulfill({ status: 200, contentType: 'text/html', body: html }));
  await page.goto('http://panel.test/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);

  const link = page.locator('nav.nav [data-nav="matrix"]');
  const box = await link.boundingBox();
  // Click at the link's own coordinates WITHOUT auto-scroll; this is what a
  // user does. If the link is clipped behind another element, the hash stays put.
  const hit = await page.evaluate(({x, y}) => {
    const e = document.elementFromPoint(x, y);
    return e ? (e.getAttribute('data-nav') || e.tagName) : null;
  }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(400);

  const result = {
    hash: await page.evaluate(() => location.hash),
    hit,
    viewHead: (await page.locator('#view').innerText()).split('\n')[0],
    hasCells: (await page.locator('#view').innerText()).includes('Mapping matrix'),
    hasLedger: (await page.locator('#view').innerText()).includes('source ledger'),
    hasIdent: (await page.locator('#view').innerText()).includes('identifiability'),
    ariaCurrent: await link.getAttribute('aria-current'),
    pageErrors,
  };
  console.log('__RESULT__' + JSON.stringify(result));
  await browser.close();
})().catch(e => { console.error('SCRIPT_FAIL', e); process.exit(2); });
"""
    import base64

    completed = subprocess.run(
        [
            node,
            "-e",
            script,
            exe,
            base64.b64encode(html.encode()).decode(),
            base64.b64encode(json.dumps(_API).encode()).decode(),
        ],
        capture_output=True,
        text=True,
        env={"NODE_PATH": playwright_dir, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        timeout=60,
    )
    assert completed.returncode == 0, f"browser script failed: {completed.stderr}"
    marker = "__RESULT__"
    assert marker in completed.stdout, f"no result from browser: {completed.stdout}"
    result = json.loads(completed.stdout.split(marker, 1)[1].strip().splitlines()[0])

    assert result["hit"] == "matrix", f"click landed on {result['hit']!r}, not the Matrix link"
    assert result["hash"] == "#/matrix", f"hash after click was {result['hash']!r}"
    assert result["viewHead"] == "Matrix & internal process", result["viewHead"]
    assert result["hasCells"] and result["hasLedger"] and result["hasIdent"], result
    assert result["ariaCurrent"] == "page", result["ariaCurrent"]
    assert not result["pageErrors"], result["pageErrors"]
