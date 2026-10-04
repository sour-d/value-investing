# stocks

Local screening, journal and portfolio tracker for NIFTY SMALLCAP 250 equities.

One command per day:

```bash
stocks
```

It syncs whatever is stale, prints what changed, and writes a dated journal entry.
If nothing changed, that is the output.

## Commands

| Command | Purpose |
|---|---|
| `stocks` | Daily run: sync stale data, report changes, journal |
| `stocks buy SYM QTY@PRICE --reason "..."` | Record a purchase |
| `stocks sell SYM QTY@PRICE --reason "..."` | Record a sale |
| `stocks why SYM` | Thesis, trade reasons, monitoring triggers |
| `stocks screen [--explain SYM] [--roic]` | Run/inspect the quantitative screen |
| `stocks watch [add\|rm\|list]` | Manage the watchlist and buy-lines |
| `stocks pnl [--benchmark]` | Realised/unrealised P&L, XIRR vs index |
| `stocks health` | Staleness, data-quality flags |
| `stocks integrity SYM` | Attest the Gate 5 manual checks |
| `stocks init [--bootstrap]` | Create DB + optionally import baseline |
| `stocks bootstrap` | Import baseline data into existing DB |
| `stocks sync [--full]` | Force a data sync |

Every command accepts `--json`.

## How it works

- **Raw data is stored; metrics are derived on read.** `fundamentals_annual` holds
  vendor values keyed by fiscal period, so fixing a formula recomputes all history.
- **`transactions` is append-only**, enforced by database triggers. Positions,
  cost basis and P&L are SQL views over the ledger, so they cannot drift.
- **Gates live in `screen.toml`**, each with a `# WHY`. Runs store a config hash;
  the report refuses to compare runs made under different thresholds, or under
  a different profitability gate (`--roic`).
- **Gate 4 is an OR.** Any one cheap metric earns the research slot; the
  qualitative layer rejects what survives. A symbol with no valuation metric at
  all fails rather than inheriting a pass from missing data.
- **Gate 3 fails on missing data** while other gates skip it: ROE and EBITDA
  margin are the profitability floor, so absence is not evidence of profit.
- **A screen passer is a research queue entry, not a buy.** Six manual checks
  (promoter pledge, auditor, regulatory, RPTs, promoter selling, audit opinion)
  must be attested before a purchase is allowed.

## Setup

```bash
uv sync
stocks init --bootstrap       # one command: schema + baseline import
# or, if you already ran init:
stocks bootstrap
```

Both require the baseline artefacts in `/tmp/opencode`:
- `smcap250.csv`  — NIFTY SMALLCAP 250 symbols
- `info.json`     — vendor profile snapshots
- `fin.pkl`       — annual statements

The importer is idempotent; re-running it refreshes values in place and
recomputes data-quality issues, so a partial or failed import is safe to repeat.

## Layout

```
screen.toml            gate thresholds
src/stocks/            cli, db, config, metrics, screen, screencmd
scripts/               one-shot bootstrap importer
data/stocks.db         local database (gitignored)
journal/               daily snapshots (committed)
thesis/                per-stock theses (committed)
.opencode/skills/      agent operating instructions
```