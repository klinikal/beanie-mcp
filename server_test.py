"""Tests for beanie-mcp — ledger.py and server.py."""

import os
import textwrap
import time
from pathlib import Path

import pytest

from ledger import LedgerManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bean(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


def _reset_manager():
    import server

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
    assert all(": " in e for e in errors)
    assert any("999" in e or "balance" in e.lower() for e in errors)


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
    from server import run_query

    result = run_query("SELECT DISTINCT account ORDER BY account")
    assert isinstance(result, dict)
    assert set(result.keys()) == {"columns", "rows", "truncated", "total_rows"}


def test_run_query_correct_results(with_ledger):
    from server import run_query

    result = run_query("SELECT DISTINCT account ORDER BY account")
    accounts = [row[0] for row in result["rows"]]
    assert "Assets:Cash" in accounts
    assert "Expenses:Food" in accounts
    assert result["truncated"] is False


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
    from server import run_query

    result = run_query("SELECT date, narration, account, position")
    assert len(result["rows"]) == 200
    assert result["truncated"] is True
    assert result["total_rows"] == 402  # 201 transactions × 2 postings each
    _reset_manager()


def test_bean_check_clean(with_ledger):
    from server import bean_check

    assert "clean" in bean_check().lower()


def test_bean_check_with_errors(tmp_path, monkeypatch):
    bean = tmp_path / "broken.bean"
    content = "2022-01-01 open Assets:Cash USD\n"
    content += "2022-01-02 balance Assets:Cash 999.00 USD\n"
    bean.write_text(content)
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from server import bean_check

    result = bean_check()
    assert "999" in result or "balance" in result.lower()
    _reset_manager()


def test_get_accounts_returns_all_accounts(tmp_path, monkeypatch):
    lines = ["2022-01-01 open Assets:Root USD"]
    for i in range(250):
        lines.append(f"2022-01-01 open Expenses:Cat{i:03d} USD")
    bean = tmp_path / "big.bean"
    bean.write_text("\n".join(lines))
    monkeypatch.setenv("BEANCOUNT_LEDGER", str(bean))
    _reset_manager()
    from server import get_accounts

    result = get_accounts()
    rows = result.strip().split("\n")
    assert len(rows) == 251
    assert "Assets:Root" in rows
    assert "Expenses:Cat000" in rows
    _reset_manager()


def test_get_tables_returns_expected_tables(with_ledger):
    from server import get_tables

    tables = get_tables().strip().split("\n")
    for expected in ["accounts", "balances", "entries", "postings", "transactions"]:
        assert expected in tables
