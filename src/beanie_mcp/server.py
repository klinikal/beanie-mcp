"""beanie-mcp — MCP server for querying Beancount v3 ledgers with BQL."""

import json
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import beanquery
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .ledger import LedgerManager

ROW_LIMIT = 200

# Reset to None in tests between runs to pick up env var changes.
_manager_cache: LedgerManager | None = None


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEANCOUNT_", env_file=".env")
    ledger: Path | None = Field(default=None)


def _require_manager() -> LedgerManager:
    global _manager_cache
    if _manager_cache is None:
        path = _Settings().ledger
        if not path:
            raise RuntimeError(
                "No ledger configured. Set the BEANCOUNT_LEDGER environment variable "
                "to the absolute path of your .bean file."
            )
        _manager_cache = LedgerManager(path)
    return _manager_cache


@asynccontextmanager
async def _lifespan(app):
    mgr = _require_manager()
    mgr.start_watcher()
    try:
        yield
    finally:
        mgr.stop_watcher()


mcp = FastMCP("beanie-mcp", lifespan=_lifespan)


def _ledger_error_response(errors: list[dict[str, object]]) -> dict:
    return {
        "error": "Ledger has bean-check errors; fix them before querying.",
        "error_type": "ledger",
        "errors": errors,
    }


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


def _run_bql(query: str, offset: int = 0) -> dict:
    mgr = _require_manager()
    conn = mgr.connection()
    errors = mgr.connection_errors()
    if errors:
        return _ledger_error_response(errors)

    try:
        cursor = conn.execute(query)
    except beanquery.Error as exc:
        return {"error": str(exc), "error_type": "bql"}

    columns = [col.name for col in cursor.description] if cursor.description else []
    skipped = cursor.fetchmany(offset) if offset > 0 else []
    fetched_rows = cursor.fetchmany(ROW_LIMIT + 1)
    truncated = len(fetched_rows) > ROW_LIMIT
    rows = fetched_rows[:ROW_LIMIT]

    return {
        "columns": columns,
        "rows": [[str(v) for v in row] for row in rows],
        "truncated": truncated,
        "returned_rows": len(rows),
        "offset": offset,
        "total_rows": None if truncated else len(skipped) + len(rows),
        "total_rows_known": not truncated,
    }


@mcp.tool()
def run_query(
    query: str = Field(
        description=(
            "A BQL query, e.g. 'SELECT account, sum(position) "
            'WHERE account ~ "Expenses" GROUP BY account\'. '
            "Results are capped at 200 returned rows. "
            "BQL is SQL-like, but FROM is a date/filter clause, not a table selector."
        )
    ),
    offset: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Number of rows to skip before returning results. BQL has no "
                "OFFSET keyword, so use this to page past the 200-row cap: if "
                "`truncated` is true, call again with offset += returned_rows to "
                "fetch the next page."
            ),
        ),
    ] = 0,
) -> dict:
    """Query the Beancount ledger using BQL.

    Returns a dict with:
      columns    — list of column name strings
      rows       — list of rows, each a list of value strings
      truncated  — true if the result was cut at 200 rows
      returned_rows — number of rows returned
      offset     — the offset this call was made with
      total_rows — exact count only when not truncated; otherwise null
      error      — present (instead of the above) if the ledger or BQL is invalid
    """
    return _run_bql(query, offset)


@mcp.tool()
def bean_check() -> dict:
    """Validate the ledger with bean-check.

    Returns structured status and loader/balance errors. Reuses the
    cached ledger connection instead of re-parsing the ledger from disk.
    """
    errors = _require_manager().connection_errors()
    if not errors:
        return {
            "ok": True,
            "message": "Ledger is clean — no errors or warnings.",
            "errors": [],
        }
    return {
        "ok": False,
        "message": f"Ledger has {len(errors)} error(s).",
        "errors": errors,
    }


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
    inventory = rows[0][0]
    try:
        position = inventory.get_only_position()
    except (ValueError, AssertionError):
        return str(inventory)
    if position is None:
        return f"0.00 {currency}"
    return str(position)


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
        units_str, _ = _inventory_amount(units)
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
                "units": units_str,
                "cost_basis": cost_str,
                "market_value": value_str,
                "gain": gain,
            }
        )

    return {"holdings": rows, "currency": currency, "count": len(rows)}


def _account_names() -> list[str]:
    conn = _require_manager().connection()
    return sorted(str(row[0]) for row in conn.tables["accounts"])


