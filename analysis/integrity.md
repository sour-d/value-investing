# Data integrity

## Outstanding issues (7)

| symbol   | severity | code                   | detail                                            |
|----------|----------|------------------------|---------------------------------------------------|
| BAGMANE  | block    | no_financials          | no statements ingested                            |
| CANHLIFE | block    | insufficient_history   | shallowest statement has 2 annual periods; need 4 |
| DUMMYHEG | block    | no_financials          | no statements ingested                            |
| RUBICON  | block    | insufficient_history   | shallowest statement has 2 annual periods; need 4 |
| VISL     | block    | insufficient_history   | shallowest statement has 2 annual periods; need 4 |
| ACC      | warn     | non_contiguous_periods | missing fiscal years: [2023]                      |
| GILLETTE | warn     | non_contiguous_periods | missing fiscal years: [2025]                      |


Every stored figure is vendor-reported through MCP. Nothing here has
been checked against a filing. Treat it as a screening input, and cite
the annual report before acting on any single number.
