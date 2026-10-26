# beanie-mcp Premier Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap between "this repo" and "the premier beancount MCP" — remove the top BQL footguns with an orientation tool, add Fava-parity report tools (net worth, account balance, holdings) that agents currently get wrong via raw BQL, fix a redundant ledger parse, and make the server installable via `uvx` from PyPI.

**Architecture:** All new tools live in `src/beanie_mcp/server.py` next to the existing `run_query`/`bean_check`/`list_accounts` tools, following the exact same pattern (`@mcp.tool()` function returning a plain `dict`, errors surfaced as `{"error": ..., "error_type": ...}` rather than raised exceptions). New report tools build one shared `_operating_currency()` / `_converted_sum()` helper pair rather than duplicating BQL string construction. No new files, no new dependencies, no changes to `ledger.py`'s public interface except reusing an already-existing method (`connection_errors()`) from one more call site.

**Tech Stack:** Python 3.10+, beanquery 0.2.0, beancount 3.1+, mcp[cli], pytest. All verified installed in `.venv` — do not add new dependencies.

## Global Constraints

- Python `>=3.10,<3.14` (from `pyproject.toml`) — no walrus-in-comprehension or other 3.11+-only syntax.
- No new third-party dependencies. Every tool in this plan is buildable with `beanquery` + `beancount` + stdlib (verified below).
- Every tool that takes an `account` string param must reject a literal `"` character (existing guard pattern from `find_unmatched_transfers`) before interpolating it into a BQL string, to prevent BQL string-literal injection.
- Every tool that takes an `as_of`/date string param must validate it against `^\d{4}-\d{2}-\d{2}$` before interpolating it into a BQL query, for the same reason (dates are bare tokens in BQL, not quoted, so a malformed value could break or hijack the query).
- Tool output stays JSON-serializable plain dicts/strings — never return beancount/beanquery objects (`Inventory`, `Amount`, `Decimal`) directly; always `str()` or extract `.number`/`.currency` first.
- Follow existing test conventions in `server_test.py`: fixtures `with_ledger`/`no_ledger`/`bean_file`/`monkeypatch`, `_reset_manager()` before/after tests that change `BEANCOUNT_LEDGER`.
- New README/tool-table entries follow the existing table format at the top of `README.md`.

## Explicit Non-Goals (do not build these — confirmed out of scope)

- **Fava wrapping.** Report math is reproduced directly via BQL `convert()`/`cost()`/`value()` (verified working below) — no Flask/Fava dependency.
- **`income_statement` / `balance_sheet` dedicated tools.** An agent composes these from `run_query` once it has `ledger_info` — adding dedicated tools here is scope creep past what's actually hard in BQL.
- **`run_query` explicit `limit` param to exceed the 200-row cap.** The cap exists specifically to bound agent-triggered output; a param that raises it defeats that purpose. `offset`-based pagination (already shipped) already solves "I need more than 200 rows" without reopening that safety property. Skipped — flag to the maintainer if they disagree.
- **Writes/edits/categorisation, multi-ledger, HTTP/SSE transport.** Confirmed non-goals per the source brainstorm — read-only, single locked ledger path, stdio-only.

## Verified Facts (checked against the real `.venv`, not assumed)

