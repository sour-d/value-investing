# AGENTS.md

Local stock screening, journal and portfolio tracker for NIFTY SMALLCAP 250 equities.

## Read the database before you refetch

Before fetching anything, check whether the answer is already stored:

```bash
stocks screen --explain SYMBOL      # gates, metrics, threshold that failed
stocks why SYMBOL                   # thesis, trade reasons, monitoring triggers
stocks health                      # staleness + data-quality flags
sqlite3 data/stocks.db "SELECT * FROM price_daily WHERE symbol='ACC' ORDER BY date DESC LIMIT 5"
```

A full 251-symbol fundamentals fetch takes minutes. `screen_daily` answers most
questions in milliseconds. Read first, fetch only what is genuinely missing.

## Hard invariants — do not break these

- **Never write to `data/stocks.db` directly.** Go through the CLI. The database
  rejects `UPDATE`/`DELETE` on `transactions` via trigger; do not work around it.
- **`transactions` is append-only.** To correct a mistake, append a reversing entry.
- **`positions`, `cost_basis`, `realised_pnl`, `cashflows` are SQL views.** They are
  derived from the ledger on every read. Never create a table that duplicates them —
  that is exactly the drift they exist to prevent.
- **Store raw, derive on read.** `fundamentals_annual` holds vendor values keyed by
  `fy_end`. Metrics are computed from it at query time. Never persist a derived
  metric as if it were an input.
- **A vendor ratio is a claim, not a fact.** `profile_snapshot` rows carry
  `trusted = 0` by default. Anything load-bearing needs a filings cross-check.

## Gating philosophy

- Gates live in `screen.toml` and each carries a `# WHY` comment. Read it before
  changing a threshold.
- **A passer is a research queue entry, never a buy.** Gates are necessary, not
  sufficient: three names cleared every gate in the pilot run and were still bad buys.
- **Never relax a gate to admit a stock you like.** If a threshold is genuinely
  wrong, change it in `screen.toml` with a commit that states the reason — do not
  special-case a symbol in code.
- When comparing two screen runs, check `config_hash` first. Different thresholds
  mean entrants/exits are meaningless and the report will refuse to print them.

## Data quality gates

`symbol_issue` rows with `severity='block'` prevent a symbol from being screened
(`no_financials`, `insufficient_history`, `non_contiguous_periods`).
Gate 0 requires `min_annual_periods` across **all three** statements — 121 of 249
symbols have mismatched statement depth, so checking income alone passes names with
no usable balance sheet.

## Management integrity

Gate 5 is attested by a human, not computed — no provider supplies promoter pledge,
auditor, RPT or regulatory data. `stocks buy` refuses while any check in
`integrity_check` is not `verified`. `--override-integrity "reason"` is allowed and
the reason is recorded in the transaction, so every override stays auditable.

Do not record a check as `verified` without evidence. `unknown` is a valid and
honest answer; a false `verified` is not.

## The daily loop

`stocks` with no subcommand is the whole daily habit: sync stale data, screen,
print a delta-first report, write `journal/YYYY-MM-DD.md`. The report is capped
at `daily.MAX_LINES` and trims from the least decision-relevant line up, always
listing what it dropped — a silently shortened list reads as a complete one.

`gate_result.blocking` distinguishes a failed check from a failed *blocking*
check. Phase 5 demoted ROE to non-blocking, so `passed = 0` alone no longer
means "rejected". Reconstructing the clean set without this made every soft-ROE
name an entrant on every run. Any new query over `gate_result` must filter on
`blocking = 1`.

## Conventions

- Python >= 3.13, managed with `uv`. `uv run stocks ...`
- Every CLI command supports `--json` so an agent can consume it unambiguously.
- Tests: `uv run pytest`. Run them before committing a change to `metrics.py`,
  `screen.py` or `portfolio.py`.
- No new runtime dependencies without a reason worth the import cost.