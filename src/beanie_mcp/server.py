"""beanie-mcp — MCP server for querying Beancount v3 ledgers with BQL."""

from contextlib import asynccontextmanager
from pathlib import Path

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


def _run_bql(query: str) -> dict:
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
    fetched_rows = cursor.fetchmany(ROW_LIMIT + 1)
    truncated = len(fetched_rows) > ROW_LIMIT
    rows = fetched_rows[:ROW_LIMIT]

    return {
        "columns": columns,
        "rows": [[str(v) for v in row] for row in rows],
        "truncated": truncated,
        "returned_rows": len(rows),
        "total_rows": None if truncated else len(rows),
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
) -> dict:
    """Query the Beancount ledger using BQL.

    Returns a dict with:
      columns    — list of column name strings
      rows       — list of rows, each a list of value strings
      truncated  — true if the result was cut at 200 rows
      returned_rows — number of rows returned
      total_rows — exact count only when not truncated; otherwise null
      error      — present (instead of the above) if the ledger or BQL is invalid
    """
    return _run_bql(query)


@mcp.tool()
def bean_check() -> dict:
    """Validate the ledger with bean-check.

    Returns structured status and loader/balance errors.
    """
    errors = _require_manager().check()
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
    "beanie://bql-guide",
    description="Short BQL guide for agents, including Beancount-specific caveats.",
    mime_type="text/markdown",
)
def get_bql_guide() -> str:
    return """# BQL Guide for beanie-mcp

BQL looks SQL-like, but it is not general SQL.

## Important caveat

`FROM` is a date/filter clause, not a table selector. Do not assume
`SELECT ... FROM accounts` queries the accounts table. Use `list_accounts`
or `beanie://accounts` for account metadata.

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