- `conn.options` is a plain `dict` populated straight from `beancount.loader.load_file`'s options map — `conn.options.get("title")` and `conn.options.get("operating_currency")` are real, confirmed by direct test (`options.get('operating_currency')` returned `['USD']` for a ledger with `option "operating_currency" "USD"`).
- `SELECT min(date), max(date)` with no `FROM`/`WHERE` executes against the default `postings` table and returns one row — confirmed. Note: this only sees dates that have at least one *posting* (transactions), not bare `open`/`close`/`price` directives with no postings.
- `convert(sum(position), '<CCY>')` and `WHERE account ~ 'regex'` work exactly as documented in Beancount's BQL — confirmed against a live ledger with an operating currency, cash, and a liability account (net worth math checked out to the exact expected `950.00 USD`).
- **Important:** when a `WHERE`-filtered aggregate query matches **zero rows** (e.g. `as_of` before any transaction), `cursor.fetchall()` returns `[]`, not a single zero-valued row. Every aggregate tool below must handle the empty-rows case explicitly.
- `cost()`, `value()`, `units()` functions all exist in `beanquery.query_env` and work as expected — confirmed with a live lot-tracked purchase (`10 AAPL {150.00 USD}`, later priced at 175.00 USD): `units=10 AAPL`, `cost_basis=1500.00 USD`, `market_value=1750.00 USD`.
- `Inventory.get_only_position()` raises when the inventory holds more than one currency (i.e. `convert()` couldn't fully convert, usually a missing price) — every holdings/balance helper must catch this and fall back to `str(inventory)` rather than crashing.
- `conn.tables["prices"]` iterates `beancount.core.data.Price` namedtuples in `(meta, date, currency, amount)` order — confirmed by direct inspection.
- `bean_check`'s current implementation (`ledger.py:95-98`, `check()`) calls `loader.load_file` directly, a **second, independent** full parse of the ledger separate from the cached `beanquery` connection used by every other tool. `connection_errors()` (`ledger.py:100-103`) already reuses the cached connection's `conn.errors` with zero extra parses when the connection is warm. This is a real, confirmed redundancy — not a hypothetical one.

---

## File Structure

| File | Change |
|---|---|
| `src/beanie_mcp/server.py` | Add 5 new tools (`ledger_info`, `net_worth`, `account_balance`, `list_prices`, `holdings`), 2 new resources (`beanie://context`, `beanie://prices`), shared helpers (`_date_range`, `_operating_currency`, `_converted_sum`, `_validate_date`, `_DATE_RE`), rewrite `bql-guide` resource text, fix `bean_check` to reuse the cached connection. |
| `server_test.py` | Add tests for every new tool/resource and the `bean_check` dedup fix, plus one new fixture (`bean_file_with_options`/`with_options_ledger`) for currency/date-range assertions. |
| `pyproject.toml` | Version bump `0.1.0` → `0.2.0`, classifier `3 - Alpha` → `4 - Beta`. |
| `CHANGELOG.md` | New file, Keep-a-Changelog format. |
| `README.md` | Tool table gets 5 new rows + 2 new resource rows; Install section leads with `uvx`; MCP client config examples updated to the `uvx` form (existing `uv run --directory` form kept as a "from source" fallback). |

No changes to `ledger.py`'s public interface (its existing `check()` method is left as-is for direct `LedgerManager` tests — only the `server.py` call site changes).

---

## Task 1: `ledger_info` tool + `beanie://context` resource

**Files:**
- Modify: `src/beanie_mcp/server.py` (imports at top; new code after `_ledger_error_response`, before `_run_bql`; resource after `get_tables`)
- Test: `server_test.py` (new fixtures + tests after the `get_tables` tests, ~line 366)

**Interfaces:**
- Consumes: `_require_manager()`, `_account_names()`, `_ledger_error_response()` (all existing, unchanged signatures).
- Produces: `ledger_info() -> dict` with keys `today, title, operating_currency, date_range, account_count, account_roots` — reused by Task 3 (`net_worth`/`account_balance` need `operating_currency` lookup logic, factored as `_operating_currency()` in Task 3, not this task) and Task 7 (`holdings`).

- [ ] **Step 1: Write the failing tests**

Add to `server_test.py`, after `test_get_tables_returns_expected_tables` (~line 366):

```python
@pytest.fixture()
def bean_file_with_options(tmp_path):
    bean = tmp_path / "ledger.bean"
    _write_bean(
        bean,
        """
        option "title" "Test Ledger"
        option "operating_currency" "USD"
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        2022-03-15 * "Lunch"
          Assets:Cash    -10.00 USD
          Expenses:Food   10.00 USD
        2022-06-20 * "Dinner"
          Assets:Cash    -20.00 USD
          Expenses:Food   20.00 USD
    """,
    )
    return bean


@pytest.fixture()
def with_options_ledger(bean_file_with_options, monkeypatch):
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean_file_with_options))
    _reset_manager()
    yield bean_file_with_options
    _reset_manager()


def test_ledger_info_returns_expected_shape(with_options_ledger):
    from beanie_mcp.server import ledger_info

    result = ledger_info()
    assert set(result.keys()) == {
        "today",
        "title",
        "operating_currency",
        "date_range",
        "account_count",
        "account_roots",
    }
    assert result["title"] == "Test Ledger"
    assert result["operating_currency"] == ["USD"]
    assert result["account_roots"] == ["Assets", "Expenses"]
    assert result["date_range"] == {"first": "2022-03-15", "last": "2022-06-20"}
    assert result["account_count"] == 2


def test_ledger_info_no_ledger_raises(no_ledger):
    from beanie_mcp.server import ledger_info

    with pytest.raises(RuntimeError, match="BEANCOUNT_LEDGER"):
        ledger_info()


def test_ledger_info_broken_ledger_returns_error(tmp_path, monkeypatch):
    bean = tmp_path / "broken.bean"
    content = "2022-01-01 open Assets:Cash USD\n"
    content += "2022-01-02 balance Assets:Cash 999.00 USD\n"
    bean.write_text(content)
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import ledger_info

    result = ledger_info()
    assert result["error_type"] == "ledger"
    _reset_manager()


def test_get_context_resource_returns_valid_json(with_options_ledger):
    import json

    from beanie_mcp.server import get_context

    payload = json.loads(get_context())
    assert payload["title"] == "Test Ledger"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest server_test.py -k "ledger_info or get_context_resource" -v`
Expected: FAIL with `ImportError: cannot import name 'ledger_info'` (and same for `get_context`).

- [ ] **Step 3: Implement `ledger_info` and `beanie://context`**

In `src/beanie_mcp/server.py`, change the top import block from:

```python
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
```

to:

```python
import json
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
```

Then insert this new function immediately after `_ledger_error_response` (before `def _run_bql`):

```python
def _date_range(conn: beanquery.Connection) -> dict[str, str | None]:
    """Min/max posting date. Only sees dates with at least one posting —
    bare open/close/price directives with no postings are invisible here."""
    cursor = conn.execute("SELECT min(date), max(date)")
    rows = cursor.fetchall()
    if not rows or rows[0][0] is None:
        return {"first": None, "last": None}
    first, last = rows[0]
    return {"first": first.isoformat(), "last": last.isoformat()}


@mcp.tool()
def ledger_info() -> dict:
    """Orientation facts for querying: base currency, date span, account
    roots, and today's date. Call this before writing BQL to avoid
    guessing the operating currency or today's date.
    """
    mgr = _require_manager()
    errors = mgr.connection_errors()
    if errors:
        return _ledger_error_response(errors)

    conn = mgr.connection()
    accounts = _account_names()
    roots = sorted({a.split(":")[0] for a in accounts})

    return {
        "today": date.today().isoformat(),
        "title": conn.options.get("title"),
        "operating_currency": conn.options.get("operating_currency") or [],
        "date_range": _date_range(conn),
        "account_count": len(accounts),
        "account_roots": roots,
    }
```

Then add the resource right after `get_tables` (before `get_bql_guide`):

```python
@mcp.resource(
    "beanie://context",
    description=(
        "Orientation facts for querying: base currency, date span, account "
        "roots, today's date. Read this before writing BQL."
    ),
    mime_type="application/json",
)
def get_context() -> str:
    """JSON-encoded ledger_info() — see that tool for field meanings."""
    return json.dumps(ledger_info())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest server_test.py -k "ledger_info or get_context_resource" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Update README tool table**

In `README.md`, add two rows to the tool/resource table (after the `find_unmatched_transfers` row and after the `beanie://bql-guide` row respectively):

```
| `ledger_info` | Tool | Orientation facts before querying: operating currency, title, date span, account count/roots, today's date. |
```
and
```
| `beanie://context` | Resource | JSON-encoded `ledger_info()` output. |
```

- [ ] **Step 6: Commit**

```bash
git add src/beanie_mcp/server.py server_test.py README.md
git commit -m "feat: add ledger_info tool and beanie://context resource"
```

---

## Task 2: BQL guide — surface `convert`/`value` conversion guidance

**Files:**
- Modify: `src/beanie_mcp/server.py:get_bql_guide` (the `beanie://bql-guide` resource body)
- Test: `server_test.py` (one assertion added to the existing bql-guide coverage, or a new small test)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new (this is a text-content change, not a new symbol).

- [ ] **Step 1: Write the failing test**

Add to `server_test.py` near `test_get_tables_returns_expected_tables`:

```python
def test_bql_guide_documents_conversion():
    from beanie_mcp.server import get_bql_guide

    guide = get_bql_guide()
    assert "convert(" in guide
    assert "ledger_info" in guide
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest server_test.py -k test_bql_guide_documents_conversion -v`
Expected: FAIL — `assert "convert(" in guide` (guide text doesn't mention `convert()` yet).

- [ ] **Step 3: Rewrite `get_bql_guide`'s returned string**

In `src/beanie_mcp/server.py`, replace the `get_bql_guide` function body's docstring/return value:

```python
@mcp.resource(
    "beanie://bql-guide",
    description="Short BQL guide for agents, including Beancount-specific caveats.",
    mime_type="text/markdown",
)
def get_bql_guide() -> str:
    return """# BQL Guide for beanie-mcp

BQL looks SQL-like, but it is not general SQL.

## Get oriented first

Call `ledger_info` (or read `beanie://context`) before writing a query —
it tells you the ledger's operating currency and today's date, so you
never have to guess either.

## Important caveat

`FROM` is a date/filter clause, not a table selector. Do not assume
`SELECT ... FROM accounts` queries the accounts table. Use `list_accounts`
or `beanie://accounts` for account metadata.

## Credit-normal accounts (Income, Liabilities, Equity)

Beancount stores Income, Liabilities, and Equity accounts in credit-normal
form internally. A row-level query on one of these accounts shows each
posting's stored sign correctly, but `sum(position)` aggregates over many
postings can read as inverted from what you'd naively expect. If an
aggregate sign looks surprising, cross-check with a row-level (non-
aggregated) query on the same account before reporting a number downstream.

## Multi-currency ledgers: always convert

A ledger with multiple currencies (or any stock/crypto holdings) will
return a mixed-currency `Inventory` from `sum(position)` — not a single
number. Wrap it in `convert(..., '<CCY>')` to force one currency, using
the operating currency from `ledger_info`:

```sql
SELECT convert(sum(position), 'USD')
WHERE account ~ "Assets|Liabilities"
```

For lot-tracked holdings (stocks, crypto), `cost(position)` gives the
booked cost basis and `value(position)` gives the current market value —
both also need `convert()` if the lot's currency differs from your
operating currency:

```sql
SELECT currency, convert(sum(cost(position)), 'USD'),
       convert(sum(value(position)), 'USD')
WHERE account ~ "Assets:Broker"
GROUP BY currency
```

For net worth or account balances, prefer the dedicated `net_worth` and
`account_balance` tools — they handle the operating-currency lookup and
conversion for you.

## Useful examples

Total by account:

```sql
SELECT account, sum(position) GROUP BY account ORDER BY account
```

Spending by expense account:

```sql
SELECT account, sum(position)
WHERE account ~ "Expenses"
GROUP BY account
ORDER BY account
```

Recent transactions for an account:

```sql
SELECT date, payee, narration, account, position
WHERE account ~ "Assets:Bank"
ORDER BY date DESC
LIMIT 50
```

Always add a `LIMIT` when inspecting transaction-level rows.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest server_test.py -k test_bql_guide_documents_conversion -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/beanie_mcp/server.py server_test.py
git commit -m "docs: surface convert()/cost()/value() guidance in the BQL guide"
```

---

## Task 3: `net_worth` and `account_balance` tools

**Files:**
- Modify: `src/beanie_mcp/server.py` (new helpers + two new tools, inserted after `ledger_info`, before `_account_names`)
- Test: `server_test.py`

**Interfaces:**
- Consumes: `_require_manager()`, `_ledger_error_response()` (existing).
- Produces:
  - `_DATE_RE: re.Pattern` — reused by any future date-taking tool.
  - `_operating_currency(conn) -> str | None` — reused by Task 7 (`holdings`).
  - `_validate_date(as_of: str | None) -> dict | None` — returns an error dict or `None` if valid; reused by `account_balance`.
  - `_converted_sum(conn, where: str, as_of: str | None, currency: str) -> str` — reused by `net_worth` and `account_balance`.
  - `net_worth(as_of: str | None = None) -> dict`
  - `account_balance(account: str, as_of: str | None = None) -> dict`

- [ ] **Step 1: Write the failing tests**

Add to `server_test.py`, after the `ledger_info`/`get_context` tests from Task 1:

```python
@pytest.fixture()
def bean_file_net_worth(tmp_path):
    bean = tmp_path / "ledger.bean"
    _write_bean(
        bean,
        """
        option "operating_currency" "USD"
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Assets:Bank USD
        2022-01-01 open Liabilities:CreditCard USD
        2022-01-01 open Equity:Opening USD
        2022-01-01 open Expenses:Food USD
        2022-01-01 * "open"
          Assets:Cash    1000.00 USD
          Equity:Opening -1000.00 USD
        2022-06-01 * "spend"
          Assets:Cash    -50.00 USD
          Expenses:Food   50.00 USD
        2022-06-05 * "borrow"
          Assets:Bank    200.00 USD
          Liabilities:CreditCard -200.00 USD
    """,
    )
    return bean


@pytest.fixture()
def with_net_worth_ledger(bean_file_net_worth, monkeypatch):
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean_file_net_worth))
    _reset_manager()
    yield bean_file_net_worth
    _reset_manager()


