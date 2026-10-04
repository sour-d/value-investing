"""Subcommand implementations.

Split from :mod:`stocks.cli` so argument parsing stays declarative and each
command's behaviour is testable on its own.
"""

from __future__ import annotations

import sys
from typing import Any, Callable


def _pending(name: str) -> int:
    print(f"'stocks {name}' is not built yet — see the roadmap in AGENTS.md")
    return 3


def _impl(name: str) -> Callable[[Callable[[Any], int]], Callable[[Any], int]]:
    """Report an unbuilt phase instead of raising an opaque ImportError.

    Each command lands in its own commit; until its module exists it should say
    so plainly rather than dump a traceback.
    """

    def deco(fn: Callable[[Any], int]) -> Callable[[Any], int]:
        def wrapper(args: Any) -> int:
            try:
                return fn(args)
            except ImportError:
                return _pending(name)

        wrapper.__name__ = fn.__name__
        return wrapper

    return deco


def cmd_init(args: Any) -> int:
    from . import db, paths

    paths.ensure_dirs()
    conn = db.connect(args.db)
    db.apply_schema(conn)
    tables = db.table_names(conn)
    views = db.view_names(conn)
    print(f"initialised {conn.execute('PRAGMA database_list').fetchone()[2]}")
    print(f"tables: {len(tables)}  views: {len(views)}")
    return 0


@_impl("sync")
def cmd_sync(args: Any) -> int:
    from . import sync
    from .cli import _emit

    result = sync.run(db_path=args.db, full=getattr(args, "full", False))
    _emit(result.payload(), getattr(args, "json", False), human=lambda _: result.summary())
    return 1 if result.errors else 0


@_impl("health")
def cmd_health(args: Any) -> int:
    from . import db, health
    from .cli import _emit

    if getattr(args, "explain", None):
        conn = db.connect(args.db)
        db.apply_schema(conn)
        try:
            lines = health.explain_code(conn, args.explain)
        finally:
            conn.close()
        _emit({"code": args.explain, "symbols": lines}, getattr(args, "json", False),
              human=lambda p: p["symbols"] and "\n".join(p["symbols"])
              or f"no open issues with code '{args.explain}'")
        return 0

    report = health.build(db_path=args.db)
    _emit(report.payload(), getattr(args, "json", False), human=lambda _: report.render())
    # Non-zero on 'bad' so a cron wrapper or pre-commit hook notices. A warn is
    # information, not a failure.
    return 1 if report.worst == health.BAD else 0


@_impl("journal")
def cmd_journal(args: Any) -> int:
    from . import journal

    print(journal.write_today(db_path=args.db))
    return 0


@_impl("screen")
def cmd_screen(args: Any) -> int:
    from . import screencmd
    from .cli import _emit

    if args.explain:
        symbol = args.explain.upper()
        result = screencmd.explain_payload(symbol, db_path=args.db)
        _emit(result, getattr(args, "json", False),
              human=lambda p: screencmd.explain(symbol, db_path=args.db))
        return 0

    result = screencmd.run(full=args.full, db_path=args.db,
                           use_roic=getattr(args, "roic", None))
    _emit(result.payload(), getattr(args, "json", False), human=lambda _: result.render())
    return 0


@_impl("pnl")
def cmd_pnl(args: Any) -> int:
    from . import portfolio
    from .cli import _emit

    report = portfolio.pnl(db_path=args.db, since=args.since, benchmark=args.benchmark)
    _emit(report.payload(), getattr(args, "json", False),
          human=lambda _: portfolio.render_pnl(
              db_path=args.db, since=args.since, benchmark=args.benchmark))
    return 0


@_impl("buy")
def cmd_buy(args: Any) -> int:
    return _record_trade(args, "BUY")


@_impl("sell")
def cmd_sell(args: Any) -> int:
    return _record_trade(args, "SELL")


def _record_trade(args: Any, side: str) -> int:
    from . import db, portfolio
    from .cli import _emit

    conn = db.connect(args.db)
    db.apply_schema(conn)
    try:
        tx_id = portfolio.record_transaction(
            conn, args.symbol, side, args.qty, args.price,
            fees=args.fees, reason=args.reason, thesis_ref=args.thesis_ref,
            override_integrity=args.override_integrity,
        )
        result = {
            "id": tx_id, "symbol": args.symbol.upper(), "side": side,
            "qty": args.qty, "price": args.price, "fees": args.fees,
            "reason": args.reason,
            "override_integrity": args.override_integrity,
            "position_qty": portfolio.held_qty(conn, args.symbol),
        }
        _emit(result, getattr(args, "json", False), human=lambda p: (
            f"recorded {p['side']} {p['qty']} {p['symbol']} @ {p['price']:g} "
            f"(tx #{p['id']}); now holding {p['position_qty']}"))
        return 0
    except portfolio.PortfolioError as e:
        # A refusal is information, not a crash: print it and exit non-zero so a
        # script notices.
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


