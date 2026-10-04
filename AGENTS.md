# AGENTS.md

NIFTY SMALLCAP 250 screening, journal and portfolio tracker, driven by an AI agent.

## You are the caller, not the user

This repo has no CLI audience. It is invoked by an agent that already has the
`india-stock`, `yfinance` and `websearch` MCP servers. Python here supplies the
deterministic half — gates, metrics, ledger, durable record — and never fetches
on the ingest path. Ingestion is a pure function of `data/inbox/`.

Read `.opencode/skills/stocks-repo/SKILL.md` first, then the skill for the job.

## The loop

```bash
uv run stocks inspect    # read before refetching
# ... fetch via MCP, write payloads to data/inbox/*.json ...
uv run stocks ingest     # inbox JSON -> normalised rows
uv run stocks screen     # evaluate gates, persist the run
uv run stocks export     # write the committed record to analysis/
```

`analysis/` is committed and holds the memory of every run; `data/` is ignored
and disposable. A run nobody can recall is not worth doing.

## Read the database before you refetch

Before fetching anything, check whether the answer is already stored:

```bash
uv run stocks inspect                 # counts, latest price date, latest run
uv run stocks screen --explain SYMBOL # gates, metrics, threshold that failed
uv run stocks why SYMBOL              # thesis, trade reasons, monitoring triggers
uv run stocks health                  # staleness + data-quality flags
```

A full 251-symbol fundamentals fetch takes minutes. `screen --explain` answers
most questions in milliseconds. Read first, fetch only what is genuinely missing.

## Hard invariants — do not break these

- **Never write to `data/stocks.db` directly.** Go through `stocks.ingest` or the
  CLI. The database rejects `UPDATE`/`DELETE` on `transactions` via trigger; do
  not work around it.
- **`transactions` is append-only.** To correct a mistake, append a reversing entry.
- **`positions`, `cost_basis`, `realised_pnl`, `cashflows` are SQL views.** They are
  derived from the ledger on every read. Never create a table that duplicates them —
  that is exactly the drift they exist to prevent.
- **Store raw, derive on read.** `fundamentals_annual` holds vendor values keyed by
  `fy_end`. Metrics are computed from it at query time. Never persist a derived
  metric as if it were an input.
- **A vendor ratio is a claim, not a fact.** `profile_snapshot` rows carry
  `trusted = 0` by default. Anything load-bearing needs a filings cross-check.
- **Never store sell-side targets.** `targetMeanPrice` and `recommendationKey`
  would anchor the valuation judgement this repo exists to make independently.
  Enforced by `tests/test_sync.py::test_analyst_targets_are_not_stored`.
- **`NaN` means absent, not zero.** The vendor emits literal `NaN`; it becomes
  `NULL`. Storing 0 would drag every downstream average toward zero.

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

`uv run stocks` with no subcommand is the whole habit: sync stale data, screen,
print a delta-first report, write `journal/YYYY-MM-DD.md`. The report is capped
at `daily.MAX_LINES` and trims from the least decision-relevant line up, always
listing what it dropped — a silently shortened list reads as a complete one.

`gate_result.blocking` distinguishes a failed check from a failed *blocking*
check. Phase 5 demoted ROE to non-blocking, so `passed = 0` alone no longer
means "rejected". A symbol is clean when it has gate rows and **no** row where
`passed = 0 AND blocking = 1`. Selecting on `blocking = 1` alone returns almost
the whole universe, because it describes every check that passed. Use the
helper rather than re-deriving it:

```python
from stocks import screencmd
clean = screencmd.clean_symbols(conn, run_id)
```

Export (`analysis/passers.md`) and delta both depend on this being right.

## Conventions

- Python >= 3.13, managed with `uv`. `uv run stocks ...`
- Every command supports `--json` so an agent can consume it unambiguously.
- Percentages are **already multiplied by 100** by `metrics.compute`, and
  `judgment._mos` stores 0-100. Formatting must not scale a second time — that
  produced a passers table claiming 1943% ROE.
- Inbox payloads mirror MCP responses verbatim. Normalise at the ingest boundary,
  never by asking the agent to transform numbers.
- Tests: `uv run pytest -q`, plus `ruff` and `mypy`. Run them before committing a
  change to `metrics.py`, `screen.py` or `portfolio.py`.
- No new runtime dependencies without a reason worth the import cost.