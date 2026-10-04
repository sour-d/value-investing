"""Judgment layer: watchlist, integrity attestation, thesis, and `stocks why`.

The quantitative screen can only say a business looks sound on ratios it can
compute. Everything that decides whether to actually own it — is the moat
narrowing, is the auditor clean, what would prove me wrong — lives here, and
none of it is available from any provider.

Two rules shape the design. Absence is never a pass: an unattested check or an
unrecorded assessment is shown as `unknown`, because a blank that reads as fine
is the failure mode that matters. And a recorded judgement is dated, so a
six-month-old thesis does not masquerade as current.

`why` is the command that makes a past decision reviewable. It assembles what
was claimed, what was measured, and what has changed since, and it separates
what the data supports from what a human asserted.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from . import db, portfolio
from .config import Config, load as load_config
from .portfolio import PortfolioError, _require_symbol, _now

# Ordered so the strongest rejection is reported first: "avoid" outranks "watch".
STATUS_ORDER = ("held", "buy", "researching", "watch", "avoid", "done")
VERDICTS = ("preferred", "watch", "avoid")
DIMENSIONS = ("moat_type", "moat_trend", "pricing_power", "business_model",
              "mgmt_integrity", "capital_allocation")


def _reject_unknown(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise PortfolioError(f"{what} must be one of {', '.join(allowed)}, got {value!r}")
    return value


# ────────────────────────────────────────────────────────────── watchlist ────


def add_watch(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    buy_line: float | None = None,
    fv_low: float | None = None,
    fv_high: float | None = None,
    status: str = "watch",
    thesis_path: str | None = None,
    entry_reason: str | None = None,
) -> str:
    """Add or update a watchlist entry.

    Upsert rather than insert, because re-adding a name with a revised buy-line
    is routine research maintenance and should not fail as a duplicate.
    """
    symbol = _require_symbol(conn, symbol)
    status = _reject_unknown(status, STATUS_ORDER, "status")
    now = _now()
    today = now[:10]
    conn.execute(
        "INSERT INTO watchlist (symbol,added_on,buy_line,fv_low,fv_high,margin_of_safety,"
        "status,thesis_path,entry_reason,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET "
        "  buy_line=excluded.buy_line, fv_low=excluded.fv_low, fv_high=excluded.fv_high,"
        "  margin_of_safety=excluded.margin_of_safety, status=excluded.status,"
        "  thesis_path=excluded.thesis_path, entry_reason=excluded.entry_reason,"
        "  updated_at=excluded.updated_at",
        (symbol, today, buy_line, fv_low, fv_high, _mos(fv_low, buy_line),
         status, thesis_path, entry_reason, now),
    )
    conn.commit()
    return symbol


def _mos(fv_low: float | None, buy_line: float | None) -> float | None:
    """Margin of safety: how far below fair value the buy-line sits.

    Only computed when both numbers exist. Deriving it on write means it cannot
    drift when either input is revised later.
    """
    if not fv_low or not buy_line or buy_line <= 0:
        return None
    return round((fv_low - buy_line) / fv_low * 100, 2)


def remove_watch(conn: sqlite3.Connection, symbol: str) -> bool:
    symbol = _require_symbol(conn, symbol)
    cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    conn.commit()
    if not cur.rowcount:
        raise PortfolioError(f"{symbol} is not on the watchlist")
    return True


def set_status(conn: sqlite3.Connection, symbol: str, status: str) -> str:
    symbol = _require_symbol(conn, symbol)
    status = _reject_unknown(status, STATUS_ORDER, "status")
    cur = conn.execute(
        "UPDATE watchlist SET status = ?, updated_at = ? WHERE symbol = ?",
        (status, _now(), symbol),
    )
    if not cur.rowcount:
        raise PortfolioError(f"{symbol} is not on the watchlist; add it first")
    conn.commit()
    return symbol


def watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All entries, ordered by how much they still matter.

    Sorted in Python rather than SQL. A CASE expression needs its status
    literals spliced into the statement text, which is not worth the injection
    surface for a list this small — and it keeps the ordering rules readable
    next to STATUS_ORDER instead of buried in a query string.
    """
    rows = [dict(r) for r in conn.execute("SELECT * FROM watchlist").fetchall()]
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        px = conn.execute(
            "SELECT close FROM price_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (d["symbol"],),
        ).fetchone()
        d["price"] = px["close"] if px else None
        # Distance to the buy-line is the number that drives action, so it is
        # computed on read rather than trusted from whenever the entry was made.
        d["to_buy_line_pct"] = (
            (d["price"] - d["buy_line"]) / d["buy_line"] * 100
            if d["price"] and d["buy_line"] else None
        )
        out.append(d)
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    out.sort(key=lambda d: (rank.get(d["status"], len(rank)), d["symbol"]))
    return out


