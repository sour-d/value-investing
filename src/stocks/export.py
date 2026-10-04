"""Write analysis results out of the database as git-trackable files.

The working database is a cache: binary, merge-hostile, and gitignored. But a
run is only useful if the next one can remember what it concluded. So the
durable record is plain Markdown and JSON under ``analysis/`` — text a human
can read, a diff can review, and a future run can compare against.

What is exported, and why each:

``analysis/runs.md``
    Human-readable log of every screen run. Why a symbol appeared or vanished is
    usually a ratio change, and "it was passing on Tuesday" is only answerable if
    Tuesday's numbers were written down.
``analysis/latest.json``
    Machine-readable latest state, for the next run's delta and for a reviewer
    who wants to diff two dates rather than read prose.
``analysis/passers.md``
    Who cleared every gate, with the actual ratios, so the conclusion can be
    argued with rather than taken on faith.
``analysis/watchlist.md``
    Thresholds being watched and how far each name is from them.
``analysis/qualitative.md``
    Moat, pricing power, management and valuation judgements with their stated
    assumptions — the part of the analysis a ratio cannot express.
``analysis/integrity.md``
    Which numbers are vendor-reported, what failed to fetch, and what is
    still unverified against filings.

Live prices are deliberately *not* exported per symbol. They change every
session; exporting them would bury the ratio movements that actually justify
re-running the screen inside noise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db, metrics, screencmd
from .paths import analysis_dir

#: The ratios that actually drove the gates, already expressed as percentages
#: by ``metrics.compute``. Showing these rather than the vendor's own profile
#: fields matters: the export has to reveal why a name passed, and the gate
#: decision was made on these numbers, not on the vendor's rounded versions.
_RATIO_FIELDS = (
    ("roe", "ROE"),
    ("roa", "ROA"),
    ("ebitda_margin", "EBITDA %"),
    ("net_margin", "Net %"),
    ("debt_to_equity", "D/E %"),
    ("interest_coverage", "Int x"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _pct(value: Any) -> str:
    """Format a value that is ALREADY a percentage.

    ``metrics.compute`` returns ratios multiplied by 100, and
    ``judgment._mos`` stores 0-100, so there is no conversion here. An earlier
    version multiplied by 100 a second time and reported ROE of 1943%.
    """
    return "—" if value is None else f"{float(value):.1f}%"


def _x(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}x"


def _num(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}"


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body.endswith("\n") else body + "\n")
    return path


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """Render a Markdown table, or a placeholder when there is nothing to show.

    An empty table renders as three unhelpful pipes; saying "none" is clearer
    and keeps consecutive diffs readable.
    """
    if not rows:
        return "_none_\n"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def line(cells: list[Any]) -> str:
        return "| " + " | ".join(
            str(cell).ljust(widths[i]) for i, cell in enumerate(cells)
        ) + " |"

    out = [line(headers), "|" + "|".join("-" * (width + 2) for width in widths) + "|"]
    out.extend(line(row) for row in rows)
    return "\n".join(out) + "\n"


def _latest_run(conn) -> dict | None:
    row = conn.execute(
        "SELECT run_id, started_at, config_hash, universe_size, n_clean "
        "FROM screen_run ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _passers(conn) -> str:
    run = _latest_run(conn)
    if run is None:
        return ("# Passers\n\nNo screen run recorded yet. "
                "Run `stocks screen` first.\n")
    clean = screencmd.clean_symbols(conn, run["run_id"])
    lines = [
        "# Passers",
        "",
        (f"Run #{run['run_id']} · {run['started_at'][:10]} · "
         f"{run['n_clean']} cleared every gate"),
        f"Config `{run['config_hash'][:12]}`",
        "",
        "> A passer is a research candidate, not a buy. Quality gates exclude the",
        "> obviously broken; they never establish that a price is worth paying.",
        "",
    ]
    if not clean:
        lines.append("_none cleared every gate._")
        return "\n".join(lines)

    # Recomputed rather than read from a cache: these are derived values and the
    # raw fundamentals are the source of truth, so a stale cache would quietly
    # misreport why a name passed.
    computed = [metrics.compute(conn, symbol) for symbol in sorted(clean)]
    computed.sort(key=lambda m: -(m.market_cap or 0))
    rows = [
        {"symbol": m.symbol,
         **{key: getattr(m, key) for key, _ in _RATIO_FIELDS},
         "market_cap": m.market_cap, "current_price": m.price}
        for m in computed
    ]
    lines.append(_table(
        ["symbol", *(label for _, label in _RATIO_FIELDS), "mcap", "price"],
        [[row["symbol"],
          *(_x(row[key]) if key == "interest_coverage" else _pct(row[key])
            for key, _ in _RATIO_FIELDS),
          _num(row["market_cap"], 0), _num(row["current_price"])] for row in rows],
    ))
    return "\n".join(lines)


def _watchlist(conn) -> str:
    rows = conn.execute(
        "SELECT symbol, status, buy_line, fv_low, fv_high, margin_of_safety, "
        "       thesis_path, entry_reason FROM watchlist ORDER BY symbol"
    ).fetchall()
    if not rows:
        return ("# Watchlist\n\n_empty — nothing being tracked for a future entry._\n")
    lines = [
        "# Watchlist",
        "",
        "Named before a buy, with the level that would justify entering and the",
        "condition that would invalidate the idea.",
        "",
    ]
    lines.append(_table(
        ["symbol", "status", "buy line", "fv low", "fv high", "MoS", "thesis"],
        [[row["symbol"], row["status"], _num(row["buy_line"]),
          _num(row["fv_low"]), _num(row["fv_high"]),
          _pct(row["margin_of_safety"]), row["thesis_path"] or "—"]
         for row in rows],
    ))
    return "\n".join(lines)


def _qualitative(conn) -> str:
    rows = conn.execute(
        "SELECT symbol, dimension, assessment, rationale, as_of "
        "FROM qualitative_assessment ORDER BY symbol, dimension, as_of DESC"
    ).fetchall()
    lines = [
        "# Qualitative assessments",
        "",
        "Judgements a ratio cannot make: competitive position, pricing power,",
        "management integrity, capital allocation. These drive no gate and carry",
        "no score — they are recorded so a later reader can disagree with them.",
        "",
    ]
    if not rows:
        lines.append("_none recorded yet_")
        return "\n".join(lines)
    current = rows[0]["as_of"][:10]
    lines.extend([f"Latest per symbol as of {current}:", ""])
    lines.append(_table(
        ["symbol", "dimension", "assessment", "rationale"],
        [[row["symbol"], row["dimension"], row["assessment"],
          (row["rationale"] or "").replace("\n", " ")[:110]] for row in rows],
    ))
    return "\n".join(lines)


def _integrity(conn) -> str:
    issues = conn.execute(
        "SELECT symbol, code, severity, detail FROM symbol_issue "
        "ORDER BY CASE severity WHEN 'block' THEN 0 ELSE 1 END, symbol"
    ).fetchall()
    failed = conn.execute(
        "SELECT symbol, COUNT(*) AS n FROM fetch_log WHERE ok = 0 "
        "GROUP BY symbol ORDER BY n DESC, symbol LIMIT 20"
    ).fetchall()
    resolved = conn.execute(
        "SELECT symbol, COUNT(*) AS n FROM fetch_log WHERE ok = 1 "
        "GROUP BY symbol ORDER BY n DESC, symbol LIMIT 20"
    ).fetchall()

    lines = ["# Data integrity", ""]
    if issues:
        lines.extend([f"## Outstanding issues ({len(issues)})", ""])
        lines.append(_table(
            ["symbol", "severity", "code", "detail"],
            [[row["symbol"], row["severity"], row["code"], (row["detail"] or "")[:70]]
             for row in issues],
        ))
    else:
        lines.append("No outstanding issues.")
    if failed:
        lines.extend(["", f"## Failed fetches ({len(failed)})", ""])
        lines.append(_table(
            ["symbol", "attempts"],
            [[row["symbol"], row["n"]] for row in failed],
        ))
    if resolved:
        lines.extend([
            "",
            f"## Symbols with successful fetches ({len(resolved)})",
            "",
            _table(["symbol", "attempts"],
                   [[row["symbol"], row["n"]] for row in resolved]),
        ])
    lines.extend([
        "",
        "Every stored figure is vendor-reported through MCP. Nothing here has",
        "been checked against a filing. Treat it as a screening input, and cite",
        "the annual report before acting on any single number.",
    ])
    return "\n".join(lines)


def _latest_json(conn) -> str:
    run = _latest_run(conn)
    if run is None:
        return json.dumps({"status": "no runs recorded"}, indent=2, sort_keys=True) + "\n"
    passers = sorted(screencmd.clean_symbols(conn, run["run_id"]))
    notes = {
        row["symbol"]: row["reason"] for row in conn.execute(
            "SELECT symbol, MAX(reason) AS reason FROM gate_result "
            "WHERE run_id = ? AND passed = 0 AND blocking = 0 "
            "AND reason IS NOT NULL GROUP BY symbol ORDER BY symbol",
            (run["run_id"],),
        )
    }
    return json.dumps({
        "run_id": run["run_id"],
        "run_started_at": run["started_at"],
        "config_hash": run["config_hash"],
        "universe_size": run["universe_size"],
        "n_clean": run["n_clean"],
        "passers": passers,
        "non_blocking_notes": notes,
        "exported_at": _now(),
    }, indent=2, sort_keys=True) + "\n"


def _runs_entry(conn) -> str:
    run = _latest_run(conn)
    if run is None:
        return ""
    current = screencmd.clean_symbols(conn, run["run_id"])
    previous, _, _, has_previous = screencmd.previous_run(conn, run["run_id"])
    if not has_previous:
        # The first run has nothing to compare against, so every passer is
        # trivially "new". Recording 250 names as entrants would bury the nine
        # that actually matter and imply a real change that never happened.
        entrants_line = f"- all {len(current)} passers are new; no earlier run to compare"
    else:
        entrants = sorted(current - previous)
        exits = sorted(previous - current)
        entrants_line = f"- entered: {', '.join(entrants) if entrants else 'none'}"
        if exits:
            entrants_line += f"\n- left the screen: {', '.join(exits)}"
    return "\n".join([
        f"## Run #{run['run_id']} · {run['started_at'][:10]}",
        "",
        f"- {run['n_clean']} passers of {run['universe_size']} screened",
        f"- config `{run['config_hash'][:12]}`",
        entrants_line,
    ]) + "\n"


def _update_runs(path: Path, entry: str) -> Path:
    """Rewrite the run log with the newest run first.

    Truncated at the 100 most recent runs: the file is committed to git, and an
    append-only log that grows forever makes every future diff expensive.
    """
    existing = path.read_text() if path.exists() else "# Screen runs\n"
    header = "# Screen runs"
    parts = existing.split("\n## ")
    entries = ["## " + part.rstrip() for part in parts[1:]] if len(parts) > 1 else []
    if entry:
        entries.insert(0, entry.rstrip())
    body = "\n\n".join([header, *entries[:100]]) + "\n"
    return _write(path, body)


def export_all(db_file: Path | None = None, directory: Path | None = None) -> list[Path]:
    """Write every export and return the paths written."""
    conn = db.connect(db_file)
    try:
        target = directory or analysis_dir()
        return [
            _write(target / "passers.md", _passers(conn)),
            _write(target / "watchlist.md", _watchlist(conn)),
            _write(target / "qualitative.md", _qualitative(conn)),
            _write(target / "integrity.md", _integrity(conn)),
            _write(target / "latest.json", _latest_json(conn)),
            _update_runs(target / "runs.md", _runs_entry(conn)),
        ]
    finally:
        conn.close()