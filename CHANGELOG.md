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
- `holdings` tool — per-commodity units, cost basis, market value, and
  unrealised gain, converted to the operating currency.
- `list_prices` tool and `beanie://prices` resource — latest known price
  per commodity.

### Changed
- `bean_check` now reuses the cached ledger connection instead of
  re-parsing the ledger from disk on every call.
- `beanie://bql-guide` documents `convert()`/`cost()`/`value()` for
  multi-currency and lot-tracked ledgers.

## [0.1.0]

Initial release: `run_query`, `bean_check`, `list_accounts`, `list_tables`,
`find_unmatched_transfers` tools; `beanie://accounts`, `beanie://tables`,
`beanie://bql-guide` resources.
