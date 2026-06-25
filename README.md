# beanie-mcp

An MCP server that lets AI agents inspect and query [Beancount](https://beancount.github.io/) v3 ledgers with BQL. Point it at your `.bean` file, connect it to any MCP-capable client, and ask questions about your finances while the server handles ledger loading, validation, and structured query results.

---

## What it does

Once connected, your agent can query your ledger directly without you touching a terminal. Under the hood, the `run_query` tool accepts [BQL](https://beancount.github.io/docs/beancount_query_language.html), not arbitrary natural language, so the agent translates your question into a valid query:

> *"What did I spend on restaurants last month?"*
> *"What's my current net worth across all accounts?"*
> *"Are there any errors or failed balance assertions in my ledger?"*
> *"Show me all transactions in my brokerage account this tax year."*

beanie-mcp exposes four tools and three resources:

| Name | Type | Description |
|---|---|---|
| `run_query` | Tool | Run a BQL query. Returns structured JSON with columns, stringified rows, returned row count, and truncation metadata. Invalid BQL or broken ledgers return an `error` field instead. |
| `bean_check` | Tool | Run bean-check on the ledger. Returns structured `{ ok, message, errors }` JSON. |
| `list_accounts` | Tool | Return all declared accounts as structured JSON, without the query row cap. |
| `list_tables` | Tool | Return beanquery table names plus the key `FROM` caveat. |
| `beanie://accounts` | Resource | All accounts in the ledger, one per line. |
| `beanie://tables` | Resource | BQL-accessible table names. |
| `beanie://bql-guide` | Resource | Short BQL guide for agents, including caveats and examples. |

## BQL notes

BQL is SQL-like, but it is not SQL. The most important caveat: `FROM` is a date/filter clause, not a table selector. A query like this does **not** list accounts from the `accounts` table:

```sql
SELECT account FROM accounts ORDER BY account
```

For account discovery, use the `list_accounts` tool or `beanie://accounts` resource instead. For table discovery, use `list_tables` or `beanie://tables`.

Useful query examples:

```sql
SELECT account, sum(position)
WHERE account ~ "Expenses"
GROUP BY account
ORDER BY account
```

```sql
SELECT date, payee, narration, account, position
WHERE account ~ "Expenses:Food"
LIMIT 50
```

```sql
SELECT account, sum(position)
WHERE account ~ "Assets|Liabilities"
GROUP BY account
```

## Result contract

`run_query` returns one of three shapes.

Successful query:

```json
{
  "columns": ["account", "sum_position"],
  "rows": [["Expenses:Food", "123.45 USD"]],
  "truncated": false,
  "returned_rows": 1,
  "total_rows": 1,
  "total_rows_known": true
}
```

Broken ledger:

```json
{
  "error": "Ledger has bean-check errors; fix them before querying.",
  "error_type": "ledger",
  "errors": [
    {
      "file": "/path/to/main.bean",
      "line": 42,
      "type": "BalanceError",
      "message": "Balance failed for ..."
    }
  ]
}
```

Invalid BQL:

```json
{
  "error": "syntax error or beanquery error message",
  "error_type": "bql"
}
```

Rows are capped at 200. To keep broad queries from materialising an entire ledger, beanie-mcp fetches at most 201 rows. When `truncated` is `true`, `total_rows` is `null` and `total_rows_known` is `false`; add a narrower `WHERE`, `ORDER BY`, or `LIMIT` clause if you need a smaller answer. Row values are returned as strings so MCP clients get stable JSON even when beanquery returns Python dates, decimals, inventories, or other typed Beancount values.

`bean_check` returns:

```json
{
  "ok": true,
  "message": "Ledger is clean - no errors or warnings.",
  "errors": []
}
```

or:

```json
{
  "ok": false,
  "message": "Ledger has 1 error(s).",
  "errors": [
    {
      "file": "/path/to/main.bean",
      "line": 42,
      "type": "BalanceError",
      "message": "Balance failed for ..."
    }
  ]
}
```

## Design goals

beanie-mcp is designed for real-world ledgers, not just small demos:

- **Structured JSON output** - switched from `BQLShell` text tables to the beanquery DB-API (`beanquery.connect()`). Agents get columns and stringified rows they can actually work with, not a text table to parse.
- **Fail-loud ledger errors** - `run_query` refuses to query ledgers with loader errors or failed balance assertions instead of returning plausible empty results.
- **Structured `bean_check` tool** - surfaces loader errors and failed balance assertions as machine-readable JSON with file, line, type, and message.
- **Resource-safe row limit** - query responses cap at 200 rows and only fetch one extra row to detect truncation.
- **Account tools bypass the cap** - account enumeration fetches the full declared account list regardless of ledger size.
- **Locked ledger path** - the ledger path is fixed by the `BEANCOUNT_LEDGER` env var at startup, so the agent cannot point the server at arbitrary files on your filesystem.
- **include-aware cache** - the ledger is only re-parsed when the root file or any loaded `include` file changes, not on every query.
- **Watchdog auto-reload** - file system watcher invalidates the cache when `.bean` files are modified, created, moved, or deleted.
- **Explicit `pydantic-settings` dependency** - configuration loading does not rely on accidental transitive dependencies.
- **Python `<3.14` ceiling** - beancount 3.x has no prebuilt wheel for Python 3.14; building from source fails on macOS (Apple ships bison 2.3, beancount needs >=3.8). The ceiling prevents a confusing build failure.

## Requirements

- Python 3.10-3.13
- [uv](https://docs.astral.sh/uv/)
- Beancount v3 ledger (`.bean` file)

> **Beancount v3 is the supported target.** Beancount v2 ledgers may not work; if you are on v2, use a v2-compatible MCP server or query tool.

## Install

Clone the repo and install the Python dependencies with `uv`:

```bash
git clone https://github.com/klinikal/beanie-mcp.git
cd beanie-mcp
uv sync
```

Find the absolute path to your main Beancount file. For example:

```bash
realpath ~/finance/main.bean
```

Use that full path as `BEANCOUNT_LEDGER` in the MCP config below. Relative paths are deliberately avoided because MCP clients may start the server from a different working directory.

You can smoke-test the server before adding it to an MCP client:

```bash
BEANCOUNT_LEDGER=/absolute/path/to/your/ledger/main.bean uv run beanie-mcp
```

The command starts an MCP stdio server and waits for a client. Press `Ctrl+C` to stop it.

## Configure an MCP client

beanie-mcp is not tied to a particular model or agent. It is a standard local MCP stdio server. Any client that can launch a local MCP command with environment variables should be able to use it. That includes Claude Code/Desktop-style configs, Codex-style agent runners, Cursor-style IDE agents, Gemini-based agents, and other MCP-compatible tools. The exact config UI or file format depends on the client.

The generic command is:

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

Some clients wrap that command in an `mcpServers` object. Others have a GUI where you enter the same command, args, and env vars separately.

### Example: Claude Code / Claude Desktop

For clients that use an `mcpServers` JSON block, add:

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

### Example: Codex

Add a server entry to your Codex config:

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

### Other MCP clients

Use the same command, args, and env vars wherever your client defines local MCP servers:

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

Restart the client after changing MCP config. MCP clients usually read the tool list only when they start.

## Verify

Once connected, ask your agent to run:

- `bean_check`
- `list_accounts`
- `list_tables`

Then try a small BQL query:

```sql
SELECT account, sum(position)
WHERE account ~ "Expenses"
GROUP BY account
LIMIT 20
```

If `bean_check` reports errors, fix the ledger first. `run_query` refuses to query a broken ledger so the agent does not mistake an invalid ledger for an empty result.

## Update

To update an existing local checkout:

```bash
cd /absolute/path/to/beanie-mcp
git pull
uv sync
```

Restart your MCP client after updating.

## Troubleshooting

**The client cannot find `uv`**  
Use the absolute path to `uv` in your config. Find it with:

```bash
which uv
```

Then replace `"command": "uv"` with something like `"command": "/opt/homebrew/bin/uv"`.

**`No ledger configured`**  
Set `BEANCOUNT_LEDGER` in the MCP config `env` block. It must point to your main `.bean` file.

**Ledger file not found**  
Use absolute paths for both `/absolute/path/to/beanie-mcp` and `BEANCOUNT_LEDGER`. `~` may not expand inside every MCP client.

**Tool list did not change after updating**  
Restart the client. Long-running MCP clients often keep the old tool list until they reconnect.

## Development

Run the MCP inspector:

```bash
BEANCOUNT_LEDGER=/absolute/path/to/your/ledger/main.bean uv run mcp dev src/beanie_mcp/server.py
```

## Running tests

```bash
uv run pytest server_test.py -v
uv run ruff check .
uv build
```

## Privacy

This tool sends parts of your Beancount ledger to whatever model/provider your MCP client uses. Only connect it to a provider and client you trust with your financial data. The relevant data handling policy is the one for the model/provider/client you choose.

> You are responsible for your financial data. Don't connect this to a service you wouldn't trust with your bank statements. Run this at your own risk.

## License

MIT. See [LICENSE](LICENSE).