@_impl("watch")
def cmd_watch(args: Any) -> int:
    from . import db, judgment
    from .cli import _emit

    conn = db.connect(args.db)
    db.apply_schema(conn)
    try:
        action = getattr(args, "action", None) or "list"
        if action == "list":
            rows = judgment.watchlist(conn)
            _emit({"entries": rows}, getattr(args, "json", False),
                  human=lambda _: judgment.render_watchlist(conn))
            return 0
        if not args.symbol:
            print(f"'stocks watch {action}' needs a symbol", file=sys.stderr)
            return 2
        if action == "add":
            sym = judgment.add_watch(
                conn, args.symbol, buy_line=args.buy_line, fv_low=args.fv_low,
                fv_high=args.fv_high, status=args.status or "watch",
                thesis_path=args.thesis, entry_reason=args.reason)
            detail = f"{sym} added as {args.status or 'watch'}"
        elif action == "rm":
            judgment.remove_watch(conn, args.symbol)
            detail = f"{args.symbol.upper()} removed"
        else:
            if not args.status:
                print("'stocks watch status' needs --status", file=sys.stderr)
                return 2
            sym = judgment.set_status(conn, args.symbol, args.status)
            detail = f"{sym} is now {args.status}"
        _emit({"detail": detail}, getattr(args, "json", False), human=lambda p: p["detail"])
        return 0
    except judgment.PortfolioError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


@_impl("why")
def cmd_why(args: Any) -> int:
    from . import judgment
    from .cli import _emit

    payload = judgment.why_payload(args.symbol, db_path=args.db)
    _emit(payload, getattr(args, "json", False), human=judgment.render_why)
    return 0


@_impl("integrity")
def cmd_integrity(args: Any) -> int:
    """Attest Gate 5 checks, record verdicts and qualitative assessments."""
    from . import config as config_mod, db, judgment
    from .cli import _emit

    conn = db.connect(args.db)
    db.apply_schema(conn)
    try:
        cfg = config_mod.load()
        # Recording a check needs a name; a bare `stocks integrity SYM` just
        # shows the ledger, which is the useful default.
        if args.check_name:
            if not args.status:
                print("--set needs --status", file=sys.stderr)
                return 2
            judgment.attest(conn, cfg, args.symbol, args.check_name,
                            args.status, args.evidence)
            detail = f"{args.symbol.upper()}: {args.check_name} = {args.status}"
            _emit({"detail": detail, "symbol": args.symbol.upper(),
                   "check_name": args.check_name, "status": args.status,
                   "evidence": args.evidence}, getattr(args, "json", False),
                  human=lambda p: p["detail"])
            return 0

        if args.verdict:
            if not args.reason:
                print("--verdict needs --reason; an unexplained call is not "
                      "reviewable", file=sys.stderr)
                return 2
            as_of = judgment.record_verdict(
                conn, args.symbol, args.verdict, args.reason, args.evidence)
            _emit({"detail": f"{args.symbol.upper()} verdict {args.verdict} on {as_of}",
                   "symbol": args.symbol.upper(), "verdict": args.verdict,
                   "reason": args.reason, "as_of": as_of},
                  getattr(args, "json", False), human=lambda p: p["detail"])
            return 0

        if args.dimension:
            if not args.assessment or not args.rationale:
                print("--dimension needs --assessment and --rationale",
                      file=sys.stderr)
                return 2
            as_of = judgment.record_assessment(
                conn, args.symbol, args.dimension, args.assessment, args.rationale)
            _emit({"detail": f"{args.symbol.upper()} {args.dimension} recorded "
                             f"({as_of})", "symbol": args.symbol.upper(),
                   "dimension": args.dimension, "as_of": as_of},
                  getattr(args, "json", False), human=lambda p: p["detail"])
            return 0

        if not args.symbol:
            print("integrity needs a symbol", file=sys.stderr)
            return 2
        _emit({"symbol": args.symbol.upper(),
               "checks": judgment.portfolio.integrity_ledger(conn, cfg, args.symbol)},
              getattr(args, "json", False),
              human=lambda _: judgment.render_integrity(conn, cfg, args.symbol))
        return 0
    except judgment.PortfolioError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def cmd_daily(args: Any) -> int:
    """The daily loop: sync stale data, screen, report, journal.

    This is what `stocks` with no subcommand runs, and the whole point of the
    tool. It writes the journal entry rather than leaving that to a separate
    step, because a habit that needs two commands is a habit that gets skipped.
    """
    from . import daily
    from .cli import _emit

    result = daily.build(db_path=getattr(args, "db", None))
    result.journal_path = str(daily.write_journal(result))
    _emit(result.payload(), getattr(args, "json", False), human=daily.render)
    # A sync error should be visible in the exit code; the report still prints.
    return 1 if result.errors else 0


@_impl("journal")
def cmd_journal(args: Any) -> int:
    """Write today's journal entry, optionally re-running the day's work."""
    from . import daily
    from .cli import _emit

    result = daily.build(db_path=args.db, do_sync=not args.no_sync)
    if args.show:
        # Dry read: show exactly what would be recorded without writing it.
        _emit(result.payload(), getattr(args, "json", False),
              human=lambda _: daily.render_journal(result, args.notes))
        return 1 if result.errors else 0
    p = daily.write_journal(result, args.notes)
    result.journal_path = str(p)
    _emit(result.payload(), getattr(args, "json", False),
          human=lambda r: f"wrote {r['journal_path']}")
    return 1 if result.errors else 0
