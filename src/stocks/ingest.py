"""Load MCP-fetched data from ``data/inbox/`` into the database.

The agent that drives this repo already has data access: the ``india-stock``
and ``yfinance`` MCP servers for structured market data, and a websearch
server for anything neither of them exposes. This module is the seam between
that fetcher and the analysis engine.

The agent writes what it fetched into ``data/inbox/`` as JSON and runs
``stocks ingest``. Nothing here calls the network: ingestion is a pure,
replayable function of what is on disk, so any run can be reproduced from the
inbox alone.

Inbox files are all optional and independently replaceable:

``universe.json``
    ``[{"symbol": "ACC", "name": ..., "industry": ..., "sector": ...}, ...]``
    or a bare list of symbols. This is the roster; a symbol absent from it is
    never screened, so it doubles as an index-membership record.
``profile.json``
    ``{"ACC": {"as_of": "2026-10-04", "marketCap": 2.2e11, ...}}``
    Vendor-computed ratios, verbatim. camelCase vendor keys map onto
    ``profile_snapshot`` columns.
``fundamentals.json``
    ``{"ACC": {"income_statement": {"Total Revenue": {"2026-03-31": 2.5e11}}, ...}}``
    Exactly the shape ``yfinance.get_financials`` returns, including its
    ``NaN`` placeholders. Transposed into ``fundamentals_annual`` on the way in.
``prices.json``
    ``{"ACC": [{"date": "2026-10-01", "open": ..., "high": ..., "low": ...,
                "close": ..., "volume": ...}]}``
``research.json``
    ``{"ACC": {"assessments": {"moat_type": {...}}, "valuation": {...}}}``
    Qualitative findings from websearch, routed into the existing
    ``qualitative_assessment`` and ``valuation_assumption`` tables.

Every write is an upsert keyed on natural keys, so re-ingesting a corrected
symbol replaces it rather than accumulating duplicates. ``symbol_issue`` is
recomputed at the end because it is derived state; the transaction ledger is
never touched.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import db
from .paths import inbox_dir

#: Vendor statement name -> internal ``fundamentals_annual.statement``.
STATEMENT_ALIASES = {
    "income_statement": "income",
    "balance_sheet": "balance",
    "cash_flow": "cashflow",
    "income": "income",
    "balance": "balance",
    "cashflow": "cashflow",
}

#: ``india-stock.get_fundamentals`` nests its fields across three sub-objects
#: rather than returning one flat record, so a verbatim dump has to be flattened
#: before the camelCase mapping above applies.
_PROFILE_SECTIONS = ("financialData", "keyStats", "summaryDetail", "defaultKeyStatistics")


def _flatten_profile(entry: dict) -> dict:
    """Merge the nested vendor sections into one camelCase-level dict."""
    flat: dict = {}
    for section in _PROFILE_SECTIONS:
        value = entry.get(section)
        if isinstance(value, dict):
            flat.update(value)
    flat.update(entry)
    return flat

#: camelCase vendor keys -> ``profile_snapshot`` columns. Only keys a vendor
#: actually returns are listed; anything else is ignored rather than guessed.
PROFILE_FIELDS = {
    "currentPrice": "current_price",
    "marketCap": "market_cap",
    "enterpriseValue": "enterprise_value",
    "trailingPE": "trailing_pe",
    "forwardPE": "forward_pe",
    "priceToBook": "price_to_book",
    "pegRatio": "peg_ratio",
    "bookValue": "book_value",
    "trailingEps": "trailing_eps",
    "forwardEps": "forward_eps",
    "dividendYield": "dividend_yield",
    "payoutRatio": "payout_ratio",
    "beta": "beta",
    "sharesOutstanding": "shares_outstanding",
    "totalCash": "total_cash",
    "totalDebt": "total_debt",
    "debtToEquity": "debt_to_equity",
    "profitMargins": "profit_margin",
    "operatingMargins": "operating_margin",
    "revenueGrowth": "revenue_growth",
    "earningsGrowth": "earnings_growth",
    "grossMargins": "gross_margin",
    # NOTE: `targetMeanPrice` and `recommendationKey` are deliberately absent.
    # Storing an analyst target would anchor the valuation judgement the repo is
    # supposed to make independently — see
    # tests/test_sync.py::test_analyst_targets_are_not_stored.
}

_PROFILE_COLUMNS = sorted(set(PROFILE_FIELDS.values()))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _num(value: Any) -> float | None:
    """Coerce a vendor number to a storable float.

    MCP servers serialise missing numbers as JSON ``null``, ``NaN`` or ``NaN``
    strings depending on the backend, and a pandas-derived dump can carry
    ``NaN`` inside a numeric column. All of them mean "absent"; storing any of
    them would poison averages downstream.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.lower() in {"nan", "none", "null", "n/a", "-", "--"}:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if math.isnan(number) or math.isinf(number) else number


