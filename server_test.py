"""Tests for beanie-mcp — ledger.py and server.py."""

import os
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beanie_mcp.ledger import LedgerManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bean(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


def _reset_manager():
    from beanie_mcp import server

    server._manager_cache = None


# ---------------------------------------------------------------------------
# LedgerManager
# ---------------------------------------------------------------------------


def test_connection_returns_queryable_result(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(
        bean,
        """
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        2022-01-02 * "Lunch"
          Assets:Cash    -10.00 USD
          Expenses:Food   10.00 USD
    """,
    )
    mgr = LedgerManager(bean)
    cursor = mgr.connection().execute("SELECT DISTINCT account ORDER BY account")
    accounts = [row[0] for row in cursor.fetchall()]
    assert "Assets:Cash" in accounts
    assert "Expenses:Food" in accounts


def test_connection_is_cached(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    assert mgr.connection() is mgr.connection()


def test_connection_reloads_on_mtime_change(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    first = mgr.connection()
    os.utime(bean, (time.time() + 1, time.time() + 1))
    assert mgr.connection() is not first


def test_invalidate_forces_reload(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    first = mgr.connection()
    mgr.invalidate()
    assert mgr.connection() is not first


def test_check_clean_ledger(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    assert LedgerManager(bean).check() == []


def test_check_returns_errors(tmp_path):
    bean = tmp_path / "test.bean"
    content = "2022-01-01 open Assets:Cash USD\n"
    content += "2022-01-02 balance Assets:Cash 999.00 USD\n"
    _write_bean(bean, content)
    errors = LedgerManager(bean).check()
    assert len(errors) > 0
    assert all({"file", "line", "type", "message"} <= set(e) for e in errors)
    assert any(
        "999" in str(e["message"]) or "balance" in str(e["message"]).lower()
        for e in errors
    )


def test_connection_reloads_on_include_mtime_change(tmp_path):
    root = tmp_path / "main.bean"
    included = tmp_path / "accounts.bean"
    _write_bean(root, 'include "accounts.bean"\n')
    _write_bean(included, "2022-01-01 open Assets:Cash USD\n")

    mgr = LedgerManager(root)
    first = mgr.connection()
    _write_bean(
        included,
        """
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        """,
    )

    assert mgr.connection() is not first


def test_watcher_handles_atomic_save_events(tmp_path):
    bean = tmp_path / "test.bean"
    _write_bean(bean, "2022-01-01 open Assets:Cash USD\n")
    mgr = LedgerManager(bean)
    first = mgr.connection()
    mgr.start_watcher()

    try:
        handler = mgr._handler
        assert handler is not None
        handler.on_any_event(
            SimpleNamespace(
                is_directory=False,
                src_path=str(tmp_path / "test.bean.tmp"),
                dest_path=str(bean),
            )
        )
        time.sleep(2.2)
        assert mgr.connection() is not first
    finally:
        mgr.stop_watcher()


# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------


@pytest.fixture()
def bean_file(tmp_path):
    bean = tmp_path / "ledger.bean"
    _write_bean(
        bean,
        """
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        2022-01-02 * "Lunch"
          Assets:Cash    -10.00 USD
          Expenses:Food   10.00 USD
    """,
    )
    return bean


@pytest.fixture()
def with_ledger(bean_file, monkeypatch):
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean_file))
    _reset_manager()
    yield bean_file
    _reset_manager()


@pytest.fixture()
def no_ledger(monkeypatch):
    monkeypatch.delenv("BEANCOUNT_LEDGER", raising=False)
    _reset_manager()
    yield
    _reset_manager()


def test_run_query_returns_dict(with_ledger):
    from beanie_mcp.server import run_query

    result = run_query("SELECT DISTINCT account ORDER BY account")
    assert isinstance(result, dict)
    assert set(result.keys()) == {
        "columns",
        "rows",
        "truncated",
        "returned_rows",
        "offset",
        "total_rows",
        "total_rows_known",
    }


def test_run_query_correct_results(with_ledger):
    from beanie_mcp.server import run_query

    result = run_query("SELECT DISTINCT account ORDER BY account")
    accounts = [row[0] for row in result["rows"]]
    assert "Assets:Cash" in accounts
    assert "Expenses:Food" in accounts
    assert result["truncated"] is False


def test_run_query_no_ledger_raises(no_ledger):
    from beanie_mcp.server import run_query

    with pytest.raises(RuntimeError, match="BEANCOUNT_LEDGER"):
        run_query("SELECT account")


def test_run_query_invalid_bql_returns_error(with_ledger):
    from beanie_mcp.server import run_query

    result = run_query("THIS IS NOT VALID BQL")
    assert "error" in result
    assert isinstance(result["error"], str)
    assert result["error_type"] == "bql"


def test_run_query_broken_ledger_returns_error(tmp_path, monkeypatch):
    bean = tmp_path / "broken.bean"
    _write_bean(
        bean,
        """
        2022-01-01 open Assets:Cash USD
        2022-01-02 balance Assets:Cash 999.00 USD
        """,
    )
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import run_query

    result = run_query("SELECT account")
    assert result["error_type"] == "ledger"
    assert result["errors"]
    assert "Balance" in result["errors"][0]["type"]
    _reset_manager()


def test_run_query_row_limit(tmp_path, monkeypatch):
    # Build a ledger with 201 transactions so postings table exceeds 200 rows
    lines = [
        "2022-01-01 open Assets:Cash USD",
        "2022-01-01 open Expenses:Food USD",
    ]
    for i in range(201):
        lines += [
            f'2022-01-{(i % 28) + 1:02d} * "Txn {i}"',
            "  Assets:Cash    -1.00 USD",
            "  Expenses:Food   1.00 USD",
        ]
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import run_query

    result = run_query("SELECT date, narration, account, position")
    assert len(result["rows"]) == 200
    assert result["truncated"] is True
    assert result["returned_rows"] == 200
    assert result["total_rows"] is None
    assert result["total_rows_known"] is False
    _reset_manager()


def test_run_query_offset_returns_next_page(tmp_path, monkeypatch):
    lines = [
        "2022-01-01 open Assets:Cash USD",
        "2022-01-01 open Expenses:Food USD",
    ]
    for i in range(201):
        lines += [
            f'2022-01-{(i % 28) + 1:02d} * "Txn {i}"',
            "  Assets:Cash    -1.00 USD",
            "  Expenses:Food   1.00 USD",
        ]
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import run_query

    # 201 transactions x 2 postings each = 402 total rows.
    result = run_query("SELECT date, narration, account, position", offset=400)
    assert len(result["rows"]) == 2
    assert result["truncated"] is False
    assert result["offset"] == 400
    assert result["total_rows"] == 402
    assert result["total_rows_known"] is True
    _reset_manager()


def test_run_query_offset_past_end_returns_empty(tmp_path, monkeypatch):
    lines = [
        "2022-01-01 open Assets:Cash USD",
        "2022-01-01 open Expenses:Food USD",
    ]
    for i in range(201):
        lines += [
            f'2022-01-{(i % 28) + 1:02d} * "Txn {i}"',
            "  Assets:Cash    -1.00 USD",
            "  Expenses:Food   1.00 USD",
        ]
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import run_query

    result = run_query("SELECT date, narration, account, position", offset=500)
    assert len(result["rows"]) == 0
    assert result["truncated"] is False
    assert result["total_rows"] == 402
    _reset_manager()


def test_bean_check_clean(with_ledger):
    from beanie_mcp.server import bean_check

    result = bean_check()
    assert result["ok"] is True
    assert "clean" in result["message"].lower()


def test_bean_check_with_errors(tmp_path, monkeypatch):
    bean = tmp_path / "broken.bean"
    content = "2022-01-01 open Assets:Cash USD\n"
    content += "2022-01-02 balance Assets:Cash 999.00 USD\n"
    bean.write_text(content)
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import bean_check

    result = bean_check()
    assert result["ok"] is False
    assert any(
        "999" in str(error["message"]) or "balance" in str(error["message"]).lower()
        for error in result["errors"]
    )
    _reset_manager()


def test_get_accounts_returns_all_accounts(tmp_path, monkeypatch):
    lines = ["2022-01-01 open Assets:Root USD"]
    for i in range(250):
        lines.append(f"2022-01-01 open Expenses:Cat{i:03d} USD")
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import get_accounts

    result = get_accounts()
    rows = result.strip().split("\n")
    assert len(rows) == 251
    assert "Assets:Root" in rows
    assert "Expenses:Cat000" in rows
    _reset_manager()


def test_get_tables_returns_expected_tables(with_ledger):
    from beanie_mcp.server import get_tables

    tables = get_tables().strip().split("\n")
    for expected in ["accounts", "balances", "entries", "postings", "transactions"]:
        assert expected in tables


def test_list_accounts_returns_structured_result(tmp_path, monkeypatch):
    bean = tmp_path / "accounts.bean"
    _write_bean(
        bean,
        """
        2022-01-01 open Assets:Cash USD
        2022-01-01 open Expenses:Food USD
        """,
    )
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import list_accounts

    result = list_accounts()
    assert result == {
        "accounts": ["Assets:Cash", "Expenses:Food"],
        "count": 2,
    }
    _reset_manager()


# ---------------------------------------------------------------------------
# find_unmatched_transfers
# ---------------------------------------------------------------------------


def _write_staging_ledger(tmp_path, entries: str) -> Path:
    bean = tmp_path / "staging.bean"
    header = """
        2022-01-01 open Assets:Bank:Checking
        2022-01-01 open Assets:Bank:Savings
        2022-01-01 open Equity:Transfers:Pending
    """
    _write_bean(bean, header + entries)
    return bean


def test_find_unmatched_transfers_matches_pair(tmp_path, monkeypatch):
    bean = _write_staging_ledger(
        tmp_path,
        """
        2022-02-01 * "Send"
          Assets:Bank:Checking       -500.00 USD
          Equity:Transfers:Pending    500.00 USD

        2022-02-02 * "Receive"
          Assets:Bank:Savings         500.00 USD
          Equity:Transfers:Pending   -500.00 USD
        """,
    )
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import find_unmatched_transfers

    result = find_unmatched_transfers("Equity:Transfers:Pending")
    assert result["matched_count"] == 1
    assert result["orphan_count"] == 0
    assert result["orphans"] == []
    _reset_manager()


def test_find_unmatched_transfers_reports_orphan(tmp_path, monkeypatch):
    bean = _write_staging_ledger(
        tmp_path,
        """
        2022-02-01 * "Send"
          Assets:Bank:Checking       -500.00 USD
          Equity:Transfers:Pending    500.00 USD
        """,
    )
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import find_unmatched_transfers

    result = find_unmatched_transfers("Equity:Transfers:Pending")
    assert result["matched_count"] == 0
    assert result["orphan_count"] == 1
    assert result["orphans"][0]["amount"] == "500.00"
    assert result["orphans"][0]["currency"] == "USD"
    _reset_manager()


def test_find_unmatched_transfers_respects_window(tmp_path, monkeypatch):
    bean = _write_staging_ledger(
        tmp_path,
        """
        2022-02-01 * "Send"
          Assets:Bank:Checking       -500.00 USD
          Equity:Transfers:Pending    500.00 USD

        2022-02-10 * "Receive late"
          Assets:Bank:Savings         500.00 USD
          Equity:Transfers:Pending   -500.00 USD
        """,
    )
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import find_unmatched_transfers

    result = find_unmatched_transfers("Equity:Transfers:Pending", window_days=2)
    assert result["matched_count"] == 0
    assert result["orphan_count"] == 2
    _reset_manager()


def test_find_unmatched_transfers_currency_filter(tmp_path, monkeypatch):
    bean = _write_staging_ledger(
        tmp_path,
        """
        2022-02-01 * "Send USD"
          Assets:Bank:Checking       -500.00 USD
          Equity:Transfers:Pending    500.00 USD

        2022-02-01 * "Send EUR"
          Assets:Bank:Checking       -500.00 EUR
          Equity:Transfers:Pending    500.00 EUR
        """,
    )
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from beanie_mcp.server import find_unmatched_transfers

    result = find_unmatched_transfers("Equity:Transfers:Pending", currency="USD")
    assert result["orphan_count"] == 1
    assert result["orphans"][0]["currency"] == "USD"
    _reset_manager()
