---
description: Run daily screening (inspect → universe → ingest → screen → export → health)
---
Load stocks-repo and stock-screen-method. Execute the daily habit: inspect, universe, ingest (if data/inbox has JSON), screen, export, health. Return only deltas: passers count, new/removed vs last run, severity=block symbol_issues. Do not refetch via MCP unless explicitly requested.
