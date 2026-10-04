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
    from . import screencmd

    print(screencmd.watch(args.action or "list", db_path=args.db))
    return 0


@_impl("why")
def cmd_why(args: Any) -> int:
    from . import screencmd

    print(screencmd.why(args.symbol, db_path=args.db))
    return 0


@_impl("integrity")
def cmd_integrity(args: Any) -> int:
    from . import integrity

    print(integrity.render(args.symbol, db_path=args.db))
    return 0