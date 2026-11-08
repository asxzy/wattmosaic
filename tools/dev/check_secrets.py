"""Fail if any tracked file looks like it contains a real secret.

Intended to run in CI and pre-commit.  Heuristics only; it is a safety net, not
a guarantee.

Run: python tools/dev/check_secrets.py
"""

from __future__ import annotations

import re
import string
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKIP_SUFFIXES = {".png", ".jpg", ".db", ".sqlite3", ".pyc", ".woff2"}
SKIP_DIRS = {".git", ".venv", "node_modules", "build", "dist", "__pycache__", "data/raw"}

PATTERNS = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "possible private IP address"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\."), "possible JWT / HA long-lived token"),
    (re.compile(r"\b(ghp_|gho_|github_pat_)[A-Za-z0-9]{20,}"), "possible GitHub token"),
    (re.compile(r"(?i)\b(api[_-]?token|password|secret)\s*[:=]\s*['\"][^'\"]{12,}['\"]"), "hardcoded credential"),
    # A Home Assistant config entry id is a ULID: Crockford base32, 26 chars,
    # starting with a millisecond timestamp whose high bits put it at "01".
    # It identifies one installation and must not appear in published docs.
    (re.compile(r"\b01[0-9A-HJKMNP-TV-Z]{24}\b"), "possible HA config entry id"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), "possible MAC address"),
]

#: Line markers that make a match acceptable.  ``172.30.32.2`` is the
#: Supervisor ingress address inside the Home Assistant add-on network, and
#: ``192.0.2.`` is the RFC 5737 documentation range used by offline tests —
#: neither can be a real household credential.  ``redaction-test`` marks
#: deliberately fake values used to prove that secrets are scrubbed.
ALLOWLIST_LINE_MARKERS = (
    "replace-with",
    "placeholder",
    "example",
    "redacted",
    "***",
    "your-token",
    "192.168.1.10",
    "192.0.2.",
    "172.30.32.2",
    "127.0.0.1",
    "0.0.0.0",
    "255.255",
    "10.0.0",
    "homeassistant.local",
    "redaction-test",
    # Fixed infrastructure ranges, not household identifiers: IANA loopback and
    # the Supervisor's internal app network (Supervisor ships the same
    # 172.30.32.0/23 constant). The gateway uses them to decide which peers may
    # reach an unauthenticated API; neither can name a real installation.
    "127.0.0.0/8",
    "172.30.32.0/23",
    # RFC 5737 documentation ranges (TEST-NET-2) used by the startup tests.
    "198.51.100.",
    "203.0.113.",
    "172.30.32.",
)


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        return [p for p in ROOT.rglob("*") if p.is_file()]
    return [ROOT / line for line in out.stdout.splitlines() if line]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if any(part in rel.split("/") for part in SKIP_DIRS):
            continue
        if path.suffix in SKIP_SUFFIXES or not path.exists():
            continue
        if path.name == ".env":
            findings.append(f"{rel}: .env must never be tracked")
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if not text or len(text) > 2_000_000:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in ALLOWLIST_LINE_MARKERS):
                continue
            for pattern, label in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{rel}:{lineno}: {label}")
    if findings:
        print("Secret scan findings:", file=sys.stderr)
        for f in findings:
            print("  " + f, file=sys.stderr)
        return 1
    print("secret scan: clean")
    _ = string  # keep import used for future entropy checks
    return 0


if __name__ == "__main__":
    sys.exit(main())
