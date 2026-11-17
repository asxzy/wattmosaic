"""Data-rendering tests for the panel's derived backend-data cards (B4-UIa).

``addon/ui/app.js`` is a plain browser script with no module seam, so — exactly
as ``test_panel.py`` does for the window helpers — these tests *execute* a slice
of the real shipped script under node rather than pattern-matching it.  A tiny
DOM shim implements the handful of primitives the render helpers use
(``document.createElement``, ``Node``, ``textContent``, ``append``), so the
assertions are on the text the browser would actually put on screen when the
engine publishes residual decomposition, the mapping matrix and identifiability
group ranges.

This is deliberately different from the shell/asset guards in ``test_panel.py``:
those pin that files are *served*; these assert that given a sample API payload,
the rendered DOM contains the expected fields and values.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "addon" / "ui" / "app.js"

# The slice of the IIFE that holds the DOM helpers, formatters, STRINGS table and
# the three derived-data renderers (up to the first unrelated card).  It contains
# only declarations, so evaluating it under node executes no boot/fetch/DOM work.
_SLICE_START = "const el = (tag, props = {}, children = []) => {"
_SLICE_END = "function systemCard(health, status) {"

# Minimal DOM stand-in.  ``el`` uses ``child instanceof Node`` and appends text
# nodes; ``textContent`` concatenates the tree the way a browser would, which is
# what the assertions below read.
_DOM_SHIM = r"""
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
  createElement: (tag) => { const n = new Node(); n.tagName = tag; return n; },
  createTextNode: (t) => { const n = new Node(); n._text = String(t); return n; },
  // ``frag`` (used by ``kv``, which the mapping card renders) builds a fragment
  // and appends children to it; a Node has both ``append`` and textContent.
  createDocumentFragment: () => new Node(),
  getElementById: () => null,
  querySelectorAll: () => [],
};
"""


def _render(payloads: dict) -> dict[str, str]:
    """Render the three data cards under node and return their text content.

    Returns ``None``-carrying callers as empty text where a card legitimately
    renders nothing (no group ranges).  Skips when node is not installed: the
    panel itself needs no node at runtime.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the panel does not need it at runtime")

    script = APP_JS.read_text(encoding="utf-8")
    start = script.index(_SLICE_START)
    end = script.index(_SLICE_END, start)
    block = script[start:end]

    literals = (
        f"const SAMPLE_RESIDUALS = {json.dumps(payloads['residuals'])};\n"
        f"const SAMPLE_MAPPING = {json.dumps(payloads['mapping'])};\n"
        f"const SAMPLE_DEVICES = {json.dumps(payloads['devices'])};\n"
    )
    program = (
        _DOM_SHIM
        + "\n"
        + block
        + "\n"
        + literals
        + "\n"
        + "const groups = groupRangesCard(SAMPLE_DEVICES);\n"
        + "console.log(JSON.stringify({\n"
        + "  residual: residualDecompositionCard(SAMPLE_RESIDUALS).textContent,\n"
        + "  mapping: mappingMatrixCard(SAMPLE_MAPPING).textContent,\n"
        + "  groups: groups ? groups.textContent : '',\n"
        + "}));\n"
    )
    completed = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


# Sample API payloads — shapes taken from the actual engine wire forms:
#   GET /api/v1/residuals -> {window_hours, count, components:[...]}
#   GET /api/v1/mapping   -> {cells:[...], coverage:{counts:{...}}}
#   GET /api/v1/devices   -> [{device_id, name, latest_result:{min_w,max_w,
#                              group_members,power_w}, ...}]
SAMPLE_RESIDUALS = {
    "window_hours": 24.0,
    "count": 2,
    "components": [
        {
            "timestamp": "2026-11-17T10:00:00+00:00",
            "residual_w": 800.0,
            "baseline_w": 300.0,
            "dynamic_w": 500.0,
            "noise_w": 12.5,
            "baseline_estimator": "median_of_window",
            "resolved": False,
            "kind": "positive",
        },
        {
            "timestamp": "2026-11-17T09:00:00+00:00",
            "residual_w": -125.0,
            "baseline_w": -125.0,
            "dynamic_w": 0.0,
            "noise_w": None,
            "baseline_estimator": "no_window_current_only",
            "resolved": False,
            "kind": "negative",
        },
    ],
}

