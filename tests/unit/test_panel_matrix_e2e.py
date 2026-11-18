"""Real-browser tests for the user-facing Matrix tab (U1).

The panel is served as flat native ES modules.  These tests serve the real
modules under a fake HA-ingress path prefix and intercept the engine API, so a
relative ``import './core.js'`` must resolve through the prefix and every API
call (including ``GET /api/v1/matrix``) must stay relative to the document base.

Between roughly 761px and 860px of viewport width the Matrix nav link used to
be clipped with no visible affordance, so a click at its apparent position
landed on the element behind it.  This test drives a real Chromium via
Playwright at that exact viewport, clicks the tab without auto-scrolling it into
view, and asserts that the Matrix view renders from the K05 fixture.

Playwright and a downloaded Chromium are development-only tools; the panel
itself needs neither at runtime.  The test skips when they are unavailable so a
plain unit-test run stays green.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "addon" / "ui"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "matrix" / "hb17_payload.json"

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
    playwright_dir = str(Path(resolved).parents[1])  # .../node_modules
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

_MATRIX = json.loads(FIXTURE.read_text(encoding="utf-8"))

# Mock API payloads so rendering is deterministic and needs no live engine.
# Crucially, /matrix is served relative to the ingress prefix (core.js resolves
# it as <base>api/v1/matrix) and /status carries a mapping_version_id for the
# cheap poll.  NO /mapping fallback is provided: the view must never use it.
_API = {
    "/health": {"status": "ok", "details": {"topology": {}}},
    "/status": {
        "inference": {"backend": "WattMosaicBackend"},
        "topology_version": 3,
        "mapping_version_id": _MATRIX["mapping_version_id"],
    },
    "/snapshot": {"results": []},
    "/devices": [],
    "/matrix": _MATRIX,
}

_SCRIPT = r"""
const { chromium } = require('playwright');
const [exe, htmlB64, apiB64, modulesB64, ingress] = process.argv.slice(1);
const html = Buffer.from(htmlB64, 'base64').toString('utf8');
const api = JSON.parse(Buffer.from(apiB64, 'base64').toString('utf8'));
const modules = JSON.parse(Buffer.from(modulesB64, 'base64').toString('utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: exe, headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 780, height: 800 } });
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  await page.route('**/api/v1/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    // core.js resolves every path against the ingress base, so the API path
    // is the suffix after the LAST '/api/v1'.
    const key = pathname.slice(pathname.lastIndexOf('/api/v1') + '/api/v1'.length);
    if (!(key in api)) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    return route.fulfill({ status: 200, contentType: 'application/json',
                           body: JSON.stringify(api[key]) });
  });
  // Serve the real ES modules through the ingress prefix with a JS MIME type.
  for (const [name, source] of Object.entries(modules)) {
    await page.route('http://panel.test' + ingress + 'ui/' + name, route =>
      route.fulfill({ status: 200, contentType: 'text/javascript', body: source }));
  }
  await page.route('http://panel.test' + ingress, route =>
    route.fulfill({ status: 200, contentType: 'text/html', body: html }));
  await page.goto('http://panel.test' + ingress, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(400);

  const link = page.locator('nav.nav [data-nav="matrix"]');
  const box = await link.boundingBox();
  const hit = await page.evaluate(({x, y}) => {
    const e = document.elementFromPoint(x, y);
    return e ? (e.getAttribute('data-nav') || e.tagName) : null;
  }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(500);

  const viewText = await page.locator('#view').innerText();
  const result = {
    hash: await page.evaluate(() => location.hash),
    hit,
    viewHead: viewText.split('\n')[0],
    hasGrid: (await page.locator('table.matrix-grid').count()) > 0,
    headerRows: await page.locator('table.matrix-grid thead tr').count(),
    hasLegend: viewText.includes('Directly measured') && viewText.includes('Group-only'),
    hasUnavailable: viewText.includes('Matrix API unavailable'),
    hasLegacyTitle: viewText.includes('internal process'),
    ariaCurrent: await link.getAttribute('aria-current'),
    pageErrors,
  };
  console.log('__RESULT__' + JSON.stringify(result));
  await browser.close();
})().catch(e => { console.error('SCRIPT_FAIL', e); process.exit(2); });
"""


def _run(api: dict) -> dict:
    node, playwright_dir, exe = _PW
    index_html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    matrix_css = (UI_DIR / "matrix.css").read_text(encoding="utf-8")
    ingress = "/api/hassio_ingress/fake-token/"
    html = (
        index_html.replace('<link rel="stylesheet" href="ui/app.css">', f"<style>{css}</style>")
        .replace('<link rel="stylesheet" href="ui/matrix.css">', f"<style>{matrix_css}</style>")
        .replace('<script type="module" src="ui/main.js"></script>',
                 f'<script type="module" src="{ingress}ui/main.js"></script>')
    )
    modules = {p.name: p.read_text(encoding="utf-8") for p in UI_DIR.glob("*.js")}
    completed = subprocess.run(
        [node, "-e", _SCRIPT, exe,
         base64.b64encode(html.encode()).decode(),
         base64.b64encode(json.dumps(api).encode()).decode(),
         base64.b64encode(json.dumps(modules).encode()).decode(),
         ingress],
        capture_output=True,
        text=True,
        env={"NODE_PATH": playwright_dir, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        timeout=60,
    )
    assert completed.returncode == 0, f"browser script failed: {completed.stderr}"
    marker = "__RESULT__"
    assert marker in completed.stdout, f"no result from browser: {completed.stdout}"
    return json.loads(completed.stdout.split(marker, 1)[1].strip().splitlines()[0])


def test_matrix_tab_click_renders_v3_grid() -> None:
    """Clicking the clipped Matrix tab must render the v3, user-facing grid."""
    if _PW is None:
        pytest.skip("Playwright / Chromium not available; browser e2e is optional")
    result = _run(_API)
    assert result["hit"] == "matrix", f"click landed on {result['hit']!r}, not the Matrix link"
    assert result["hash"] == "#/matrix", f"hash after click was {result['hash']!r}"
    assert result["viewHead"] == "Matrix", (result, result["pageErrors"])
    assert result["hasGrid"], "the v3 grid must render from GET /matrix"
    assert result["headerRows"] == 5, "L0–L4 header rows (AT-12)"
    assert result["hasLegend"], "all eight categories have distinct text"
    assert not result["hasUnavailable"], result
    assert not result["hasLegacyTitle"], "the matrix must not be framed as a debug page"
    assert result["ariaCurrent"] == "page", result["ariaCurrent"]
    assert not result["pageErrors"], result["pageErrors"]


def test_matrix_404_renders_unavailable_state() -> None:
    """A missing /matrix renders an explicit unavailable state, never /mapping."""
    if _PW is None:
        pytest.skip("Playwright / Chromium not available; browser e2e is optional")
    api = dict(_API)
    api.pop("/matrix")
    result = _run(api)
    assert result["hash"] == "#/matrix"
    assert result["hasUnavailable"], result
    assert not result["hasGrid"], "no grid without a v3 payload"
    assert not result["pageErrors"], result["pageErrors"]


_POLL_SCRIPT = r"""
const { chromium } = require('playwright');
const [exe, htmlB64, apiB64, modulesB64, ingress] = process.argv.slice(1);
const html = Buffer.from(htmlB64, 'base64').toString('utf8');
const api = JSON.parse(Buffer.from(apiB64, 'base64').toString('utf8'));
const modules = JSON.parse(Buffer.from(modulesB64, 'base64').toString('utf8'));
(async () => {
  const browser = await chromium.launch({ executablePath: exe, headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 780, height: 800 } });
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  let matrixFetches = 0;
  let currentVersion = api['/status'].mapping_version_id;
  await page.addInitScript(() => { window.__WM_MATRIX_POLL_MS = 120; });
  await page.route('**/api/v1/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    const key = pathname.slice(pathname.lastIndexOf('/api/v1') + '/api/v1'.length);
    if (key === '/matrix') matrixFetches += 1;
    const payload = key === '/status'
      ? { ...api[key], mapping_version_id: currentVersion }
      : api[key];
    if (payload === undefined) return route.fulfill({ status: 404, body: '{}' });
    return route.fulfill({ status: 200, contentType: 'application/json',
                           body: JSON.stringify(payload) });
  });
  for (const [name, source] of Object.entries(modules)) {
    await page.route('http://panel.test' + ingress + 'ui/' + name, route =>
      route.fulfill({ status: 200, contentType: 'text/javascript', body: source }));
  }
  await page.route('http://panel.test' + ingress, route =>
    route.fulfill({ status: 200, contentType: 'text/html', body: html }));
  await page.goto('http://panel.test' + ingress, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(200);
  await page.evaluate(() => { location.hash = '#/matrix'; });
  await page.waitForTimeout(600);
  const unchanged = matrixFetches;
  // Simulate a mapping-version bump; the next poll must refetch exactly once.
  currentVersion = 'map-8';
  await page.waitForTimeout(600);
  const changed = matrixFetches;
  console.log('__RESULT__' + JSON.stringify({ unchanged, changed, pageErrors }));
  await browser.close();
})().catch(e => { console.error('SCRIPT_FAIL', e); process.exit(2); });
"""


def test_matrix_polls_status_and_refetches_only_on_version_change() -> None:
    """Polling /status must not refetch /matrix until the version changes."""
    if _PW is None:
        pytest.skip("Playwright / Chromium not available; browser e2e is optional")
    node, playwright_dir, exe = _PW
    index_html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    matrix_css = (UI_DIR / "matrix.css").read_text(encoding="utf-8")
    ingress = "/api/hassio_ingress/fake-token/"
    html = (
        index_html.replace('<link rel="stylesheet" href="ui/app.css">', f"<style>{css}</style>")
        .replace('<link rel="stylesheet" href="ui/matrix.css">', f"<style>{matrix_css}</style>")
        .replace('<script type="module" src="ui/main.js"></script>',
                 f'<script type="module" src="{ingress}ui/main.js"></script>')
    )
    modules = {p.name: p.read_text(encoding="utf-8") for p in UI_DIR.glob("*.js")}
    completed = subprocess.run(
        [node, "-e", _POLL_SCRIPT, exe,
         base64.b64encode(html.encode()).decode(),
         base64.b64encode(json.dumps(_API).encode()).decode(),
         base64.b64encode(json.dumps(modules).encode()).decode(),
         ingress],
        capture_output=True,
        text=True,
        env={"NODE_PATH": playwright_dir, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        timeout=60,
    )
    assert completed.returncode == 0, f"browser script failed: {completed.stderr}"
    result = json.loads(completed.stdout.split("__RESULT__", 1)[1].strip().splitlines()[0])
    assert result["unchanged"] == 1, f"unchanged version refetched: {result}"
    assert result["changed"] == 2, f"version change did not refetch exactly once: {result}"
    assert not result["pageErrors"], result["pageErrors"]
