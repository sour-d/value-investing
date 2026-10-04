"""Subcommand implementations.

Split from :mod:`stocks.cli` so argument parsing stays declarative and each
command's behaviour is testable on its own.
"""

from __future__ import annotations

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
    from . import health

    print(health.build(args.db).render())
    return 0


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
                           use_roic=getattr(args, "roic", False))
    _emit(result.payload(), getattr(args, "json", False), human=lambda _: result.render())
    return 0


@_impl("pnl")
def cmd_pnl(args: Any) -> int:
    from . import portfolio

    print(portfolio.render_pnl(db_path=args.db, since=args.since, benchmark=args.benchmark))
    return 0


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