def render_watchlist(conn: sqlite3.Connection) -> str:
    rows = watchlist(conn)
    if not rows:
        return "watchlist is empty"
    lines: list[str] = []
    for d in rows:
        px = f"{d['price']:.1f}" if d["price"] else "no price"
        if d["buy_line"] and d["to_buy_line_pct"] is not None:
            gap = f"  buy-line {d['buy_line']:.1f} ({d['to_buy_line_pct']:+.1f}%)"
        else:
            gap = "  no buy-line"
        mos = f"  MoS {d['margin_of_safety']:.0f}%" if d["margin_of_safety"] else ""
        lines.append(f"{d['status']:<12} {d['symbol']:<10} {px:>10}{gap}{mos}")
    return "\n".join(lines)


# ───────────────────────────────────────────────────── integrity (Gate 5) ────


def attest(
    conn: sqlite3.Connection,
    cfg: Config,
    symbol: str,
    check_name: str,
    status: str,
    evidence: str | None = None,
) -> None:
    """Record one Gate 5 check.

    `verified` requires evidence. This is the whole integrity of the gate: a
    bare `verified` with nothing behind it is indistinguishable from a guess, and
    it is the exact failure the gate exists to prevent.
    """
    symbol = _require_symbol(conn, symbol)
    status = _reject_unknown(status, ("verified", "failed", "unknown"), "status")
    if status == "verified" and not (evidence and evidence.strip()):
        raise PortfolioError(
            f"evidence is required to mark {check_name} verified for {symbol}. "
            "Name the filing, page or disclosure you checked, so this can be "
            "re-examined later. Use 'unknown' if you have not checked it yet — "
            "that is an honest answer and it keeps the buy blocked."
        )
    required = cfg.get("gate5.integrity.required_checks") or []
    if required and check_name not in required:
        raise PortfolioError(
            f"{check_name!r} is not a configured Gate 5 check. "
            f"Required: {', '.join(required)}. Add it in screen.toml "
            "[gate5.integrity] if it should gate purchases."
        )
    conn.execute(
        "INSERT INTO integrity_check (symbol,check_name,status,evidence,verified_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(symbol,check_name) DO UPDATE SET "
        "  status=excluded.status, evidence=excluded.evidence, "
        "  verified_at=excluded.verified_at",
        (symbol, check_name, status,
         evidence.strip() if evidence else None,
         _now() if status == "verified" else None),
    )
    conn.commit()


def render_integrity(conn: sqlite3.Connection, cfg: Config, symbol: str) -> str:
    ledger = portfolio.integrity_ledger(conn, cfg, symbol.upper())
    if not ledger:
        return f"{symbol.upper()}: no Gate 5 checks configured"
    mark = {"verified": "ok  ", "failed": "FAIL", "unknown": "?   "}
    lines = [f"{symbol.upper()} — Gate 5"]
    for d in ledger:
        tag = "" if d["required"] else " (extra)"
        lines.append(f"  [{mark[d['status']]}] {d['check_name']}{tag}")
        if d["evidence"]:
            lines.append(f"           {d['evidence']}")
    blocking = [d["check_name"] for d in ledger
                if d["required"] and d["status"] != "verified"]
    if blocking:
        lines.append("")
        lines.append(f"buy blocked: {len(blocking)} check(s) not verified — "
                     + ", ".join(blocking))
        lines.append(f"  stocks integrity {symbol.upper()} --set CHECK verified "
                     "--evidence \"...\"")
    else:
        lines.append("")
        lines.append("all required checks verified")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────── the thesis ────


