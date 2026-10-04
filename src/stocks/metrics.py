"""Metrics derived from stored raw fundamentals.

Every function here is pure: raw vendor values in, number out. Nothing is
cached as an input. That is deliberate — fixing a formula here recomputes all
history instead of leaving stale numbers in the database.

Two conventions are load-bearing:

* A series is ``[(fy_end, value), ...]`` ordered oldest to newest, with ``None``
  values dropped. ``fy_end`` is a period label, never a position, so a missing
  fiscal year is a gap rather than a shift.
* Item names have documented fallbacks because the vendor renames them between
  versions and filings.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

Series = list[tuple[str, float]]

# Item name -> ordered fallbacks. First name with data wins.
ITEMS = {
    "net_income": ("Net Income Common Stockholders", "Net Income"),
    "revenue": ("Total Revenue", "Operating Revenue"),
    "ebitda": ("EBITDA",),
    "ebit": ("EBIT",),
    "pretax_income": ("Pretax Income",),
    "tax_provision": ("Tax Provision",),
    "interest_expense": ("Interest Expense", "Interest Expense Non Operating"),
    "ocf": ("Operating Cash Flow", "Total Cash From Operating Activities"),
    "capex": ("Capital Expenditure",),
    "fcf": ("Free Cash Flow",),
    "d_and_a": ("Depreciation And Amortization",),
    "change_in_wc": ("Change In Working Capital",),
    "receivables": ("Accounts Receivable",),
    "inventory": ("Inventory",),
    "change_in_receivables": ("Change In Receivables",),
    "total_assets": ("Total Assets",),
    "equity": ("Stockholders Equity",),
    "total_debt": ("Total Debt", "Long Term Debt"),
    "cash": ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
    "invested_capital": ("Invested Capital", "Total Capitalization"),
    "goodwill_intangibles": ("Goodwill And Other Intangible Assets",),
}

STATEMENT_OF = {
    **{k: "income" for k in (
        "net_income", "revenue", "ebitda", "ebit", "pretax_income",
        "tax_provision", "interest_expense")},
    **{k: "cashflow" for k in (
        "ocf", "capex", "fcf", "d_and_a", "change_in_wc", "change_in_receivables")},
    **{k: "balance" for k in (
        "receivables", "inventory", "total_assets", "equity", "total_debt",
        "cash", "invested_capital", "goodwill_intangibles")},
}


def num(v: object) -> float | None:
    """Coerce to float, mapping anything unusable to None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def pos_frac(values: Iterable[float | None]) -> float | None:
    """Fraction of periods with a strictly positive value."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v > 0) / len(vals)


# ────────────────────────────────────────────────────────── data access ────


def load_statement(
    conn: sqlite3.Connection, symbol: str, statement: str
) -> dict[str, dict[str, float]]:
    """Return ``{fy_end: {canonical_item: value}}`` for one statement.

    Fallback names are resolved once per symbol: the first name that yields any
    value wins, matching the vendor's own precedence.
    """
    rows = conn.execute(
        "SELECT fy_end, item, value FROM fundamentals_annual "
        "WHERE symbol = ? AND statement = ? AND value IS NOT NULL",
        (symbol, statement),
    ).fetchall()

    by_period: dict[str, dict[str, float]] = {}
    present: dict[str, set[str]] = {}
    for r in rows:
        by_period.setdefault(r["fy_end"], {})[r["item"]] = r["value"]
        present.setdefault(r["item"], set()).add(r["fy_end"])

    resolved: dict[str, str] = {}
    for key, names in ITEMS.items():
        if STATEMENT_OF.get(key) != statement:
            continue
        for name in names:
            if present.get(name):
                resolved[key] = name
                break

    out: dict[str, dict[str, float]] = {}
    for period, items in by_period.items():
        row: dict[str, float] = {}
        for key, name in resolved.items():
            v = num(items.get(name))
            if v is not None:
                row[key] = v
        if row:
            out[period] = row
    return dict(sorted(out.items()))


def series(stmt: dict[str, dict[str, float]], key: str) -> Series:
    """Ordered (period, value) list for one canonical item."""
    return [(p, row[key]) for p, row in stmt.items() if key in row]


def period_labels(stmt: dict[str, dict[str, float]]) -> list[str]:
    return sorted(stmt)


# ─────────────────────────────────────────────────────────── metrics ────


@dataclass
class Metrics:
    symbol: str
    n_periods_income: int = 0
    n_periods_balance: int = 0
    n_periods_cashflow: int = 0
    periods_income: tuple[str, ...] = ()
    periods_balance: tuple[str, ...] = ()
    periods_cashflow: tuple[str, ...] = ()
    is_financial: bool = False

    ni_avg: float | None = None
    rev_avg: float | None = None
    ebitda_avg: float | None = None
    ocf_avg: float | None = None
    fcf_avg: float | None = None
    eq_avg: float | None = None
    debt_avg: float | None = None
    cash_avg: float | None = None
    invcap_avg: float | None = None
    intx_avg: float | None = None
    pretax_avg: float | None = None
    tax_avg: float | None = None
    ta_avg: float | None = None

    # ratios (percent unless noted)
    ni_pos: float | None = None
    ocf_pos: float | None = None
    fcf_pos: float | None = None
    ocf_conv: float | None = None
    fcf_conv: float | None = None
    accruals: float | None = None
    roe: float | None = None
    roa: float | None = None
    ebitda_margin: float | None = None
    net_margin: float | None = None
    net_debt_to_equity: float | None = None
    interest_coverage: float | None = None
    debt_to_equity: float | None = None
    roic: float | None = None
    wacc: float | None = None
    roic_spread: float | None = None
    owner_earnings: float | None = None
    recv_rev_ratio: float | None = None
    npm_change: float | None = None
    intangibles_pct: float | None = None

    # valuation (percent unless noted)
    pe_avg: float | None = None
    ev_ebitda: float | None = None
    fcf_yield: float | None = None
    pb: float | None = None
    div_yield: float | None = None

    # vendor profile context (untrusted)
    price: float | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    vendor_debt_to_equity: float | None = None
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    missing_items: tuple[str, ...] = field(default_factory=tuple)

    def periods(self) -> tuple[list[str], int]:
        """Every fiscal period seen in any statement, and the shallowest count."""
        allp = sorted(set(self.periods_income) | set(self.periods_balance)
                      | set(self.periods_cashflow))
        depth = min(self.n_periods_income, self.n_periods_balance, self.n_periods_cashflow)
        return allp, depth


_FIN_KWARKS = ("bank", "finance", "financial", "insurance", "asset management",
               "capital market", "credit", "microfinance", "housing finance", "wealth")


def classify_financial(sector: str | None, industry: str | None) -> bool:
    """Heuristic classification.

    Decides which gates apply, so a misclassification produces confidently wrong
    numbers. Universe rows record `classification_src` so the guess stays visible
    and can be overridden by hand.
    """
    if sector and sector.strip() == "Financial Services":
        return True
    ind = (industry or "").lower()
    return any(k in ind for k in _FIN_KWARKS)


def compute(
    conn: sqlite3.Connection,
    symbol: str,
    risk_free_rate: float | None = None,
    equity_risk_premium: float | None = None,
) -> Metrics:
    """Compute every metric for one symbol from stored raw values."""
    m = Metrics(symbol=symbol)

    inc = load_statement(conn, symbol, "income")
    bal = load_statement(conn, symbol, "balance")
    cfs = load_statement(conn, symbol, "cashflow")

    m.periods_income = tuple(period_labels(inc))
    m.periods_balance = tuple(period_labels(bal))
    m.periods_cashflow = tuple(period_labels(cfs))
    m.n_periods_income = len(inc)
    m.n_periods_balance = len(bal)
    m.n_periods_cashflow = len(cfs)

    ni, rev = series(inc, "net_income"), series(inc, "revenue")
    ebitda, ebit = series(inc, "ebitda"), series(inc, "ebit")
    pretax, tax = series(inc, "pretax_income"), series(inc, "tax_provision")
    intx = series(inc, "interest_expense")

    ocf, fcf = series(cfs, "ocf"), series(cfs, "fcf")
    capex, dna = series(cfs, "capex"), series(cfs, "d_and_a")
    d_wc = series(cfs, "change_in_wc")

    eq, ta = series(bal, "equity"), series(bal, "total_assets")
    debt, cash = series(bal, "total_debt"), series(bal, "cash")
    invcap = series(bal, "invested_capital")
    recv, gw = series(bal, "receivables"), series(bal, "goodwill_intangibles")

    def mean(s: Sequence[tuple[str, float]]) -> float | None:
        return avg([v for _, v in s])

    m.ni_avg, m.rev_avg = mean(ni), mean(rev)
    m.ebitda_avg, m.ocf_avg, m.fcf_avg = mean(ebitda), mean(ocf), mean(fcf)
    m.eq_avg, m.ta_avg = mean(eq), mean(ta)
    m.debt_avg, m.cash_avg, m.invcap_avg = mean(debt), mean(cash), mean(invcap)
    m.intx_avg, m.pretax_avg, m.tax_avg = mean(intx), mean(pretax), mean(tax)

    vals = lambda s: [v for _, v in s]  # noqa: E731

    m.ni_pos = pos_frac(vals(ni))
    m.ocf_pos = pos_frac(vals(ocf))
    m.fcf_pos = pos_frac(vals(fcf)) if fcf else None
    m.ocf_conv = safe_div(m.ocf_avg, m.ni_avg)
    m.fcf_conv = safe_div(m.fcf_avg, m.ni_avg)
    m.accruals = safe_div(
        (m.ni_avg - m.ocf_avg) if (m.ni_avg is not None and m.ocf_avg is not None) else None,
        m.ta_avg,
    )

    m.roe = pct(safe_div(m.ni_avg, m.eq_avg))
    m.roa = pct(safe_div(m.ni_avg, m.ta_avg))
    m.ebitda_margin = pct(safe_div(m.ebitda_avg, m.rev_avg))
    m.net_margin = pct(safe_div(m.ni_avg, m.rev_avg))
    m.debt_to_equity = safe_div(m.debt_avg, m.eq_avg)
    m.net_debt_to_equity = safe_div(
        (m.debt_avg - m.cash_avg) if (m.debt_avg is not None and m.cash_avg is not None) else None,
        m.eq_avg,
    )
    m.interest_coverage = safe_div(m.ebitda_avg, m.intx_avg)

    # Owner earnings. Maintenance capex is not separable from growth capex in
    # any vendor feed, so this uses total capex and is an approximation — see
    # the [unmeasurable] block in screen.toml. Each addend is optional; a
    # missing line reduces the estimate rather than breaking the calculation.
    if m.ni_avg is not None and (capex or fcf):
        dna_avg = avg(vals(dna))
        capex_avg = avg(vals(capex))
        if capex_avg is not None:
            # Vendor capex is reported as a negative outflow.
            oe = m.ni_avg + (dna_avg or 0.0) + capex_avg
            m.owner_earnings = oe
        else:
            m.owner_earnings = m.fcf_avg

    # ROIC and WACC. Invested capital is computed here per the framework's own
    # formula rather than trusting the vendor's Invested Capital field.
    m.roic = roic_pct(ebit, pretax, tax, debt, eq, cash, invcap)
    if risk_free_rate is not None and equity_risk_premium is not None:
        m.wacc = wacc_pct(risk_free_rate, equity_risk_premium, intx, debt, eq, pretax, tax)
        if m.roic is not None and m.wacc is not None:
            m.roic_spread = m.roic - m.wacc

    # Red-flag inputs
    if len(recv) >= 2 and len(rev) >= 2 and recv[0][1] > 0 and rev[0][1] > 0:
        m.recv_rev_ratio = (recv[-1][1] / recv[0][1]) / (rev[-1][1] / rev[0][1])
    if len(ni) >= 2 and len(rev) >= 2 and rev[0][1] > 0:
        m.npm_change = ((ni[-1][1] / rev[-1][1]) - (ni[0][1] / rev[0][1])) * 100
    m.intangibles_pct = safe_div(avg(vals(gw)), m.ta_avg)

    _apply_profile(conn, m)
    m.is_financial = classify_financial(m.sector, m.industry)
    m.missing_items = tuple(
        k for k in ITEMS
        if k not in ("net_income", "revenue", "ocf", "equity", "total_assets")
        and not (series(inc, k) or series(bal, k) or series(cfs, k))
    )
    return m


def pct(x: float | None) -> float | None:
    return None if x is None else x * 100


def roic_pct(
    ebit: Sequence[tuple[str, float]],
    pretax: Sequence[tuple[str, float]],
    tax: Sequence[tuple[str, float]],
    debt: Sequence[tuple[str, float]],
    equity: Sequence[tuple[str, float]],
    cash: Sequence[tuple[str, float]],
    invcap_vendor: Sequence[tuple[str, float]],
) -> float | None:
    """ROIC = NOPAT / Invested Capital, in percent.

    NOPAT is EBIT scaled by the effective tax rate, so a loss-making year does
    not produce a flattering operating margin. Invested capital is computed as
    debt + equity - cash per the framework formula; the vendor field is used
    only as a fallback when the balance-sheet components are missing.
    """
    ebit_a = avg([v for _, v in ebit])
    pretax_a = avg([v for _, v in pretax])
    tax_a = avg([v for _, v in tax])
    if ebit_a is None:
        return None

    eff_tax = None
    if pretax_a and tax_a is not None and pretax_a > 0:
        eff_tax = min(max(tax_a / pretax_a, 0.0), 0.6)  # clamp: outliers distort
    nopat = ebit_a * (1 - (eff_tax or 0.0))

    d, e, c = (avg([v for _, v in s]) for s in (debt, equity, cash))
    ic = None
    if d is not None and e is not None and c is not None:
        ic = d + e - c
    if not ic:
        ic = avg([v for _, v in invcap_vendor])
    if not ic:
        return None
    return (nopat / ic) * 100


def wacc_pct(
    risk_free: float,
    erp: float,
    interest_expense: Sequence[tuple[str, float]],
    debt: Sequence[tuple[str, float]],
    equity: Sequence[tuple[str, float]],
    pretax: Sequence[tuple[str, float]],
    tax: Sequence[tuple[str, float]],
) -> float | None:
    """Weighted average cost of capital, in percent.

    Cost of debt is derived as interest expense / total debt rather than taken
    from a vendor field.
    """
    d, e = avg([v for _, v in debt]), avg([v for _, v in equity])
    if d is None or e is None or (d + e) <= 0:
        return None
    intx = avg([v for _, v in interest_expense])
    kd_pretax = safe_div(intx, d)
    if kd_pretax is None or not (0 < kd_pretax < 1):
        return None

    pretax_a, tax_a = avg([v for _, v in pretax]), avg([v for _, v in tax])
    eff_tax = 0.0
    if pretax_a and tax_a is not None and pretax_a > 0:
        eff_tax = min(max(tax_a / pretax_a, 0.0), 0.6)

    we, wd = e / (d + e), d / (d + e)
    return 100 * (we * (risk_free + erp) + wd * kd_pretax * (1 - eff_tax))


def _apply_profile(conn: sqlite3.Connection, m: Metrics) -> None:
    """Attach vendor profile context. These are untrusted claims."""
    row = conn.execute(
        "SELECT * FROM profile_snapshot WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
        (m.symbol,),
    ).fetchone()
    if row is None:
        return
    m.price = num(row["current_price"])
    m.market_cap = num(row["market_cap"])
    m.enterprise_value = num(row["enterprise_value"])
    m.vendor_debt_to_equity = num(row["debt_to_equity"])
    m.div_yield = num(row["dividend_yield"])
    pb = num(row["price_to_book"])
    if pb is not None:
        m.pb = pb

    u = conn.execute("SELECT * FROM universe WHERE symbol = ?", (m.symbol,)).fetchone()
    if u is not None:
        m.name = u["name"]
        m.sector = u["sector"]
        m.industry = u["industry"]

    m.pe_avg = safe_div(m.market_cap, m.ni_avg)
    m.ev_ebitda = safe_div(m.enterprise_value, m.ebitda_avg)
    m.fcf_yield = pct(safe_div(m.fcf_avg, m.market_cap))