---
description: Fetch missing data via MCP into data/inbox/
---
Load data-acquisition. Use india-stock, yfinance, websearch to fetch only what is missing per `stocks universe` and `stocks health`. Write payloads to data/inbox/ matching: universe.json (optional), profile.json, fundamentals.json, prices.json, research.json. Prefer india-stock_get_historical over large yfinance price ranges. Do not transform numbers. After writing, report which files created and symbol coverage.
