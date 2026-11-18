"""Shared node harness for the panel's native ES modules.

The panel is shipped as flat ES modules under ``addon/ui``.  Node can import
them directly with a DOM shim on ``globalThis``; this module provides:

* the DOM shim source (two variants: a read-only card shim and an event-capable
  one), reused by the panel tests;
* :func:`import_program`, which emits a small ESM driver that imports real
  modules and prints a JSON result;
* :func:`run_esm`, which runs such a driver under node in a temp directory
  containing copies of the modules.

The shims and helpers used to live inline in ``test_panel_data.py``; they are
centralised here so the module-based tests all execute the same real code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "addon" / "ui"

#: Minimal DOM stand-in for the read-only card renderers.
DOM_SHIM = r"""
class Node {
  constructor() {
    this.children = [];
    this._text = '';
    this.dataset = {};
    this.attrs = {};
    this.className = '';
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (c) => (c instanceof Node ? c.textContent : String(c))
    ).join('');
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...cs) { for (const c of cs) this.children.push(c); }
  get firstChild() { return this.children[0] || null; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); }
}
global.Node = Node;
global.document = {
  baseURI: 'http://localhost/',
  createElement: (tag) => { const n = new Node(); n.tagName = tag; return n; },
  createTextNode: (t) => { const n = new Node(); n._text = String(t); return n; },
  createDocumentFragment: () => new Node(),
  getElementById: () => null,
  querySelectorAll: () => [],
};
global.location = { href: 'http://localhost/', hash: '', replace() {} };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
"""

#: Event-capable DOM stand-in (adds listeners, id lookup, value/disabled).
EVENT_DOM_SHIM = r"""
class Node {
  constructor() {
    this.children = [];
    this._text = '';
    this.dataset = {};
    this.attrs = {};
    this.className = '';
    this._listeners = {};
    this._parent = null;
    this.value = '';
    this.disabled = false;
    this.id = '';
    this.tagName = '';
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (c) => (c instanceof Node ? c.textContent : String(c))
    ).join('');
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  fire(type, ev) {
    for (const fn of (this._listeners[type] || [])) fn.call(this, ev || { target: this });
  }
  append(...cs) {
    for (const c of cs) {
      if (c instanceof Node) c._parent = this;
      this.children.push(c);
    }
  }
  remove() {
    if (this._parent) {
      const i = this._parent.children.indexOf(this);
      if (i >= 0) this._parent.children.splice(i, 1);
      this._parent = null;
    }
  }
  get firstChild() { return this.children[0] || null; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); }
}
global.Node = Node;
function _walk(node, out) {
  for (const c of node.children) {
    if (c instanceof Node) { if (c.id) out.push(c); _walk(c, out); }
  }
  return out;
}
const _body = new Node(); _body.tagName = 'body';
global.document = {
  baseURI: 'http://localhost/',
  body: _body,
  createElement: (tag) => { const n = new Node(); n.tagName = tag; return n; },
  createTextNode: (t) => { const n = new Node(); n._text = String(t); return n; },
  createDocumentFragment: () => new Node(),
  getElementById: (id) => _walk(_body, []).find((n) => n.id === id) || null,
  querySelectorAll: () => [],
};
global.location = { href: 'http://localhost/', hash: '', replace() {} };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
"""


def node_path() -> str | None:
    """The node executable, or ``None`` when node is unavailable."""
    import shutil

    return shutil.which("node")


def run_esm(program: str, *, shim: str = DOM_SHIM, modules: tuple[str, ...] = ()) -> dict:
    """Execute an ESM program under node against copies of the real modules.

    ``program`` must import the modules it uses via relative paths (e.g.
    ``import { el } from './core.js'``) and end with a
    ``console.log(JSON.stringify(...))``.  Returns the parsed JSON.
    """
    import tempfile

    node = node_path()
    if not node:
        raise RuntimeError("node is not installed")

    with tempfile.TemporaryDirectory(prefix="wm-panel-") as tmp:
        target = Path(tmp)
        # A full copy keeps relative imports (./core.js etc.) working.
        for src in UI_DIR.glob("*.js"):
            shutil.copy2(src, target / src.name)
        # Static imports are hoisted above the shim, so rewrite leading
        # ``import {X} from './m.js';`` statements into dynamic imports that run
        # only after the DOM shim is installed on globalThis.
        import re

        stmts = re.findall(r"import \{([^}]*)\} from '([^']+)';", program)
        body = re.sub(r"import \{[^}]*\} from '[^']+';", "", program)
        preamble = ""
        for names, spec in stmts:
            parts = []
            for part in names.split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    orig, alias = (p.strip() for p in part.split(" as ", 1))
                    parts.append(f"{orig}: {alias}")
                else:
                    parts.append(part)
            preamble += f"const {{ {', '.join(parts)} }} = await import('{spec}');\n"
        driver = target / "driver.mjs"
        driver.write_text(shim + "\n" + preamble + body, encoding="utf-8")
        completed = subprocess.run(
            [node, str(driver)],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(target),
        )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines, f"no JSON result from node: {completed.stdout!r}\n{completed.stderr}"
    return json.loads(lines[-1])


def lit(value) -> str:
    """A JS literal for a Python value."""
    return json.dumps(value)
