# Stocks (AI-agent operated)

This is an AI-agent–driven screening and thesis tracker for NIFTY SMALLCAP 250 equities. The `uv run stocks ...` commands are deterministic primitives; the agent is the user that sequences them. Durable memory lives in `analysis/` (git-tracked) and `journal/`/`thesis/` (git-tracked); `data/` is a disposable cache.

## First time, from a clean clone

For an agent encountering this repo for the first time, start here.

```text
Load skill: stocks-repo
```

Prompt the agent with something like:

```text
Load stocks-repo. Summarise the loop (inspect → fetch via MCP → ingest → screen → export), state where data/inbox lives, what must never be stored (sell-side targets), and the clean-symbol rule (passed=0 AND blocking=1). Do not modify anything. Report back in <4 lines.
```

## Daily habit (run every day/after refresh)

Treat this as a checklist the agent must follow, not a script to edit. Have it load `stock-screen-method` and `stock-data-integrity` if staleness is in question.

```text
Load stocks-repo. Then:
1. uv run stocks inspect
2. Check for missing fundamentals: uv run stocks universe
3. Fetch any missing roster/profile/fundamentals/prices/research via MCP into data/inbox/*.json using the data-acquisition skill (respect the inbox schema: universe.json, profile.json, fundamentals.json, prices.json, research.json). Do not call yfinance_price_history for large ranges; use india-stock_get_historical.
4. uv run stocks ingest; uv run stocks screen; uv run stocks export
5. uv run stocks health and list any symbol_issue severity=block
6. Summarise passers count, new/removed vs last run, and anything that would block buys. Return only deltas.
```

## Roster (reference data — must be correct)

NSE index constituents have no exposed MCP feed. Validate the committed roster before screening.

```text
Load stocks-repo. Run: uv run stocks universe
If it reports errors (size_mismatch, duplicate_name/isin/symbol, placeholder_row, no_source), stop and fix universe/smcap250.json. If only warnings, reconcile membership: uv run stocks universe --apply (refused on errors). Explain any removals or entrants in one line.
```

If the index actually changed (rebalance), fix the JSON (set `effective` to the rebalance effective date, correct `source`, then `--apply`). Never delete database rows — demotion flips `in_index` and sets `removed_on`.

## Data acquisition (which MCP to call)

The agent must pick the right server. Use the `data-acquisition` skill for this.

```text
Load data-acquisition. For the symbols reported as uncovered by `stocks universe` / `stocks health`:
- India-smallcap universe/metrics/ratios: use india-stock (search_funds only for MFs). Get quote, get_fundamentals, get_historical as needed. Map to profile.json/prices.json.
- Full financial statements not present: use yfinance (get_financials, get_ticker_info) — write per-symbol fundamentals.json in the item-major shape: {item: {fy_end: value}}.
- Integrity/evidence (promoter pledges, auditor changes, related-party, regulatory, mgmt commentary, industry): use websearch — write compact research.json excerpts with citations.
- Never fabricate URLs. Write all payloads to data/inbox/ and do not transform numbers; ingest normalises them.
```

## Buffett-style screening & thesis

Use `buffett-analysis` for judgement. It reads the 8 references under `.opencode/skills/buffett-analysis/references/` and writes only to `qualitative_assessment`, `valuation_assumption`, `research_verdict` (via `research.json`).

```text
Load buffett-analysis. Take the current passers (analysis/passers.md) and, for each, answer the eight questions: Circle of Competence, Moat, Management, Financials, Returns, Valuation, Risks, Verdict. For each symbol you keep, write a one-paragraph thesis to thesis/SYMBOL.md, propose fv_low/fv_high/buy_line (INR), and emit a research.json fragment covering qualitative_assessment + valuation_assumption + research_verdict. Do not import sell-side mean price/recommendations. Explain why fv excludes them.
```

## Record holdings & decisions