def record_verdict(
    conn: sqlite3.Connection, symbol: str, verdict: str, reason: str,
    evidence: str | None = None,
) -> str:
    symbol = _require_symbol(conn, symbol)
    verdict = _reject_unknown(verdict, VERDICTS, "verdict")
    if not reason or not reason.strip():
        raise PortfolioError("a verdict needs a reason; an unexplained call is not reviewable")
    as_of = _now()[:10]
    conn.execute(
        "INSERT INTO research_verdict (symbol,as_of,verdict,reason,evidence,reviewer) "
        "VALUES (?,?,?,?,?,'sour-d') "
        "ON CONFLICT(symbol,as_of) DO UPDATE SET "
        "  verdict=excluded.verdict, reason=excluded.reason, evidence=excluded.evidence",
        (symbol, as_of, verdict, reason.strip(), evidence),
    )
    conn.commit()
    return as_of


def record_assessment(
    conn: sqlite3.Connection, symbol: str, dimension: str, assessment: str,
    rationale: str,
) -> str:
    """Record one framework dimension.

    Every dimension is in `unmeasurable` because no feed supplies it, so this is
    always a human judgement with a written rationale.
    """
    symbol = _require_symbol(conn, symbol)
    dimension = _reject_unknown(dimension, DIMENSIONS, "dimension")
    if not rationale.strip():
        raise PortfolioError("a qualitative assessment needs a rationale")
    as_of = _now()[:10]
    conn.execute(
        "INSERT INTO qualitative_assessment (symbol,dimension,assessment,rationale,as_of) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(symbol,dimension,as_of) DO UPDATE SET "
        "  assessment=excluded.assessment, rationale=excluded.rationale",
        (symbol, dimension, assessment.strip(), rationale.strip(), as_of),
    )
    conn.commit()
    return as_of


# ─────────────────────────────────────────────────────────────── stocks why ────


