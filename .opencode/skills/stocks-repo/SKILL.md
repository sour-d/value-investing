---
name: stocks-repo
description: Use when working in /home/sourav/Projects/stocks, the local NIFTY SMALLCAP 250 screening and journal tracker — running `stocks` commands, editing its gates, metrics, schema, portfolio ledger or daily report, or answering questions about stored stock data. Covers the read-before-refetch rule, the append-only ledger invariants, and which test to run.
---

# The stocks repo

A local, offline tracker for NIFTY SMALLCAP 250 equities: screen, journal, and
keep a position ledger. Everything is derived from `data/stocks.db`, which is
gitignored and can be rebuilt. Nothing here talks to a broker.

Run everything with `uv run` from the repo root. Every command takes `--json`,
so prefer it when you need to read values rather than show them.

## Read before you refetch

A full 251-symbol fundamentals fetch takes minutes. Most questions are answered
from stored data in milliseconds:

```bash
uv run stocks screen --explain SYMBOL   # every gate, metric and threshold
uv run stocks why SYMBOL                # thesis, integrity, trades, what is unassessed
uv run stocks health                    # staleness and data-quality flags
uv run stocks health --explain CODE     # which symbols carry one issue
```

Only fetch what is genuinely missing. `uv run stocks sync` fetches stale data
only; `--full` ignores the staleness budgets and is rarely what you want.

## The commands

| Command | What it does |
| --- | --- |
| `stocks` (no args) | the daily loop: sync, screen, print a delta report, write the journal |
| `stocks journal` | write `journal/YYYY-MM-DD.md`; `--print` shows it without writing, `--no-sync` skips the fetch |
| `stocks health` | three-valued `ok`/`warn`/`bad` data report; nonzero exit only on `bad` |
| `stocks screen` | run the gates; `--explain SYMBOL`, `--full`, `--roic`/`--no-roic` |
| `stocks watch` | `list`/`add`/`rm`/`status`, with `--buy-line`, `--fv-low`, `--fv-high`, `--reason` |
| `stocks integrity` | attest Gate 5 checks by hand: `--set`, `--status`, `--evidence`, plus `--verdict` and `--dimension` |
| `stocks why SYMBOL` | measured facts, human claims, unassessed inputs, integrity, trades |
| `stocks buy` / `stocks sell` | append to the ledger; `--reason` is required |
| `stocks pnl` | realised and unrealised P&L, XIRR; `--benchmark` refuses without a confirmed index wire |

## Invariants that are load-bearing

Breaking any of these breaks the thing the repo exists to do, so read the
reason before changing the code.

- **Never write to `data/stocks.db` directly.** Go through the CLI.
- **`transactions` is append-only**, enforced by trigger. To correct a mistake,
  append a reversing entry. Never `UPDATE` or `DELETE`, and never disable the
  trigger.
- **`positions`, `cost_basis`, `realised_pnl`, `cashflows`, `v_*` are views.**
  They are recomputed from the ledger on every read. Creating a table that
  duplicates one is the exact drift they exist to prevent.
- **Store raw, derive on read.** `fundamentals_annual` holds vendor values keyed
  by `fy_end`; metrics are computed at query time. Never persist a derived
  metric as though it were an input.
- **A vendor ratio is a claim.** `profile_snapshot` rows carry `trusted = 0`
  until a filings cross-check sets it. Do not mark a vendor number verified
  because it looks reasonable.
- **`gate_result.blocking` distinguishes a failed check from a failed blocking
  check.** ROE is non-blocking. Any new query over `gate_result` must filter on
  `blocking = 1`, or soft-ROE names become permanent "entrants" in every delta.
- **A passer is a research queue entry, never a buy.** Gates are necessary, not
  sufficient.

## Where things live

| Path | Role |
| --- | --- |
| `src/stocks/screen.toml` | every threshold, each with a `# WHY` comment |
| `src/stocks/screen.py` | gate evaluation, blocking vs non-blocking checks |
| `src/stocks/metrics.py` | metrics derived from raw fundamentals — change with care |
| `src/stocks/sync.py`, `providers/` | staleness budgets and vendor adapters |
| `src/stocks/portfolio.py` | cost basis, P&L, XIRR, Gate 5 enforcement |
| `src/stocks/judgment.py` | watchlist, integrity attestations, verdicts, `why` |
| `src/stocks/daily.py` | the daily report, its line budget and the journal |
| `src/stocks/schema.sql`, `db.py` | schema; additive columns go in `_ADDITIVE_COLUMNS` |
| `journal/*.md` | tracked, because the reasoning is the irreplaceable part |

## Before you commit

```bash
uv run pytest        # 162 tests
```

`metrics.py`, `screen.py` and `portfolio.py` changes always need the full run.
Changing a threshold means editing `screen.toml` and committing a message that
states why — never special-casing a symbol in code. Compare `config_hash`
between runs before reading anything into an entrant or exit.