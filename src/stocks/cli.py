"""Command-line entrypoint.

Every subcommand accepts ``--json`` so an agent can consume output
unambiguously instead of scraping formatted text.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .judgment import DIMENSIONS, STATUS_ORDER


def _add_global_args(p: argparse.ArgumentParser) -> None:
    """Accept the global flags after the subcommand too.

    `stocks --json screen` is valid argparse but nobody types it that way, and
    AGENTS.md promises every command supports --json. SUPPRESS matters: a
    subparser default would otherwise overwrite a value given before the
    subcommand, so both orders have to work.
    """
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="emit machine-readable JSON instead of formatted text")
    p.add_argument("--db", type=Path, default=argparse.SUPPRESS,
                   help="override database path")


def _emit(payload: Any, as_json: bool, human: Callable[[Any], str] | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif human is not None:
        print(human(payload))
    else:
        print(json.dumps(payload, indent=2, default=str))


def _common_trade_args(p: argparse.ArgumentParser) -> None:
    """Arguments shared by `buy` and `sell`.

    `--reason` is required rather than optional. A trade with no recorded
    rationale cannot be reviewed later, and the whole point of keeping a ledger
    is that the reason outlives the mood at the time.
    """
    p.add_argument("--fees", type=float, default=0.0, help="brokerage and statutory charges")
    p.add_argument("--reason", required=True, help="why this trade")
    p.add_argument("--thesis-ref", help="path to the research note behind it")
    p.add_argument("--override-integrity", metavar="WHY",
                   help="record an attested check as reviewed anyway; WHY is stored "
                        "on the transaction permanently")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stocks",
        description=(
            "Local screening, journal and portfolio tracker for "
            "NIFTY SMALLCAP 250 equities.\n\n"
            "Run `stocks` with no arguments for the daily loop: sync stale data, "
            "run the screen, print a short delta, and write the journal entry."
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
    p.set_defaults(json=False, db=None)

    sub = p.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="create the database and apply the schema")
    init_p.add_argument("--bootstrap", action="store_true",
                        help="also import the baseline NIFTY SMALLCAP 250 data "
                             "(requires /tmp/opencode artefacts)")
    sub.add_parser("bootstrap", help="import baseline data into an existing database")
    sync_p = sub.add_parser("sync", help="fetch stale prices, fundamentals and profile data")
    sync_p.add_argument("--full", action="store_true", help="ignore staleness and refetch everything")

    journal_p = sub.add_parser("journal", help="write today's journal entry")
    journal_p.add_argument("--notes", help="free text appended to the entry")
    journal_p.add_argument("--print", action="store_true", dest="show",
                           help="print the entry instead of writing it")
    journal_p.add_argument("--no-sync", action="store_true",
                           help="skip the sync step; use stored data as-is")
    health_p = sub.add_parser("health", help="staleness and data-quality report")
    health_p.add_argument("--explain", metavar="CODE",
                          help="list the symbols carrying one issue code")

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

    buy = sub.add_parser("buy", help="record a purchase")
    buy.add_argument("symbol")
    buy.add_argument("qty", type=int)
    buy.add_argument("price", type=float)
    _common_trade_args(buy)

    sell = sub.add_parser("sell", help="record a sale")
    sell.add_argument("symbol")
    sell.add_argument("qty", type=int)
    sell.add_argument("price", type=float)
    _common_trade_args(sell)

    watch = sub.add_parser("watch", help="manage the watchlist")
    watch.add_argument("action", nargs="?", choices=["list", "add", "rm", "status"],
                       default="list")
    watch.add_argument("symbol", nargs="?")
    watch.add_argument("--status", choices=list(STATUS_ORDER))
    watch.add_argument("--buy-line", type=float)
    watch.add_argument("--fv-low", type=float)
    watch.add_argument("--fv-high", type=float)
    watch.add_argument("--reason")
    watch.add_argument("--thesis", help="path to the research note")

    why = sub.add_parser("why", help="measured facts, thesis, and what is unverified")
    why.add_argument("symbol")

    integrity = sub.add_parser("integrity", help="attest the manual Gate 5 checks")
    integrity.add_argument("symbol", nargs="?")
    integrity.add_argument("--set", dest="check_name",
                           help="check name to record, e.g. promoter_pledge")
    integrity.add_argument("--status", choices=["verified", "failed", "unknown"])
    integrity.add_argument("--evidence", help="filing, page or disclosure checked")
    # --reason carries the rationale for both a verdict and an assessment; one
    # flag keeps the command single-purpose per invocation.
    integrity.add_argument("--reason", help="why this verdict or assessment holds")
    integrity.add_argument("--verdict", choices=["preferred", "watch", "avoid"],
                           help="record a research verdict")
    integrity.add_argument("--dimension", choices=list(DIMENSIONS),
                           help="record one qualitative framework dimension")
    integrity.add_argument("--assessment", help="the assessment text")
    integrity.add_argument("--rationale", help="why that assessment holds")

    for action in sub.choices.values():
        _add_global_args(action)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # No subcommand means the daily loop. This is the one command the tool
        # exists for, so it should not need to be named.
        from . import commands

        return commands.cmd_daily(args)

    from . import commands

    handler = getattr(commands, f"cmd_{args.command}", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())