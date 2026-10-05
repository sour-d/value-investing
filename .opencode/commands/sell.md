---
description: Record a sell (append-only ledger)
---
Load stock-thesis. Record a sale via `uv run stocks sell SYMBOL --qty QTY --price PRICE_INR --broker BROKER --notes "..."`. Ledger is append-only; never delete. After recording, run `uv run stocks pnl` and `uv run stocks export`. Show realised P&L and remaining position.
