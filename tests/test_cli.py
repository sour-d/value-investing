"""The command line surface itself.

These are the flags and orderings an agent or a script depends on. They are easy
to break silently by moving a flag between the top-level parser and a
subparser, and the failure is an unparseable command rather than a wrong number,
so it is worth pinning down explicitly.
"""

from __future__ import annotations

import pytest

from stocks import cli

# Commands that take required positionals, so a flag-order test has to supply them.
POSITIONALS = {
    # Trades require --reason, so the flag-order tests must supply one.
    "buy": ["ACC", "10", "100.0", "--reason", "test"],
    "sell": ["ACC", "5", "100.0", "--reason", "test"],
    "why": ["ACC"],
}


def _parse(argv):
    return cli.build_parser().parse_args(argv)


def _with_positionals(command):
    return [command, *POSITIONALS.get(command, [])]


@pytest.mark.parametrize("command", [
    "init", "sync", "journal", "health", "screen", "pnl", "buy", "sell",
    "watch", "why", "integrity",
])
def test_json_works_after_the_subcommand(command):
    """The natural way to type it, and the one AGENTS.md promises."""
    args = _parse([*_with_positionals(command), "--json"])
    assert args.json is True
    assert args.command == command


@pytest.mark.parametrize("command", [
    "init", "sync", "journal", "health", "screen", "pnl", "buy", "sell",
    "watch", "why", "integrity",
])
def test_json_also_works_before_the_subcommand(command):
    """argparse accepts this order too, so it must keep working."""
    args = _parse(["--json", *_with_positionals(command)])
    assert args.json is True


@pytest.mark.parametrize("command", ["health", "screen", "why"])
def test_defaults_are_false_not_missing(command):
    """A SUPPRESS default must not leave the attribute absent: commands read
    args.json directly."""
    args = _parse(_with_positionals(command))
    assert args.json is False
    assert args.db is None


def test_db_flag_after_the_subcommand():
    args = _parse(["why", "ACC", "--db", "/tmp/x.db"])
    assert str(args.db) == "/tmp/x.db"


def test_db_flag_before_the_subcommand_is_not_overwritten():
    """The reason the subparser default is SUPPRESS rather than None."""
    args = _parse(["--db", "/tmp/before.db", "why", "ACC"])
    assert str(args.db) == "/tmp/before.db"


def test_no_subcommand_is_the_daily_loop():
    args = _parse([])
    assert args.command is None


def test_journal_flags():
    args = _parse(["journal", "--no-sync", "--notes", "hello", "--print"])
    assert args.no_sync is True and args.notes == "hello" and args.show is True


def test_roic_flag_is_tristate():
    """None means follow screen.toml; the two explicit values must survive."""
    assert _parse(["screen"]).roic is None
    assert _parse(["screen", "--roic"]).roic is True
    assert _parse(["screen", "--no-roic"]).roic is False


def test_trade_reason_is_required():
    """A trade with no rationale cannot be reviewed later."""
    with pytest.raises(SystemExit):
        _parse(["buy", "ACC", "10", "100.0"])


def test_help_mentions_the_daily_loop():
    assert "no arguments" in cli.build_parser().format_help()


def test_every_command_has_a_handler():
    """`stocks <cmd>` must not fall through to printing help."""
    import importlib

    commands = importlib.import_module("stocks.commands")
    parser = cli.build_parser()
    missing = [c for c in parser._subparsers._group_actions[0].choices
               if getattr(commands, f"cmd_{c}", None) is None]
    assert missing == []


def test_main_prints_help_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "init" in capsys.readouterr().out
