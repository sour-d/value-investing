# stocks

A screening, journaling and portfolio toolkit for NIFTY SMALLCAP 250 equities.

**This repo is driven by an AI agent, not by a human at a terminal.** You bring
the data — via the `india-stock`, `yfinance` and `websearch` MCP servers. This
repo supplies the part that must be deterministic: gates, metrics, a portfolio
ledger, and a durable record of what every run concluded.

```
MCP fetch  →  data/inbox/*.json  →  stocks ingest  →  SQLite  →  stocks screen  →  stocks export  →  analysis/*.md
```

The Python never touches the network on the ingest path. Ingestion is a pure,
replayable function of what is on disk, so any run can be reproduced from the
inbox alone.

## Setup

```bash
uv sync
uv run stocks init      # create the schema; safe to re-run
uv run stocks inspect   # what is stored, and how fresh
```

That is the whole setup. There is no bundled dataset and nothing to download —
a fresh clone is a working repo with an empty database, which is the correct
starting state.

## The loop

### 1. Read before you refetch

```bash
uv run stocks inspect                # counts, latest price date, latest run
uv run stocks health                 # staleness and data-quality flags
uv run stocks screen --explain ACC   # every gate, metric and threshold
```

A 251-symbol fundamentals fetch takes minutes. Most questions are already
answered in milliseconds. Fetch only the gap.

### 2. Fetch via MCP, write to `data/inbox/`

The inbox shapes **match the MCP responses verbatim** — no number conversion,
only key choice. Symbols are keyed by bare NSE ticker (`ACC`, not `ACC.NS`).

| File | Source tool | Shape |
|---|---|---|
| `universe.json` | your index list | `[{"symbol": "ACC", "name": ..., "industry": ...}]` |
| `profile.json` | `india-stock_get_fundamentals` | `{"ACC": {"financialData": {...}, "keyStats": {...}, "summaryDetail": {...}}}` |
| `fundamentals.json` | `yfinance_get_financials` | `{"ACC": {"income_statement": {"Total Revenue": {"2026-03-31": 2.5e11}}}}` |
| `prices.json` | `india-stock_get_historical` | `{"ACC": {"data": [{"date": ..., "close": ...}]}}` |
| `research.json` | `websearch` findings | see below |

Every file is optional and independently replaceable, so a refresh writes only
what was actually fetched.

The three-way cascade — stop at the first server that answers:

1. **`india-stock`** — Indian market. Best ratios. Bare symbols.
2. **`yfinance`** — full statements. Suffix symbols (`ACC.NS`).
3. **`websearch`** — promoter pledge, auditor changes, related-party
   transactions, regulatory action, management commentary, industry structure.
   Neither market-data server has any of this.

> `yfinance_get_price_history` returns a **markdown table**, not JSON. For
> machine-readable bars use `india-stock_get_historical`.

### 3. Ingest and screen

```bash
uv run stocks ingest        # reports per-file counts
uv run stocks screen        # evaluate every gate, persist the run
uv run stocks export        # write the git-tracked record
```

Re-ingesting is idempotent — everything upserts on natural keys, so a corrected
symbol is replaced rather than duplicated.

### 4. Reason, then record

A passer is a research candidate, **not a buy**. Gates are necessary, not
sufficient: the pilot run cleared three names through every gate that were still
bad buys.

Write the judgement down so the next session starts from your conclusion:

```bash
uv run stocks watch ACC --buy-line 1800 --fv-low 2200 --fv-high 2600 \
    --reason "why this level" --thesis thesis/ACC.md
uv run stocks export
```

Qualitative findings and valuation assumptions go through `research.json` →
`stocks ingest`, landing in `qualitative_assessment`, `valuation_assumption` and
`research_verdict`.

## What is stored where

| Path | Tracked | Contents |
|---|---|---|
| `data/inbox/` | no | Raw MCP payloads. Re-fetchable. |
| `data/stocks.db` | no | Working cache. Binary, gitignored. |
| `analysis/` | **yes** | `passers.md`, `runs.md`, `watchlist.md`, `qualitative.md`, `integrity.md`, `latest.json` |
| `journal/` | **yes** | Dated narrative of what happened and why. |
| `thesis/` | **yes** | Per-symbol investment thesis. |
| `screen.toml` | **yes** | Gate thresholds, each with a `# WHY`. |
| `.opencode/skills/` | **yes** | One skill per job, so the agent knows what to do. |

`data/` is disposable; `analysis/` is the memory. A run is only useful if the
next one can remember it, which is why the conclusions are committed and the
binary cache is not.

## Skills

The repo teaches the agent how to use it. Load the relevant one:

| Skill | Use it for |
|---|---|
| `stocks-repo` | Orientation: the loop, the invariants, the ledger. **Read first.** |
| `data-acquisition` | Which MCP server to use, the inbox contract, coverage gaps |
| `stock-screen-method` | Gates, thresholds, ROIC vs ROE, why a name entered or left |
| `buffett-analysis` | Moat, pricing power, management integrity, valuation, when to sell |
| `stock-thesis` | Writing a thesis, setting a buy line, recording a verdict |
| `stock-data-integrity` | Stale or missing data, vendor ratios, what needs filing verification |

## Design commitments

- **A vendor ratio is a claim, not a fact.** Every ingested `profile_snapshot`
  row lands with `trusted = 0`. Anything load-bearing needs a filings
  cross-check.
- **Sell-side targets are not stored.** `targetMeanPrice` is deliberately
  ignored: it would anchor the valuation judgement this repo is supposed to make
  independently. Enforced by a test.
- **NaN means absent, not zero.** The vendor emits literal `NaN`; it becomes
  `NULL`, and a period where every item is missing is dropped rather than
  averaged in as zeros.
- **Store raw, derive on read.** `fundamentals_annual` holds vendor values;
  metrics are computed at query time. No derived metric is ever persisted as if
  it were an input.
- **The transaction ledger is append-only.** A mistake is corrected by appending
  its inverse, never by editing or deleting.
- **Live prices are not exported per symbol.** They change every session and
  would bury the ratio movements that justify a re-run.
- **A passer never becomes a verdict.** Anything load-bearing waits on Gate 5, a
  human-attested integrity check that no provider can supply.

## Commands

Every command takes `--json` for unambiguous machine consumption.

| Command | Purpose |
|---|---|
| `init` | Create the database and apply the schema |
| `inspect` | What is stored, and how fresh |
| `ingest` | Load `data/inbox/*.json` |
| `screen` | Run or inspect the quantitative screen |
| `export` | Write the git-tracked analysis record |
| `health` | Staleness and data-quality report |
| `sync` | Refresh stale data from the configured provider |
| `buy` / `sell` / `pnl` | Append-only ledger and profit and loss |
| `watch` | Manage the watchlist and buy lines |
| `why` | Measured facts, thesis, and what is unverified |
| `integrity` | Attest the manual Gate 5 checks |
| `journal` | Write today's journal entry |

`stocks` with no subcommand runs the whole daily habit: sync, screen, report
the delta, write `journal/YYYY-MM-DD.md`.

## Development

```bash
uv run pytest -q
uv run ruff check src scripts tests
uv run mypy src/stocks
```

`scripts/import_baseline.py` imports offline vendor artefacts
(`smcap250.csv`, `info.json`, `fin.pkl`). It exists only to reproduce the pinned
pilot result in `tests/test_pilot_reproduction.py`; those tests **skip** when the
artefacts are absent. **New data must come through MCP.**

See `AGENTS.md` for the invariants that must not be broken, and
`.opencode/skills/` for how the agent is expected to work.