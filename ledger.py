"""Ledger lifecycle — loading, mtime caching, bean-check, and watchdog auto-reload."""

import os
import threading
from pathlib import Path

import beanquery
from beancount import loader
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _ChangeHandler(FileSystemEventHandler):
    """Debounced watcher — invalidates LedgerManager cache on any .bean change."""

    def __init__(self, manager: "LedgerManager") -> None:
        self._manager = manager
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event) -> None:
        src = str(event.src_path)
        bean_file = src.endswith(".bean") or src.endswith(".beancount")
        if not event.is_directory and bean_file:
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(2.0, self._manager.invalidate)
                self._timer.start()


class LedgerManager:
    """Manages a beanquery connection to a Beancount ledger with mtime-based caching.

    Thread-safe cache refresh. Watchdog invalidates the cache when any .bean file
    in the ledger directory tree changes (covers root file and all includes).
    """

    def __init__(self, ledger_path: Path) -> None:
        self._path = Path(ledger_path)
        self._conn: beanquery.Connection | None = None
        self._mtime: float | None = None
        self._lock = threading.Lock()
        self._observer: Observer | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connection(self) -> beanquery.Connection:
        """Return a cached beanquery Connection, reloading if the file has changed."""
        with self._lock:
            if self._stale():
                # Capture mtime before connecting so a concurrent write leaves
                # self._mtime older than the file, forcing a reload next call.
                mtime = self._file_mtime()
                self._conn = beanquery.connect(
                    "beancount:" + self._path.absolute().as_posix()
                )
                self._mtime = mtime
            return self._conn

    def check(self) -> list[str]:
        """Run beancount.loader on the ledger and return formatted error strings.

        Each entry is "filepath:lineno: message". Returns an empty list if clean.
        """
        _, errors, _ = loader.load_file(str(self._path))
        result = []
        for err in errors:
            source = getattr(err, "source", None)
            if source:
                loc = f"{source.get('filename', '?')}:{source.get('lineno', '?')}"
            else:
                loc = "unknown"
            result.append(f"{loc}: {err.message}")
        return result

    def invalidate(self) -> None:
        """Force the next connection() call to reload from disk."""
        with self._lock:
            self._conn = None
            self._mtime = None

    def start_watcher(self) -> None:
        """Start a watchdog observer on the ledger directory (recursive)."""
        if self._observer is not None:
            return
        handler = _ChangeHandler(self)
        observer = Observer()
        observer.schedule(handler, str(self._path.parent), recursive=True)
        observer.start()
        self._observer = observer

    def stop_watcher(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _file_mtime(self) -> float:
        return os.path.getmtime(self._path)

    def _stale(self) -> bool:
        if self._conn is None:
            return True
        try:
            return self._mtime != self._file_mtime()
        except OSError:
            return True
