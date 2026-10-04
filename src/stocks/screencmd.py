"""Screen command: run the gates, persist the result, render the delta.

The report is delta-first and deliberately short. A screen that prints 251 rows
gets skimmed; one that prints "3 new, 2 gone, and here is what changed" gets read.

Comparing two runs is only meaningful under the same thresholds, so a
``config_hash`` mismatch blocks entrant/exit output rather than reporting a
threshold change as a fundamental change.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config as config_mod
from . import db, metrics as M, screen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ScreenResult:
    run_id: int
    config_hash: str
    n_clean: int
    universe_size: int
    passers: list[str] = field(default_factory=list)
    entrants: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    comparable: bool = True
    note: str | None = None
    unmeasurable: tuple[str, ...] = ()
    variant: str = "roic"
    # Populated only when the profitability variant itself changed. Comparing
    # across variants is normally refused, so this is the one case where the
    # difference is the point rather than a reason to distrust the numbers.
    variant_delta: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "variant": self.variant,
            "n_clean": self.n_clean,
            "universe_size": self.universe_size,
            "passers": self.passers,
            "entrants": self.entrants,
            "exits": self.exits,
            "comparable": self.comparable,
            "note": self.note,
            "unmeasurable": list(self.unmeasurable),
            "variant_delta": self.variant_delta,
        }

    def render(self) -> str:
        lines: list[str] = []
        head = f"run #{self.run_id} · {self.n_clean}/{self.universe_size} clean"
        lines.append(head)
        lines.append(f"config {self.config_hash[:12]}")

        if not self.comparable:
            lines.append(f"no delta: {self.note}")
            if self.variant_delta:
                d = self.variant_delta
                lines.append(f"profitability gate {d['from']} -> {d['to']}: "
                             f"{len(d['entered'])} in, {len(d['left'])} out, "
                             f"{len(d['stable'])} unchanged")
                for label, key in (("only passes " + d["to"], "entered"),
                                   ("only passed " + d["from"], "left")):
                    if d[key]:
                        lines.append(f"  {label}: {', '.join(d[key])}")
                lines.append("  (a gate change, not a change in the business)")
        elif not self.entrants and not self.exits:
            lines.append("no change since last run")
        else:
            if self.entrants:
                lines.append(f"NEW  {', '.join(self.entrants)}")
            if self.exits:
                lines.append(f"GONE {', '.join(self.exits)}")

        if self.passers:
            lines.append("clean: " + " ".join(self.passers))

        missing = [i for i in self.unmeasurable if i not in ()]
        if missing:
            lines.append(f"unmeasurable: {', '.join(missing)}")
        lines.append("passers are research candidates, not buys")
        return "\n".join(lines)


def run(
    full: bool = False,
    db_path: str | None = None,
    use_roic: bool | None = None,
) -> ScreenResult:
    """Evaluate the universe and persist a ``screen_run`` with its gate results."""
    cfg = config_mod.load()
    conn = db.connect(db_path)
    db.apply_schema(conn)
    try:
        return _run_locked(conn, cfg, full=full, use_roic=use_roic)
    finally:
        conn.close()


def _previous_run(
    conn: sqlite3.Connection, before_run: int
) -> tuple[set[str], str | None, str | None, bool]:
    """The most recent earlier run.

    Returns (clean symbols, config_hash, gate variant, whether any earlier run
    exists). The variant matters as much as the hash: a ROE run and a ROIC run
    apply different gates, so comparing them would report a threshold change as
    a fundamental change in the business.
    """
    row = conn.execute(
        "SELECT run_id, config_hash, notes FROM screen_run "
        "WHERE run_id < ? AND finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1",
        (before_run,),
    ).fetchone()
    if row is None:
        return set(), None, None, False

    # A symbol is clean when no BLOCKING check failed. Non-blocking failures are
    # recorded context, not rejections, so they must not be read as one.
    failed = {
        r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM gate_result "
            "WHERE run_id = ? AND passed = 0 AND blocking = 1",
            (row["run_id"],),
        )
    }
    seen = {
        r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM gate_result WHERE run_id = ?",
            (row["run_id"],),
        )
    }
    # A blocked symbol (no gate rows at all) is not clean either.
    return seen - failed, row["config_hash"], row["notes"], True


def variant_of(cfg: config_mod.Config, use_roic: bool | None = None) -> str:
    """Which profitability gate is in force.

    Resolved from config so the variant recorded in `screen_run.notes` cannot
    disagree with what was actually evaluated. `None` means "ask the config";
    an explicit bool overrides for back-to-back comparison.
    """
    if use_roic is None:
        use_roic = str(cfg.get("gate3.profitability.primary", "roic")).lower() == "roic"
    return "roic" if use_roic else "roe"


def _run_locked(
    conn: sqlite3.Connection, cfg: config_mod.Config, full: bool,
    use_roic: bool | None = None,
) -> ScreenResult:
    variant = variant_of(cfg, use_roic)
    use_roic = variant == "roic"
    verdicts = screen.evaluate_universe(conn, cfg, use_roic=use_roic)
    passers = sorted(v.symbol for v in verdicts if v.clean)
    universe_size = len(verdicts)

    started = _now()
    cur = conn.execute(
        "INSERT INTO screen_run (started_at,config_hash,config_json,universe_size,n_clean,notes) "
        "VALUES (?,?,?,?,?,?)",
        (started, cfg.config_hash,
         json.dumps(cfg.resolved(), sort_keys=True, default=str),
         universe_size, len(passers), variant),
    )
    run_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO gate_result "
        "(run_id,symbol,gate,check_name,passed,metric,value,threshold,reason,note,"
        "blocking) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, v.symbol, *g.as_row(), int(g.blocking))
         for v in verdicts for g in v.gates],
    )
    conn.execute("UPDATE screen_run SET finished_at = ? WHERE run_id = ?", (_now(), run_id))
    conn.commit()

    prev_pasers, prev_hash, prev_variant, has_previous = _previous_run(conn, run_id)
    result = ScreenResult(
        run_id=run_id,
        config_hash=cfg.config_hash,
        n_clean=len(passers),
        universe_size=universe_size,
        passers=passers,
        unmeasurable=tuple(cfg.get("unmeasurable.items", ()) or ()),
    )

    if not has_previous:
        result.note = "first recorded run; no baseline to compare against"
        result.comparable = False
    elif prev_variant != variant:
        # The gates differ, so an ordinary run-over-run delta would report a
        # threshold change as if the business had changed. That is exactly the
        # misreading to avoid — but when the variant flip IS the event under
        # review (the Phase 5 switch), naming the difference is the whole point.
        result.comparable = False
        result.variant_delta = {
            "from": prev_variant,
            "to": variant,
            "previous_passers": sorted(prev_pasers),
            "current_passers": list(passers),
            "entered": sorted(set(passers) - prev_pasers),
            "left": sorted(prev_pasers - set(passers)),
            "stable": sorted(set(passers) & prev_pasers),
            "note": (
                "the profitability gate changed, so this list shows what that "
                "choice did. It is not evidence that any business changed."
            ),
        }
        result.note = (
            f"previous run used the {prev_variant} gate, this one uses {variant}; "
            "different gates, so entrants/exits would be meaningless. "
            "See variant_delta for the effect of that change"
        )
    elif prev_hash and prev_hash != cfg.config_hash:
        result.comparable = False
        result.note = (
            f"thresholds changed since run (prev {prev_hash[:8]}, "
            f"now {cfg.config_hash[:8]}); entrants/exits would be meaningless"
        )
    else:
        result.entrants = sorted(set(passers) - prev_pasers)
        result.exits = sorted(prev_pasers - set(passers))
    return result


def explain(symbol: str, db_path: str | None = None) -> str:
    """Every gate for one symbol, with the observed value and the threshold."""
    cfg = config_mod.load()
    conn = db.connect(db_path)
    db.apply_schema(conn)
    try:
        # The valuation inputs must be passed in, or WACC is never computed and
        # the spread gate fails for a reason that has nothing to do with the
        # company being explained.
        verdict = screen.evaluate_symbol(
            conn, symbol.upper(), cfg,
            risk_free=cfg.get("valuation.inputs.risk_free_rate"),
            erp=cfg.get("valuation.inputs.equity_risk_premium"))
        m = M.compute(conn, symbol.upper(),
                      risk_free_rate=cfg.get("valuation.inputs.risk_free_rate"),
                      equity_risk_premium=cfg.get("valuation.inputs.equity_risk_premium"))
    finally:
        conn.close()

    lines = [f"{symbol.upper()} — {m.name or 'unknown'}"]
    if verdict.blocked:
        lines.append(f"BLOCKED: {verdict.blocked}")
        return "\n".join(lines)

    lines.append(f"tier {verdict.tier} · quality {verdict.quality_score} · "
                 f"{verdict.n_failed} failed gate(s)")
    for g in verdict.gates:
        mark = "pass" if g.passed else "FAIL"
        bits = [f"[{mark}]", g.gate, g.name]
        if g.value is not None:
            bits.append(f"= {g.value:.2f}")
        if g.threshold is not None and g.op:
            bits.append(f"(need {g.op} {g.threshold:g})")
        elif g.threshold is not None:
            bits.append(f"(ref {g.threshold:g})")
        if not g.blocking:
            bits.append("[secondary]")
        if g.reason:
            bits.append(f"({g.reason})")
        elif g.note:
            bits.append(f"({g.note})")
        lines.append("  " + " ".join(bits))

    lines.append("")
    lines.append(f"periods: income={m.n_periods_income} balance={m.n_periods_balance} "
                 f"cashflow={m.n_periods_cashflow}")
    if m.missing_items:
        lines.append(f"missing items: {', '.join(m.missing_items[:8])}")
    return "\n".join(lines)


def explain_payload(symbol: str, db_path: str | None = None) -> dict[str, Any]:
    """Machine-readable form of :func:`explain`."""
    cfg = config_mod.load()
    conn = db.connect(db_path)
    db.apply_schema(conn)
    try:
        # The valuation inputs must be passed in, or WACC is never computed and
        # the spread gate fails for a reason that has nothing to do with the
        # company being explained.
        verdict = screen.evaluate_symbol(
            conn, symbol.upper(), cfg,
            risk_free=cfg.get("valuation.inputs.risk_free_rate"),
            erp=cfg.get("valuation.inputs.equity_risk_premium"))
        m = M.compute(conn, symbol.upper(),
                      risk_free_rate=cfg.get("valuation.inputs.risk_free_rate"),
                      equity_risk_premium=cfg.get("valuation.inputs.equity_risk_premium"))
    finally:
        conn.close()

    return {
        "symbol": verdict.symbol,
        "name": m.name,
        "tier": verdict.tier,
        "quality_score": verdict.quality_score,
        "blocked": verdict.blocked,
        "clean": verdict.clean,
        "n_failed": verdict.n_failed,
        # blocking=False marks a recorded check that cannot reject the symbol,
        # so a consumer counting failures does not treat it as one.
        "gates": [
            {"gate": g.gate, "name": g.name, "passed": g.passed, "blocking": g.blocking,
             "metric": g.metric, "value": g.value, "threshold": g.threshold,
             "reason": g.reason, "note": g.note}
            for g in verdict.gates
        ],
        "metrics": {
            "roe": m.roe, "roic": m.roic, "wacc": m.wacc, "roic_spread": m.roic_spread,
            "ebitda_margin": m.ebitda_margin, "pe_avg": m.pe_avg, "ev_ebitda": m.ev_ebitda,
            "fcf_yield": m.fcf_yield, "net_debt_to_equity": m.net_debt_to_equity,
            "interest_coverage": m.interest_coverage, "ocf_conv": m.ocf_conv,
            "accruals": m.accruals,
        },
        "periods": {"income": m.n_periods_income, "balance": m.n_periods_balance,
                    "cashflow": m.n_periods_cashflow},
        "missing_items": list(m.missing_items),
    }