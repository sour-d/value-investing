---
description: Take a research view on one symbol and record what is measurable, claimed, and missing.
---

Research `$1` in `/home/sourav/Projects/stocks` and record the outcome.

Use the `stocks-repo`, `stock-screen-method`, `stock-thesis` and
`stock-data-integrity` skills. Symbol: `$1`.

Start by reading what is stored, not by fetching:

```bash
cd /home/sourav/Projects/stocks
uv run stocks screen --explain $1
uv run stocks why $1
uv run stocks health --explain no_financials
```

Only `uv run stocks sync --full` if data for this symbol is actually missing —
check `health` first, because a full fetch of all 251 symbols takes minutes.

Then report these separately, because collapsing them is how a story becomes a
fact:

1. **Measured** — the gates it passes and fails, each with the metric, the
   threshold and the margin. Lead with the ROIC-over-WACC spread.
2. **Claimed** — what the qualitative framework and prior notes say, attributed
   as judgement.
3. **Unassessed** — framework inputs and Gate 5 checks with no answer. An empty
   is a blank, not a pass.
4. **Verdict** — `preferred`, `watch` or `avoid`, with the reason, plus the
   kill condition: the specific measurable fact that would prove the thesis
   wrong.

Offer to record it, and only write when the user agrees:

```bash
uv run stocks watch add $1 --buy-line N --fv-low A --fv-high B --reason "..."
uv run stocks integrity $1 --set CHECK --status verified --evidence "filing, page or disclosure"
uv run stocks integrity $1 --verdict VERDICT --reason "..."
uv run stocks integrity $1 --dimension DIM --assessment "..." --rationale "..."
```

Rules:

- Never record a Gate 5 check as `verified` without evidence. `unknown` is a
  valid answer; a false `verified` is not.
- Never relax a threshold to admit this symbol, and never special-case a symbol
  in code. If a threshold is genuinely wrong, change `screen.toml` in a commit
  that says why.
- A passer is a research queue entry, not a buy. Do not recommend a purchase
  unless the user has asked for a view on the trade itself, and say plainly
  that gates are necessary and not sufficient.
- Do not record a buy into the ledger without an explicit instruction. `stocks
  buy` requires `--reason` and enforces Gate 5.