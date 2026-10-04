"""Data-quality issue detection, shared by every ingestion path.

Gate 0 exists to stop a name being judged on numbers that were never really
there. That only works if the flags describing what is missing are recomputed
from the same database the gates read, so detection lives here and both the
MCP inbox path and the offline pickle importer call it.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: Minimum distinct annual periods per statement. Mirrors
#: ``gate0.min_annual_periods`` in ``screen.toml``; kept as a literal because a
#: schema-level floor must hold even for a partially-loaded config.
MIN_PERIODS = 4


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def recompute_issues(conn) -> dict[str, int]:
    """Rebuild ``symbol_issue`` from scratch and return counts by code.

    This is a full recompute rather than an incremental update: the table
    describes the *current* state of the fundamentals, so a symbol whose data
    has since been fetched must lose its old flag. Clearing first is what
    makes a corrected import actually clear the error.

    ``symbol_issue`` is derived state. The transaction ledger is the opposite
    and is never rewritten.
    """
    now = _now()
    counts = {"no_financials": 0, "insufficient_history": 0, "non_contiguous": 0}
    conn.execute("DELETE FROM symbol_issue")

    symbols = [row["symbol"] for row in conn.execute(
        "SELECT symbol FROM universe WHERE in_index = 1 ORDER BY symbol"
    )]
    for symbol in symbols:
        rows = conn.execute(
            "SELECT statement, COUNT(DISTINCT fy_end) AS n FROM fundamentals_annual "
            "WHERE symbol = ? GROUP BY statement",
            (symbol,),
        ).fetchall()
        if not rows:
            conn.execute(
                "INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at) "
                "VALUES (?,?,'block',?,?)",
                (symbol, "no_financials", "no statements ingested", now),
            )
            counts["no_financials"] += 1
            continue

        # Depth is the MINIMUM across statements: plenty of symbols have a deep
        # income statement and a two-year balance sheet, and testing only one
        # statement would admit names with no usable capital base.
        depth = min(row["n"] for row in rows)
        if depth < MIN_PERIODS:
            conn.execute(
                "INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at) "
                "VALUES (?,?,'block',?,?)",
                (symbol, "insufficient_history",
                 f"shallowest statement has {depth} annual periods; need {MIN_PERIODS}", now),
            )
            counts["insufficient_history"] += 1

        periods = conn.execute(
            "SELECT DISTINCT fy_end FROM fundamentals_annual WHERE symbol = ?", (symbol,)
        ).fetchall()
        years = {int(period["fy_end"][:4]) for period in periods}
        if years:
            gaps = [year for year in range(min(years), max(years) + 1) if year not in years]
            if gaps:
                conn.execute(
                    "INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at) "
                    "VALUES (?,?,'warn',?,?)",
                    (symbol, "non_contiguous_periods",
                     f"missing fiscal years: {gaps}", now),
                )
                counts["non_contiguous"] += 1

    conn.commit()
    return counts


def open_issues(conn, severity: str | None = None) -> list[dict]:
    """Outstanding issues, most severe first, as plain dicts."""
    sql = "SELECT symbol,code,severity,detail,detected_at FROM symbol_issue"
    params: tuple = ()
    if severity:
        sql += " WHERE severity = ?"
        params = (severity,)
    sql += " ORDER BY CASE severity WHEN 'block' THEN 0 ELSE 1 END, symbol, code"
    return [dict(row) for row in conn.execute(sql, params)]