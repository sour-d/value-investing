"""Market data provider.

One place that knows how to talk to Yahoo Finance. Everything above this layer
sees plain Python values, so the sync logic and its tests never depend on a
network call or on yfinance's shape.

Two conventions worth knowing:

* NSE symbols are suffixed ``.NS`` on the wire but stored bare. The suffix never
  reaches the database.
* Prices are fetched with ``auto_adjust=False`` and the split/dividend events are
  taken separately. Adjusting silently rewrites history on every corporate
  action, which would make a stored close disagree with what a broker shows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol

NSE_SUFFIX = ".NS"

# Yahoo's index codes for the benchmarks we track.
#
# `NIFTY_SMALLCAP250` is deliberately absent. As of 2026-10 the vendor feed
# returns no bars for ^NIFTY_SMALLCAP250, and the codes that *do* return bars
# report index names that disagree with their own price levels, so the identity
# cannot be confirmed from this feed. Guessing would put a silently wrong index
# into every relative-performance number, so the wire code must be set
# explicitly in screen.toml once confirmed.
BENCHMARK_CODES = {
    "NIFTY50": "^NSEI",
    "NIFTY_MIDCAP150": "^NSMIDCP",
    "NIFTY_SMALLCAP100": "^CNXSC",
    "NIFTY500": "^CRSLDX",
}
UNRESOLVED_BENCHMARKS = ("NIFTY_SMALLCAP250",)


def wire_symbol(symbol: str) -> str:
    return symbol if symbol.startswith("^") else f"{symbol}{NSE_SUFFIX}"


@dataclass
class PriceBar:
    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adj_close: float | None = None
    volume: int | None = None


@dataclass
class Quote:
    symbol: str
    as_of: str
    price: float | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    avg_50d: float | None = None
    avg_200d: float | None = None
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class Financials:
    symbol: str
    statements: dict[str, dict[str, dict[str, float]]]  # stmt -> fy_end -> item -> value
    period_sets: dict[str, list[str]] = field(default_factory=dict)


class Provider(Protocol):
    """What the sync layer needs. Implemented by :class:`YahooProvider`."""

    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def prices(self, symbols: list[str], start: str, end: str) -> dict[str, list[PriceBar]]: ...
    def financials(self, symbols: list[str]) -> dict[str, Financials]: ...
    def benchmark(self, index_code: str, start: str, end: str) -> list[PriceBar]: ...


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def _rows(frame: Any) -> list[dict[str, Any]]:
    """Normalise a yfinance frame into a list of dicts.

    yfinance returns a DataFrame or, for some calls, a Series; both are handled
    here so callers never branch on it.
    """
    if frame is None:
        return []
    if hasattr(frame, "reset_index") and hasattr(frame, "columns"):
        out = frame.reset_index()
        return out.to_dict("records")
    if isinstance(frame, list):
        return frame
    return []


class YahooProvider:
    """Live provider. The only module that imports yfinance."""

    name = "yahoo"

    def __init__(self, chunk: int = 40) -> None:
        import yfinance as yf

        self._yf = yf
        self.chunk = chunk

    def _batched(self, symbols: list[str]):
        for i in range(0, len(symbols), self.chunk):
            yield symbols[i : i + self.chunk]

    def prices(self, symbols: list[str], start: str, end: str) -> dict[str, list[PriceBar]]:
        out: dict[str, list[PriceBar]] = {}
        for batch in self._batched([wire_symbol(s) for s in symbols]):
            frame = self._yf.download(
                batch, start=start, end=end, interval="1d",
                auto_adjust=False, progress=False, group_by="ticker",
                threads=True,
            )
            for wire in batch:
                bare = wire[: -len(NSE_SUFFIX)] if wire.endswith(NSE_SUFFIX) else wire
                sub = None
                if frame is not None and not frame.empty:
                    try:
                        sub = frame[wire] if len(batch) > 1 else frame
                    except KeyError:
                        sub = None
                out[bare] = _to_bars(sub)
        return out

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        tickers = self._yf.Tickers(" ".join(wire_symbol(s) for s in symbols))
        today = dt.date.today().isoformat()
        for bare in symbols:
            wire = wire_symbol(bare)
            try:
                info = tickers.tickers[wire].get_info()
            except Exception:            # noqa: BLE001 - a bad ticker must not abort the batch
                info = {}
            if not isinstance(info, dict):
                info = {}
            out[bare] = Quote(
                symbol=bare,
                as_of=today,
                price=_f(info.get("currentPrice")),
                market_cap=_f(info.get("marketCap")),
                enterprise_value=_f(info.get("enterpriseValue")),
                week52_high=_f(info.get("fiftyTwoWeekHigh")),
                week52_low=_f(info.get("fiftyTwoWeekLow")),
                avg_50d=_f(info.get("fiftyDayAverage")),
                avg_200d=_f(info.get("twoHundredDayAverage")),
                profile=info,
            )
        return out

    def financials(self, symbols: list[str]) -> dict[str, Financials]:
        out: dict[str, Financials] = {}
        for bare in symbols:
            wire = wire_symbol(bare)
            try:
                reports = _reports(self._yf.Ticker(wire))
            except Exception:            # noqa: BLE001 - one bad ticker must not abort the batch
                reports = {}
            out[bare] = _to_financials(bare, reports)
        return out

    def benchmark(self, index_code: str, start: str, end: str) -> list[PriceBar]:
        """Daily bars for a benchmark.

        Accepts either a logical name from `BENCHMARK_CODES` or a vendor wire
        code; the sync layer resolves the wire code first, so an unconfirmed
        benchmark never reaches this function.
        """
        wire = BENCHMARK_CODES.get(index_code, index_code)
        frame = self._yf.download(
            wire, start=start, end=end, interval="1d",
            auto_adjust=False, progress=False,
        )
        return _to_bars(frame)


def _to_bars(frame: Any) -> list[PriceBar]:
    """Convert an OHLCV frame to bars. Column names vary by yfinance version."""
    bars: list[PriceBar] = []
    for row in _rows(frame):
        date = row.get("Date") or row.get("index") or row.get("Datetime")
        if date is None:
            continue
        if hasattr(date, "strftime"):
            date = date.strftime("%Y-%m-%d")
        else:
            date = str(date)[:10]
        close = _f(row.get("Close"))
        adj = _f(row.get("Adj Close"))
        bars.append(PriceBar(
            date=date,
            open=_f(row.get("Open")),
            high=_f(row.get("High")),
            low=_f(row.get("Low")),
            close=close,
            adj_close=adj if adj is not None else close,
            volume=int(row["Volume"]) if _f(row.get("Volume")) is not None else None,
        ))
    return sorted(bars, key=lambda b: b.date)


_STATEMENT_KEYS = {
    "income": ("income_stmt", "incomestmt", "get_income_stmt"),
    "balance": ("balance_sheet", "balancesheet", "get_balance_sheet"),
    "cashflow": ("cashflow", "get_cashflow"),
}


def _reports(ticker: Any) -> dict[str, Any]:
    """Collect the three annual statements from a Ticker.

    yfinance has moved these between `financials_data`, properties and
    `get_*` methods across versions, so all three shapes are probed rather than
    pinned to whichever exists today.
    """
    reports: dict[str, Any] = {}
    data = getattr(ticker, "financials_data", None)
    if isinstance(data, dict):
        reports.update(data)

    for keys in _STATEMENT_KEYS.values():
        for key in keys:
            value = getattr(ticker, key, None)
            if value is None and callable(value):
                continue
            if value is not None and hasattr(value, "columns"):
                reports.setdefault(key, value)
    return reports


def _to_financials(symbol: str, reports: Any) -> Financials:
    """Flatten yfinance's column-oriented statements into period-keyed rows.

    yfinance returns one column per fiscal period, which is exactly the shape
    that invites positional bugs downstream. This inverts it so each value
    carries its own period label.
    """
    statements: dict[str, dict[str, dict[str, float]]] = {}
    period_sets: dict[str, list[str]] = {}
    if not isinstance(reports, dict):
        return Financials(symbol, statements, period_sets)

    for stmt, keys in _STATEMENT_KEYS.items():
        frame = next((reports[k] for k in keys if k in reports), None)
        if frame is None or not hasattr(frame, "columns"):
            continue
        rows: dict[str, dict[str, float]] = {}
        for item in frame.index:
            values: dict[str, float] = {}
            for period in frame.columns:
                v = _f(frame.at[item, period])
                if v is not None:
                    values[str(period)[:10]] = v
            if values:
                rows[str(item)] = values
                for p in values:
                    period_sets.setdefault(stmt, []).append(p)
        if rows:
            statements[stmt] = rows
    for stmt in period_sets:
        period_sets[stmt] = sorted(set(period_sets[stmt]))
    return Financials(symbol, statements, period_sets)