def test_net_worth_current(with_net_worth_ledger):
    from beanie_mcp.server import net_worth

    result = net_worth()
    assert result["currency"] == "USD"
    assert result["net_worth"] == "950.00 USD"
    assert result["as_of"] is None


def test_net_worth_as_of_excludes_later_transactions(with_net_worth_ledger):
    from beanie_mcp.server import net_worth

    result = net_worth(as_of="2021-01-01")
    assert result["net_worth"] == "0.00 USD"


def test_net_worth_rejects_malformed_date(with_net_worth_ledger):
    from beanie_mcp.server import net_worth

    result = net_worth(as_of="not-a-date")
    assert result["error_type"] == "bql"


def test_net_worth_no_operating_currency(tmp_path, monkeypatch):
    bean = tmp_path / "no_ccy.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import net_worth

    result = net_worth()
    assert result["error_type"] == "ledger"
    _reset_manager()


def test_account_balance_exact_and_subtree(with_net_worth_ledger):
    from beanie_mcp.server import account_balance

    cash = account_balance("Assets:Cash")
    assert cash["balance"] == "950.00 USD"

    assets = account_balance("Assets")
    assert assets["balance"] == "1150.00 USD"


def test_account_balance_rejects_quote_in_account(with_net_worth_ledger):
    from beanie_mcp.server import account_balance

    result = account_balance('Assets"; DROP')
    assert result["error_type"] == "bql"


