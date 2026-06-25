"""beanie-mcp — MCP server for querying Beancount v3 ledgers in plain English."""

from contextlib import asynccontextmanager
from pathlib import Path

import beanquery
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger import LedgerManager

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


@mcp.tool()
def run_query(
    query: str = Field(
        description=(
            "A BQL query, e.g. 'SELECT account, sum(position) "
            'WHERE account ~ "Expenses" GROUP BY account\'. '
            "Results are capped at 200 rows — add LIMIT for smaller sets."
        )
    ),
) -> dict:
    """Query the Beancount ledger using BQL.

    Returns a dict with:
      columns    — list of column name strings
      rows       — list of rows, each a list of value strings
      truncated  — true if the result was cut at 200 rows
      total_rows — full result count before any truncation
      error      — present (instead of the above) if the BQL is invalid
    """
    conn = _require_manager().connection()
    try:
        cursor = conn.execute(query)
    except beanquery.Error as exc:
        return {"error": str(exc)}

    columns = [col.name for col in cursor.description] if cursor.description else []
    all_rows = cursor.fetchall()
    truncated = len(all_rows) > ROW_LIMIT

    return {
        "columns": columns,
        "rows": [[str(v) for v in row] for row in all_rows[:ROW_LIMIT]],
        "truncated": truncated,
        "total_rows": len(all_rows),
    }


@mcp.tool()
def bean_check() -> str:
    """Validate the ledger with bean-check.

    Returns a clean confirmation message if there are no problems,
    or a newline-separated list of errors with file path and line number.
    """
    errors = _require_manager().check()
    if not errors:
        return "Ledger is clean — no errors or warnings."
    return "\n".join(errors)


@mcp.resource("beanie://accounts")
def get_accounts() -> str:
    """All accounts declared in the ledger, one per line, sorted alphabetically.
    Not subject to the 200-row query cap.
    """
    conn = _require_manager().connection()
    accounts = sorted(str(row[0]) for row in conn.tables["accounts"])
    return "\n".join(accounts)


@mcp.resource("beanie://tables")
def get_tables() -> str:
    """BQL-accessible tables (accounts, balances, entries, postings, prices, etc.)."""
    conn = _require_manager().connection()
    tables = sorted(k for k in conn.tables if k is not None and k != "")
    return "\n".join(tables)
