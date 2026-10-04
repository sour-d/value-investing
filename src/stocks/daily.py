"""The daily loop: sync, screen, print a short delta, write a journal entry.

`stocks` with no arguments runs this. The design constraint is ADHD-friendly
rather than completeness: a fixed order, a line budget, no scrolling to find the
conclusion, and no jargon before the number.

Order is fixed and printed, because a report whose sections move around is one
that cannot be skimmed. The line budget is enforced in code, not by good
intentions — an over-long day is a report that gets ignored, and one day's
ignoring is how a name stays on the list after the thesis broke. Sections are
prioritised rather than truncated: cut the least decision-relevant first, and
say what was dropped instead of silently clipping.

What is deliberately absent: alerts, notifications, and a "top 10". A passer is a
research queue entry, so the report says what changed and where to look rather
than issuing instructions about what to buy.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, paths
from .config import Config
from .config import load as load_config

# Reading budget. Exceeding it costs the least decision-relevant line, so this
# is a cap on output width, not on information.
MAX_LINES = 14


@dataclass
class DailyResult:
    date: str
    run_id: int | None = None
    lines: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    journal_path: str | None = None
    sync: dict[str, Any] = field(default_factory=dict)
    screen: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "date": self.date, "run_id": self.run_id, "lines": self.lines,
            "dropped": self.dropped, "journal_path": self.journal_path,
            "sync": self.sync, "screen": self.screen, "health": self.health,
            "errors": self.errors,
        }


def _today() -> str:
    return dt.date.today().isoformat()  # noqa: DTZ011


def _n_clean(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT n_clean FROM screen_run WHERE finished_at IS NOT NULL "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    return int(row["n_clean"]) if row else 0


def _held(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT symbol, qty FROM v_positions ORDER BY symbol")]


def _movers(conn: sqlite3.Connection, limit: int = 3) -> list[dict[str, Any]]:
    """Largest one-day moves among held names.

    Only held names. A big move in an unheld stock is not actionable, and
    spending report lines on it would displace something that is.
    """
    return [dict(r) for r in conn.execute(
        "WITH last2 AS ("
        "  SELECT symbol, close,"
        "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn"
        "  FROM price_daily WHERE symbol IN (SELECT symbol FROM v_positions)"
        ")"
        "SELECT a.symbol, a.close AS price, b.close AS prev,"
        "       (a.close - b.close) / b.close * 100 AS pct"
        "  FROM last2 a JOIN last2 b ON a.symbol = b.symbol AND b.rn = 2"
        " WHERE a.rn = 1 AND b.close > 0"
        " ORDER BY ABS((a.close - b.close) / b.close * 100) DESC"
        f" LIMIT {int(limit)}"
    )]


def _watch_actionable(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Watchlist entries at or below their buy-line.

    These are the only names where the report should imply action. Everything
    else on the watchlist is context.
    """
    rows = []
    for r in conn.execute(
        "SELECT symbol, buy_line, status FROM watchlist "
        "WHERE buy_line IS NOT NULL AND status IN ('watch','researching','buy')"
    ):
        px = conn.execute(
            "SELECT close FROM price_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (r["symbol"],),
        ).fetchone()
        if px and px["close"] <= r["buy_line"]:
            rows.append({"symbol": r["symbol"], "price": px["close"],
                         "buy_line": r["buy_line"], "status": r["status"]})
    return rows


