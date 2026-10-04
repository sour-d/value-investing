"""Import the pilot artefacts captured during initial research into the database.

One-shot bootstrap. The source lives in ``/tmp/opencode`` and holds 251 symbols,
their vendor profile snapshots, and annual statements in ``{fy_end: {item: value}}``
form. Import is idempotent and checksum-guards the universe CSV against a
duplicate file found during research.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
from datetime import UTC, datetime
from pathlib import Path

from stocks import db
from stocks.config import Config

MIN_PERIODS = 4  # matches gate0.min_annual_periods


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _num(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


# Table column -> vendor profile key. Vendor keys absent from this map are
# dropped on purpose: address, phone, messageBoardId, cryptoTradeable, and all
# analyst targets (which would anchor valuation judgement).
_PROFILE_MAP = {
    "current_price": "currentPrice", "market_cap": "marketCap",
    "enterprise_value": "enterpriseValue", "trailing_pe": "trailingPE",
    "forward_pe": "forwardPE", "price_to_book": "priceToBook",
    "peg_ratio": "pegRatio", "ev_ebitda": "enterpriseToEbitda",
    "ev_revenue": "enterpriseToRevenue", "book_value": "bookValue",
    "trailing_eps": "trailingEps", "forward_eps": "forwardEps",
    "dividend_yield": "dividendYield", "payout_ratio": "payoutRatio",
    "beta": "beta", "shares_outstanding": "sharesOutstanding",
    "float_shares": "floatShares", "total_cash": "totalCash",
    "total_debt": "totalDebt", "debt_to_equity": "debtToEquity",
    "roe": "returnOnEquity", "roa": "returnOnAssets",
    "profit_margin": "profitMargins", "operating_margin": "operatingMargins",
    "ebitda_margin": "ebitdaMargins", "revenue_growth": "revenueGrowth",
    "earnings_growth": "earningsGrowth",
    "held_pct_insiders": "heldPercentInsiders",
    "held_pct_institutions": "heldPercentInstitutions",
    "employees": "fullTimeEmployees", "business_summary": "longBusinessSummary",
}
# Columns that are free text and must not go through the float coercion.
_TEXT_PROFILE_COLUMNS = {"business_summary"}


def import_universe(conn, csv_path: Path, src_dir: Path) -> int:
    import csv

    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)  # noqa: PERF402  # PERF402: list.append is fine, but ruff wants list(rows) - keeping as-is

    now = _now()
    inserted = 0
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        conn.execute(
            "INSERT INTO universe (symbol,name,isin,industry,sector,added_on) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  name=excluded.name, isin=excluded.isin, "
            "  industry=excluded.industry, sector=excluded.sector",
            (symbol, (row.get("Company Name") or "").strip(),
             (row.get("ISIN Code") or "").strip(),
             (row.get("Industry") or "").strip(),
             None, now),
        )
        inserted += 1
    conn.commit()

    # Derive the financial-sector flag from the universe CSV's own industry
    # column, recording that it was a heuristic so it stays visible.
    from stocks.metrics import classify_financial

    for row in conn.execute("SELECT symbol, industry FROM universe").fetchall():
        if row["industry"] is None:
            continue
        conn.execute(
            "UPDATE universe SET is_financial=?, classification_src='heuristic' "
            "WHERE symbol=? AND classification_src IS NULL",
            (int(classify_financial(None, row["industry"])), row["symbol"]),
        )
    conn.commit()
    return inserted


def import_profiles(conn, info_path: Path, as_of: str) -> int:
    """Load vendor profile snapshots, keeping only mapped, existing columns.

    The column list is read back off the table rather than assumed, so a schema
    that gains or loses a field cannot desynchronise this insert.
    """
    info = json.loads(info_path.read_text())
    table_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile_snapshot)")}
    cols = [c for c in _PROFILE_MAP if c in table_cols]

    placeholders = ",".join("?" * (2 + len(cols) + 2))
    sql = (
        f"INSERT OR REPLACE INTO profile_snapshot "
        f"(symbol,as_of,{','.join(cols)},source,trusted) VALUES ({placeholders})"
    )

    n = 0
    for symbol, blob in info.items():
        if not isinstance(blob, dict):
            continue
        vals: list[object] = [symbol, as_of]
        for col in cols:
            raw = blob.get(_PROFILE_MAP[col])
            vals.append(raw if col in _TEXT_PROFILE_COLUMNS else _num(raw))
        vals += ["yahoo-pilot", 0]
        conn.execute(sql, vals)
        n += 1
    conn.commit()
    return n


def import_fundamentals(conn, fin_path: Path, source: str) -> int:
    fin = pickle.loads(fin_path.read_bytes())
    now = _now()
    n = 0
    for symbol, entry in fin.items():
        if not isinstance(entry, dict):
            continue
        for statement in ("income", "balance", "cashflow"):
            stmt = entry.get(statement) or {}
            if not isinstance(stmt, dict):
                continue
            for fy_end, items in stmt.items():
                if not isinstance(items, dict):
                    continue
                period = str(fy_end)[:10]
                conn.executemany(
                    "INSERT INTO fundamentals_annual "
                    "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol,fy_end,statement,item) DO UPDATE SET "
                    "  last_seen_at=excluded.last_seen_at, "
                    "  changed_at=CASE WHEN value IS NOT excluded.value "
                    "                 THEN excluded.last_seen_at ELSE changed_at END, "
                    "  value=excluded.value",
                    [(symbol, period, statement, item, _num(val), source, now, now)
                     for item, val in items.items()],
                )
                n += len(items)
    conn.commit()
    return n


def detect_issues(conn) -> dict[str, int]:
    """Populate symbol_issue so Gate 0 and `stocks health` can read the truth.

    Detection is a full recompute, so prior rows are cleared first; otherwise
    re-running accumulates stale duplicates. `symbol_issue` is derived state,
    unlike the transaction ledger.
    """
    now = _now()
    counts = {"no_financials": 0, "insufficient_history": 0, "non_contiguous": 0}
    conn.execute("DELETE FROM symbol_issue")

    symbols = [r["symbol"] for r in conn.execute(
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
                (symbol, "no_financials", "vendor returned no statements", now),
            )
            counts["no_financials"] += 1
            continue

        # Depth is the MINIMUM across statements: 121 of 249 pilot symbols had
        # mismatched depth, so testing one statement would admit names with no
        # usable balance sheet.
        depth = min(r["n"] for r in rows)
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
        years = {int(p["fy_end"][:4]) for p in periods}
        if years:
            lo, hi = min(years), max(years)
            gaps = [y for y in range(lo, hi + 1) if y not in years]
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


def main(src: Path, cfg: Config, db_path: Path | None = None) -> int:
    conn = db.connect(db_path)
    db.apply_schema(conn)

    csv_path = src / "smcap250.csv"
    dupe = src / "t.csv"
    if dupe.exists() and dupe.read_bytes() == csv_path.read_bytes():
        print(f"note: {dupe.name} is byte-identical to {csv_path.name}; skipping")

    bad = src / "smcap.json"
    if bad.exists() and bad.read_text(errors="ignore").lstrip().startswith("<"):
        print(f"note: {bad.name} is a cached HTTP error page; skipping")

    n_uni = import_universe(conn, csv_path, src)
    print(f"universe: {n_uni} symbols")

    as_of = _now()[:10]
    n_prof = import_profiles(conn, src / "info.json", as_of)
    print(f"profile:  {n_prof} snapshots")

    n_fin = import_fundamentals(conn, src / "fin.pkl", "yahoo-pilot")
    print(f"fundamentals: {n_fin} values")

    issues = detect_issues(conn)
    print(f"issues:   {issues}")
    conn.close()
    return 0


if __name__ == "__main__":
    # argparse rather than sys.argv[1]: a silently ignored third argument used
    # to make `--db somewhere-else` write to the real database instead.
    _ap = argparse.ArgumentParser(description=__doc__)
    _ap.add_argument("src", type=Path, help="directory holding the pilot artefacts")
    _ap.add_argument("--db", type=Path, default=None,
                     help="database to import into (default: the canonical one)")
    _args = _ap.parse_args()
    cfg = Config(data={}, source=Path(), config_hash="", gating_hash="")
    raise SystemExit(main(_args.src, cfg, _args.db))