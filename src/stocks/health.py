"""Health: what is stale, what is missing, what cannot be known.

This is the command that keeps absence visible. The screen's whole value rests
on not confusing "no data" with "no problem", so health enumerates every way
that confusion could happen:

* data that exists but is older than its budget,
* symbols the screen will refuse to evaluate, and why,
* inputs no provider supplies, so they are maintained by hand and go stale
  silently,
* framework inputs that are structurally unavailable.

A hand-maintained input is the most dangerous kind: it is a number nobody
re-checks, feeding a gate that decides what gets researched.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db, paths
from .config import Config
from .config import load as load_config
from .sync import _hours_since, _last_fetch, resolve_benchmark

OK = "ok"
WARN = "warn"
BAD = "bad"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None

    def payload(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}


@dataclass
class Health:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> list[Check]:
        return [c for c in self.checks if c.status == OK]

    @property
    def warn(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def bad(self) -> list[Check]:
        return [c for c in self.checks if c.status == BAD]

    @property
    def worst(self) -> str:
        if self.bad:
            return BAD
        return WARN if self.warn else OK

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.worst,
            "counts": {"ok": len(self.ok), "warn": len(self.warn), "bad": len(self.bad)},
            "checks": [c.payload() for c in self.checks],
        }

    def render(self) -> str:
        mark = {OK: " ok ", WARN: "warn", BAD: "BAD "}
        lines = []
        for c in self.checks:
            if c.status == OK:
                continue
            lines.append(f"[{mark[c.status]}] {c.name}: {c.detail}")
            if c.fix:
                lines.append(f"        → {c.fix}")
        if not lines:
            lines.append("all checks passed")
        counts = f"{len(self.ok)} ok, {len(self.warn)} warn, {len(self.bad)} bad"
        return "\n".join([*lines, "", counts])


# ───────────────────────────────────────────────────────────── staleness ────


def _age_check(conn: sqlite3.Connection, cfg: Config, kind: str, label: str,
               budget_hours: float) -> Check:
    ts = _last_fetch(conn, kind)
    hours = _hours_since(ts)
    if hours is None:
        return Check(label, BAD, "never fetched", "stocks sync")
    if hours >= budget_hours:
        return Check(label, WARN,
                     f"{hours:.0f}h old, budget {budget_hours:g}h",
                     "stocks sync")
    return Check(label, OK, f"{hours:.0f}h old")


def staleness_checks(conn: sqlite3.Connection, cfg: Config) -> list[Check]:
    return [
        _age_check(conn, cfg, "prices", "prices",
                   float(cfg.get("sync.price_staleness_hours", 20))),
        _age_check(conn, cfg, "financials", "fundamentals",
                   float(cfg.get("sync.fundamentals_staleness_days", 7)) * 24),
        _age_check(conn, cfg, "profile", "profile",
                   float(cfg.get("sync.profile_staleness_days", 7)) * 24),
    ]


def data_checks(conn: sqlite3.Connection, cfg: Config) -> list[Check]:
    universe = db.count(conn, "universe", "in_index = 1")
    with_fin = conn.execute(
        "SELECT COUNT(DISTINCT symbol) AS n FROM fundamentals_annual"
    ).fetchone()["n"]
    checks = [
        Check("universe", OK if universe else BAD, f"{universe} symbols in index"),
        Check("fundamentals coverage", OK if with_fin >= universe * 0.95 else WARN,
              f"{with_fin}/{universe} symbols have statements"),
    ]

    # Compare symbol counts, not row counts: one symbol is ~500 price bars.
    priced = conn.execute("SELECT COUNT(DISTINCT symbol) AS n FROM price_daily").fetchone()["n"]
    no_price = universe - priced
    if no_price > 0:
        checks.append(Check("price coverage", WARN,
                            f"{no_price} symbol(s) have no bars",
                            "stocks sync --full"))
    elif universe:
        checks.append(Check("price coverage", OK, f"all {priced} symbol(s) have bars"))
    else:
        checks.append(Check("price coverage", BAD, "universe is empty",
                            "re-run scripts/import_baseline.py"))

    # Data-quality issues that will make the screen skip a symbol.
    blocks = conn.execute(
        "SELECT code, COUNT(*) AS n FROM symbol_issue "
        "WHERE severity = 'block' AND resolved_at IS NULL GROUP BY code ORDER BY n DESC"
    ).fetchall()
    for r in blocks:
        checks.append(Check(f"issue: {r['code']}", WARN,
                            f"{r['n']} symbol(s) blocked from screening",
                            f"stocks health --explain {r['code']}"))
    return checks


def benchmark_check(conn: sqlite3.Connection, cfg: Config) -> Check:
    """The benchmark's identity must be confirmed, not assumed."""
    index_code, wire = resolve_benchmark(cfg)
    bars = db.count(conn, "price_benchmark")
    if not wire:
        return Check(
            "benchmark", WARN,
            f"{index_code}: vendor wire code unconfirmed, so no bars are stored",
            "set universe.benchmark_wire in screen.toml once verified against "
            "the official index factsheet",
        )
    if not bars:
        return Check("benchmark", WARN, f"{index_code} ({wire}): no bars stored",
                      "stocks sync --full")
    last = conn.execute(
        "SELECT MAX(date) AS d FROM price_benchmark WHERE index_code = ?", (index_code,)
    ).fetchone()["d"]
    return Check("benchmark", OK, f"{index_code} ({wire}), {bars} bars, last {last}")