def _date(value: Any) -> str | None:
    """Normalise a date to ``YYYY-MM-DD``.

    Accepts epoch seconds, ``YYYY-MM-DD``, and ISO-8601 with a timezone, the
    last of which is what the price-history tool returns.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    text = text.split("T")[0].split(" ")[0]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _load(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _read(payload: Any) -> Any:
    """Handle both a bare payload and the ``{"data": ...}`` wrapper some
    MCP transports add when dumping a tool result to disk."""
    if isinstance(payload, dict) and set(payload) == {"data"}:
        return payload["data"]
    return payload


def _ensure_symbol(conn, symbol: str) -> bool:
    """Register a symbol that arrived without a roster entry.

    Every data table has a foreign key onto ``universe``, so an agent that
    fetched a quote before writing ``universe.json`` would otherwise hit an
    integrity error and lose the fetch. Fetching is treated as intent to track
    the name, so the symbol is added as an index member and the roster can be
    corrected later by re-ingesting it explicitly.

    Returns ``True`` when the row was newly inserted.
    """
    exists = conn.execute(
        "SELECT 1 FROM universe WHERE symbol = ?", (symbol,)
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO universe (symbol, in_index, added_on) VALUES (?,1,?)",
        (symbol, _now()),
    )
    return True


def ingest_universe(conn, payload: Any) -> int:
    """Upsert the roster. Symbols are never deleted; ``in_index`` is the flag."""
    rows = _read(payload)
    if not rows:
        return 0
    if isinstance(rows, dict):
        rows = [{"symbol": key, **(value if isinstance(value, dict) else {})}
                for key, value in rows.items()]
    now = _now()
    n = 0
    for row in rows:
        if isinstance(row, str):
            row = {"symbol": row}
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        conn.execute(
            "INSERT INTO universe (symbol, name, industry, sector, in_index, added_on) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  name=COALESCE(excluded.name, universe.name), "
            "  industry=COALESCE(excluded.industry, universe.industry), "
            "  sector=COALESCE(excluded.sector, universe.sector), "
            "  in_index=excluded.in_index, removed_on=NULL",
            (symbol, row.get("name"), row.get("industry"), row.get("sector"),
             int(row.get("in_index", 1)), now),
        )
        n += 1
    conn.commit()
    return n


def ingest_profile(conn, payload: Any) -> int:
    """Upsert one vendor ratio snapshot per symbol per ``as_of``.

    Snapshots accumulate rather than overwrite: a valuation ratio is only
    meaningful alongside the date it was observed, and a later run that fetches
    a narrower field set must not erase what an earlier one recorded.
    """
    rows = _read(payload)
    if not isinstance(rows, dict):
        return 0
    n = 0
    for symbol, entry in rows.items():
        if not isinstance(entry, dict):
            continue
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        _ensure_symbol(conn, symbol)
        as_of = _date(entry.get("as_of")) or datetime.now(UTC).strftime("%Y-%m-%d")
        flat = _flatten_profile(entry)
        values = {column: _num(flat.get(vendor))
                  for vendor, column in PROFILE_FIELDS.items()}
        if all(value is None for value in values.values()):
            continue
        columns = ["symbol", "as_of", "source", *sorted(values)]
        # `trusted` stays 0: these are vendor-published ratios that no filing
        # has confirmed, and Gate 5 requires that distinction to stay visible.
        conn.execute(
            f"INSERT INTO profile_snapshot ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))}) "
            f"ON CONFLICT(symbol, as_of) DO UPDATE SET "
            + ",".join(f"{column}=excluded.{column}"
                       for column in sorted(values)),
            (symbol, as_of, "mcp",
             *(values[column] for column in sorted(values))),
        )
        n += 1
    conn.commit()
    return n


def ingest_fundamentals(conn, payload: Any) -> int:
    """Upsert statements, transposing the MCP's ``{item: {date: value}}`` layout.

    Periods are sorted before insert so that gap detection sees a consistent
    ordering regardless of how the vendor ordered its keys.
    """
    rows = _read(payload)
    if not isinstance(rows, dict):
        return 0
    now = _now()
    n = 0
    for symbol, entry in rows.items():
        if not isinstance(entry, dict):
            continue
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        _ensure_symbol(conn, symbol)
        for vendor_statement, statement in STATEMENT_ALIASES.items():
            items_by_period = entry.get(vendor_statement)
            if not isinstance(items_by_period, dict):
                continue
            # The vendor nests as {item: {fy_end: value}}, so the outer key is
            # the line item and the inner map is the periods. Transpose into
            # {fy_end: [(item, value)]} to match how the table is keyed.
            by_period: dict[str, list[tuple[str, float | None]]] = {}
            for item, series in items_by_period.items():
                if not isinstance(series, dict):
                    continue
                for raw_period, value in series.items():
                    period = _date(raw_period)
                    if period is None:
                        continue
                    by_period.setdefault(period, []).append((str(item), _num(value)))

            for period, values in sorted(by_period.items()):
                if not any(value is not None for _, value in values):
                    continue
                conn.executemany(
                    "INSERT INTO fundamentals_annual "
                    "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,'mcp',?,?) "
                    "ON CONFLICT(symbol,fy_end,statement,item) DO UPDATE SET "
                    "  last_seen_at=excluded.last_seen_at, "
                    "  changed_at=CASE WHEN value IS NOT excluded.value "
                    "                 THEN excluded.last_seen_at ELSE changed_at END, "
                    "  value=excluded.value",
                    [(symbol, period, statement, item, value, now, now)
                     for item, value in values],
                )
                n += len(values)
    conn.commit()
    return n


def ingest_prices(conn, payload: Any) -> int:
    """Upsert daily bars. ``adj_close`` falls back to ``close`` when absent."""
    rows = _read(payload)
    if not isinstance(rows, dict):
        return 0
    now = _now()
    n = 0
    for symbol, bars in rows.items():
        if isinstance(bars, dict):
            bars = bars.get("data")
        if not isinstance(bars, list):
            continue
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        _ensure_symbol(conn, symbol)
        values = []
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            date = _date(bar.get("date"))
            close = _num(bar.get("close"))
            if date is None or close is None:
                continue
            adj_close = _num(bar.get("adj_close"))
            values.append((symbol, date, _num(bar.get("open")), _num(bar.get("high")),
                           _num(bar.get("low")), close,
                           close if adj_close is None else adj_close,
                           int(_num(bar.get("volume")) or 0), "mcp", now))
        if values:
            conn.executemany(
                "INSERT INTO price_daily "
                "(symbol,date,open,high,low,close,adj_close,volume,source,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,date) DO UPDATE SET "
                "  open=excluded.open, high=excluded.high, low=excluded.low, "
                "  close=excluded.close, adj_close=excluded.adj_close, "
                "  volume=excluded.volume, fetched_at=excluded.fetched_at",
                values,
            )
            n += len(values)
    conn.commit()
    return n


def ingest_research(conn, payload: Any) -> int:
    """Record websearch findings into the qualitative and valuation tables.

    These are the judgements no ratio can make — moat, pricing power, whether
    management is honest, what growth and discount rate a valuation rests on.
    They are stored as free text with their rationale rather than as a score:
    the schema already treats them as narrative, and a number here would imply
    a precision nobody has. Nothing written here feeds a gate.

    Expected shape::

        {"ACC": {
           "assessments": {"moat_type": {"assessment": "...", "rationale": "..."}},
           "valuation":   {"method": "dcf", "params": {"g": "0.10", "r": "0.09"}},
           "verdict":     "watch"
        }}
    """
    rows = _read(payload)
    if not isinstance(rows, dict):
        return 0
    as_of = datetime.now(UTC).strftime("%Y-%m-%d")
    n = 0
    for symbol, entry in rows.items():
        if not isinstance(entry, dict):
            continue
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        _ensure_symbol(conn, symbol)

        assessments = entry.get("assessments")
        if isinstance(assessments, dict):
            for dimension, value in assessments.items():
                if not isinstance(value, dict):
                    continue
                assessment = str(value.get("assessment") or "").strip()
                rationale = str(value.get("rationale") or "").strip()
                if not assessment or not rationale:
                    continue
                conn.execute(
                    "INSERT INTO qualitative_assessment "
                    "(symbol,dimension,assessment,rationale,as_of) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(symbol,dimension,as_of) DO UPDATE SET "
                    "  assessment=excluded.assessment, rationale=excluded.rationale",
                    (symbol, str(dimension), assessment, rationale, as_of),
                )
                n += 1

        valuation = entry.get("valuation")
        if isinstance(valuation, dict):
            method = str(valuation.get("method") or "dcf").strip()
            params = valuation.get("params")
            rationale = str(valuation.get("rationale") or "").strip()
            if isinstance(params, dict):
                for param, value in sorted(params.items()):
                    conn.execute(
                        "INSERT INTO valuation_assumption "
                        "(symbol,as_of,method,param,value,rationale) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(symbol,as_of,method,param) DO UPDATE SET "
                        "  value=excluded.value, rationale=excluded.rationale",
                        (symbol, as_of, method, str(param), str(value), rationale),
                    )
                    n += 1

        verdict = entry.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            conn.execute(
                "INSERT INTO research_verdict (symbol,as_of,verdict,reason,evidence) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(symbol,as_of) DO UPDATE SET "
                "  verdict=excluded.verdict, reason=excluded.reason, "
                "  evidence=excluded.evidence",
                (symbol, as_of, verdict.strip(),
                 str(entry.get("reason") or "").strip(),
                 str(entry.get("evidence") or "").strip()),
            )
            n += 1
    conn.commit()
    return n


def ingest_all(db_file: Path | None = None,
               inbox: Path | None = None) -> dict[str, int]:
    """Ingest every inbox file present, then recompute data-quality flags.

    Returns a per-file count so a run can report what actually landed. Absent
    files contribute zero rather than failing, which lets an agent refresh only
    the slice it just fetched.
    """
    from .issues import recompute_issues

    conn = db.connect(db_file)
    try:
        db.apply_schema(conn)
        directory = inbox or inbox_dir()
        result = {
            "universe": ingest_universe(conn, _load(directory / "universe.json")),
            "profile": ingest_profile(conn, _load(directory / "profile.json")),
            "fundamentals": ingest_fundamentals(conn, _load(directory / "fundamentals.json")),
            "prices": ingest_prices(conn, _load(directory / "prices.json")),
            "research": ingest_research(conn, _load(directory / "research.json")),
        }
        result["issues"] = sum(recompute_issues(conn).values())
        return result
    finally:
        conn.close()