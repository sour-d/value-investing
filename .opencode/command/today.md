---
description: Run the daily loop and report what actually changed.
---

Run the daily loop in `/home/sourav/Projects/stocks` and report the result.

```bash
cd /home/sourav/Projects/stocks && uv run stocks
```

That one command syncs stale data, runs the screen, prints a delta-first
report, and writes `journal/YYYY-MM-DD.md`.

Then read the journal entry it just wrote and report, in this order and no
longer than the report itself:

1. What changed in the screen — entrants and exits, or "no change". If the run
   says thresholds changed or the gates differed, say that plainly instead; a
   method change is not news about a company.
2. Any name at its buy-line, together with any open Gate 5 check on it. A name
   at its line with unverified integrity is a trap, and the two facts only mean
   something next to each other.
3. Position in one line, and any note about a holding that cannot be priced.
4. Health warnings, only those needing attention, with `stocks health
   --explain CODE` for the symbol lists.
5. The unmeasurable inputs, as a standing reminder that they were not passed.

Rules for this report:

- A passer is a research candidate, not a buy. Do not recommend a purchase.
- If sync or the screen failed, say so first. "Nothing changed" and "nothing
  ran" are different and must never be reported the same way.
- Do not add a summary, a ranking, or an opinion the report did not contain.
  If there is nothing to say beyond the lines, say that.
- When the user asks a follow-up about a symbol, use `uv run stocks screen
  --explain SYMBOL` and `uv run stocks why SYMBOL` rather than re-running the
  screen.