SAMPLE_MAPPING = {
    "cells": [
        {
            "device_id": "light.kitchen",
            "reader_id": "ct_kitchen",
            "status": "confirmed",
            "observability": "direct",
            "estimated_contribution_w": 45.0,
            "min_contribution_w": 40.0,
            "max_contribution_w": 50.0,
            "relation_type": "one_to_one",
            "is_unknown_load": False,
        },
        {
            "device_id": "light.hall",
            "reader_id": "ct_shared",
            "status": "non_identifiable",
            "observability": "indirect",
            "estimated_contribution_w": None,
            "min_contribution_w": 0.0,
            "max_contribution_w": 90.0,
            "relation_type": "many_to_one",
            "is_unknown_load": False,
        },
    ],
    "coverage": {
        "counts": {
            "cell_count": 2,
            "by_status": {"confirmed": 1, "non_identifiable": 1},
            "unknown_load_count": 0,
        }
    },
}

SAMPLE_DEVICES = [
    {
        "device_id": "light.hall",
        "name": "Hall lights",
        "latest_result": {
            "power_w": 0.0,  # 0.0 placeholder — NOT a per-device measurement
            "min_w": 30.0,
            "max_w": 90.0,
            "group_members": ["light.hall", "light.entry"],
            "source": "inferred",
        },
    },
    {
        "device_id": "light.entry",
        "name": "Entry lights",
        "latest_result": {
            "power_w": 0.0,
            "min_w": 30.0,
            "max_w": 90.0,
            "group_members": ["light.hall", "light.entry"],
            "source": "inferred",
        },
    },
]


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return _render(
        {"residuals": SAMPLE_RESIDUALS, "mapping": SAMPLE_MAPPING, "devices": SAMPLE_DEVICES}
    )


def test_panel_renders_residual_decomposition(rendered: dict[str, str]) -> None:
    """The residual card shows the three-way split, estimator and resolved state."""
    text = rendered["residual"]
    assert "Residual decomposition" in text
    # Signed residual plus each published component and its unit.
    for value in ("800 W", "300 W", "500 W", "12.5 W", "125 W"):
        assert value in text, f"missing residual field value {value!r}"
    # The estimator and the resolved state, not just the scalar unknown_residual.
    assert "median_of_window" in text
    assert "no_window_current_only" in text
    assert "Unresolved" in text
    assert "Kind" in text  # the table exposes the residual's signed kind column


def test_panel_renders_mapping_matrix(rendered: dict[str, str]) -> None:
    """The mapping card shows cells with status, observability and bounds."""
    text = rendered["mapping"]
    assert "Device-reader mapping" in text
    for value in (
        "light.kitchen",
        "ct_kitchen",
        "Confirmed",
        "Direct",
        # The panel's shared watts() formatter keeps one decimal for 10..100 W,
        # so a faithful render of 45.0/40.0/50.0/90.0 is "45.0 W" etc.
        "45.0 W",
        "40.0 W",
        "50.0 W",
        "One-to-one",
        "Non-identifiable",
        "Indirect",
        "Many-to-one",
        "90.0 W",
    ):
        assert value in text, f"missing mapping field value {value!r}"
    assert "Coverage" in text


def test_panel_renders_group_ranges(rendered: dict[str, str]) -> None:
    """Group ranges show min_w/max_w with a NON-IDENTIFIABLE label and members."""
    text = rendered["groups"]
    assert "Identifiability ranges" in text
    assert "NON-IDENTIFIABLE" in text
    assert "30.0 W" in text and "90.0 W" in text  # min_w / max_w
    assert "Group total of:" in text
    assert "light.hall" in text and "light.entry" in text  # group_members


def test_non_identifiable_device_has_no_fabricated_watt(rendered: dict[str, str]) -> None:
    """A non-identifiable device shows a group range, never a per-device watt.

    Both sample devices carry ``power_w: 0.0`` — a placeholder the engine writes
    for inseparable groups.  Rendering that value would fabricate a measurement
    ("0.00 W"), so the groups card must show only the group range and label.
    """
    text = rendered["groups"]
    assert "NON-IDENTIFIABLE" in text
    assert "30.0 W" in text and "90.0 W" in text
    # The 0.0 power_w placeholder must never surface as a fabricated watt, and
    # the card must not present a per-device "Estimated power" figure.
    assert "0.00 W" not in text
    assert "Estimated power" not in text