def why_payload(symbol: str, db_path: str | None = None,
                cfg: Config | None = None,
                conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Assemble the full record for one name, split by provenance.

    Three sources are kept apart on purpose: what the data says, what a human
    decided, and what has changed since the decision was recorded. Merging them
    would make a six-month-old opinion look current.
    """
    from . import metrics as M
    from . import screen, screencmd

    cfg = cfg or load_config()
    own = conn is None
    conn = conn or db.connect(db_path)
    if own:
        db.apply_schema(conn)
    try:
        symbol = _require_symbol(conn, symbol)
        verdict = screen.evaluate_symbol(
            conn, symbol, cfg,
            risk_free=cfg.get("valuation.inputs.risk_free_rate"),
            erp=cfg.get("valuation.inputs.equity_risk_premium"),
        )
        m = M.compute(conn, symbol,
                      risk_free_rate=cfg.get("valuation.inputs.risk_free_rate"),
                      equity_risk_premium=cfg.get("valuation.inputs.equity_risk_premium"))
        watch = conn.execute("SELECT * FROM watchlist WHERE symbol = ?",
                             (symbol,)).fetchone()
        latest_verdict = conn.execute(
            "SELECT * FROM research_verdict WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        assessments = [
            dict(r) for r in conn.execute(
                "SELECT dimension,assessment,rationale,as_of FROM qualitative_assessment "
                "WHERE symbol = ? ORDER BY as_of DESC, dimension", (symbol,))
        ]
        trades = [dict(r) for r in conn.execute(
            "SELECT ts,side,qty,price,fees,reason,integrity_override FROM transactions "
            "WHERE symbol = ? ORDER BY ts, id", (symbol,))]
        unverified, _ = portfolio.integrity_status(conn, cfg, symbol)

        # Framework dimensions with no recorded assessment. Missing means
        # unreviewed, not "fine".
        by_dim = {a["dimension"]: a for a in assessments}
        missing = [d for d in DIMENSIONS if d not in by_dim]

        return {
            "symbol": symbol,
            "name": m.name,
            "measured": {
                "tier": verdict.tier, "quality_score": verdict.quality_score,
                "clean": verdict.clean, "blocked": verdict.blocked,
                "gates": [{"gate": g.gate, "name": g.name, "passed": g.passed,
                           "blocking": g.blocking, "value": g.value,
                           "threshold": g.threshold, "reason": g.reason}
                          for g in verdict.gates],
                "metrics": {"roe": m.roe, "roic": m.roic, "wacc": m.wacc,
                            "roic_spread": m.roic_spread, "ebitda_margin": m.ebitda_margin,
                            "pe_avg": m.pe_avg, "ev_ebitda": m.ev_ebitda,
                            "fcf_yield": m.fcf_yield,
                            "net_debt_to_equity": m.net_debt_to_equity,
                            "interest_coverage": m.interest_coverage,
                            "ocf_conv": m.ocf_conv, "accruals": m.accruals},
                "missing_items": list(m.missing_items),
            },
            "claimed": {
                "watchlist": dict(watch) if watch else None,
                "verdict": dict(latest_verdict) if latest_verdict else None,
                "assessments": assessments,
            },
            "unassessed_dimensions": missing,
            "integrity": {
                "unverified": unverified,
                "buy_allowed": not unverified,
                "checks": portfolio.integrity_ledger(conn, cfg, symbol),
            },
            "trades": trades,
            "holding_qty": portfolio.held_qty(conn, symbol),
        }
    finally:
        if own:
            conn.close()


def render_why(payload: dict[str, Any]) -> str:
    sym = payload["symbol"]
    lines = [f"{sym} — {payload['name'] or 'unknown'}"]
    m = payload["measured"]
    if m["blocked"]:
        lines.append(f"BLOCKED: {m['blocked']}")
        return "\n".join(lines)

    lines.append(f"tier {m['tier']} · quality {m['quality_score']} · "
                 f"{'clears all gates' if m['clean'] else 'fails at least one gate'}")

    lines.append("")
    lines.append("measured:")
    for label, key, fmt in (("ROIC", "roic", "{:.1f}%"), ("WACC", "wacc", "{:.1f}%"),
                            ("spread", "roic_spread", "{:+.1f}pp"),
                            ("ROE", "roe", "{:.1f}%"),
                            ("EBITDA margin", "ebitda_margin", "{:.1f}%"),
                            ("PE", "pe_avg", "{:.1f}"),
                            ("EV/EBITDA", "ev_ebitda", "{:.1f}"),
                            ("FCF yield", "fcf_yield", "{:.1f}%"),
                            ("net debt/equity", "net_debt_to_equity", "{:.2f}")):
        v = m["metrics"].get(key)
        lines.append(f"  {label:<18} {fmt.format(v) if v is not None else 'n/a'}")
    failed = [g for g in m["gates"] if not g["passed"] and g["blocking"]]
    for g in failed:
        lines.append(f"  FAILS {g['name']}: {g['reason']}")

    c = payload["claimed"]
    lines.append("")
    if c["verdict"]:
        v = c["verdict"]
        lines.append(f"verdict ({v['as_of']}): {v['verdict'].upper()}")
        lines.append(f"  {v['reason']}")
        if v["evidence"]:
            lines.append(f"  evidence: {v['evidence']}")
    else:
        lines.append("verdict: none recorded — this name has no human verdict")

    if c["watchlist"]:
        w = c["watchlist"]
        bl = f"buy-line {w['buy_line']:.1f}" if w["buy_line"] else "no buy-line"
        lines.append(f"watchlist: {w['status']} · {bl}"
                     + (f" · fair value {w['fv_low']:.0f}-{w['fv_high']:.0f}"
                        if w["fv_low"] and w["fv_high"] else ""))
    else:
        lines.append("watchlist: not tracked")

    if c["assessments"]:
        lines.append("")
        lines.append("qualitative (human, not from any feed):")
        for a in c["assessments"]:
            lines.append(f"  {a['dimension']:<20} {a['assessment']}")
    missing = payload["unassessed_dimensions"]
    if missing:
        lines.append("")
        lines.append("unassessed (unknown, not 'fine'): " + ", ".join(missing))

    integ = payload["integrity"]
    lines.append("")
    if integ["buy_allowed"]:
        lines.append("integrity: all required checks verified")
    else:
        lines.append(f"integrity: BUY BLOCKED — {len(integ['unverified'])} unverified: "
                     + ", ".join(integ["unverified"]))

    if payload["trades"]:
        lines.append("")
        lines.append("transactions:")
        for t in payload["trades"]:
            lines.append(f"  {t['ts'][:10]} {t['side']} {t['qty']} @ {t['price']:.1f} "
                         f"— {t['reason']}")
            if t["integrity_override"]:
                lines.append(f"    integrity override: {t['integrity_override']}")

    if payload["holding_qty"]:
        lines.append("")
        lines.append(f"holding {payload['holding_qty']} share(s)")
    return "\n".join(lines)