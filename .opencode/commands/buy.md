---
description: Record a buy (append-only ledger)
---
Load stock-thesis. Record a purchase via `uv run stocks buy SYMBOL --qty QTY --price PRICE_INR --broker BROKER --notes "..."`. Never edit transactions. After recording, run `uv run stocks pnl` and `uv run stocks export`. Show position change and cost basis.
