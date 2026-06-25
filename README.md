# beanie-mcp

An MCP server for querying [Beancount](https://beancount.github.io/) ledgers in plain English. Point it at your `.bean` file, connect it to Claude, and ask your finances anything.

---

## What it does

Once connected, Claude can query your ledger directly without you touching a terminal:

> *"What did I spend on restaurants last month?"*
> *"What's my current net worth across all accounts?"*
> *"Are there any errors or failed balance assertions in my ledger?"*
> *"Show me all transactions in my brokerage account this tax year."*

beanie-mcp exposes two tools and two resources to Claude:

| Name | Type | Description |
|---|---|---|
| `run_query` | Tool | Run a BQL query. Returns structured JSON — columns, rows, and a truncation flag if results exceed 200 rows. |
| `bean_check` | Tool | Run bean-check on the ledger. Returns a clean confirmation or a list of errors with file location and line number. |
| `beanquery://accounts` | Resource | All accounts in the ledger — not subject to the row cap. |
| `beanquery://tables` | Resource | BQL-accessible tables (accounts, balances, entries, postings, etc.). |

## What's different

The upstream `beanquery-mcp` is a solid proof-of-concept. beanie-mcp hardens it for real-world use:

- **Structured JSON output** — switched from `BQLShell` text tables to the beanquery DB-API (`beanquery.connect()`). Claude gets columns and typed rows it can actually work with, not a text table to parse.
- **`bean_check` tool** — surfaces loader errors and failed balance assertions with file and line number. The upstream server would silently return empty results if your ledger had errors.
- **Row limit with truncation signal** — results cap at 200 rows; the response tells Claude when data was cut so it can ask you to add a `LIMIT` clause.
- **`get_accounts` bypasses the cap** — account enumeration fetches the full list regardless of ledger size.
- **Removed `set_ledger_file`** — the upstream tool let the LLM point the server at any file on your filesystem at runtime. Removed. Ledger path is locked to the `BEANCOUNT_LEDGER` env var set at startup.
- **mtime cache** — the ledger is only re-parsed when the file actually changes, not on every query.
- **Watchdog auto-reload** — file system watcher invalidates the cache when any `.bean` file in your ledger directory changes, including files pulled in via `include` directives.
- **Explicit `pydantic-settings` dependency** — the upstream omitted this from `pyproject.toml`; it worked only as a transitive dep that could silently break.
- **Python `<3.14` ceiling** — beancount 3.x has no prebuilt wheel for Python 3.14; building from source fails on macOS (Apple ships bison 2.3, beancount needs ≥3.8). The ceiling prevents a confusing build failure.

## Requirements

- Python 3.10–3.13
- [uv](https://docs.astral.sh/uv/)
- Beancount v3 ledger (`.bean` file)

> **Beancount v3 only.** If you're on v2, use the upstream [vanto/beanquery-mcp](https://github.com/vanto/beanquery-mcp).

## Setup

### Claude Code (recommended)

Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "beanie": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/beanie-mcp", "mcp", "run", "server.py"],
      "env": {
        "BEANCOUNT_LEDGER": "/absolute/path/to/your/ledger/main.bean"
      }
    }
  }
}
```

### Claude Desktop

```bash
uv run mcp install server.py \
  --name "beanie-mcp" \
  -v BEANCOUNT_LEDGER=/absolute/path/to/your/ledger/main.bean
```

### Development / inspection

```bash
BEANCOUNT_LEDGER=/path/to/ledger.bean uv run mcp dev server.py
```

## Running tests

```bash
uv run pytest server_test.py -v
```

## Privacy

This tool sends parts of your Beancount ledger to whatever LLM you connect it to. Only connect it to a provider you trust with your financial data. If you use Claude via Anthropic's API or Claude.ai, Anthropic's standard data handling policies apply.

> You are responsible for your financial data. Don't connect this to a service you wouldn't trust with your bank statements. Run this at your own risk.

## Disclaimer
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. USE IS AT YOUR OWN RISK. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE. By downloading or using this software you indemnify and release the creators from and against any liability whatsoever resulting from or in connection with this software.

## License

MIT. See [LICENSE](LICENSE).