def test_account_balance_rejects_malformed_date(with_net_worth_ledger):
    from beanie_mcp.server import account_balance

    result = account_balance("Assets:Cash", as_of="06-01-2022")
    assert result["error_type"] == "bql"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest server_test.py -k "net_worth or account_balance" -v`
Expected: FAIL with `ImportError: cannot import name 'net_worth'`.

- [ ] **Step 3: Implement the helpers and tools**

In `src/beanie_mcp/server.py`, add `import re` to the top imports:

```python
import json
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
```

Insert this block immediately after the `ledger_info` tool (before `def _account_names`):

```python
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _operating_currency(conn: beanquery.Connection) -> str | None:
    currencies = conn.options.get("operating_currency") or []
    return currencies[0] if currencies else None


def _validate_date(as_of: str | None) -> dict | None:
    if as_of is not None and not _DATE_RE.match(as_of):
        return {
            "error": "as_of must be an ISO date (YYYY-MM-DD)",
            "error_type": "bql",
        }
    return None


def _converted_sum(
    conn: beanquery.Connection, where: str, as_of: str | None, currency: str
) -> str:
    query = f"SELECT convert(sum(position), '{currency}') WHERE {where}"
    if as_of:
        query += f" AND date <= {as_of}"
    cursor = conn.execute(query)
    rows = cursor.fetchall()
    if not rows:
        return f"0.00 {currency}"
    return str(rows[0][0])


