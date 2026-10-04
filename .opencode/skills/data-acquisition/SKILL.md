---
name: data-acquisition
description: Use when fetching stock data into this repo, filling coverage gaps, refreshing prices or fundamentals, or deciding where to get a number. Covers the india-stock → yfinance → websearch MCP cascade, exactly which tool answers which question, the data/inbox JSON contract each payload must match, units and NaN handling, and the read-before-refetch rule that avoids burning minutes on data already stored.
---

# Getting data in

You have the MCP servers; this repo does not fetch anything itself. Your job is
to fetch through MCP and hand the payloads over as JSON. Ingestion is then a
pure, replayable function of what is on disk.

**Never write to `data/stocks.db` directly.** Write to `data/inbox/` and run
`uv run stocks ingest`.

## Read before you refetch

A 251-symbol fundamentals fetch takes minutes. Check first:

```bash
uv run stocks inspect                       # what is stored, and how fresh
uv run stocks health                        # staleness + data-quality flags
uv run stocks screen --explain SYMBOL       # gates, metrics, which check failed
```

Most questions are already answered. Fetch only the gap.

## The MCP cascade

Go down this list and stop at the first server that has the answer. Each step is
cheaper and more authoritative than the one below it.

### 1. `india-stock` — Indian market, best ratios

The default for anything on an NSE symbol. Symbols are **bare** (`ACC`, not `ACC.NS`).

| Question | Tool |
|---|---|
| Ratios, growth, margins, D/E, EPS | `india-stock_get_fundamentals` |
| Price, volume, 52-week range, mcap | `india-stock_get_quote` |
| Historical OHLCV | `india-stock_get_historical` |
| Index level and constituents | `india-stock_get_index` |
| Movers | `india-stock_get_gainers_losers` |
| Corporate actions | `india-stock_get_corporate_actions` |
| Holdings' aggregate metrics | `india-stock_portfolio_summary` |

### 2. `yfinance` — full statements

Use when you need the underlying statements rather than vendor ratios.

| Question | Tool |
|---|---|
| Income / balance sheet / cash flow | `yfinance_yfinance_get_financials` |
| Company profile and sector | `yfinance_yfinance_get_ticker_info` |
| Analyst estimates | `yfinance_yfinance_get_analyst_estimates` |
| Holders, insider trades | `yfinance_yfinance_get_holders` |

Symbols carry the suffix: `ACC.NS`.

> `yfinance_get_price_history` returns a **markdown table**, not JSON. For
> machine-readable bars use `india-stock_get_historical`, which returns clean
> JSON. Use the yfinance one only for eyeballing a chart.

### 3. `websearch` — what no vendor supplies

Neither market-data server has these. Use websearch, then record the finding as
a *judgement with a source*, never as a number that looks like vendor data.

- Promoter holding and pledge, promoter remuneration
- Auditor identity and any qualification or resignation
- Related-party transactions, contingent liabilities
- Regulatory actions, SEBI/CCI/NCLT proceedings
- Management commentary, shareholder letters, earnings-call transcripts
- Promoter or management turnover — a moat eroding quietly shows up here first
- Industry structure, competitor share, pricing trends

## The inbox contract

Write these files into `data/inbox/`. Every one is optional and independently
replaceable, so refresh only what you just fetched. **Shapes below match the MCP
responses verbatim** — do not transform numbers, only choose keys.

### `fundamentals.json`

Exactly `yfinance_get_financials` output, keyed by bare NSE symbol. Note the
vendor nests as `{item: {fy_end: value}}`:

```json
{"ACC": {"income_statement": {"Total Revenue": {"2026-03-31": 250453900000.0}},
         "balance_sheet":  {"Total Debt": {"2026-03-31": 5397700000.0}},
         "cash_flow":      {"Free Cash Flow": {"2026-03-31": -27911700000.0}}}}
```

Statement keys are recognised in both spellings: `income_statement`/`income`,
`balance_sheet`/`balance`, `cash_flow`/`cashflow`.

### `profile.json`

Exactly `india-stock_get_fundamentals` output. Fields nest across `financialData`,
`keyStats` and `summaryDetail`; they are flattened automatically.

```json
{"ACC": {"as_of": "2026-10-04",
         "financialData":  {"currentPrice": 1182.8, "debtToEquity": 2.085},
         "keyStats":        {"trailingEps": 101.37, "priceToBook": 1.08},
         "summaryDetail":   {"marketCap": 222114791424, "trailingPE": 11.67}}}
```

Add `as_of` or the fetch date is used. Every row lands with `trusted = 0`.

### `prices.json`

```json
{"ACC": {"data": [{"date": "2026-10-01", "open": 1201.1, "high": 1203.9,
                   "low": 1166.1, "close": 1182.8, "volume": 146983}]}}
```

A bare `[{...}]` list per symbol also works.

### `universe.json`

The roster. Anything absent is never screened, so this doubles as an
index-membership record.

```json
[{"symbol": "ACC", "name": "Accelyon", "industry": "Industrial", "sector": "Industrials"}]
```

### `research.json`

Websearch findings, routed into the existing Buffett tables. See `buffett-analysis`.

```json
{"ACC": {"assessments": {"moat_type": {"assessment": "...", "rationale": "..."}},
         "valuation":   {"method": "dcf", "rationale": "...",
                         "params": {"growth_5y": "0.12", "discount_rate": "0.11"}},
         "verdict": "watch", "reason": "...", "evidence": "Annual report FY26"}}
```

Valid dimensions: `moat_type`, `moat_trend`, `pricing_power`, `business_model`,
`mgmt_integrity`, `capital_allocation`.

## Then

```bash
uv run stocks ingest     # reports per-file counts
uv run stocks inspect    # confirm the counts moved
```

Re-ingesting is idempotent — upserts on natural keys, so a corrected symbol is
replaced rather than duplicated.

## Traps

- **`NaN` means absent, not zero.** The vendor emits literal `NaN`. Ingestion
  stores `NULL` and skips a period where every item is missing, so averages are
  never dragged toward zero. Do not substitute `0`.
- **Units are rupees as reported.** No thousands or millions conversion. A
  mismatch between vendor ratios and statements is itself a finding worth
  recording — `india-stock` and `yfinance` disagree on ZENSARTECH cash, for
  instance.
- **Symbols differ between servers.** `india-stock` wants `ACC`;
  `yfinance` wants `ACC.NS`. The inbox is keyed by the **bare** symbol.
- **Missing fundamentals must be ingested as missing.** Do not omit a symbol from
  `universe.json` to hide a gap — that makes the name silently unscreenable.
  Record the failure so `symbol_issue` and Gate 0 see it.
- **Partial refresh is fine.** Write only the symbols you fetched; ingestion
  upserts and leaves everything else untouched.

## When a value is missing

Do not guess and do not interpolate. Leave it absent, then decide:

- **Structurally missing** (no such line item for this business) — fine, the gates
  handle it and record it as unmeasurable.
- **Fetch failed** — record it, then retry with the next server in the cascade.
- **Genuinely not disclosed** — that is a finding. Note it in `research.json` as
  a management-quality signal, because a company that will not disclose is
  telling you something.

## Offline path

`scripts/import_baseline.py` imports a directory of vendor artefacts
(`smcap250.csv`, `info.json`, `fin.pkl`). It exists only to reproduce the pinned
pilot result in `tests/test_pilot_reproduction.py` and needs files that are not
in the repo. **New data must come through MCP.**