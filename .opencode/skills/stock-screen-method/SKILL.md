---
name: stock-screen-method
description: Use when explaining, questioning or changing how stocks are screened — gate thresholds in screen.toml, ROIC versus ROE versus WACC, what a passer means, tiering, or why a symbol entered or left the screen. Triggers on "gate", "screen", "threshold", "passer", "ROIC", "WACC", "why did ACC drop out".
---

# The screening method

Five gates, run in order. Each is a necessary condition, none is sufficient.
The threshold for every one lives in `screen.toml` with a `# WHY` comment —
read that comment before changing a number, because the comment is the argument
and the number is only its conclusion.

```bash
uv run stocks screen                      # run it
uv run stocks screen --explain SYMBOL     # every check, metric, threshold, failure reason
uv run stocks screen --roic / --no-roic   # force the profitability variant
```

## The gates

**G0 — data sufficiency.** Four contiguous annual periods across *all three*
statements. Checking income alone admits names with no usable balance sheet;
121 of 249 symbols have mismatched statement depth, so this gate does real
work. Blocking issues (`no_financials`, `insufficient_history`,
`non_contiguous_periods`) keep a symbol out of the screen entirely.

**G1 — earnings quality.** Profit has to be cash. Multi-year positive net
income, operating cash flow and free cash flow, OCF-to-NI conversion, and a
conservative accruals ratio. A business that reports profit and keeps missing on
cash fails here regardless of how good the story is.

**G2 — balance sheet.** Net debt to equity, interest coverage, debt to equity.
Survival first.

**G3 — profitability, ROIC over WACC.** The primary gate. Return on invested
capital must clear both an absolute floor and a spread over the cost of
capital. Default: `min_roic = 15.0`, `min_roic_wacc_spread = 5.0`, with
`risk_free_rate` and `equity_risk_premium` set by hand in
`valuation.inputs` because no feed supplies them honestly.

ROE is recorded as a **non-blocking secondary** check. A low ROE on a
capital-light business is not disqualifying, and blocking on it rejected
companies the primary gate liked. It is still recorded — context, not a verdict.

WACC is derived from the capital structure: cost of equity from CAPM, cost of
debt from interest expense, weighted by debt and equity at market values. The
spread is what says whether growth is worth paying for. A high ROIC on
reinvested capital that earns less than its cost destroys value while looking
excellent on every ratio.

**G4 — value.** Price/earnings, EV/EBITDA, FCF yield, price/book, combined as
alternatives rather than a conjunction, because demanding all of them at once
only finds the cheapest wreckage. Cheapest is not the same as best.

**G5 — management integrity, attested by a human.** Promoter pledge, auditor,
related-party transactions, regulatory actions, promoter selling, institutional
shareholder letters. No vendor supplies these, so they cannot be computed and
are never auto-verified. `stocks buy` refuses while any required check is not
`verified`. An override is allowed and the reason is recorded on the
transaction permanently.

## What a passer is

A research queue entry. In the pilot, three names cleared every gate and were
still bad buys. Never relax a gate to admit a stock you like — if a threshold
is genuinely wrong, change `screen.toml` in a commit that says why.

## Comparing two runs

Check `config_hash` first. Different thresholds mean entrants and exits are
meaningless, and the report refuses to print them rather than presenting a
method change as a change in the business. The ROIC variant is compared
separately, and a variant flip is reported as a gate change, never as a
fundamental move.

Any query over `gate_result` must filter on `blocking = 1`. A soft-ROE failure
is recorded context, not a rejection, and reading it as one made the same name
appear as a new entrant on every single run.

## Tiering

Tier 1 clears every gate. Tiers below allow limited failures and apply a
quality-score floor, so a near-miss is ranked rather than discarded. Tiers exist
to order a research queue, never to relax G0 or G5.

## Where this sits in the loop

```bash
uv run stocks inspect      # is the data fresh enough to trust a rerun?
uv run stocks screen       # evaluate; persists the run
uv run stocks export       # write analysis/passers.md and latest.json
```

`analysis/latest.json` is the machine-readable record of the latest run;
`analysis/passers.md` is the same run as a table. Both are committed, so the next
session compares against the previous run instead of re-deriving it.

If a rerun reports an unexpected entrant or exit, check `config_hash` first — if
the thresholds changed, the delta describes your config edit, not the business.
See `analysis/runs.md`.
