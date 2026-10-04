---
name: stocks-repo
description: Use in ANY session that touches this repo — it is an AI-driven stock analysis toolkit, not a human CLI. Covers the agent loop (MCP fetch → stocks ingest → stocks screen → stocks export), what lives in data/ versus analysis/, the read-before-refetch rule, which file to open for which job, and the hard invariants that must not be broken.
---

# The stocks repo

An analysis toolkit for NIFTY SMALLCAP 250 equities, driven by you — not a CLI
for a human at a terminal. You already have the data tools (`india-stock`,
`yfinance`, `websearch` MCP servers); this repo supplies the deterministic part:
gates, metrics, a portfolio ledger, and a durable record of what each run
concluded.

## The loop

```
MCP fetch  →  data/inbox/*.json  →  stocks ingest  →  SQLite  →  stocks screen  →  stocks export  →  analysis/*.md
```

1. **Check what you already have** — `stocks inspect`. Never refetch blindly.
2. **Fetch only what is missing** via MCP, write the payloads to `data/inbox/`.
3. **`uv run stocks ingest`** — turns inbox JSON into normalised rows.
4. **`uv run stocks screen`** — evaluates every gate, persists the run.
5. **`uv run stocks export`** — writes the git-tracked record.
6. **Reason about the result, then record the judgement** (see `stock-thesis`).

Each command takes `--json` for unambiguous consumption. Use it whenever you
intend to parse the output rather than read it.

## What goes where

| Path | Tracked? | Contents |
|---|---|---|
| `data/inbox/` | no | Raw MCP payloads, verbatim. Re-fetchable. |
| `data/stocks.db` | no | Working cache. Binary, gitignored. |
| `analysis/` | **yes** | The durable record: passers, runs, watchlist, qualitative, integrity. |
| `journal/` | **yes** | Dated narrative of what happened and why. |
| `thesis/` | **yes** | Per-symbol investment thesis. |
| `screen.toml` | **yes** | Gate thresholds. Read the `# WHY` before changing one. |
| `AGENTS.md` | **yes** | Hard invariants. Read it before writing code. |

A run is only useful if the next one can remember it. That is why `analysis/` is
committed and `data/` is not: the binary cache is disposable, the conclusions are
not.

## Which skill for which job

| Task | Skill |
|---|---|
| Getting data in, filling coverage gaps | `data-acquisition` |
| Understanding why a stock passed or failed | `stock-screen-method` |
| Judging business quality, moat, management, price | `buffett-analysis` |
| Writing down a decision | `stock-thesis` |
| Recording fills, correcting a mistake, P&L | `stocks-repo` (ledger section below) |
| Attesting data integrity, Gate 5 | `stock-data-integrity` |

## Hard invariants

Read `AGENTS.md` in full before changing code. The short version:

- **Never write to `data/stocks.db` directly.** Go through the CLI or `stocks.ingest`.
- **`transactions` is append-only.** Correct a mistake by appending a reversing
  entry, never by deleting one.
- **`positions`, `cost_basis`, `realised_pnl` are SQL views.** Never create a
  table that duplicates them; that drift is exactly what they prevent.
- **Store raw, derive on read.** `fundamentals_annual` holds vendor values;
  metrics are computed at query time. Never persist a derived metric as an input.
- **A vendor ratio is a claim.** `profile_snapshot.trusted` defaults to `0`.
- **A passer is a research candidate, never a buy.** Gates are necessary, not
  sufficient — the pilot run produced three names that cleared every gate and
  were still bad buys.

## Any query over `gate_result` must filter `blocking = 1`

A check can fail *without* rejecting the symbol. `passed = 0` alone does not mean
rejected; ROE was demoted to a non-blocking check, and reading it as a rejection
made every soft-ROE name an entrant on every run.

A symbol is **clean** when it has gate rows and no row where
`passed = 0 AND blocking = 1`. Use the existing helper rather than re-deriving it:

```python
from stocks import screencmd
clean = screencmd.clean_symbols(conn, run_id)
```

## The ledger

```bash
uv run stocks buy  SYMBOL --qty N --price P   # append a fill
uv run stocks sell SYMBOL --qty N --price P   # append a disposal
uv run stocks pnl                             # realised + unrealised
```

A wrong entry is fixed by appending its inverse, not by editing or deleting:

```bash
uv run stocks sell SYMBOL --qty N --price P --reason "reversal of mistaken buy"
```

`stocks buy` refuses while any `integrity_check` is not `verified`.
`--override-integrity "reason"` is allowed and the reason is stored on the
transaction, so every override stays auditable.

## Before you commit

```bash
uv run pytest -q
uv run ruff check src scripts tests
uv run mypy src/stocks
```