def _unverified(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Watchlist names whose Gate 5 checks are still open.

    The pair of facts matters together: a name at its buy-line with unverified
    integrity is a trap, because the price says yes and the gate says you cannot
    act yet.
    """
    required = cfg.get("gate5.integrity.required_checks") or []
    if not required:
        return []
    watching = [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM watchlist WHERE status IN ('watch','researching','buy')"
    )]
    if not watching:
        return []
    out: list[str] = []
    for sym in watching:
        rows = {
            r["check_name"]: r["status"]
            for r in conn.execute(
                "SELECT check_name, status FROM integrity_check WHERE symbol = ?", (sym,)
            )
        }
        missing = [c for c in required if rows.get(c) != "verified"]
        if missing:
            out.append(f"{sym} ({len(missing)})")
    return out


def build(
    db_path: str | None = None,
    cfg: Config | None = None,
    conn: sqlite3.Connection | None = None,
    do_sync: bool = True,
    do_screen: bool = True,
) -> DailyResult:
    """Sync stale data, run the screen, and assemble the report.

    Every step degrades rather than aborts. A failed sync should still leave a
    usable report saying the data is stale, because "nothing today" is itself
    the information that matters.
    """
    cfg = cfg or load_config()
    own = conn is None
    conn = conn or db.connect(db_path)
    if own:
        db.apply_schema(conn)
    result = DailyResult(date=_today())
    try:
        from . import health as health_mod
        from . import screencmd
        from . import sync as sync_mod

        if do_sync:
            # Deliberately not run with full=True: only stale data is fetched,
            # so a daily run stays cheap.
            try:
                s = sync_mod.run(db_path=db_path, conn=conn, cfg=cfg)
                result.sync = {"summary": s.summary(), "errors": s.errors}
            except Exception as e:  # noqa: BLE001 - a sync failure must not end the day
                result.sync = {"summary": "sync failed", "errors": [str(e)]}
                result.errors.append(f"sync: {e}")

        if do_screen:
            try:
                r = screencmd._run_locked(conn, cfg, full=False)
                result.run_id = r.run_id
                result.screen = r.payload()
            except Exception as e:  # noqa: BLE001
                result.screen = {"note": f"screen failed: {e}"}
                result.errors.append(f"screen: {e}")

        h = health_mod.build(cfg=cfg, conn=conn)
        result.health = h.payload()

        result.lines, result.dropped = compose(conn, cfg, result)
        return result
    finally:
        if own:
            conn.close()


def compose(
    conn: sqlite3.Connection, cfg: Config, result: DailyResult
) -> tuple[list[str], list[str]]:
    """Assemble the report, then enforce the line budget by priority.

    Section order is fixed. Within the budget, each section drops its least
    important entries first and says so, because a silently shortened list reads
    as a complete one.
    """
    lines: list[str] = []
    dropped: list[str] = []

    # 1. What changed in the screen. The single most important line.
    s = result.screen
    if result.run_id:
        if s.get("comparable") and (s.get("entrants") or s.get("exits")):
            if s["entrants"]:
                lines.append(f"NEW  {', '.join(s['entrants'])}")
            if s["exits"]:
                lines.append(f"GONE {', '.join(s['exits'])}")
        else:
            note = s.get("note") or "no change"
            lines.append(f"screen: {_n_clean(conn)} clean · {note}")
    elif s.get("note"):
        lines.append(f"screen: {s['note']}")

    # 2. Holdings at their buy-line. Actionable by definition.
    actionable = _watch_actionable(conn)
    for a in actionable[:3]:
        lines.append(f"at buy-line {a['symbol']} {a['price']:.1f} <= {a['buy_line']:.1f}")
    if len(actionable) > 3:
        dropped.append(f"{len(actionable) - 3} more watch names at their buy-line")

    # 3. Integrity gaps on those names, since they block the trade above.
    unverified = _unverified(conn, cfg)
    if unverified:
        lines.append(f"gate5 open: {', '.join(unverified[:3])}"
                     + ("…" if len(unverified) > 3 else ""))

    # 4. Held names' largest moves.
    movers = _movers(conn, limit=3)
    if movers:
        parts = ", ".join(f"{m['symbol']} {m['pct']:+.1f}%" for m in movers)
        lines.append(f"moves: {parts}")

    # 5. Portfolio position, one line.
    held = _held(conn)
    if held:
        from . import portfolio
        try:
            rep = portfolio.pnl(conn=conn, cfg=cfg)
            pct = f" ({rep.unrealised_pct:+.1f}%)" if rep.unrealised_pct is not None else ""
            lines.append(f"held: {len(held)} names · {rep.unrealised:+,.0f}{pct} unrealised")
            for n in rep.notes:
                dropped.append(n)  # noqa: PERF402
        except Exception:  # noqa: BLE001
            lines.append(f"held: {len(held)} name(s)")

    # 6. Data health, only if something needs attention.
    bad = [c for c in result.health.get("checks", []) if c["status"] != "ok"]
    if bad:
        names = ", ".join(c["name"] for c in bad[:3])
        lines.append(f"health: {names}" + ("…" if len(bad) > 3 else ""))

    # 7. The unmeasurable list, because an absent input is never a pass.
    unmeasurable = cfg.get("unmeasurable.items") or []
    if unmeasurable:
        lines.append(f"unmeasurable: {len(unmeasurable)} "
                     f"({', '.join(unmeasurable[:3])}…)")

    lines.append("passers are research candidates, not buys")

    # Enforce the budget, dropping from the bottom of the priority order.
    while len(lines) > MAX_LINES:
        for i in range(len(lines) - 1, -1, -1):
            if not lines[i].startswith(("NEW", "GONE", "at buy-line")):
                dropped.append(lines[i])
                del lines[i]
                break
        else:
            # Everything left is decision-critical; stop rather than mangle.
            dropped.append("report exceeds the line budget with only critical lines")
            break
    return lines, dropped


def journal_path(date: str | None = None, root: Path | None = None) -> Path:
    return (root or paths.journal_dir()) / f"{date or _today()}.md"


def write_journal(result: DailyResult, notes: str | None = None,
                  root: Path | None = None) -> Path:
    """Write today's entry to the journal directory.

    One file per day, overwritten rather than appended. A day produces one
    report, so a second file for the same date means something ran twice, and
    silently keeping both would make the journal disagree with itself.
    """
    root = root or paths.journal_dir()
    root.mkdir(parents=True, exist_ok=True)
    p = journal_path(result.date, root)
    p.write_text(render_journal(result, notes), encoding="utf-8")
    return p


def render_journal(result: DailyResult, notes: str | None = None) -> str:
    """The entry's Markdown, without writing it.

    Split from the write so `--print` can show exactly what would be recorded
    without touching the journal directory.
    """
    s = result.screen
    lines = [
        f"# {result.date}",
        "",
        f"- screen run: {result.run_id}" if result.run_id else "- screen run: none",
        f"- sync: {result.sync.get('summary', 'skipped')}",
    ]
    if s.get("n_clean") is not None:
        lines.append(f"- {s['n_clean']}/{s.get('universe_size', '?')} clean")
    for key, label in (("entrants", "new"), ("exits", "gone")):
        if s.get(key):
            lines.append(f"- {label}: {', '.join(s[key])}")
    if s.get("variant_delta"):
        d = s["variant_delta"]
        lines.append(f"- profitability gate {d['from']} -> {d['to']}: "
                     f"{len(d['entered'])} in, {len(d['left'])} out")
    lines.append("")
    lines.append("## Report")
    lines.append("")
    lines.extend(f"- {ln}" for ln in result.lines)
    if result.dropped:
        lines.append("")
        lines.append("## Omitted from the report")
        lines.append("")
        lines.extend(f"- {d}" for d in result.dropped)
    if notes:
        lines.extend(["", "## Notes", "", notes])
    if result.errors:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {e}" for e in result.errors)
    lines.append("")
    return "\n".join(lines)


def render(result: DailyResult | dict[str, Any]) -> str:
    """Human output. Accepts a DailyResult or its payload, since _emit hands
    over whatever was serialised."""
    if isinstance(result, dict):
        lines = list(result.get("lines", []))
        dropped = list(result.get("dropped", []))
        errors = list(result.get("errors", []))
        journal = result.get("journal_path")
    else:
        lines, dropped = list(result.lines), list(result.dropped)
        errors, journal = list(result.errors), result.journal_path
    out = list(lines)
    if dropped:
        out.append(f"({len(dropped)} line(s) omitted — see "
                   f"{journal or 'the journal entry'})")
    if errors:
        out.append("errors: " + "; ".join(errors))
    return "\n".join(out)