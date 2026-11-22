#!/opt/venv/bin/python
"""Dev-only auto-reload supervisor for the engine container.

``docker-compose.reload.yml`` bind-mounts this file over
``/opt/venv/bin/wattmosaic-engine``, the command ``addon/run.sh`` execs, and
bind-mounts ``engine/wattmosaic_engine`` over the installed package.  This
script then runs the real engine (``python -m wattmosaic_engine.main``) as a
child and restarts it whenever a watched source file changes.

Polling (default 1 s) instead of inotify: events through Docker Desktop bind
mounts on macOS are unreliable, and a stat walk of the package is cheap.

* SIGTERM/SIGINT are forwarded to the engine, so ``docker stop`` still lets it
  checkpoint the SQLite WAL.
* A crashed engine is not respawned in a tight loop: the supervisor waits for
  the next source change (or ``WM_RELOAD_CRASH_RETRY_S``) before trying again.

Never used in production images: nothing in the Dockerfile references it.

Environment:
  WM_RELOAD_PATHS          colon-separated dirs to watch
                           (default: the installed wattmosaic_engine package)
  WM_RELOAD_INTERVAL_S     poll interval (default 1.0)
  WM_RELOAD_DEBOUNCE_S     quiet period after a change before restart (0.5)
  WM_RELOAD_STOP_TIMEOUT_S grace before SIGKILL (25)
  WM_RELOAD_CRASH_RETRY_S  retry a crashed engine after this many s (30)
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WATCH_SUFFIXES = (".py", ".json", ".yaml", ".yml", ".sql")


def _log(msg: str, level: str = "info") -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f'{stamp} level={level} component=reload msg="{msg}"', flush=True)


def _default_paths() -> list[str]:
    import importlib.util

    spec = importlib.util.find_spec("wattmosaic_engine")
    if spec is None or not spec.submodule_search_locations:
        return []
    return list(spec.submodule_search_locations)


def _snapshot(paths: list[str]) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    for root in paths:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
            for name in filenames:
                if not name.endswith(WATCH_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                state[full] = (st.st_mtime_ns, st.st_size)
    return state


def _changed(old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]) -> list[str]:
    keys = set(old) | set(new)
    return sorted(k for k in keys if old.get(k) != new.get(k))


class Supervisor:
    def __init__(self) -> None:
        raw = os.environ.get("WM_RELOAD_PATHS", "")
        self.paths = [p for p in raw.split(":") if p] or _default_paths()
        self.interval = float(os.environ.get("WM_RELOAD_INTERVAL_S", "1.0"))
        self.debounce = float(os.environ.get("WM_RELOAD_DEBOUNCE_S", "0.5"))
        self.stop_timeout = float(os.environ.get("WM_RELOAD_STOP_TIMEOUT_S", "25"))
        self.crash_retry = float(os.environ.get("WM_RELOAD_CRASH_RETRY_S", "30"))
        self.child: subprocess.Popen[bytes] | None = None
        self.stopping = False

    def start_child(self) -> None:
        cmd = [sys.executable, "-m", "wattmosaic_engine.main", *sys.argv[1:]]
        self.child = subprocess.Popen(cmd)
        _log(f"engine started pid={self.child.pid}")

    def stop_child(self) -> int | None:
        child = self.child
        if child is None or child.poll() is not None:
            return child.returncode if child else None
        child.send_signal(signal.SIGTERM)
        try:
            return child.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            _log("engine did not stop in time; killing", "warn")
            child.kill()
            return child.wait()

    def _on_signal(self, signum: int, _frame: object) -> None:
        self.stopping = True
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signum)

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        if not self.paths:
            _log("no paths to watch; running engine without reload", "warn")
        _log(f"watching {', '.join(self.paths)} every {self.interval}s")
        state = _snapshot(self.paths)
        self.start_child()
        crashed_at: float | None = None
        while True:
            time.sleep(self.interval)
            if self.stopping:
                code = self.stop_child()
                return int(code or 0)
            child = self.child
            if crashed_at is None and child is not None and child.poll() is not None:
                crashed_at = time.monotonic()
                _log(
                    f"engine exited code={child.returncode}; waiting for a source change "
                    f"(or {self.crash_retry:.0f}s) before restarting",
                    "warn",
                )
            new = _snapshot(self.paths)
            diff = _changed(state, new)
            if diff:
                # Debounce: an editor save or a git checkout touches many files.
                while True:
                    time.sleep(self.debounce)
                    settled = _snapshot(self.paths)
                    if settled == new:
                        break
                    new = settled
                state = new
                shown = ", ".join(Path(p).name for p in diff[:5])
                more = f" (+{len(diff) - 5} more)" if len(diff) > 5 else ""
                _log(f"change detected: {shown}{more}; restarting engine")
                self.stop_child()
                self.start_child()
                crashed_at = None
            elif crashed_at is not None and time.monotonic() - crashed_at >= self.crash_retry:
                _log("retrying crashed engine")
                self.start_child()
                crashed_at = None


if __name__ == "__main__":
    raise SystemExit(Supervisor().run())
