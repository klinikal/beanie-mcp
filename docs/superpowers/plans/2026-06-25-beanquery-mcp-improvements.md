# beanquery-mcp Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden and extend the forked `klinikal/beanquery-mcp` server — fixing a missing dependency, removing the path-injection tool, adding ledger health-check, switching to structured JSON output via DB-API, mtime caching, and watchdog auto-reload.

**Architecture:** Extract ledger lifecycle (loading, caching, watchdog) into a new `ledger.py` module with a `LedgerManager` class. Rewrite `server.py` to use the beanquery DB-API (`beanquery.connect()`) instead of `BQLShell`, returning structured JSON so Claude can parse results directly. The ledger path is locked to the `BEANCOUNT_LEDGER` env var — the `set_ledger_file` runtime tool is removed entirely. The manager is built lazily inside `_require_manager()` so tests can configure it via env var without `importlib.reload` hacks.

**Tech Stack:** Python 3.10–3.13, beancount 3.x, beanquery 0.2.0 (DB-API), mcp[cli], pydantic-settings, watchdog, pytest

## Global Constraints

- Python `>=3.10, <3.14` — beancount 3.x has no cp314 wheel; building from source fails on macOS (Apple bison 2.3, needs >=3.8). Pin to 3.13 max.
- beancount `>=3.1.0` (v3 only — v2 import paths are different)
- beanquery `>=0.2.0` — use `beanquery.connect()` DB-API, NOT `BQLShell`
- All tests use `pytest`; run with `uv run pytest`
- Commit identity: `klinikal` / `264607427+klinikal@users.noreply.github.com`; no Co-Authored-By trailers
- Never add `set_ledger_file` back — the env var is the only configuration point
- `get_accounts` resource must NOT inherit the 200-row cap from `run_query` — it calls the cursor directly

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Deps, Python floor+ceiling, entry point |
| `.python-version` | Create | Pins uv to Python 3.13 to guarantee wheel availability |
| `ledger.py` | Create | LedgerManager: lazy connection, mtime cache, bean-check, watchdog |
| `server.py` | Modify | MCP tools + resources wired to lazy LedgerManager |
| `server_test.py` | Rewrite | Full test suite: bean_check, JSON output, row limits, no-ledger errors, accounts completeness |

---

## Task 1: Fix `pyproject.toml` and pin Python 3.13

**Files:**
- Modify: `pyproject.toml`
- Create: `.python-version`

**Interfaces:**
- Produces: correct dep declarations consumed by `uv sync` in all later tasks

- [ ] **Step 1: Create `.python-version`**

```
3.13
```

This tells `uv` to use Python 3.13 by default, where beancount has a prebuilt arm64 wheel.

- [ ] **Step 2: Replace `pyproject.toml`**

```toml
[project]
name = "beanquery-mcp"
version = "0.2.0"
description = "MCP server for querying Beancount ledgers via BQL — hardened fork of vanto/beanquery-mcp"
readme = "README.md"
requires-python = ">=3.10,<3.14"
dependencies = [
    "beancount>=3.1.0",
    "beanquery>=0.2.0",
    "mcp[cli]>=1.5.0",
    "pydantic-settings>=2.0.0",
    "watchdog>=3.0.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3.5",
    "ruff>=0.8.5",
]

[tool.ruff.lint]
select = ["E", "F", "I", "UP"]
ignore = []

[tool.ruff]
line-length = 88
target-version = "py310"

[tool.pytest.ini_options]
xfail_strict = true
```

- [ ] **Step 3: Sync deps**

```bash
uv sync
```

