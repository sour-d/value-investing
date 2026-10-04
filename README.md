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
| `stocks screen [--explain SYM]` | Run/inspect the quantitative screen |
| `stocks watch [add\|rm\|list]` | Manage the watchlist and buy-lines |
| `stocks pnl [--benchmark]` | Realised/unrealised P&L, XIRR vs index |
| `stocks health` | Staleness, data-quality flags |
| `stocks integrity SYM` | Attest the Gate 5 manual checks |
| `stocks sync [--full]` | Force a data sync |

Every command accepts `--json`.

## How it works

- **Raw data is stored; metrics are derived on read.** `fundamentals_annual` holds
  vendor values keyed by fiscal period, so fixing a formula recomputes all history.
- **`transactions` is append-only**, enforced by database triggers. Positions,
  cost basis and P&L are SQL views over the ledger, so they cannot drift.
- **Gates live in `screen.toml`**, each with a `# WHY`. Runs store a config hash;
  the report refuses to compare runs made under different thresholds.
- **A screen passer is a research queue entry, not a buy.** Six manual checks
  (promoter pledge, auditor, regulatory, RPTs, promoter selling, audit opinion)
  must be attested before a purchase is allowed.

## Setup

```bash
uv sync
stocks init
```

## Layout

```
screen.toml            gate thresholds
src/stocks/            cli, db, metrics, screen, portfolio, providers
data/stocks.db         local database (gitignored)
journal/               daily snapshots (committed)
thesis/                per-stock theses (committed)
.opencode/skills/      agent operating instructions
```