def manual_input_checks(cfg: Config) -> list[Check]:
    """Hand-maintained inputs. Nobody re-checks these, so flag them."""
    checks: list[Check] = []
    stale_after = float(cfg.get("valuation.inputs.stale_after_days", 180))
    row = _config_mtime()
    risk_free = cfg.get("valuation.inputs.risk_free_rate")
    if risk_free is None:
        checks.append(Check("risk_free_rate", BAD, "unset, so ROIC>WACC cannot be judged",
                            "set valuation.inputs.risk_free_rate"))
    elif row is not None:
        age = (dt.datetime.now(dt.UTC) - row).days
        if age >= stale_after:
            checks.append(Check(
                "risk_free_rate", WARN,
                f"{risk_free} set {age}d ago, refresh every {stale_after:g}d "
                "(the 10Y G-Sec yield moves)",
                "update valuation.inputs.risk_free_rate"))
        else:
            checks.append(Check("risk_free_rate", OK, f"{risk_free} ({age}d old)"))
    return checks


def _config_mtime() -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(paths.config_path().stat().st_mtime, dt.UTC)
    except OSError:
        return None


def integrity_checks(conn: sqlite3.Connection, cfg: Config) -> list[Check]:
    """Gate 5 is human-attested, so show what is unverified rather than empty."""
    required = cfg.get("gate5.integrity.required_checks") or []
    if not required:
        return [Check("integrity", BAD, "no required_checks configured; Gate 5 cannot block")]

    watching = [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM watchlist WHERE status IN ('watch','researching','buy')"
    )]
    if not watching:
        return [Check("integrity", OK, f"{len(required)} checks defined, nothing under review")]

    placeholders = ",".join("?" * len(watching))
    rows = conn.execute(
        f"SELECT check_name, status, COUNT(DISTINCT symbol) AS n FROM integrity_check "
        f"WHERE symbol IN ({placeholders}) AND check_name IN "
        f"({','.join('?' * len(required))}) GROUP BY check_name, status",
        [*watching, *required],
    ).fetchall()
    verified = {r["check_name"] for r in rows if r["status"] == "verified"}
    missing = [c for c in required if c not in verified]
    if missing:
        return [Check(
            "integrity", WARN,
            f"{len(missing)} of {len(required)} checks unverified across "
            f"{len(watching)} name(s): {', '.join(missing[:4])}"
            + ("…" if len(missing) > 4 else ""),
            "stocks integrity SYM",
        )]
    return [Check("integrity", OK, f"all {len(required)} checks verified where required")]


def unmeasurable_check(cfg: Config) -> Check:
    items = cfg.get("unmeasurable.items") or []
    if not items:
        return Check("unmeasurable", WARN,
                     "no items listed; unmeasured inputs could be mistaken for passes",
                     "list them in screen.toml [unmeasurable]")
    return Check("unmeasurable", WARN,
                 f"{len(items)} framework input(s) unavailable from any feed: "
                 + ", ".join(items),
                 "assess these by hand; they are never auto-passed")


def filing_check(conn: sqlite3.Connection, cfg: Config) -> Check:
    n = db.count(conn, "universe", "in_index = 1 AND requires_filing = 1")
    if not n:
        return Check("filing analysis", OK, "no sector requires filings-based analysis")
    return Check("filing analysis", WARN,
                 f"{n} symbol(s) flagged as requiring NPA/CET1/provision analysis, "
                 "which no feed supplies",
                 "screen these from filings, not from the ratio gates")


def last_screen_check(conn: sqlite3.Connection, cfg: Config) -> Check:
    budget = float(cfg.get("sync.full_screen_staleness_days", 7)) * 24
    row = conn.execute(
        "SELECT MAX(finished_at) AS ts FROM screen_run WHERE finished_at IS NOT NULL"
    ).fetchone()
    hours = _hours_since(row["ts"] if row else None)
    if hours is None:
        return Check("screen run", WARN, "never run", "stocks screen")
    if hours >= budget:
        return Check("screen run", WARN, f"{hours / 24:.0f}d old, budget {budget / 24:g}d",
                     "stocks screen")
    return Check("screen run", OK, f"{hours:.0f}h old")


# ────────────────────────────────────────────────────────────────── build ────


def explain_code(conn: sqlite3.Connection, code: str) -> list[str]:
    """Symbols carrying one issue code, with the detail that flagged them.

    `stocks health` reports a count; this reports who and why, so a block can be
    triaged without opening the database.
    """
    rows = conn.execute(
        "SELECT symbol, detail FROM symbol_issue "
        "WHERE code = ? AND resolved_at IS NULL ORDER BY symbol",
        (code,),
    ).fetchall()
    return [f"{r['symbol']}: {r['detail']}" if r["detail"] else r["symbol"] for r in rows]


def build(db_path=None, cfg: Config | None = None, conn: sqlite3.Connection | None = None) -> Health:
    cfg = cfg or load_config()
    own = conn is None
    conn = conn or db.connect(db_path)
    if own:
        db.apply_schema(conn)
    try:
        h = Health()
        h.checks.extend(staleness_checks(conn, cfg))
        h.checks.extend(data_checks(conn, cfg))
        h.checks.append(benchmark_check(conn, cfg))
        h.checks.extend(manual_input_checks(cfg))
        h.checks.append(filing_check(conn, cfg))
        h.checks.append(last_screen_check(conn, cfg))
        h.checks.extend(integrity_checks(conn, cfg))
        h.checks.append(unmeasurable_check(cfg))
        return h
    finally:
        if own:
            conn.close()