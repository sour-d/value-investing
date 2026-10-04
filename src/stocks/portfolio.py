"""Portfolio ledger: positions, cost basis, P&L and money-weighted returns.

The ledger is append-only and every position figure here is derived from it. No
module writes a "current position" row, because a stored position is a claim
that can drift from the transactions that justify it.

Cost basis is average-cost, matching Indian broker convention, where a sale is
matched to the running average rather than to specific lots. Two consequences
are deliberate: realised P&L recognises the average price at the time of sale
rather than permitting lot selection, and fees are capitalised into basis so a
round trip is never flattered by ignoring costs.

The integrity gate is enforced at write time, not read time. A refusal at
`stocks buy` is worth far more than a warning printed later, because the moment
to not buy is before the money moves. `--override-integrity` exists and records
its reason on the transaction itself, so an override stays auditable forever
rather than becoming folklore.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import Config
from .config import load as load_config


class PortfolioError(Exception):
    """A refused write. The message is shown to the user verbatim."""


class IntegrityError(PortfolioError):
    """Gate 5 is not satisfied for this symbol."""


# ────────────────────────────────────────────────────────── transactions ────


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _require_symbol(conn: sqlite3.Connection, symbol: str) -> str:
    symbol = symbol.upper()
    row = conn.execute(
        "SELECT symbol FROM universe WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is None:
        known = conn.execute(
            "SELECT symbol FROM universe WHERE symbol LIKE ? LIMIT 5",
            (f"{symbol[:1]}%",),
        ).fetchall()
        hint = ", ".join(r["symbol"] for r in known)
        raise PortfolioError(
            f"{symbol} is not in the tracked universe"
            + (f". Did you mean: {hint}?" if hint else "")
        )
    return symbol


def integrity_status(
    conn: sqlite3.Connection, cfg: Config, symbol: str
) -> tuple[list[str], dict[str, str]]:
    """Gate 5 for one symbol.

    Returns (blocking failures, status per check). A missing row counts as
    unverified, not as absent — an answer nobody recorded is not a pass.
    """
    required = cfg.get("gate5.integrity.required_checks") or []
    rows = {
        r["check_name"]: r["status"]
        for r in conn.execute(
            "SELECT check_name, status FROM integrity_check WHERE symbol = ?", (symbol,)
        )
    }
    unverified = [c for c in required if rows.get(c) != "verified"]
    return unverified, rows


def record_transaction(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    *,
    fees: float = 0.0,
    reason: str,
    thesis_ref: str | None = None,
    cfg: Config | None = None,
    db_path: str | None = None,
    override_integrity: str | None = None,
    ts: str | None = None,
) -> int:
    """Append one transaction, after checking it should be allowed.

    Order matters here. Integrity is checked before the sell-size check, because
    a sell that would otherwise be refused for size must still not bypass Gate
    5 quietly, and because the integrity failure is the more actionable message.
    """
    cfg = cfg or load_config()
    symbol = _require_symbol(conn, symbol)
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise PortfolioError(f"side must be BUY or SELL, not {side!r}")
    if qty <= 0:
        raise PortfolioError(f"qty must be positive, got {qty}")
    if price <= 0:
        raise PortfolioError(f"price must be positive, got {price}")
    if fees < 0:
        raise PortfolioError(f"fees cannot be negative, got {fees}")
    if not reason or not reason.strip():
        raise PortfolioError("reason is required; an unexplained trade cannot be reviewed")

    if side == "BUY":
        unverified, _ = integrity_status(conn, cfg, symbol)
        if unverified and not override_integrity:
            checks = ", ".join(unverified)
            raise IntegrityError(
                f"Gate 5 not satisfied for {symbol}: {checks}. No provider supplies "
                f"these, so they must be attested with evidence:\n"
                f"  stocks integrity {symbol}\n"
                f"If you have verified them anyway, re-run with "
                f"--override-integrity \"why\" and the reason is stored on the "
                f"transaction."
            )
    else:
        held = held_qty(conn, symbol)
        if qty > held:
            raise PortfolioError(
                f"cannot sell {qty} {symbol}: holding {held}. "
                "The ledger is the only source of position truth, so an unrecorded "
                "buy cannot be sold against."
            )

    cur = conn.execute(
        "INSERT INTO transactions "
        "(ts,symbol,side,qty,price,fees,reason,thesis_ref,integrity_override,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts or _now(), symbol, side, int(qty), float(price), float(fees),
         reason.strip(), thesis_ref,
         override_integrity.strip() if override_integrity else None, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


# ───────────────────────────────────────────────────────────── positions ────


@dataclass
class Position:
    symbol: str
    qty: int
    avg_cost: float
    gross_buy: float
    invested: float
    first_ts: str | None = None
    last_ts: str | None = None
    price: float | None = None       # latest close
    prev_price: float | None = None  # previous close, for the day change
    market_value: float | None = None
    unrealised: float | None = None
    unrealised_pct: float | None = None
    day_change_pct: float | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "qty": self.qty, "avg_cost": self.avg_cost,
            "price": self.price, "market_value": self.market_value,
            "unrealised": self.unrealised, "unrealised_pct": self.unrealised_pct,
            "day_change_pct": self.day_change_pct, "invested": self.invested,
        }


def held_qty(conn: sqlite3.Connection, symbol: str) -> int:
    """Shares currently held, from the ledger alone."""
    row = conn.execute("SELECT qty FROM v_positions WHERE symbol = ?",
                       (symbol.upper(),)).fetchone()
    return int(row["qty"]) if row else 0


def integrity_ledger(conn: sqlite3.Connection, cfg: Config, symbol: str
                     ) -> list[dict[str, Any]]:
    """Gate 5 checks for one symbol, with evidence and timestamps.

    Every required check is listed even when no row exists, so an unattested
    check reads as `unknown` rather than vanishing from the list.
    """
    _, _rows = integrity_status(conn, cfg, symbol.upper())
    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT check_name, status, evidence, verified_at FROM integrity_check "
        "WHERE symbol = ? ORDER BY check_name", (symbol.upper(),)
    ):
        required = r["check_name"] in (cfg.get("gate5.integrity.required_checks") or [])
        out.append({
            "check_name": r["check_name"],
            "status": r["status"],
            "evidence": r["evidence"],
            "verified_at": r["verified_at"],
            "required": required,
        })
    known = {d["check_name"] for d in out}
    for c in cfg.get("gate5.integrity.required_checks") or []:
        if c not in known:
            out.append({"check_name": c, "status": "unknown", "evidence": None,
                        "verified_at": None, "required": True})
    return out


def _prices(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    rows = conn.execute(
        "SELECT symbol, close, rn FROM ("
        "  SELECT symbol, date, close,"
        "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn"
        "  FROM price_daily"
        ") WHERE rn <= 2 ORDER BY symbol, rn"
    ).fetchall()
    out: dict[str, tuple[float, float | None]] = {}
    for r in rows:
        if r["rn"] == 1:
            out[r["symbol"]] = (r["close"], None)
        else:
            out[r["symbol"]] = (out[r["symbol"]][0], r["close"])
    # mypy: after the loop all entries have both values filled
    return out  # type: ignore[return-value]


def positions(conn: sqlite3.Connection) -> list[Position]:
    """Open positions with average cost and mark-to-market.

    Cost basis is derived from the ledger rather than read from a view: the
    view in the schema exposes gross buy value, but average cost needs the
    running-average logic that lives here.
    """
    prices = _prices(conn)
    out: list[Position] = []
    for r in conn.execute("SELECT * FROM v_positions ORDER BY symbol"):
        symbol = r["symbol"]
        basis = _average_cost(conn, symbol)
        qty = int(r["qty"])
        invested = basis * qty
        p = Position(
            symbol=symbol, qty=qty, avg_cost=basis,
            gross_buy=float(r["gross_buy"]), invested=invested,
            first_ts=r["first_ts"], last_ts=r["last_ts"],
        )
        px = prices.get(symbol)
        if px:
            p.price, p.prev_price = px
            p.market_value = p.price * qty
            p.unrealised = p.market_value - invested
            p.unrealised_pct = (p.unrealised / invested * 100) if invested else None
            if p.prev_price:
                p.day_change_pct = (p.price - p.prev_price) / p.prev_price * 100
        out.append(p)
    return out


def _average_cost(conn: sqlite3.Connection, symbol: str) -> float:
    """Average cost per share currently held, fees included.

    Walked chronologically rather than aggregated, because a sale reduces the
    quantity at the then-current average and does not change that average.
    """
    qty = 0
    value = 0.0
    for r in conn.execute(
        "SELECT side, qty, price, fees FROM transactions "
        "WHERE symbol = ? ORDER BY ts, id", (symbol,)
    ):
        q, p, f = int(r["qty"]), float(r["price"]), float(r["fees"])
        if r["side"] == "BUY":
            # Fees capitalise into basis: a round trip should never look cheaper
            # than it was by ignoring the cost of entering it.
            value += q * p + f
            qty += q
        else:
            if qty:
                avg = value / qty
                value -= avg * q
                qty -= q
            # Selling more than held cannot happen: record_transaction refuses.
    return value / qty if qty else 0.0


# ────────────────────────────────────────────────────────────────── P&L ────


@dataclass
class PnL:
    positions: list[Position] = field(default_factory=list)
    invested: float = 0.0
    market_value: float = 0.0
    unrealised: float = 0.0
    unrealised_pct: float | None = None
    realised: float = 0.0
    fees_paid: float = 0.0
    day_change: float | None = None
    benchmark: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "invested": self.invested, "market_value": self.market_value,
            "unrealised": self.unrealised, "unrealised_pct": self.unrealised_pct,
            "realised": self.realised, "fees_paid": self.fees_paid,
            "day_change": self.day_change, "benchmark": self.benchmark,
            "positions": [p.payload() for p in self.positions], "notes": self.notes,
        }


def realised_pnl(conn: sqlite3.Connection, symbol: str | None = None,
                 since: str | None = None) -> float:
    """Realised profit using the average cost at the time of each sale."""
    if symbol:
        rows = conn.execute(
            "SELECT side, qty, price, fees, ts FROM transactions "
            "WHERE symbol = ? ORDER BY ts, id", (symbol.upper(),)
        )
    else:
        rows = conn.execute(
            "SELECT symbol, side, qty, price, fees, ts FROM transactions "
            "ORDER BY symbol, ts, id"
        )
    by_symbol: dict[str, dict[str, float]] = {}
    total = 0.0
    for r in rows:
        sym = symbol.upper() if symbol else r["symbol"]
        state = by_symbol.setdefault(sym, {"qty": 0.0, "value": 0.0})
        q, p, f = float(r["qty"]), float(r["price"]), float(r["fees"])
        if r["side"] == "BUY":
            state["value"] += q * p + f
            state["qty"] += q
        else:
            if state["qty"] > 0:
                avg = state["value"] / state["qty"]
                proceeds = q * p - f
                if since is None or r["ts"] >= since:
                    total += proceeds - avg * q
                state["value"] -= avg * q
                state["qty"] -= q
    return total


def xirr(cashflows: list[tuple[str, float]]) -> float | None:
    """Money-weighted return (XIRR) from dated cash flows.

    Newton's method on the NPV function. Cash flows must be ordered and must
    contain at least one sign change, which every portfolio does: money goes out
    before it comes back. Returns None rather than a number when it cannot
    converge, because a fabricated IRR is worse than no IRR.
    """
    if len(cashflows) < 2:
        return None
    flows = sorted(cashflows)
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    origin = dt.date.fromisoformat(flows[0][0][:10])
    days = [(dt.date.fromisoformat(d[:10]) - origin).days / 365.0 for d, _ in flows]
    amounts = [a for _, a in flows]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** t for a, t in zip(amounts, days))

    lo, hi = -0.9999, 10.0
    f_lo = npv(lo)
    if f_lo * npv(hi) > 0:
        # No sign change across the bracket: the return is outside any range
        # worth quoting.
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def pnl(
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    cfg: Config | None = None,
    since: str | None = None,
    benchmark: bool = False,
) -> PnL:
    cfg = cfg or load_config()
    own = conn is None
    conn = conn or db.connect(db_path)
    if own:
        db.apply_schema(conn)
    try:
        pos = positions(conn)
        out = PnL(positions=pos)
        out.invested = sum(p.invested for p in pos)
        priced = [p for p in pos if p.market_value is not None]
        out.market_value = sum(p.market_value or 0.0 for p in priced)
        out.unrealised = sum(p.unrealised or 0.0 for p in priced)
        if out.invested:
            out.unrealised_pct = out.unrealised / out.invested * 100
        out.realised = realised_pnl(conn, since=since)
        out.fees_paid = float(conn.execute(
            "SELECT COALESCE(SUM(fees),0) AS f FROM transactions"
        ).fetchone()["f"])
        day = [p.day_change_pct for p in priced if p.day_change_pct is not None]
        if day:
            # mypy: priced filters None day_change_pct, but market_value still float | None
            out.day_change = sum((p.market_value or 0.0) * p.day_change_pct / 100
                                 for p in priced if p.day_change_pct is not None)

        unpriced = [p.symbol for p in pos if p.market_value is None]
        if unpriced:
            out.notes.append(
                f"no price for {', '.join(unpriced)}; their value is excluded "
                "from the totals rather than assumed unchanged"
            )
        if benchmark:
            out.benchmark = benchmark_performance(conn, cfg)
        return out
    finally:
        if own:
            conn.close()


def benchmark_performance(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Portfolio return against the index, over the holding period.

    Refuses rather than guessing. Without a confirmed index identity, any number
    here would be measured against the wrong series and would look authoritative.
    """
    index_code = cfg.get("universe.benchmark")
    wire = cfg.get("universe.benchmark_wire")
    if not wire:
        return {
            "available": False,
            "index": index_code,
            "reason": "benchmark wire code unconfirmed; relative performance is "
                      "not measured against an unidentified index",
        }
    row = conn.execute(
        "SELECT MIN(date) AS first, MAX(date) AS last FROM price_benchmark "
        "WHERE index_code = ?", (index_code,)
    ).fetchone()
    if not row or not row["first"]:
        return {"available": False, "index": index_code,
                "reason": "no benchmark bars stored; run stocks sync --full"}
    return {
        "available": True,
        "index": index_code,
        "wire": wire,
        "first_date": row["first"],
        "last_date": row["last"],
    }