def _table_names() -> list[str]:
    conn = _require_manager().connection()
    return sorted(k for k in conn.tables if k is not None and k != "")


@mcp.tool(
    description="List every account declared in the ledger, without the query row cap."
)
def list_accounts() -> dict:
    """Return all declared accounts as structured JSON."""
    accounts = _account_names()
    return {"accounts": accounts, "count": len(accounts)}


@mcp.tool(
    description=(
        "List beanquery table names. Note: BQL FROM is not a SQL table selector; "
        "use dedicated tools for accounts."
    )
)
def list_tables() -> dict:
    """Return BQL-accessible table names and the key BQL caveat."""
    return {
        "tables": _table_names(),
        "warning": (
            "In BQL, FROM is a date/filter clause, not a SQL-style table selector."
        ),
    }


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


@mcp.tool()
def find_unmatched_transfers(
    account: str = Field(
        description=(
            "Exact name of the staging/suspense account to reconcile, e.g. "
            "'Equity:Transfers:Pending'."
        )
    ),
    window_days: Annotated[
        int,
        Field(
            ge=0,
            description="Max days apart two legs can be and still count as a match.",
        ),
    ] = 2,
    min_amount: Annotated[
        float,
        Field(
            ge=0,
            description="Ignore postings below this absolute amount threshold.",
        ),
    ] = 0,
    currency: Annotated[
        str | None, Field(description="Restrict matching to this currency only.")
    ] = None,
) -> dict:
    """Greedy-match postings to a staging/suspense account.

    Matches postings by opposite sign, equal amount, same currency, and date
    within `window_days`. Returns matched_count, orphan_count, and the
    unmatched ("orphan") postings — not subject to the 200-row query cap.
    """
    if '"' in account:
        return {
            "error": "account must not contain a quote character",
            "error_type": "bql",
        }

    mgr = _require_manager()
    errors = mgr.connection_errors()
    if errors:
        return _ledger_error_response(errors)

    conn = mgr.connection()
    try:
        cursor = conn.execute(
            f'SELECT date, narration, position '
            f'WHERE account = "{account}" ORDER BY date'
        )
    except beanquery.Error as exc:
        return {"error": str(exc), "error_type": "bql"}

    min_amount_decimal = Decimal(str(min_amount))
    postings = []
    for date, narration, position in cursor.fetchall():
        amount = position.units.number
        posting_currency = position.units.currency
        if currency and posting_currency != currency:
            continue
        if abs(amount) < min_amount_decimal:
            continue
        postings.append(
            {
                "date": date,
                "narration": narration,
                "amount": amount,
                "currency": posting_currency,
            }
        )

    window = timedelta(days=window_days)
    unmatched = list(postings)
    matched_count = 0
    i = 0
    while i < len(unmatched):
        p = unmatched[i]
        match_idx = None
        for j in range(i + 1, len(unmatched)):
            q = unmatched[j]
            if (
                q["currency"] == p["currency"]
                and q["amount"] == -p["amount"]
                and abs(q["date"] - p["date"]) <= window
            ):
                match_idx = j
                break
        if match_idx is not None:
            del unmatched[match_idx]
            del unmatched[i]
            matched_count += 1
        else:
            i += 1

    orphans = [
        {
            "date": p["date"].isoformat(),
            "amount": str(p["amount"]),
            "currency": p["currency"],
            "narration": p["narration"],
        }
        for p in unmatched
    ]

    return {
        "matched_count": matched_count,
        "orphans": orphans,
        "orphan_count": len(orphans),
    }


@mcp.resource(
    "beanie://accounts",
    description=(
        "All accounts declared in the ledger, one per line, sorted alphabetically. "
        "Not subject to the 200-row query cap."
    ),
    mime_type="text/plain",
)
def get_accounts() -> str:
    """All accounts declared in the ledger, one per line, sorted alphabetically.
    Not subject to the 200-row query cap.
    """
    return "\n".join(_account_names())


@mcp.resource(
    "beanie://tables",
    description=(
        "Names of beanquery tables available to BQL. Use the list for discovery; "
        "do not use SQL-style FROM table_name to select them."
    ),
    mime_type="text/plain",
)
def get_tables() -> str:
    """BQL-accessible tables (accounts, balances, entries, postings, prices, etc.)."""
    return "\n".join(_table_names())


@mcp.resource(
    "beanie://prices",
    description="Latest known price for every commodity, as JSON.",
    mime_type="application/json",
)
def get_prices() -> str:
    """JSON-encoded list_prices() output."""
    return json.dumps(list_prices())


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


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
