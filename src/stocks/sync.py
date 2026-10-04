"""Sync: fetch only what is stale.

The product promise is that ``stocks`` is a one-command daily run, which only
works if the fetch is cheap when nothing changed. So staleness is decided per
kind from ``screen.toml`` and recorded in ``fetch_log``; the next run reads that
log rather than trusting an in-memory flag.

Every fetch is recorded with what it returned, including a partial result. A
sync that silently skips 40 symbols is worse than one that fails.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

from . import db, paths
from .config import Config
from .config import load as load_config
from .providers import Provider, YahooProvider

ONE_YEAR_BACK = "2y"  # yfinance interval shorthand; ~2 calendar years of bars


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _today() -> str:
    return dt.date.today().isoformat()  # noqa: DTZ011


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        then = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - then).total_seconds() / 3600.0


def _days_since(ts: str | None) -> float | None:
    hours = _hours_since(ts)
    return None if hours is None else hours / 24.0


@dataclass
class SyncPlan:
    """What needs fetching, and why."""

    prices: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    financials: list[str] = field(default_factory=list)
    benchmark: bool = False
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.prices or self.quotes or self.financials or self.benchmark)

    def payload(self) -> dict[str, object]:
        return {
            "prices": self.prices, "quotes": self.quotes,
            "financials": self.financials, "benchmark": self.benchmark,
            "reasons": self.reasons,
        }


@dataclass
class SyncResult:
    plan: SyncPlan
    n_prices: int = 0
    n_quotes: int = 0
    n_financials: int = 0
    n_benchmark: int = 0
    errors: list[str] = field(default_factory=list)
    full: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "full": self.full,
            "planned": self.plan.payload(),
            "wrote": {
                "price_rows": self.n_prices,
                "quotes": self.n_quotes,
                "symbols_financials": self.n_financials,
                "benchmark_rows": self.n_benchmark,
            },
            "errors": self.errors,
        }

    def summary(self) -> str:
        bits = []
        if self.n_prices:
            bits.append(f"{self.n_prices} price bars")
        if self.n_quotes:
            bits.append(f"{self.n_quotes} quotes")
        if self.n_financials:
            bits.append(f"{self.n_financials} fundamentals")
        if self.n_benchmark:
            bits.append(f"{self.n_benchmark} benchmark bars")

        # Judge on what was written, not on whether a plan was drawn: a plan can
        # be non-empty and still fetch nothing.
        if not bits and not self.full:
            head = "everything fresh — nothing to fetch"
            bits = []
        else:
            head = "full sync" if self.full else "synced stale data"

        line = head + (": " + ", ".join(bits) if bits else "")
        if self.errors:
            line += f"\n{len(self.errors)} error(s): " + "; ".join(self.errors[:3])
        return line


# ─────────────────────────────────────────────────────────── staleness ────


def _last_fetch(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM fetch_log WHERE kind = ? AND ok = 1", (kind,)
    ).fetchone()
    return row["ts"] if row else None


def plan(conn: sqlite3.Connection, cfg: Config, full: bool = False) -> SyncPlan:
    """Decide what to fetch from the configured staleness budgets."""
    p = SyncPlan()
    symbols = [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM universe WHERE in_index = 1 ORDER BY symbol"
    )]
    if full:
        p.prices, p.quotes, p.financials, p.benchmark = (
            list(symbols), list(symbols), list(symbols), True,
        )
        p.reasons["all"] = "forced full sync"
        return p

    price_hours = float(cfg.get("sync.price_staleness_hours", 20))
    fund_days = float(cfg.get("sync.fundamentals_staleness_days", 7))
    profile_days = float(cfg.get("sync.profile_staleness_days", 7))

    def stale(kind: str, age: float) -> bool:
        """Never fetched counts as maximally stale, not as fresh."""
        hours = _hours_since(_last_fetch(conn, kind))
        return hours is None or hours >= age

    p.reasons["price"] = f"last ok fetch > {price_hours:g}h ago"
    if stale("prices", price_hours):
        p.prices = list(symbols)

    p.reasons["fundamentals"] = f"last ok fetch > {fund_days:g}d ago"
    if stale("financials", fund_days * 24):
        p.financials = list(symbols)

    p.reasons["profile"] = f"last ok fetch > {profile_days:g}d ago"
    if stale("profile", profile_days * 24):
        p.quotes = list(symbols)

    p.reasons["benchmark"] = "no benchmark bars stored, or stale with prices"
    _, wire = resolve_benchmark(cfg)
    if not wire:
        # Nothing to do: `stocks health` reports the unconfirmed benchmark, and
        # retrying every sync would only repeat the same error.
        p.reasons["benchmark"] = "wire code unconfirmed; see stocks health"
    elif p.prices or db.count(conn, "price_benchmark") == 0:
        p.benchmark = True
    return p


# ────────────────────────────────────────────────────────────── writes ────


def _log(conn: sqlite3.Connection, source: str, kind: str, symbol: str | None,
         ok: bool, n_items: int = 0, period_sets: list[str] | None = None,
         error: str | None = None, ms: int | None = None) -> None:
    import json

    conn.execute(
        "INSERT INTO fetch_log (ts,source,kind,symbol,ok,n_items,period_set_json,error,duration_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (_now(), source, kind, symbol, int(ok), n_items,
         json.dumps(period_sets) if period_sets else None, error, ms),
    )


def write_prices(conn: sqlite3.Connection, symbol: str, bars, source: str) -> int:
    """Upsert daily bars. Returns the number of rows written."""
    ts = _now()
    payload = [
        (symbol, b.date, b.open, b.high, b.low, b.close, b.adj_close, b.volume, source, ts)
        for b in bars
    ]
    conn.executemany(
        "INSERT INTO price_daily (symbol,date,open,high,low,close,adj_close,volume,source,fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,date) DO UPDATE SET "
        "  open=excluded.open, high=excluded.high, low=excluded.low, "
        "  close=excluded.close, adj_close=excluded.adj_close, "
        "  volume=excluded.volume, fetched_at=excluded.fetched_at",
        payload,
    )
    return len(payload)


def write_benchmark(conn: sqlite3.Connection, index_code: str, bars, source: str) -> int:
    ts = _now()
    payload = [
        (index_code, b.date, b.open, b.high, b.low, b.close, source, ts) for b in bars
    ]
    conn.executemany(
        "INSERT INTO price_benchmark (index_code,date,open,high,low,close,source,fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(index_code,date) DO UPDATE SET "
        "  open=excluded.open, high=excluded.high, low=excluded.low, "
        "  close=excluded.close, fetched_at=excluded.fetched_at",
        payload,
    )
    return len(payload)


def write_quotes(conn: sqlite3.Connection, quotes: dict, source: str) -> int:
    """Store the quote snapshot and refresh the profile row.

    ``profile_snapshot`` is keyed by date, so a re-fetch on the same day replaces
    the row rather than accumulating one per run.
    """
    n = 0
    for symbol, q in quotes.items():
        conn.execute(
            "INSERT OR REPLACE INTO quote_snapshot "
            "(symbol,as_of,all_time_high,all_time_low,vendor_52w_high,vendor_52w_low,"
            "vendor_50d_avg,vendor_200d_avg,source) VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol, q.as_of, None, None, q.week52_high, q.week52_low,
             q.avg_50d, q.avg_200d, source),
        )
        if q.profile:
            _upsert_profile(conn, symbol, q.as_of, q.profile, source)
        n += 1
    return n


_PROFILE_COLUMNS = {
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
    "employees": "fullTimeEmployees",
}
_PROFILE_TEXT = {"business_summary": "longBusinessSummary"}


def _upsert_profile(conn: sqlite3.Connection, symbol: str, as_of: str,
                    info: dict, source: str) -> None:
    """Refresh the vendor profile row for one symbol.

    Only mapped fields are written, and `trusted` stays 0: these are vendor
    claims that have not been cross-checked against filings. A newly seen field
    is inserted as NULL so a later fetch fills it.
    """
    table_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile_snapshot)")}
    cols = [c for c in (*_PROFILE_COLUMNS, *_PROFILE_TEXT) if c in table_cols]
    if not cols:
        return

    def num(v):
        if v is None or isinstance(v, bool):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f or f in (float("inf"), float("-inf")) else f  # noqa: PLR0124

    values: list[object] = [symbol, as_of]
    for col in cols:
        if col in _PROFILE_TEXT:
            raw = info.get(_PROFILE_TEXT[col])
            values.append(raw if isinstance(raw, str) else None)
        else:
            values.append(num(info.get(_PROFILE_COLUMNS.get(col, ""))))

    placeholders = ",".join("?" * (2 + len(cols) + 2))
    conn.execute(
        f"INSERT INTO profile_snapshot (symbol,as_of,{','.join(cols)},source,trusted) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(symbol,as_of) DO UPDATE SET "
        f"  {','.join(f'{c}=excluded.{c}' for c in cols)}, source=excluded.source",
        [*values, source, 0],
    )


def write_financials(conn: sqlite3.Connection, fin, source: str) -> int:
    """Upsert annual statements, recording when a restated value changes."""
    ts = _now()
    payload = []
    for stmt, items in fin.statements.items():
        for item, by_period in items.items():
            for fy_end, value in by_period.items():
                payload.append((fin.symbol, fy_end, stmt, item, value, source, ts, ts))
    conn.executemany(
        "INSERT INTO fundamentals_annual "
        "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,fy_end,statement,item) DO UPDATE SET "
        "  last_seen_at=excluded.last_seen_at, "
        "  changed_at=CASE WHEN value IS NOT excluded.value "
        "                 THEN excluded.last_seen_at ELSE changed_at END, "
        "  value=excluded.value",
        payload,
    )
    return len(payload)


def mark_filing_required(conn: sqlite3.Connection, cfg: Config) -> int:
    """Flag sectors that need filings-based analysis.

    Set from config, not inferred in code, so the rule is reviewable.
    """
    overrides = cfg.get("sector_overrides", {}) or {}
    financial = overrides.get("financials", {}) or {}
    if not financial.get("requires_filing_analysis"):
        return 0
    cur = conn.execute(
        "UPDATE universe SET requires_filing = 1 "
        "WHERE in_index = 1 AND (is_financial = 1 OR sector = 'Financial Services')"
    )
    return cur.rowcount


# ──────────────────────────────────────────────────────────────── driver ────


def run(db_path=None, full: bool = False, provider: Provider | None = None,
        cfg: Config | None = None, conn: sqlite3.Connection | None = None) -> SyncResult:
    """Fetch whatever is stale and return a summary.

    Pass ``conn`` to run inside a caller's transaction (tests, or a command that
    needs the fetch and the read to be atomic).
    """
    paths.ensure_dirs()
    cfg = cfg or load_config()
    own = conn is None
    conn = conn or db.connect(db_path)
    if own:
        db.apply_schema(conn)
    result = SyncResult(plan=SyncPlan(), full=full)
    try:
        plan_ = plan(conn, cfg, full=full)
        result.plan = plan_
        if plan_.empty:
            return result

        mark_filing_required(conn, cfg)
        provider = provider or YahooProvider()
        source = getattr(provider, "name", "provider")

        if plan_.prices:
            result.n_prices += _sync_prices(conn, provider, plan_.prices, source, result)
        # Gated on the wire code even when forced: an unconfirmed benchmark is a
        # configuration gap for `stocks health` to report, not a fetch failure to
        # repeat on every full sync.
        _, wire = resolve_benchmark(cfg)
        if plan_.benchmark and wire:
            result.n_benchmark += _sync_benchmark(conn, provider, cfg, source, result)
        if plan_.quotes:
            result.n_quotes += _sync_quotes(conn, provider, plan_.quotes, source, result)
        if plan_.financials:
            result.n_financials += _sync_financials(conn, provider, plan_.financials,
                                                     source, result)
        conn.commit()
        return result
    finally:
        if own:
            conn.close()


def _window() -> tuple[str, str]:
    """Two calendar years of bars, so 52-week maths has room."""
    end = _today()
    start = (dt.date.today() - dt.timedelta(days=760)).isoformat()  # noqa: DTZ011
    return start, end


def _sync_prices(conn, provider, symbols, source, result) -> int:
    start, end = _window()
    try:
        data = provider.prices(symbols, start, end)
    except Exception as exc:                        # noqa: BLE001
        _log(conn, source, "prices", None, False, error=str(exc))
        result.errors.append(f"prices: {exc}")
        conn.commit()
        return 0

    total = 0
    empty: list[str] = []
    for symbol in symbols:
        bars = data.get(symbol) or []
        if not bars:
            empty.append(symbol)
            _log(conn, source, "prices", symbol, False, error="no rows returned")
            continue
        total += write_prices(conn, symbol, bars, source)
        _log(conn, source, "prices", symbol, True, n_items=len(bars))
    conn.commit()
    if empty:
        result.errors.append(f"prices: {len(empty)} symbol(s) returned nothing "
                             f"(e.g. {', '.join(empty[:3])})")
    return total


def resolve_benchmark(cfg: Config) -> tuple[str, str | None]:
    """Return (logical index code, vendor wire code or None).

    The wire code is None when it cannot be confirmed. Callers must treat that
    as 'no benchmark data' rather than falling back to a different index: a
    silently substituted benchmark corrupts every relative number.
    """
    index_code = cfg.get("universe.benchmark", "NIFTY_SMALLCAP250")
    wire = cfg.get("universe.benchmark_wire")
    if wire:
        return index_code, str(wire)
    from .providers import BENCHMARK_CODES
    return index_code, BENCHMARK_CODES.get(index_code)


def _sync_benchmark(conn, provider, cfg: Config, source, result) -> int:
    index_code, wire = resolve_benchmark(cfg)
    if not wire:
        _log(conn, source, "benchmark", index_code, False,
             error="vendor wire code unconfirmed; set universe.benchmark_wire in screen.toml")
        result.errors.append(
            f"benchmark {index_code}: wire code unconfirmed — relative performance "
            "is unavailable until universe.benchmark_wire is set"
        )
        conn.commit()
        return 0

    start, end = _window()
    try:
        bars = provider.benchmark(wire, start, end)
    except Exception as exc:                        # noqa: BLE001
        _log(conn, source, "benchmark", index_code, False, error=str(exc))
        result.errors.append(f"benchmark {index_code} ({wire}): {exc}")
        conn.commit()
        return 0

    if not bars:
        _log(conn, source, "benchmark", index_code, False,
             error=f"{wire} returned no bars")
        result.errors.append(f"benchmark {index_code}: {wire} returned no bars")
        conn.commit()
        return 0

    n = write_benchmark(conn, index_code, bars, source)
    _log(conn, source, "benchmark", index_code, True, n_items=n)
    conn.commit()
    return n


def _sync_quotes(conn, provider, symbols, source, result) -> int:
    try:
        quotes = provider.quotes(symbols)
    except Exception as exc:                        # noqa: BLE001
        _log(conn, source, "profile", None, False, error=str(exc))
        result.errors.append(f"profile: {exc}")
        conn.commit()
        return 0
    n = write_quotes(conn, quotes, source)
    for symbol in symbols:
        _log(conn, source, "profile", symbol, symbol in quotes,
             n_items=1 if symbol in quotes else 0)
    conn.commit()
    return n


def _sync_financials(conn, provider, symbols, source, result) -> int:
    try:
        data = provider.financials(symbols)
    except Exception as exc:                        # noqa: BLE001
        _log(conn, source, "financials", None, False, error=str(exc))
        result.errors.append(f"fundamentals: {exc}")
        conn.commit()
        return 0

    n = 0
    empty: list[str] = []
    for symbol in symbols:
        fin = data.get(symbol)
        if fin is None or not fin.statements:
            empty.append(symbol)
            _log(conn, source, "financials", symbol, False, error="no statements")
            continue
        n += write_financials(conn, fin, source)
        _log(conn, source, "financials", symbol, True,
             n_items=len(fin.statements),
             period_sets=sorted({p for ps in fin.period_sets.values() for p in ps}))
    conn.commit()
    if empty:
        result.errors.append(f"fundamentals: {len(empty)} symbol(s) empty "
                             f"(e.g. {', '.join(empty[:3])})")
    return n