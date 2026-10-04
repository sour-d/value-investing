"""Command-line entrypoint.

Every subcommand accepts ``--json`` so an agent can consume output
unambiguously instead of scraping formatted text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__


def _emit(payload: Any, as_json: bool, human: Callable[[Any], str] | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif human is not None:
        print(human(payload))
    else:
        print(json.dumps(payload, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stocks",
        description=(
            "Local screening, journal and portfolio tracker for "
            "NIFTY SMALLCAP 250 equities."
        ),
    )
    p.add_argument("--version", action="version", version=f"stocks {__version__}")
    p.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of formatted text",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="override database path",
    )

    sub = p.add_subparsers(dest="command")

    sub.add_parser("init", help="create the database and apply the schema")
    sync_p = sub.add_parser("sync", help="fetch stale prices, fundamentals and profile data")
    sync_p.add_argument("--full", action="store_true", help="ignore staleness and refetch everything")
    health_p = sub.add_parser("health", help="staleness and data-quality report")
    health_p.add_argument("--explain", metavar="CODE",
                          help="list the symbols carrying one issue code")
    sub.add_parser("journal", help="write and commit today's journal entry")

    screen = sub.add_parser("screen", help="run or inspect the quantitative screen")
    screen.add_argument("--explain", metavar="SYMBOL", help="show gates for one symbol")
    screen.add_argument("--full", action="store_true", help="force a full re-run")
    screen.add_argument(
        "--roic",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="force the ROIC/WACC profitability gate on or off "
             "(default: follow gate3.profitability.primary in screen.toml)",
    )

    pnl = sub.add_parser("pnl", help="realised and unrealised profit and loss")
    pnl.add_argument("--benchmark", action="store_true", help="compare with the index")
    pnl.add_argument("--since", metavar="YYYY-MM-DD")

    watch = sub.add_parser("watch", help="manage the watchlist")
    watch.add_argument("action", nargs="?", choices=["list", "add", "rm"])

    why = sub.add_parser("why", help="thesis, reasons and monitoring triggers")
    why.add_argument("symbol")

    integrity = sub.add_parser("integrity", help="attest the manual Gate 5 checks")
    integrity.add_argument("symbol")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    from . import commands

    handler = getattr(commands, f"cmd_{args.command}", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())