`transactions` is append-only. Views (`positions`, `cost_basis`, `realised_pnl`, `cashflows`) are derived. Never UPDATE/DELETE `transactions`.

```text
Load stock-thesis. To record an initiated position: stocks buy SYMBOL --qty N --price P_INR --broker BROKER --notes "thesis anchor". To trim/exit: stocks sell SYMBOL --qty N --price P_INR --broker BROKER --notes "...". After any trade, run stocks pnl and stocks export. Show realised/unrealised against current holdings only.
```

## Quick sanity (after any change to gates/metrics/ingest/export)

```text
Run: uv run ruff check src scripts tests && uv run mypy src/stocks && uv run pytest -q
If you touched metrics.py, screen.py or portfolio.py, also inspect the relevant tests that enforce: no sell-side targets stored; NaN -> NULL; clean_symbols uses (passed=0 AND blocking=1); percentage formatting not double-scaled.
```

## How this repo teaches an agent to use it

| Skill | Purpose |
|---|---|
| `stocks-repo` | Loop, invariants, ledger, paths. **Load first.** |
| `data-acquisition` | MCP cascade + inbox schema + coverage gaps |
| `stock-screen-method` | Gates, ROIC/ROE nuance, why names move |
| `buffett-analysis` | 8-question filter + references; writes to existing research tables only |
| `stock-thesis` | Thesis, buy line, verdict, watchlist discipline |
| `stock-data-integrity` | Staleness, `symbol_issue` (block), filing-verification needs, Gate 5 |

## Repo facts (for the agent, not to repeat)

- **Seam:** MCP → `data/inbox/*.json` → `stocks ingest` → `data/stocks.db` → `stocks screen` → `stocks export` → `analysis/*.md`.
- **Roster:** `universe/smcap250.json` (committed). `stocks universe` validates and `--apply` reconciles without deleting history; `added_on` ≠ `membership_as_of`.
- **Clean passers:** `screencmd.clean_symbols()` = symbols with gate rows and **no** row having `passed == 0 AND blocking == 1`. Do not filter on `blocking == 1` alone.
- **Trust model:** `profile_snapshot.trusted = 0` by default; filing verification required for load-bearing items. `NaN` → `NULL`. Store raw, derive on read.
- **Provenance:** `fundamentals_annual` carries `source` + `first_seen_at`/`last_seen_at`; `source` becomes `mcp` after ingest (do not leave legacy `yahoo-pilot`).
- **Evidence:** `research.json` → `qualitative_assessment`, `valuation_assumption`, `research_verdict` (no extra `research` table). Sell-side targets are deliberately not stored (test-enforced).
- **No auto-fetch:** No network in ingest. `universe.json` in inbox must come from MCP or be the committed roster reconciled. Pilot artefacts (`/tmp/opencode/*`) exist only for the pinned test and are skipped if absent.
- **Ledger:** `transactions` append-only; views derived. Gate 5 (`integrity_check`) blocks buys until attested or explicitly overridden with an auditable reason.

## Slash commands (opencode)

Project-level slash commands live in `.opencode/commands/`. If you're running opencode inside this repository, typing `/` will show them. If they don't appear in an existing session, restart opencode in this workspace.

| Command | What it does |
|---|---|
| `/screen` | Runs the daily habit (inspect, universe, ingest if inbox has JSON, screen, export, health) and reports only deltas. |
| `/fetch` | Uses MCP (`india-stock`, `yfinance`, `websearch`) to populate `data/inbox/` for missing coverage. |
| `/health` | Quick roster + staleness + blockers summary. |
| `/reconcile` | Validates and reconciles roster membership (`stocks universe` / `--apply`). |
| `/thesis` | Buffett-style analysis on passers; writes thesis files + `research.json` fragments. |
| `/buy` | Records a buy (append-only ledger). |
| `/sell` | Records a sell (append-only ledger). |
| `/pnl` | Shows positions and P&L. |