Expected: resolves without building beancount from source; `pydantic-settings` and `watchdog` appear in output. If it still tries to build beancount from source, verify `.python-version` was picked up: `uv run python --version` should show `3.13.x`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock .python-version
git commit -m "fix: declare pydantic-settings and watchdog deps; cap python <3.14; pin 3.13 for wheel availability"
```

---

## Task 2: LedgerManager (`ledger.py`)

**Files:**
- Create: `ledger.py`

**Interfaces:**
- Consumes: nothing from prior tasks
- Produces:
  - `LedgerManager(ledger_path: Path)` — class, importable from `ledger`
  - `manager.connection() -> beanquery.Connection` — returns cached, mtime-checked connection; captures mtime BEFORE connecting to avoid race
  - `manager.check() -> list[str]` — returns list of formatted `"file:line: message"` strings; empty = clean
  - `manager.invalidate() -> None` — force cache clear (called by watchdog)
  - `manager.start_watcher() -> None` — starts watchdog observer watching ledger dir recursively; call once at server startup
  - `manager.stop_watcher() -> None` — stops observer; call at shutdown

- [ ] **Step 1: Write failing tests**

Replace `server_test.py` with the following (completely — existing tests are incompatible with the rewritten server):

```python
import os
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_bean(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# LedgerManager tests
# ---------------------------------------------------------------------------

from ledger import LedgerManager


def test_ledger_manager_connection_returns_result(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, """
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        2022-01-02 * "Lunch"
          Assets:Cash    -10.00 USD
          Expenses:Food   10.00 USD
    """)
    mgr = LedgerManager(bean)
    conn = mgr.connection()
    cursor = conn.execute("SELECT DISTINCT account ORDER BY account")
    accounts = [row[0] for row in cursor.fetchall()]
    assert "Assets:Cash" in accounts
    assert "Expenses:Food" in accounts


def test_ledger_manager_caches_connection(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    conn1 = mgr.connection()
    conn2 = mgr.connection()
    assert conn1 is conn2  # same object = cache hit


def test_ledger_manager_reloads_on_mtime_change(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    conn1 = mgr.connection()
    # Bump mtime forward 1 second to guarantee a change
    future = time.time() + 1
    os.utime(bean, (future, future))
    conn2 = mgr.connection()
    assert conn1 is not conn2  # different object = cache miss → reload


def test_ledger_manager_check_clean(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    errors = mgr.check()
    assert errors == []


def test_ledger_manager_check_returns_errors(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n2022-01-02 balance Assets:Cash 999.00 USD\n")
    mgr = LedgerManager(bean)
    errors = mgr.check()
    assert len(errors) > 0
    assert any("balance" in e.lower() or "999" in e for e in errors)


def test_ledger_manager_check_error_format(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n2022-01-02 balance Assets:Cash 999.00 USD\n")
    mgr = LedgerManager(bean)
    errors = mgr.check()
    # Each error should be "path:lineno: message" or "unknown: message"
    for e in errors:
        assert ": " in e


def test_ledger_manager_invalidate(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    conn1 = mgr.connection()
    mgr.invalidate()
    conn2 = mgr.connection()
    assert conn1 is not conn2


# ---------------------------------------------------------------------------
# Server tool tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def bean_file(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, """
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        2022-01-02 * "Lunch"
          Assets:Cash    -10.00 USD
          Expenses:Food   10.00 USD
    """)
    return bean


@pytest.fixture()
def with_ledger(bean_file, monkeypatch):
    """Point BEANCOUNT_LEDGER at a small test ledger."""
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean_file))
    # Reset the cached manager so _require_manager picks up the env var
    import server
    server._manager_cache = None
    yield bean_file
    server._manager_cache = None


@pytest.fixture()
def no_ledger(monkeypatch):
    monkeypatch.delenv("BEANCOUNT_LEDGER", raising=False)
    import server
    server._manager_cache = None
    yield
    server._manager_cache = None


def test_run_query_returns_dict(with_ledger):
    from server import run_query
    result = run_query("SELECT DISTINCT account ORDER BY account")
    assert isinstance(result, dict)
    assert "columns" in result
    assert "rows" in result
    assert "truncated" in result
    assert "total_rows" in result


def test_run_query_has_expected_accounts(with_ledger):
    from server import run_query
    result = run_query("SELECT DISTINCT account ORDER BY account")
    accounts = [row[0] for row in result["rows"]]
    assert "Assets:Cash" in accounts
    assert "Expenses:Food" in accounts


def test_run_query_no_ledger_raises(no_ledger):
    from server import run_query
    with pytest.raises(RuntimeError, match="BEANCOUNT_LEDGER"):
        run_query("SELECT account")


def test_run_query_invalid_bql_returns_error(with_ledger):
    from server import run_query
    result = run_query("THIS IS NOT VALID BQL")
    assert "error" in result
    assert isinstance(result["error"], str)


def test_run_query_row_limit(tmp_path, monkeypatch):
    lines = ["2022-01-01 open Assets:Root USD"]
    for i in range(250):
        lines.append(f"2022-01-01 open Expenses:Cat{i:03d} USD")
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    import server
    server._manager_cache = None
    result = run_query("SELECT DISTINCT account ORDER BY account")
    assert len(result["rows"]) == 200
    assert result["truncated"] is True
    assert result["total_rows"] == 251
    server._manager_cache = None


def test_bean_check_clean(with_ledger):
    from server import bean_check
    result = bean_check()
    assert "clean" in result.lower()


def test_bean_check_with_errors(tmp_path, monkeypatch):
    bean = tmp_path / "broken.bean"
    bean.write_text("2022-01-01 open Assets:Cash USD\n2022-01-02 balance Assets:Cash 999.00 USD\n")
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    import server
    server._manager_cache = None
    from server import bean_check
    result = bean_check()
    assert "999" in result or "balance" in result.lower()
    server._manager_cache = None


def test_get_accounts_returns_all_accounts(tmp_path, monkeypatch):
    """get_accounts must not be silently capped at 200 rows."""
    lines = ["2022-01-01 open Assets:Root USD"]
    for i in range(250):
        lines.append(f"2022-01-01 open Expenses:Cat{i:03d} USD")
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    import server
    server._manager_cache = None
    from server import get_accounts
    result = get_accounts()
    account_lines = [a for a in result.strip().split("\n") if a]
    assert len(account_lines) == 251
    server._manager_cache = None


def test_get_tables_returns_known_tables(with_ledger):
    from server import get_tables
    result = get_tables()
    tables = result.strip().split("\n")
    for expected in ["accounts", "balances", "entries", "postings", "transactions"]:
        assert expected in tables
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest server_test.py -k "test_ledger_manager_connection" -v
```

Expected: `ModuleNotFoundError: No module named 'ledger'`

- [ ] **Step 3: Create `ledger.py`**

```python
import os
import threading
from pathlib import Path
from typing import Optional

import beanquery
from beancount import loader
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _BeanFileHandler(FileSystemEventHandler):
    def __init__(self, manager: "LedgerManager") -> None:
        self._manager = manager
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def on_modified(self, event) -> None:
        src = str(event.src_path)
        if not event.is_directory and (src.endswith(".bean") or src.endswith(".beancount")):
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(2.0, self._manager.invalidate)
                self._timer.start()


class LedgerManager:
    def __init__(self, ledger_path: Path) -> None:
        self._path = Path(ledger_path)
        self._conn: Optional[beanquery.Connection] = None
        self._mtime: Optional[float] = None
        self._lock = threading.Lock()
        self._observer: Optional[Observer] = None

    def _current_mtime(self) -> float:
        return os.path.getmtime(self._path)

    def _needs_reload(self) -> bool:
        if self._conn is None:
            return True
        try:
            return self._mtime != self._current_mtime()
        except OSError:
            return True

    def connection(self) -> beanquery.Connection:
        with self._lock:
            if self._needs_reload():
                # Capture mtime BEFORE connecting so a concurrent write
                # leaves self._mtime older than the file, forcing a reload.
                mtime = self._current_mtime()
                self._conn = beanquery.connect(
                    "beancount:" + self._path.absolute().as_posix()
                )
                self._mtime = mtime
            return self._conn

    def check(self) -> list[str]:
        _, errors, _ = loader.load_file(str(self._path))
        lines = []
        for error in errors:
            source = getattr(error, "source", None)
            if source:
                loc = f"{source.get('filename', '?')}:{source.get('lineno', '?')}"
            else:
                loc = "unknown"
            lines.append(f"{loc}: {error.message}")
        return lines

    def invalidate(self) -> None:
        with self._lock:
            self._conn = None
            self._mtime = None

    def start_watcher(self) -> None:
        if self._observer is not None:
            return
        handler = _BeanFileHandler(self)
        observer = Observer()
        # Watch recursively so edits to included sub-files (per-year, per-account)
        # also invalidate the cache.
        observer.schedule(handler, str(self._path.parent), recursive=True)
        observer.start()
        self._observer = observer

    def stop_watcher(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
```

- [ ] **Step 4: Run LedgerManager tests**

```bash
uv run pytest server_test.py -k "test_ledger_manager" -v
```

Expected: all 7 LedgerManager tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ledger.py server_test.py
git commit -m "feat: LedgerManager with mtime cache, watchdog auto-reload (recursive), and bean-check"
```

---

## Task 3: Rewrite `server.py`

**Files:**
- Modify: `server.py`

**Interfaces:**
- Consumes: `LedgerManager` from `ledger.py`; `BEANCOUNT_LEDGER` env var
- Produces:
  - Module-level `_manager_cache: Optional[LedgerManager]` — reset to `None` by tests between runs
  - `_require_manager() -> LedgerManager` — lazily builds from `BEANCOUNT_LEDGER`; raises `RuntimeError` with env var name if unset
  - `run_query(query: str) -> dict` — `{"columns", "rows", "truncated", "total_rows"}` or `{"error": str}` on bad BQL
  - `bean_check() -> str`
  - Resource `beanquery://tables` — newline-joined sorted table names
  - Resource `beanquery://accounts` — newline-joined accounts, NO row cap

- [ ] **Step 1: Run existing server tests to see current failures**

```bash
uv run pytest server_test.py -k "not test_ledger_manager" -v
```

Expected: failures on `run_query` (returns str not dict), `get_tables` (trailing newline), `test_run_query_no_ledger` (wrong error message), etc.

- [ ] **Step 2: Replace `server.py`**

```python
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import beanquery
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import get_logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger import LedgerManager

logger = get_logger(__name__)

ROW_LIMIT = 200

# Module-level cache reset by tests via `server._manager_cache = None`
_manager_cache: Optional[LedgerManager] = None


class _Settings(BaseSettings):
    model_config: SettingsConfigDict = SettingsConfigDict(
        env_prefix="BEANCOUNT_", env_file=".env"
    )
    ledger: Optional[Path] = Field(None)


def _require_manager() -> LedgerManager:
    global _manager_cache
    if _manager_cache is None:
        settings = _Settings()
        if not settings.ledger:
            raise RuntimeError(
                "No ledger configured. Set the BEANCOUNT_LEDGER environment variable "
                "to the absolute path of your .bean file."
            )
        _manager_cache = LedgerManager(settings.ledger)
    return _manager_cache


@asynccontextmanager
async def lifespan(app):
    mgr = _require_manager()
    mgr.start_watcher()
    try:
        yield
    finally:
        mgr.stop_watcher()


mcp = FastMCP("Beanquery MCP", dependencies=["beancount", "beanquery"], lifespan=lifespan)


@mcp.tool()
def run_query(
    query: str = Field(
        description="BQL query to run against the Beancount ledger. "
        "Results are capped at 200 rows — add a LIMIT clause for smaller sets."
    ),
) -> dict:
    """Run a BQL query against the Beancount ledger.

    Returns a dict with:
      columns    — list of column name strings
      rows       — list of rows, each a list of value strings
      truncated  — true if results were cut at 200 rows
      total_rows — full result count before truncation
      error      — present instead of the above if BQL is invalid
    """
    mgr = _require_manager()
    conn = mgr.connection()
    try:
        cursor = conn.execute(query)
    except beanquery.Error as exc:
        return {"error": str(exc)}

    columns = [col.name for col in cursor.description] if cursor.description else []
    all_rows = cursor.fetchall()
    truncated = len(all_rows) > ROW_LIMIT
    rows = [[str(v) for v in row] for row in all_rows[:ROW_LIMIT]]

    return {
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "total_rows": len(all_rows),
    }


@mcp.tool()
def bean_check() -> str:
    """Run bean-check on the ledger. Returns a clean confirmation or a list of
    errors/warnings with their file location and message."""
    mgr = _require_manager()
    errors = mgr.check()
    if not errors:
        return "Ledger is clean — no errors or warnings."
    return "\n".join(errors)


@mcp.resource("beanquery://tables")
def get_tables() -> str:
    """List BQL-accessible tables (accounts, balances, entries, postings, etc.)."""
    mgr = _require_manager()
    conn = mgr.connection()
    tables = sorted(k for k in conn.tables if k is not None and k != "")
    return "\n".join(tables)


@mcp.resource("beanquery://accounts")
def get_accounts() -> str:
    """List all accounts in the ledger. Not subject to the 200-row query cap."""
    mgr = _require_manager()
    conn = mgr.connection()
    cursor = conn.execute("SELECT DISTINCT account ORDER BY account")
    return "\n".join(str(row[0]) for row in cursor.fetchall())
```

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest server_test.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Run lint**

```bash
uv run ruff check server.py ledger.py
uv run ruff format --check server.py ledger.py
```

Fix any issues: `uv run ruff format server.py ledger.py`

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "feat: rewrite server with lazy LedgerManager, structured JSON output, bean_check, row limit, ParseError handling"
```

- [ ] **Step 6: Push**

```bash
git push origin main
```

---

## Self-Review

**Spec coverage:**
- ✅ `pydantic-settings` declared as explicit dep — Task 1
- ✅ Python `<3.14` ceiling + `.python-version` pin — Task 1
- ✅ `set_ledger_file` removed — Task 3
- ✅ `bean_check` tool — Task 3
- ✅ Structured JSON output — Task 3
- ✅ Row limit (200) with truncation flag — Task 3
- ✅ ParseError caught and returned as `{"error": str}` — Task 3
- ✅ mtime race fixed (capture mtime before connecting) — Task 2
- ✅ mtime-based cache — Task 2
- ✅ Watchdog recursive (catches includes in subdirs) — Task 2
- ✅ `.bean` and `.beancount` extension coverage — Task 2
- ✅ `get_accounts` bypasses row cap — Task 3
- ✅ `get_tables` uses `conn.tables` registry directly (no dead `SELECT * FROM #`) — Task 3
- ✅ Lazy manager via `_manager_cache` — avoids `importlib.reload` in tests — Task 3
- ✅ Existing incompatible tests replaced — Task 2 Step 1

**No placeholder language present.**

**Type consistency:** `LedgerManager.connection()` → `beanquery.Connection` (Task 2); consumed as `conn = mgr.connection()` (Task 3) — consistent. `check()` → `list[str]` (Task 2); consumed as `"\n".join(errors)` (Task 3) — consistent. `_manager_cache` module global → `Optional[LedgerManager]`, reset to `None` by test fixtures — consistent.

**Known limitation (documented, not fixed):** `str(v)` on `Inventory`/`Position` values is display-string lossy (e.g. empty inventory → `"()"`). Acceptable for Claude's use case; a future improvement could special-case `Decimal`, `date`, and `Inventory` types for cleaner serialisation.

**Thread safety note:** `beanquery.Connection` is not safe for concurrent `execute()` calls on shared table state. For this single-user stdio MCP the risk is negligible (Claude issues queries serially), but do not add concurrent query paths without giving each caller its own connection.
