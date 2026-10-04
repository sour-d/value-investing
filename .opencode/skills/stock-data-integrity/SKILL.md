---
name: stock-data-integrity
description: Use when a number looks wrong, a stock is missing data, or something must be verified against filings — stale prices, unknown NIFTY SMALLCAP 250 benchmark wire, absent NPA/CET1 data, promoter pledge, related-party transactions, "vendor ratio", "trusted", or interpreting `stocks health` warnings. Triggers on data quality, staleness, missing financials, benchmark, index code.
---

# Data integrity

The repository's position is that a missing input is never a pass, and a vendor
ratio is a claim rather than a fact. Every mechanism below exists to keep an
unknown visibly unknown instead of quietly averaging into a decision.

```bash
uv run stocks health                    # every check: ok / warn / bad
uv run stocks health --explain CODE     # which symbols carry one issue
uv run stocks sync                      # fetch stale data only
uv run stocks sync --full               # ignore staleness budgets; rarely wanted
sqlite3 data/stocks.db "SELECT * FROM price_daily WHERE symbol='ACC' ORDER BY date DESC LIMIT 5"
```

## Three-valued health, on purpose

`ok`, `warn`, `bad` — not a pass/fail binary. `unknown` is the honest state for
most management data, and collapsing it into a failure would train you to ignore
the check. `health` exits nonzero only on `bad`, so warnings stay visible in
routine use instead of becoming an error you have learned to skip.

Current known warnings, all expected and all honest: three symbols with
insufficient history, two with no financials (`BAGMANE`, `DUMMYHEG` are
placeholders), the unconfirmed benchmark wire, 38 symbols needing
filing-level NPA/CET1/provision analysis, and the unmeasurable-input list.

## Staleness is per-kind

Prices go stale in hours; annual fundamentals go stale in quarters. One global
budget is wrong for both — either it refetches fundamentals daily or it trades
on month-old prices. `sync.price_staleness_hours` and its siblings are separate
per kind, and `fetch_log` records what was attempted so a failure is visible
rather than inferred from an unchanged row.

## What no feed supplies

These are `warn`, permanently, unless someone reads filings:

- **NPA, CET1, provisioning** — 38 smallcaps are flagged as needing
  filing-level analysis. Ratio gates cannot substitute; screen them from the
  annual report.
- **The six framework inputs** — `pricing_power`, `moat_trend`,
  `maintenance_capex`, `shareholder_letters`, `sector_regulation`,
  `related_party_pricing`. They appear in every daily report so they cannot be
  forgotten, and they are assessed by hand.
- **Management integrity** — the whole of Gate 5. Attested by a person with
  evidence, never auto-verified.

## The benchmark is deliberately unresolved

`universe.benchmark_wire` is empty. `^NIFTY_SMALLCAP250` returns no bars, and
candidate vendor codes carry inconsistent names and price levels. Rather than
silently adopt one and produce relative returns against the wrong thing, the
repo fetches nothing and says so:

- no benchmark bars are stored,
- `stocks pnl --benchmark` refuses rather than inventing a comparison,
- `health` warns with the instruction to confirm the code against the official
index factsheet.

Verify the wire against the factsheet before setting it. A benchmark that is
subtly the wrong index is worse than none, because relative performance is the
number that feels authoritative.

## Vendor ratios are claims

`profile_snapshot` carries `trusted = 0` by default. Market cap, enterprise
value, invested capital and the vendor's own ratios are unverified inputs. That
matters because ROIC's denominator and WACC's capital structure weights come
from them — so a load-bearing vendor number needs a filings cross-check before
anything depending on it is treated as settled.

## Rules for the database

Never write to `data/stocks.db` directly; go through the CLI. Raw vendor values
are stored keyed by `fy_end` and metrics are derived on read, so a metric
recomputed with new logic never has to be backfilled. The database holds only
derived data and can be rebuilt:

```bash
rm data/stocks.db && uv run stocks init && uv run python scripts/import_baseline.py /tmp/opencode
```

The one exception to "derived": `transactions` is the input. It is append-only
by trigger, and correcting it means appending a reversing entry.