def render_pnl(
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    cfg: Config | None = None,
    since: str | None = None,
    benchmark: bool = False,
) -> str:
    r = pnl(db_path=db_path, conn=conn, cfg=cfg, since=since, benchmark=benchmark)
    lines: list[str] = []
    lines.append(f"{len(r.positions)} open position(s)")
    for p in r.positions:
        px = f"{p.price:.1f}" if p.price is not None else "no price"
        uv = f"{p.unrealised:+.0f}" if p.unrealised is not None else "n/a"
        pct = f"{p.unrealised_pct:+.1f}%" if p.unrealised_pct is not None else "n/a"
        lines.append(f"  {p.symbol:<10} {p.qty:>6} @ {p.avg_cost:>8.1f}  "
                     f"now {px:>10}  {uv:>10} ({pct})")
    lines.append("")
    lines.append(f"invested {r.invested:,.0f}  value {r.market_value:,.0f}  "
                 f"unrealised {r.unrealised:+,.0f}"
                 + (f" ({r.unrealised_pct:+.1f}%)" if r.unrealised_pct is not None else ""))
    if since:
        lines.append(f"realised since {since}: {r.realised:+,.0f}")
    elif r.realised:
        lines.append(f"realised: {r.realised:+,.0f}")
    lines.append(f"fees paid: {r.fees_paid:,.0f}")
    if r.benchmark:
        if r.benchmark.get("available"):
            lines.append(f"benchmark {r.benchmark['index']}: "
                         f"{r.benchmark['first_date']} to {r.benchmark['last_date']}")
        else:
            lines.append(f"benchmark unavailable: {r.benchmark['reason']}")
    for note in r.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)