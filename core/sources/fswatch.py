"""macOS FSEvents background watcher with ~250ms debounce.

Watches (strictly read-only, skips missing directories gracefully):
- RESEARCH_ROOT / "projects"
- CODEX_SESSIONS_ROOT (~/.codex/sessions)
- MAGPIE_ROUTING_ROOT (~/.config/magpie/routing)
- TMP_ROOT / {xy_figure_gate, xy_prose_gate, xy_workflow_guard, xiaoyan_cmd_summary}
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    FileSystemEvent = object  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(
        self,
        channel: str,
        callback: Callable[[str, set[str]], None],
        debounce_s: float = 0.25,
    ) -> None:
        super().__init__()
        self._channel = channel
        self._callback = callback
        self._debounce_s = debounce_s
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None

    def on_any_event(self, event: FileSystemEvent) -> None:
        src = getattr(event, "src_path", None)
        if not src:
            return
        with self._lock:
            self._pending.add(str(src))
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = set(self._pending)
            self._pending.clear()
            self._timer = None
        if paths:
            try:
                self._callback(self._channel, paths)
            except Exception:
                # Never crash observer thread
                pass

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending.clear()


class WorkspaceWatcher:
    """FSEvents watcher triggering debounced callbacks for macro and micro updates."""

    def __init__(
        self,
        research_root: Path,
        codex_sessions_root: Path,
        magpie_routing_root: Path,
        tmp_root: Path,
        on_macro_change: Callable[[set[str]], None] | None = None,
        on_micro_change: Callable[[set[str]], None] | None = None,
        debounce_s: float = 0.25,
    ) -> None:
        self.research_root = Path(research_root)
        self.codex_sessions_root = Path(codex_sessions_root)
        self.magpie_routing_root = Path(magpie_routing_root)
        self.tmp_root = Path(tmp_root)
        self._on_macro_change = on_macro_change
        self._on_micro_change = on_micro_change
        self._debounce_s = debounce_s
        self._observer: Any = None
        self._handlers: list[_DebouncedHandler] = []
        self._running = False

    def _dispatch(self, channel: str, paths: set[str]) -> None:
        if channel == "macro":
            if self._on_macro_change is not None:
                self._on_macro_change(paths)
            # Delegation briefs also inform micro attribution
            if self._on_micro_change is not None and any("99_audit/delegation" in p for p in paths):
                self._on_micro_change(paths)
        elif channel == "micro":
            if self._on_micro_change is not None:
                self._on_micro_change(paths)

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running or not WATCHDOG_AVAILABLE or Observer is None:
            return False

        observer = Observer()
        observer.daemon = True
        watch_specs: list[tuple[Path, str, bool]] = [
            (self.research_root / "projects", "macro", True),
            (self.codex_sessions_root, "micro", True),
            (self.magpie_routing_root, "micro", False),
            (self.tmp_root / "xy_figure_gate", "micro", False),
            (self.tmp_root / "xy_prose_gate", "micro", False),
            (self.tmp_root / "xy_workflow_guard", "micro", False),
            (self.tmp_root / "xiaoyan_cmd_summary", "micro", False),
        ]

        scheduled = 0
        for path, channel, recursive in watch_specs:
            if not path.is_dir():
                continue
            handler = _DebouncedHandler(channel, self._dispatch, debounce_s=self._debounce_s)
            try:
                observer.schedule(handler, str(path), recursive=recursive)
                self._handlers.append(handler)
                scheduled += 1
            except OSError:
                continue

        if scheduled == 0:
            return False

        try:
            observer.start()
            self._observer = observer
            self._running = True
            return True
        except OSError:
            return False

    def stop(self) -> None:
        self._running = False
        for h in self._handlers:
            h.cancel()
        self._handlers.clear()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
