"""Subcommand implementations.

Split from :mod:`stocks.cli` so argument parsing stays declarative and each
command's behaviour is testable on its own.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _pending(name: str) -> int:
    print(f"'stocks {name}' is not built yet — see the roadmap in AGENTS.md")
    return 3


def _today() -> str:
    """Date only. Membership and rebalance facts are dated, not timestamped."""
    return datetime.now(UTC).date().isoformat()


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

    if getattr(args, "bootstrap", False):
        return cmd_bootstrap(args)

    return 0


def cmd_bootstrap(args: Any) -> int:
    """Legacy offline import from a directory of vendor artefacts.

    Kept because the pilot contract in ``tests/test_pilot_reproduction.py``
    pins a specific historical result, and reproducing it needs the original
    pickles. New data should arrive via MCP instead: ``stocks ingest``.

    Requires ``smcap250.csv``, ``info.json`` and ``fin.pkl`` in ``source``.
    """
    import subprocess
    import sys
    from pathlib import Path

    source = getattr(args, "source", None)
    src = Path(source) if source else Path("/tmp/opencode")
    if not (src / "smcap250.csv").exists():
        print(f"ERROR: {src / 'smcap250.csv'} not found", file=sys.stderr)
        print("This offline path needs smcap250.csv, info.json and fin.pkl.", file=sys.stderr)
        print("For normal use, fetch via MCP and run `stocks ingest`.", file=sys.stderr)
        return 1

    db_path = getattr(args, "db", None)
    cmd = [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts" / "import_baseline.py"),
           str(src)]
    if db_path:
        cmd += ["--db", str(db_path)]

    try:
        subprocess.run(cmd, check=True)
        print("baseline imported")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"import failed: {e}", file=sys.stderr)
        return e.returncode


@_impl("ingest")
def cmd_ingest(args: Any) -> int:
    """Load ``data/inbox/*.json`` — whatever an MCP fetch wrote — into the DB."""
    from . import ingest
    from .cli import _emit
    from .paths import inbox_dir

    inbox = getattr(args, "inbox", None)
    directory = inbox or inbox_dir()
    if not directory.is_dir() or not any(directory.glob("*.json")):
        print(f"no inbox JSON in {directory}", file=sys.stderr)
        print("Fetch data with the india-stock / yfinance MCP servers, write it to",
              file=sys.stderr)
        print(f"{directory}/ as JSON, then re-run `stocks ingest`.", file=sys.stderr)
        return 1

    counts = ingest.ingest_all(args.db, directory)

    def human(_: dict) -> str:
        return "\n".join(f"{name:12} {count:>9,}" for name, count in counts.items())

    _emit(counts, getattr(args, "json", False), human=human)
    return 0


@_impl("universe")
def cmd_universe(args: Any) -> int:
    """Validate the roster and report membership drift.

    Read-only by default: this is the check to run *before* a screen, because a
    stale or short roster produces plausible results for the wrong companies.
    ``--apply`` reconciles membership, and is refused while the roster has
    errors.
    """
    from . import roster as roster_mod
    from .cli import _emit

    try:
        parsed, findings, detail = roster_mod.reconcile_file(
            getattr(args, "roster", None), _today(), getattr(args, "db", None)
        )
    except FileNotFoundError as e:
        print(f"roster not found: {e}", file=sys.stderr)
        print("No MCP server exposes index constituents, so the roster is a",
              file=sys.stderr)
        print("committed file. Create it, then re-run.", file=sys.stderr)
        return 1

    errors = roster_mod.has_errors(findings)
    applied = None
    if getattr(args, "apply", False):
        if errors:
            print("refusing to apply: fix the findings below first", file=sys.stderr)
        else:
            from . import db

            conn = db.connect(getattr(args, "db", None))
            try:
                db.apply_schema(conn)
                applied = roster_mod.apply_roster(conn, parsed, _today())
            finally:
                conn.close()

    payload = {
        "index_code": parsed.index_code,
        "source": parsed.source,
        "effective": parsed.effective,
        "constituents": len(parsed.constituents),
        "declared_size": parsed.declared_size,
        "findings": [f.__dict__ for f in findings],
        "errors": errors,
        "entrants": detail["entrants"],
        "removals": detail["removals"],
        "coverage": detail["coverage"],
        "applied": applied,
    }

    def human(_: dict) -> str:
        return roster_mod.format_report(parsed, findings, detail)

    _emit(payload, getattr(args, "json", False), human=human)
    return 1 if errors else 0


@_impl("export")
def cmd_export(args: Any) -> int:
    """Write the durable, git-trackable record of what the analysis concluded."""
    from . import export
    from .cli import _emit

    written = export.export_all(args.db)
    payload = {"written": [str(path) for path in written]}
    _emit(payload, getattr(args, "json", False),
          human=lambda p: "\n".join(p["written"]))
    return 0


@_impl("inspect")
def cmd_inspect(args: Any) -> int:
    """What is stored, and how fresh. First call in any session."""
    from . import db, paths
    from .cli import _emit

    paths.ensure_dirs()
    conn = db.connect(args.db)
    try:
        db.apply_schema(conn)
        tables = ("universe", "profile_snapshot", "fundamentals_annual",
                  "price_daily", "screen_run", "gate_result", "transactions",
                  "watchlist", "qualitative_assessment", "valuation_assumption",
                  "research_verdict", "symbol_issue")
        counts = {name: db.count(conn, name) for name in tables}
        last_price = conn.execute(
            "SELECT MAX(date) FROM price_daily"
        ).fetchone()[0]
        last_run = conn.execute(
            "SELECT run_id, started_at, n_clean, universe_size FROM screen_run "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        payload: dict = {
            "database": str(Path(conn.execute(
                "PRAGMA database_list").fetchone()[2]).name),
            "counts": counts,
            "latest_price_date": last_price,
            "latest_run": dict(last_run) if last_run else None,
            "inbox_files": sorted(p.name for p in paths.inbox_dir().glob("*.json")),
            "analysis_dir": str(paths.analysis_dir()),
        }
    finally:
        conn.close()

    def human(p: dict) -> str:
        lines = [f"database: {p['database']}",
                 f"latest price date: {p['latest_price_date'] or 'never'}"]
        run = p["latest_run"]
        lines.append(
            f"latest run: #{run['run_id']} {run['started_at'][:10]} "
            f"{run['n_clean']}/{run['universe_size']} clean" if run
            else "latest run: none")
        lines.append("")
        lines.extend(f"{name:24} {count:>9,}" for name, count in p["counts"].items())
        lines.append("")
        lines.append(f"inbox: {', '.join(p['inbox_files']) or 'empty'}")
        return "\n".join(lines)

    _emit(payload, getattr(args, "json", False), human=human)
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

    run_result = screencmd.run(full=args.full, db_path=args.db,
                              use_roic=getattr(args, "roic", None))
    _emit(run_result.payload(), getattr(args, "json", False), human=lambda _: run_result.render())
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
    from . import config as config_mod
    from . import db, judgment
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
