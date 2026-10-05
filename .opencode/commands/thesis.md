---
description: Run Buffett analysis on passers
---
Load buffett-analysis. Read analysis/passers.md and analysis/latest.json. For each passer (or specified SYMBOLs), produce thesis files (thesis/SYMBOL.md), emit research.json fragments for ingest (qualitative_assessment, valuation_assumption, research_verdict), and propose fv_low/fv_high/buy_line (INR) with reasoning. Do not use sell-side targets. After emitting, show which files changed.
