---
name: stock-thesis
description: Use when taking a position-research view on an Indian smallcap — writing or reviewing an investment thesis, setting a buy line and fair-value range, recording a verdict or qualitative assessment, or asking "why do I own this" / "why is this on my watchlist". Triggers on thesis, watchlist, buy line, fair value, position sizing, "should I buy".
---

# Investment thesis discipline

A thesis is a falsifiable claim with a price attached. The repo stores the
claim, the evidence for it, and — the part that matters — what would prove it
wrong. Anything less is a mood with a ticker symbol.

```bash
uv run stocks watch add SYMBOL --buy-line N --fv-low A --fv-high B --reason "..."
uv run stocks watch status SYMBOL watch
uv run stocks watch list
uv run stocks integrity SYMBOL --set promoter_pledge --status verified --evidence "..."
uv run stocks integrity SYMBOL --verdict preferred --reason "..."
uv run stocks integrity SYMBOL --dimension moat_type --assessment "..." --rationale "..."
uv run stocks why SYMBOL        # everything above, assembled, with gaps left visible
uv run stocks buy SYMBOL QTY PRICE --reason "..." --thesis-ref notes/ACC.md
```

## Buy line and fair value are different numbers

The buy line is where the thesis gets *cheaper*, set from the margin of safety
against the fair-value range. The fair-value range is what the business is
worth. Setting them the same number is how a buy line stops being a discipline
and becomes a prediction.

`stocks why` recomputes the margin of safety on write and the distance to the
buy line on read, so a stale number never sits in a row looking current.

## Separate what is measured from what is claimed

`stocks why` prints these as distinct groups, and the distinction is the point:

- **measured** — ROIC, spread over WACC, coverage, FCF yield: derived from
  stored vendor figures.
- **claimed** — qualitative framework dimensions (`moat_type`, `moat_trend`,
  `pricing_power`, `business_model`, `mgmt_integrity`, `capital_allocation`)
  and research verdicts (`preferred`/`watch`/`avoid`): human judgement, recorded
  with a reason.
- **unassessed** — framework inputs no feed supplies, listed so a blank stays a
  blank rather than reading as a pass.
- **integrity** — Gate 5 check by check, with the evidence for each `verified`.

Never let a vendor ratio migrate into the claimed column, or a judgement into
the measured one. That is how a story becomes a fact.

## An honest `unknown` beats a false `verified`

Gate 5 is attested by a person. `unknown` is a valid answer and the most common
one. A false `verified` is not. Do not record a check as verified without a
filing, page or disclosure to point at — `--evidence` is where the pointer goes.

Integrity is enforced, not advisory: `stocks buy` refuses while any required
check is unverified. `--override-integrity "reason"` is allowed and the reason
is stored on the transaction permanently, so an override stays auditable
forever. Sells are exempt, since you should never be trapped by a gate.

## Write down what would change your mind

A thesis with no kill condition cannot be reviewed, only defended. When you
record one, keep it where `stocks why` will show it next to the trade, and put
a number on it. "Growth stalls" is not a trigger. "ROIC below 12% for two
consecutive years" is.

## Trade reasons outlive moods

`--reason` is required on `buy` and `sell` for exactly that reason. The ledger is
append-only: to correct a mistake, append a reversing entry rather than editing
the original, so the record shows both what you thought and what you did.