@mcp.tool()
def net_worth(
    as_of: Annotated[
        str | None,
        Field(
            description=(
                "ISO date (YYYY-MM-DD). Balance as of this date; "
                "omit for the current balance."
            )
        ),
    ] = None,
) -> dict:
    """Net worth (Assets + Liabilities), converted to the ledger's
    operating currency. Uses the first configured operating_currency.
    """
    date_error = _validate_date(as_of)
    if date_error:
        return date_error

    mgr = _require_manager()
    errors = mgr.connection_errors()
    if errors:
        return _ledger_error_response(errors)

    conn = mgr.connection()
    currency = _operating_currency(conn)
    if currency is None:
        return {
            "error": "Ledger has no operating_currency configured.",
            "error_type": "ledger",
        }

    try:
        value = _converted_sum(conn, "account ~ 'Assets|Liabilities'", as_of, currency)
    except beanquery.Error as exc:
        return {"error": str(exc), "error_type": "bql"}

    return {"net_worth": value, "currency": currency, "as_of": as_of}


@mcp.tool()
def account_balance(
    account: str = Field(
        description=(
            "Account name or subtree root, e.g. 'Assets:Bank' matches "
            "Assets:Bank and every child account beneath it."
        )
    ),
    as_of: Annotated[
        str | None,
        Field(
            description=(
                "ISO date (YYYY-MM-DD). Balance as of this date; "
                "omit for the current balance."
            )
        ),
    ] = None,
) -> dict:
    """Balance of an account (and its subtree), converted to the ledger's
    operating currency.
    """
    if '"' in account:
        return {
            "error": "account must not contain a quote character",
            "error_type": "bql",
        }
    date_error = _validate_date(as_of)
    if date_error:
        return date_error

    mgr = _require_manager()
    errors = mgr.connection_errors()
    if errors:
        return _ledger_error_response(errors)

    conn = mgr.connection()
    currency = _operating_currency(conn)
    if currency is None:
        return {
            "error": "Ledger has no operating_currency configured.",
            "error_type": "ledger",
        }

    try:
        value = _converted_sum(conn, f'account ~ "^{account}"', as_of, currency)
    except beanquery.Error as exc:
        return {"error": str(exc), "error_type": "bql"}

    return {
        "account": account,
        "balance": value,
        "currency": currency,
        "as_of": as_of,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest server_test.py -k "net_worth or account_balance" -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Update README tool table**

Add two rows after the `ledger_info` row added in Task 1:

```
| `net_worth` | Tool | Net worth (Assets + Liabilities) converted to the operating currency. Optional `as_of` date. |
| `account_balance` | Tool | Balance of an account (and its subtree) converted to the operating currency. Optional `as_of` date. |
```

- [ ] **Step 6: Commit**

```bash
git add src/beanie_mcp/server.py server_test.py README.md
git commit -m "feat: add net_worth and account_balance tools"
```

---

## Task 4: `bean_check` reuses the cached connection (dedup fix)

**Files:**
- Modify: `src/beanie_mcp/server.py:bean_check` (~line 128-144 in the original file)
- Test: `server_test.py`

**Interfaces:**
- Consumes: `LedgerManager.connection_errors()` (existing, unchanged).
- Produces: no new symbols — `bean_check()`'s return shape (`{"ok", "message", "errors"}`) is unchanged, only its internals change.

- [ ] **Step 1: Write the failing test**

Add to `server_test.py`, after `test_bean_check_with_errors`:

```python
def test_bean_check_reuses_cached_connection(with_ledger, monkeypatch):
    from beanie_mcp import ledger as ledger_module
    from beanie_mcp.server import bean_check, run_query

    original_load_file = ledger_module.loader.load_file
    calls = []

    def counting_load_file(*args, **kwargs):
        calls.append(1)
        return original_load_file(*args, **kwargs)

    monkeypatch.setattr(ledger_module.loader, "load_file", counting_load_file)

    run_query("SELECT DISTINCT account ORDER BY account")
    bean_check()

    assert len(calls) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest server_test.py -k test_bean_check_reuses_cached_connection -v`
Expected: FAIL — `assert len(calls) == 1` fails because `bean_check()` currently calls `loader.load_file` a second time via `LedgerManager.check()`.

- [ ] **Step 3: Fix `bean_check`**

In `src/beanie_mcp/server.py`, change:

```python
@mcp.tool()
def bean_check() -> dict:
    """Validate the ledger with bean-check.

    Returns structured status and loader/balance errors.
    """
    errors = _require_manager().check()
    if not errors:
```

to:

```python
@mcp.tool()
def bean_check() -> dict:
    """Validate the ledger with bean-check.

    Returns structured status and loader/balance errors. Reuses the
    cached ledger connection instead of re-parsing the ledger from disk.
    """
    errors = _require_manager().connection_errors()
    if not errors:
```

(The rest of the function body is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest server_test.py -k "bean_check" -v`
Expected: PASS (3 tests — `test_bean_check_clean`, `test_bean_check_with_errors`, `test_bean_check_reuses_cached_connection`).

Then run the full suite once to confirm nothing else regressed:

Run: `.venv/bin/pytest server_test.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/beanie_mcp/server.py server_test.py
git commit -m "fix: bean_check reuses cached connection instead of re-parsing the ledger"
```

---

## Task 5: `list_prices` tool + `beanie://prices` resource

**Files:**
- Modify: `src/beanie_mcp/server.py` (new tool after `list_tables`, new resource after `beanie://tables`)
- Test: `server_test.py`

**Interfaces:**
- Consumes: `_require_manager()` (existing).
- Produces: `list_prices() -> dict` with keys `prices` (list of `{commodity, date, price}`), `count`.

- [ ] **Step 1: Write the failing tests**

Add to `server_test.py`, after `test_get_tables_returns_expected_tables`:

```python
@pytest.fixture()
def bean_file_prices(tmp_path):
    bean = tmp_path / "prices.bean"
    _write_bean(
        bean,
        """
        2022-01-01 open Assets:Cash USD
        2022-01-01 price AAPL 150.00 USD
        2022-06-01 price AAPL 175.00 USD
        2022-01-01 price GOOG 2500.00 USD
    """,
    )
    return bean


@pytest.fixture()
def with_prices_ledger(bean_file_prices, monkeypatch):
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean_file_prices))
    _reset_manager()
    yield bean_file_prices
    _reset_manager()


def test_list_prices_returns_latest_per_commodity(with_prices_ledger):
    from beanie_mcp.server import list_prices

    result = list_prices()
    assert result["count"] == 2
    by_commodity = {p["commodity"]: p for p in result["prices"]}
    assert by_commodity["AAPL"]["date"] == "2022-06-01"
    assert by_commodity["AAPL"]["price"] == "175.00 USD"
    assert by_commodity["GOOG"]["date"] == "2022-01-01"


def test_list_prices_empty_ledger(with_ledger):
    from beanie_mcp.server import list_prices

    result = list_prices()
    assert result == {"prices": [], "count": 0}


def test_get_prices_resource_returns_valid_json(with_prices_ledger):
    import json

    from beanie_mcp.server import get_prices

    payload = json.loads(get_prices())
    assert payload["count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest server_test.py -k "list_prices or get_prices_resource" -v`
Expected: FAIL with `ImportError: cannot import name 'list_prices'`.

- [ ] **Step 3: Implement `list_prices` and `beanie://prices`**

In `src/beanie_mcp/server.py`, insert this after `list_tables` (before `find_unmatched_transfers`):

```python
@mcp.tool(
    description="Latest known price for every commodity with price history in the ledger."
)
def list_prices() -> dict:
    """Latest price per commodity, from the prices table. Not subject to
    the 200-row query cap.
    """
    conn = _require_manager().connection()
    latest: dict[str, tuple] = {}
    for _, price_date, currency, amount in conn.tables["prices"]:
        prev = latest.get(currency)
        if prev is None or price_date > prev[0]:
            latest[currency] = (price_date, amount)

    prices = [
        {"commodity": currency, "date": price_date.isoformat(), "price": str(amount)}
        for currency, (price_date, amount) in sorted(latest.items())
    ]
    return {"prices": prices, "count": len(prices)}
```

Then add the resource right after `get_tables` (before `get_context` from Task 1, order doesn't matter as long as both exist before `get_bql_guide`):

```python
@mcp.resource(
    "beanie://prices",
    description="Latest known price for every commodity, as JSON.",
    mime_type="application/json",
)
def get_prices() -> str:
    """JSON-encoded list_prices() output."""
    return json.dumps(list_prices())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest server_test.py -k "list_prices or get_prices_resource" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Update README tool table**

```
| `list_prices` | Tool | Latest known price for every commodity with price history, without the 200-row cap. |
```
and
```
| `beanie://prices` | Resource | JSON-encoded `list_prices()` output. |
```

- [ ] **Step 6: Commit**

```bash
git add src/beanie_mcp/server.py server_test.py README.md
git commit -m "feat: add list_prices tool and beanie://prices resource"
```

---

## Task 6: PyPI publish prep (`uvx beanie-mcp`)

**Files:**
- Modify: `pyproject.toml` (version, classifier)
- Create: `CHANGELOG.md`
- Modify: `README.md` (Install section, MCP client config examples)

**Interfaces:** None — packaging/docs only, no code symbols.

- [ ] **Step 1: Bump version and classifier**

In `pyproject.toml`, change:

```toml
[project]
name = "beanie-mcp"
version = "0.1.0"
```

to:

```toml
[project]
name = "beanie-mcp"
version = "0.2.0"
```

And change:

```
    "Development Status :: 3 - Alpha",
```

to:

```
    "Development Status :: 4 - Beta",
```

- [ ] **Step 2: Create `CHANGELOG.md`**

Create `CHANGELOG.md`:

```markdown
# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0]

### Added
- `ledger_info` tool and `beanie://context` resource — operating currency,
  title, date span, account roots, today's date.
- `net_worth` tool — Assets + Liabilities converted to the operating
  currency, with an optional `as_of` date.
- `account_balance` tool — balance of an account or subtree converted to
  the operating currency, with an optional `as_of` date.
- `list_prices` tool and `beanie://prices` resource — latest known price
  per commodity.
- `holdings` tool — per-commodity units, cost basis, market value, and
  unrealised gain, converted to the operating currency.

### Changed
- `bean_check` now reuses the cached ledger connection instead of
  re-parsing the ledger from disk on every call.
- `beanie://bql-guide` documents `convert()`/`cost()`/`value()` for
  multi-currency and lot-tracked ledgers.

## [0.1.0]

Initial release: `run_query`, `bean_check`, `list_accounts`, `list_tables`,
`find_unmatched_transfers` tools; `beanie://accounts`, `beanie://tables`,
`beanie://bql-guide` resources.
```

- [ ] **Step 3: Rewrite the README Install section to lead with `uvx`**

In `README.md`, replace the `## Install` section body (currently `git clone` + `uv sync`) with:

```markdown
## Install

The recommended way to run beanie-mcp is via `uvx` — no clone, no local
checkout to keep updated:

```bash
uvx beanie-mcp
```

Find the absolute path to your main Beancount file. For example:

```bash
realpath ~/finance/main.bean
```

Use that full path as `BEANCOUNT_LEDGER` in the MCP config below. Relative paths are deliberately avoided because MCP clients may start the server from a different working directory.

You can smoke-test the server before adding it to an MCP client:

```bash
BEANCOUNT_LEDGER=/absolute/path/to/your/ledger/main.bean uvx beanie-mcp
```

The command starts an MCP stdio server and waits for a client. Press `Ctrl+C` to stop it.

### Running from source (development)

```bash
git clone https://github.com/klinikal/beanie-mcp.git
cd beanie-mcp
uv sync
BEANCOUNT_LEDGER=/absolute/path/to/your/ledger/main.bean uv run beanie-mcp
```
```

- [ ] **Step 4: Update MCP client config examples to `uvx`**

Replace each of the four `uv run --directory ... beanie-mcp` JSON/TOML blocks in the "Configure an MCP client", "Example: Claude Code / Claude Desktop", "Example: Codex", and "Other MCP clients" sections.

Generic command block — replace:

```json
{
  "command": "uv",
  "args": [
    "run",
    "--directory",
    "/absolute/path/to/beanie-mcp",
    "beanie-mcp"
  ],
  "env": {
    "BEANCOUNT_LEDGER": "/absolute/path/to/your/ledger/main.bean"
  }
}
```

with:

```json
{
  "command": "uvx",
  "args": ["beanie-mcp"],
  "env": {
    "BEANCOUNT_LEDGER": "/absolute/path/to/your/ledger/main.bean"
  }
}
```

Claude Code / Claude Desktop block — replace:

```json
{
  "mcpServers": {
    "beanie": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/beanie-mcp",
        "beanie-mcp"
      ],
      "env": {
        "BEANCOUNT_LEDGER": "/absolute/path/to/your/ledger/main.bean"
      }
    }
  }
}
```

with:

```json
{
  "mcpServers": {
    "beanie": {
      "command": "uvx",
      "args": ["beanie-mcp"],
      "env": {
        "BEANCOUNT_LEDGER": "/absolute/path/to/your/ledger/main.bean"
      }
    }
  }
}
```

Codex block — replace:

```toml
[mcp_servers.beanie]
command = "uv"
args = [
  "run",
  "--directory",
  "/absolute/path/to/beanie-mcp",
  "beanie-mcp",
]

[mcp_servers.beanie.env]
BEANCOUNT_LEDGER = "/absolute/path/to/your/ledger/main.bean"
```

with:

```toml
[mcp_servers.beanie]
command = "uvx"
args = ["beanie-mcp"]

[mcp_servers.beanie.env]
BEANCOUNT_LEDGER = "/absolute/path/to/your/ledger/main.bean"
```

"Other MCP clients" block — same replacement as the generic command block above.

Below each replaced block, add one line: `For running from source instead, see [Running from source](#running-from-source-development).`

- [ ] **Step 5: Verify the package builds and installs cleanly**

Run: `uv build`
Expected: `Successfully built dist/beanie_mcp-0.2.0.tar.gz and dist/beanie_mcp-0.2.0-py3-none-any.whl` (or equivalent — confirms the version bump took and the package still builds).

Run: `uvx --from ./dist/beanie_mcp-0.2.0-py3-none-any.whl beanie-mcp --help 2>&1 | head -5`
Expected: either a usage message or the "No ledger configured" `RuntimeError` message from `_require_manager` — either output proves the entry point resolves and imports cleanly from the built wheel (do not expect a clean exit; the point is confirming it launches, not a full run).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml CHANGELOG.md README.md
git commit -m "chore: prep v0.2.0 for PyPI — uvx install path, beta classifier, changelog"
```

**Note:** This task prepares the release. Actually publishing to PyPI (`uv publish` or `twine upload`) requires PyPI account credentials and is a one-way, irreversible action (a version number can never be reused once uploaded) — confirm with the maintainer before running it. Do not run a publish command as part of this task.

---

## Task 7 (optional/stretch): `holdings` tool

This is the highest-effort, highest-risk task in the plan (Opus's own assessment: "stretch goal"). It is independent of every other task — safe to drop without affecting Tasks 1-6. Attempt it last, and only if time/budget remains.

**Files:**
- Modify: `src/beanie_mcp/server.py` (new tool + one helper, inserted after `account_balance` from Task 3)
- Test: `server_test.py`

**Interfaces:**
- Consumes: `_operating_currency()` (from Task 3), `_require_manager()`, `_ledger_error_response()`.
- Produces: `holdings() -> dict` with key `holdings` (list of `{commodity, units, cost_basis, market_value, gain}`), `currency`, `count`.

- [ ] **Step 1: Write the failing tests**

Add to `server_test.py`, after the Task 3 `account_balance` tests:

```python
@pytest.fixture()
def bean_file_holdings(tmp_path):
    bean = tmp_path / "holdings.bean"
    _write_bean(
        bean,
        """
        option "operating_currency" "USD"
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Assets:Broker:AAPL AAPL
        2022-01-01 open Equity:Opening USD
        2022-01-01 price AAPL 150.00 USD
        2022-06-01 price AAPL 175.00 USD
        2022-01-01 * "fund"
          Assets:Cash    5000.00 USD
          Equity:Opening -5000.00 USD
        2022-01-01 * "buy"
          Assets:Broker:AAPL 10 AAPL {150.00 USD}
          Assets:Cash    -1500.00 USD
    """,
    )
    return bean


@pytest.fixture()
def with_holdings_ledger(bean_file_holdings, monkeypatch):
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean_file_holdings))
    _reset_manager()
    yield bean_file_holdings
    _reset_manager()


def test_holdings_reports_units_cost_value_gain(with_holdings_ledger):
    from beanie_mcp.server import holdings

    result = holdings()
    assert result["currency"] == "USD"
    assert result["count"] == 1
    row = result["holdings"][0]
    assert row["commodity"] == "AAPL"
    assert row["units"] == "10 AAPL"
    assert row["cost_basis"] == "1500.00 USD"
    assert row["market_value"] == "1750.00 USD"
    assert row["gain"] == "250.00"


def test_holdings_no_holdings_returns_empty(with_ledger):
    from beanie_mcp.server import holdings

    result = holdings()
    assert result["holdings"] == []
    assert result["count"] == 0


def test_holdings_no_operating_currency(tmp_path, monkeypatch):
    bean = tmp_path / "no_ccy.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import holdings

    result = holdings()
    assert result["error_type"] == "ledger"
    _reset_manager()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest server_test.py -k holdings -v`
Expected: FAIL with `ImportError: cannot import name 'holdings'`.

- [ ] **Step 3: Implement `holdings`**

Insert after `account_balance` in `src/beanie_mcp/server.py`:

```python
def _inventory_amount(inv) -> tuple[str, str | None]:
    """Return (display_string, numeric_string). numeric_string is None if
    the inventory holds more than one currency (conversion incomplete,
    usually a missing price) — display_string falls back to str(inv)."""
    try:
        pos = inv.get_only_position()
    except (ValueError, AssertionError):
        return str(inv), None
    if pos is None:
        return "0.00", "0.00"
    return str(pos.units), str(pos.units.number)


@mcp.tool(
    description=(
        "Per-commodity holdings: units, cost basis, market value, and "
        "unrealised gain, converted to the operating currency."
    )
)
def holdings() -> dict:
    """Fava-style holdings report. Excludes the operating currency itself
    (cash isn't a 'holding')."""
    mgr = _require_manager()
    errors = mgr.connection_errors()
    if errors:
        return _ledger_error_response(errors)

    conn = mgr.connection()
    currency = _operating_currency(conn)
    if currency is None:
        return {
            "error": "Ledger has no operating_currency configured.",
            "error_type": "ledger",
        }

    query = (
        "SELECT currency, sum(units(position)) as units, "
        f"convert(sum(cost(position)), '{currency}') as cost_basis, "
        f"convert(sum(value(position)), '{currency}') as market_value "
        f"WHERE account ~ 'Assets' AND currency != '{currency}' "
        "GROUP BY currency ORDER BY currency"
    )
    try:
        cursor = conn.execute(query)
    except beanquery.Error as exc:
        return {"error": str(exc), "error_type": "bql"}

    rows = []
    for commodity, units, cost_inv, value_inv in cursor.fetchall():
        cost_str, cost_num = _inventory_amount(cost_inv)
        value_str, value_num = _inventory_amount(value_inv)
        gain = (
            str(Decimal(value_num) - Decimal(cost_num))
            if cost_num is not None and value_num is not None
            else None
        )
        rows.append(
            {
                "commodity": commodity,
                "units": str(units),
                "cost_basis": cost_str,
                "market_value": value_str,
                "gain": gain,
            }
        )

    return {"holdings": rows, "currency": currency, "count": len(rows)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest server_test.py -k holdings -v`
Expected: PASS (3 tests).

Then run the full suite:

Run: `.venv/bin/pytest server_test.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Update README tool table**

```
| `holdings` | Tool | Per-commodity units, cost basis, market value, and unrealised gain, converted to the operating currency. |
```

- [ ] **Step 6: Commit**

```bash
git add src/beanie_mcp/server.py server_test.py README.md
git commit -m "feat: add holdings tool"
```

---

## Final Verification (run after all tasks)

- [ ] Run the full test suite: `.venv/bin/pytest server_test.py -v` — every test passes, none skipped.
- [ ] Run lint: `.venv/bin/ruff check src/ server_test.py` — no errors.
- [ ] Read through `README.md`'s tool table once — count matches the actual `@mcp.tool()`/`@mcp.resource()` decorators in `server.py` (should be 8 tools, 5 resources after this plan, 6 tools if Task 7 is skipped).
- [ ] Confirm `git log --oneline -10` shows one commit per task, no stray uncommitted changes (`git status`).
