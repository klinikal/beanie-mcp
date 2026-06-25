"""Ledger lifecycle: loading, include-aware caching, bean-check, and auto-reload."""

import os
import threading
from pathlib import Path

import beanquery
from beancount import loader
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

LedgerError = dict[str, object]
FileState = dict[Path, tuple[int, int] | None]


def _format_loader_error(err) -> LedgerError:
    source = getattr(err, "source", None)
    if source:
        filename = source.get("filename")
        line = source.get("lineno")
    else:
        filename = None
        line = None

    return {
        "file": filename,
        "line": line,
        "type": type(err).__name__,
        "message": getattr(err, "message", str(err)),
    }


class _ChangeHandler(FileSystemEventHandler):
    """Debounced watcher — invalidates LedgerManager cache on any .bean change."""

    def __init__(self, manager: "LedgerManager") -> None:
        self._manager = manager
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event) -> None:
        paths = [str(event.src_path)]
        dest_path = getattr(event, "dest_path", None)
        if dest_path:
            paths.append(str(dest_path))

        bean_file = any(path.endswith((".bean", ".beancount")) for path in paths)
        if not event.is_directory and bean_file:
            self.schedule_invalidation()

    def schedule_invalidation(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(2.0, self._manager.invalidate)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None


class LedgerManager:
    """Manages a beanquery connection to a Beancount ledger with mtime-based caching.

    Thread-safe cache refresh. Watchdog invalidates the cache when any .bean file
    in the ledger directory tree changes (covers root file and all includes).
    """

    def __init__(self, ledger_path: Path) -> None:
        self._path = Path(ledger_path)
        self._conn: beanquery.Connection | None = None
        self._file_state: FileState | None = None
        self._lock = threading.Lock()
        self._observer: Observer | None = None
        self._handler: _ChangeHandler | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connection(self) -> beanquery.Connection:
        """Return a cached beanquery Connection, reloading if the file has changed."""
        with self._lock:
            if self._stale():
                self._conn = beanquery.connect(
                    "beancount:" + self._path.absolute().as_posix()
                )
                self._file_state = self._connection_file_state(self._conn)
            return self._conn

    def check(self) -> list[LedgerError]:
        """Run beancount.loader on the ledger and return structured errors."""
        _, errors, _ = loader.load_file(str(self._path))
        return [_format_loader_error(err) for err in errors]

    def connection_errors(self) -> list[LedgerError]:
        """Return loader errors from the cached beanquery connection."""
        conn = self.connection()
        return [_format_loader_error(err) for err in getattr(conn, "errors", [])]

    def invalidate(self) -> None:
        """Force the next connection() call to reload from disk."""
        with self._lock:
            self._conn = None
            self._file_state = None

    def start_watcher(self) -> None:
        """Start a watchdog observer on the ledger directory (recursive)."""
        if self._observer is not None:
            return
        self._handler = _ChangeHandler(self)
        observer = Observer()
        observer.schedule(self._handler, str(self._path.parent), recursive=True)
        observer.start()
        self._observer = observer

    def stop_watcher(self) -> None:
        """Stop the watchdog observer."""
        if self._handler:
            self._handler.cancel()
            self._handler = None
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _file_fingerprint(self, path: Path) -> tuple[int, int] | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _paths_for_connection(self, conn: beanquery.Connection) -> set[Path]:
        includes = conn.options.get("include") or []
        paths = {self._path}
        paths.update(Path(path) for path in includes)
        return paths

    def _file_state_for(self, paths: set[Path]) -> FileState:
        return {path: self._file_fingerprint(path) for path in paths}

    def _connection_file_state(self, conn: beanquery.Connection) -> FileState:
        return self._file_state_for(self._paths_for_connection(conn))

    def _stale(self) -> bool:
        if self._conn is None or self._file_state is None:
            return True
        return self._file_state_for(set(self._file_state)) != self._file_state
