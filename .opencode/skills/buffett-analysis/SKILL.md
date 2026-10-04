---
name: buffett-analysis
description: Use when judging whether a business is worth owning — moat, pricing power, management integrity, capital allocation, intrinsic value and margin of safety — including on any stock, financial report, annual report or shareholder letter, and for buy/hold/sell calls. Runs the 8-question filter, reads the reference files, then records the verdict through the repo's own valuation_assumption / qualitative_assessment / research_verdict tables so the reasoning persists for future runs.
---

# Buffett analysis on a stock

Thinking the way Buffett thinks: not applying formulas, but asking the
questions he would ask, in his order, and refusing to answer them with numbers
that do not exist.

> **Read the reference files before analysing.** Use the Read tool against
> `{Base directory}/references/NN-name.md`. Do not rely on built-in knowledge as
> a substitute — the point of loading this skill is the framework, not the
> recall.
>
> Read on demand. Read only what the task needs:
> - Business quality and moat → `03`, `08`
> - Management and governance → `04`
> - Reading financials → `05`
> - Valuation, capital allocation → `06`
> - Risk, when to sell, behaviour → `07`
> - Frameworks and philosophy → `01`, `02`

## Step 1 — the 8-question filter

Two minutes. Four "No" answers means stop and move on; integrity (Q7) is an
automatic veto regardless of the rest.

| # | Dimension | Question | No means |
|---|---|---|---|
| 1 | Circle of competence | Can I explain in one paragraph how this makes money? | Outside your circle — skip |
| 2 | Durability | Still here and more competitive in 10 years? | Disruption risk |
| 3 | Moat | Could a competitor replicate this with serious effort? | No moat |
| 4 | Pricing power | Can it raise prices 5–10% without losing real customers? | Commodity |
| 5 | Earnings quality | Does profit convert to cash? | Accounting problem |
| 6 | Debt safety | Survive revenue −30%? | Leverage risk |
| 7 | Management integrity | Do they confront problems, or hide them? | **Automatic veto** |
| 8 | Reasonable price | Is the gap to intrinsic value wide enough? | Wait or skip |

Questions 1–6 are answerable from data already in the repo. **Questions 7 and 8
are not** — they need the websearch step below and a stated valuation.

## Step 2 — start from what the screen already knows

Do not refetch. The gates have already computed the quantitative part:

```bash
uv run stocks screen --explain SYMBOL     # every gate, metric and threshold
uv run stocks why SYMBOL                  # thesis, reasons, monitoring triggers
uv run stocks export                      # refresh analysis/passers.md
```

Take ROE, ROA, margin structure, accruals, cash conversion, leverage and interest
coverage from the gate output. `analysis/passers.md` holds the current table.

Then ask what the ratios **cannot** tell you — which is most of what matters.

## Step 3 — research what no vendor supplies

Neither market-data MCP server has any of this. Use websearch. See the
`data-acquisition` skill for where each source fits.

- **Management integrity (Q7)** — promoter holding and pledge, promoter
  remuneration, auditor identity and any qualification, related-party
  transactions, contingent liabilities, regulatory proceedings, sudden
  management or auditor turnover.
- **Moat (Q3)** — market share trend, competitor capacity, switching costs,
  regulatory protection, network effects, cost curve position.
- **Pricing power (Q4)** — did gross margin hold while revenue grew? Price
  increases taken without volume loss?
- **Durability (Q2)** — is the business being disrupted quietly? Check capex
  trends, customer concentration, and whether the moat is eroding before the
  income statement shows it.

Cite what you find. A claim without a source is an opinion.

## Step 4 — value it, and state the assumptions

Write the assumptions down explicitly; they are the analysis. Store them via
`research.json` → `stocks ingest`, which writes `valuation_assumption`.

Be pessimistic and consistent:

- **Growth** — what the business can sustain, not what management guides to.
- **Discount rate** — never below the risk-free rate. A 4% discount rate on an
  Indian smallcap is a way of assuming the answer.
- **Margin of safety** — required for anything you actually buy. A great
  business at a full price is not a buy.

```bash
uv run stocks watch SYMBOL --buy-line P --fv-low L --fv-high H \
    --reason "why this level" --thesis thesis/SYMBOL.md
```

Margin of safety is derived from fair value and the buy-line, so revising either
later cannot leave it stale.

## Step 5 — record it so the next run knows

This is the step that makes the repo worth more than a one-off answer.

```json
{"SYMBOL": {
  "assessments": {
    "moat_type":       {"assessment": "...", "rationale": "...cited..."},
    "pricing_power":   {"assessment": "...", "rationale": "..."},
    "mgmt_integrity":  {"assessment": "...", "rationale": "..."},
    "capital_allocation": {"assessment": "...", "rationale": "..."}
  },
  "valuation": {"method": "dcf", "rationale": "...",
                "params": {"growth_5y": "0.12", "discount_rate": "0.11"}},
  "verdict": "preferred",
  "reason": "...",
  "evidence": "Annual report FY26; management integrity verified <date>"
}}
```

```bash
uv run stocks ingest && uv run stocks export
```

Valid verdict values are `preferred`, `watch`, `avoid`. Valid dimensions are
`moat_type`, `moat_trend`, `pricing_power`, `business_model`, `mgmt_integrity`,
`capital_allocation`.

## Rules that override any temptation

- **`unverified` is a valid, honest answer. A false `verified` is not.** If you
  did not read the filing, you have not verified it.
- **Never relax a gate to admit a stock you like.** Change `screen.toml` in a
  commit that states why. Do not special-case a symbol in code.
- **A passer is a research candidate, not a buy.** The pilot run cleared three
  names through every gate that were still bad buys.
- **Say what you do not know.** "Unknown" in the rationale is more useful than a
  confident guess, and `stocks why` surfaces it as unverified.
- **When you sell, it is about the thesis breaking, not the price falling.**
  Re-run the 8 questions. If the business is unchanged and the price fell, that
  is the opportunity, not the exit.

## Output shape

When asked to analyse a stock, answer in this order:

1. **Verdict** — one line, plus whether it is a buy, a watch, or neither.
2. **The 8 questions** — answer each with evidence, not adjectives.
3. **Valuation** — the assumptions, then the number.
4. **What would make this wrong** — the falsifiers, and what you would watch.
5. **What is unverified** — explicit, because this is where you are weakest.

Then write it into the repo so the next session starts from your conclusion
rather than re-deriving it.