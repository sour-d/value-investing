"""Universe roster: validation, membership reconciliation and drift reporting.

The roster is reference data, not market data. It changes twice a year at index
rebalance, it is needed before anything else can be screened, and a wrong one
silently corrupts every run comparison — so it is committed to git rather than
fetched per run, and every read is validated.

No MCP server exposes Indian index constituents (see README), so the roster is
maintained as a checked-in artefact. This module is what makes that safe:

* :func:`validate` refuses a roster that is internally inconsistent.
* :func:`apply_roster` refuses to reconcile an invalid roster, so a broken file
  cannot corrupt the universe table.
* :func:`diff` reports entrants and removals, which is the only way to notice
  the roster changed underneath a run history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `IN` + issuer code + 8-char NSIN + check digit.
_ISIN_RE = re.compile(r"^IN[A-Z][0-9A-Z]{8}[0-9]$")
# Bare NSE symbol: no vendor suffix, no series dash, no whitespace.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9&]*$")
# NSE pads short indices with filler rows so the file always shows full size.
_DUMMY_NAME_RE = re.compile(r"^dummy\b", re.IGNORECASE)


def _is_placeholder(row: dict[str, Any]) -> bool:
    """True for an NSE index-CSV filler row rather than a real company.

    Two independent signals, because either alone can mislead: an ISIN that
    does not start with ``IN`` cannot be an Indian ISIN at all, and NSE names
    its filler rows ``Dummy <something> Ltd.`` A real company would fail neither
    test, and a filler row trips both.
    """
    isin = str(row.get("isin") or "").strip().upper()
    if isin and not isin.startswith("IN"):
        return True
    return bool(_DUMMY_NAME_RE.match(str(row.get("name") or "").strip()))


@dataclass(frozen=True)
class Finding:
    """One validation result. ``error`` blocks reconciliation; ``warn`` does not."""

    level: str
    code: str
    message: str


@dataclass(frozen=True)
class Roster:
    """A parsed roster plus the provenance needed to interpret it."""

    index_code: str
    index_name: str
    declared_size: int | None
    effective: str | None
    captured: str | None
    source: str
    constituents: tuple[dict[str, Any], ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(str(c["symbol"]) for c in self.constituents)


def default_roster_path() -> Path:
    from .paths import repo_root

    return repo_root() / "universe" / "smcap250.json"


def _rows(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("constituents"), list):
            return payload["constituents"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
        # Map form: {"ACC": {...}}
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return [{"symbol": k, **v} for k, v in payload.items()]
    return []


def parse(payload: Any) -> Roster:
    """Normalise any accepted roster shape into a :class:`Roster`."""
    meta = payload if isinstance(payload, dict) else {}
    out: list[dict[str, Any]] = []
    for row in _rows(payload):
        if isinstance(row, str):
            row = {"symbol": row}
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out.append({**row, "symbol": symbol})

    declared = meta.get("declared_size")
    try:
        declared_size = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared_size = None

    return Roster(
        index_code=str(meta.get("index_code") or "NIFTY_SMALLCAP250"),
        index_name=str(meta.get("index_name") or "NIFTY SMALLCAP 250"),
        declared_size=declared_size,
        effective=(str(meta["effective"]) if meta.get("effective") else None),
        captured=(str(meta["captured"]) if meta.get("captured") else None),
        source=str(meta.get("source") or ""),
        constituents=tuple(out),
    )


def load(path: str | Path | None = None) -> Roster:
    """Read a roster file. ``None`` means the committed default."""
    target = Path(path) if path else default_roster_path()
    if not target.is_file():
        raise FileNotFoundError(target)
    return parse(json.loads(target.read_text()))


def validate(roster: Roster) -> list[Finding]:
    """Check the roster is internally consistent and honestly labelled.

    The size check is the one that matters most: an index whose name declares
    250 members but whose file holds a different number is either stale or
    corrupt, and screening it anyway produces plausible-looking results for the
    wrong set of companies.
    """
    found: list[Finding] = []
    symbols = list(roster.symbols)
    count = len(symbols)

    if count == 0:
        found.append(Finding("error", "empty", "roster has no constituents"))
        return found

    if not roster.source:
        found.append(Finding(
            "error", "no_source",
            "roster has no `source` — membership is untraceable without provenance",
        ))

    if roster.declared_size is None:
        found.append(Finding(
            "warn", "no_declared_size",
            "roster has no `declared_size`, so a size mismatch cannot be detected",
        ))
    elif roster.declared_size != count:
        found.append(Finding(
            "error", "size_mismatch",
            f"{roster.index_name} declares {roster.declared_size} constituents "
            f"but the roster holds {count}",
        ))

    if roster.effective is None:
        found.append(Finding(
            "warn", "no_effective_date",
            "roster has no `effective` date, so membership cannot be placed on the "
            "index timeline; run-to-run membership claims are unverifiable",
        ))

    seen: dict[str, int] = {}
    dupes: list[str] = []
    for sym in symbols:
        seen[sym] = seen.get(sym, 0) + 1
        if seen[sym] == 2:
            dupes.append(sym)
    if dupes:
        found.append(Finding(
            "error", "duplicate_symbol",
            f"{len(dupes)} duplicate symbol(s): {', '.join(sorted(dupes)[:8])}",
        ))

    def _dupes(field: str) -> list[str]:
        counts: dict[str, int] = {}
        for row in roster.constituents:
            val = str(row.get(field) or "").strip()
            if val:
                counts[val] = counts.get(val, 0) + 1
        return sorted(v for v, n in counts.items() if n > 1)

    dup_names = _dupes("name")
    if dup_names:
        found.append(Finding(
            "error", "duplicate_name",
            f"{len(dup_names)} duplicate company name(s) — the same business listed "
            f"twice inflates the universe: {', '.join(dup_names[:5])}",
        ))

    dup_isins = _dupes("isin")
    if dup_isins:
        found.append(Finding(
            "error", "duplicate_isin",
            f"{len(dup_isins)} duplicate ISIN(s) — two symbols share one issuer: "
            f"{', '.join(dup_isins[:5])}",
        ))

    bad = [str(r["symbol"]) for r in roster.constituents if not _SYMBOL_RE.match(str(r["symbol"]))]
    if bad:
        found.append(Finding(
            "error", "bad_symbol_format",
            f"{len(bad)} symbol(s) are not bare NSE symbols (no suffix, dash or space): "
            f"{', '.join(bad[:5])}",
        ))

    placeholders = [
        row for row in roster.constituents
        if _is_placeholder(row)
    ]
    if placeholders:
        found.append(Finding(
            "error", "placeholder_row",
            f"{len(placeholders)} placeholder row(s) present: "
            f"{', '.join(sorted(str(r['symbol']) for r in placeholders)[:5])}. "
            "NSE index CSVs pad short indices with a dummy row that is not a company; "
            "it inflates every count and denominator in the analysis",
        ))

    bad_isins = [
        str(r["isin"]) for r in roster.constituents
        if r.get("isin") and str(r["isin"]).strip().upper().startswith("IN")
        and not _ISIN_RE.match(str(r["isin"]).strip().upper())
    ]
    if bad_isins:
        found.append(Finding(
            "warn", "bad_isin_format",
            f"{len(bad_isins)} ISIN(s) are not 12-character Indian ISINs "
            f"(format only, no check digit): {', '.join(bad_isins[:5])}",
        ))

    missing_industry = sum(1 for r in roster.constituents if not r.get("industry"))
    if missing_industry:
        found.append(Finding(
            "warn", "missing_industry",
            f"{missing_industry} constituent(s) carry no industry; the roster is the "
            "only source of it, vendors do not agree",
        ))

    return found


def _current_members(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r["symbol"])
        for r in conn.execute("SELECT symbol FROM universe WHERE in_index = 1")
    }


def diff(conn: sqlite3.Connection, roster: Roster) -> dict[str, list[str]]:
    """Compare a roster against recorded membership.

    ``entrants`` are members the database has not seen; ``removals`` are members
    the database still records but the roster no longer lists.
    """
    incoming = set(roster.symbols)
    current = _current_members(conn)
    return {
        "entrants": sorted(incoming - current),
        "removals": sorted(current - incoming),
        "unchanged": sorted(incoming & current),
    }


def coverage(conn: sqlite3.Connection, roster: Roster) -> dict[str, Any]:
    """How much of the roster the database can actually analyse."""
    symbols = set(roster.symbols)
    if not symbols:
        return {"members": 0, "with_profile": 0, "with_fundamentals": 0,
                "missing_profile": 0, "missing_fundamentals": 0,
                "missing_fundamentals_symbols": []}

    marks = ",".join("?" * len(symbols))
    params = tuple(sorted(symbols))

    def _count(table: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT symbol) FROM {table} WHERE symbol IN ({marks})",
            params,
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    with_profile = _count("profile_snapshot")
    with_fundamentals = _count("fundamentals_annual")

    def _missing(table: str) -> list[str]:
        have = {
            str(r["symbol"])
            for r in conn.execute(
                f"SELECT DISTINCT symbol FROM {table} WHERE symbol IN ({marks})", params
            )
        }
        return sorted(symbols - have)

    missing_profile = _missing("profile_snapshot")
    missing_fundamentals = _missing("fundamentals_annual")

    return {
        "members": len(symbols),
        "with_profile": with_profile,
        "with_fundamentals": with_fundamentals,
        # Fundamentals are what Gate 0 needs, so their absence is what blocks
        # screening. A missing profile does not.
        "missing_profile": len(missing_profile),
        "missing_fundamentals": len(missing_fundamentals),
        "missing_fundamentals_symbols": missing_fundamentals[:20],
    }


def apply_roster(conn: sqlite3.Connection, roster: Roster, as_of: str) -> dict[str, Any]:
    """Reconcile ``universe`` with a validated roster.

    Members are marked in and absent names marked out; nothing is ever deleted,
    so the membership timeline survives every rebalance. Refuses to run if
    validation reported an error, because a bad roster must not be able to
    demote real members.

    Two timelines are kept deliberately apart:

    * ``added_on`` / ``removed_on`` record *when this database noticed* — the
      operational history, stamped ``as_of``.
    * ``membership_source`` / ``membership_as_of`` record the claim being made —
      where the roster came from and what date it says is effective.

    Collapsing them would make "when did we see it" indistinguishable from
    "when did the index add it", which is the distinction the columns exist for.
    """
    errors = [f for f in validate(roster) if f.level == "error"]
    if errors:
        raise ValueError(
            "refusing to apply an invalid roster: "
            + "; ".join(f"{f.code}: {f.message}" for f in errors)
        )

    members = set(roster.symbols)
    source = roster.source
    claimed_as_of = roster.effective or roster.captured

    for row in roster.constituents:
        conn.execute(
            "INSERT INTO universe "
            "(symbol,name,isin,industry,in_index,added_on,membership_source,membership_as_of) "
            "VALUES (?,?,?,?,1,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  name=COALESCE(excluded.name, universe.name), "
            "  isin=COALESCE(excluded.isin, universe.isin), "
            "  industry=COALESCE(excluded.industry, universe.industry), "
            "  in_index=1, removed_on=NULL, "
            "  membership_source=excluded.membership_source, "
            "  membership_as_of=excluded.membership_as_of, "
            "  added_on=CASE WHEN universe.in_index=0 THEN excluded.added_on "
            "                 ELSE universe.added_on END",
            (row["symbol"], row.get("name"), row.get("isin"), row.get("industry"),
             as_of, source, claimed_as_of),
        )

    marks = ",".join("?" * len(members))
    removed = [
        str(r["symbol"])
        for r in conn.execute(
            f"SELECT symbol FROM universe WHERE in_index = 1 AND symbol NOT IN ({marks})",
            tuple(sorted(members)),
        )
    ]
    if removed:
        conn.execute(
            f"UPDATE universe SET in_index = 0, removed_on = ? "
            f"WHERE in_index = 1 AND symbol NOT IN ({marks})",
            (as_of, *sorted(members)),
        )
    conn.commit()

    return {"members": len(members), "marked_in": len(members),
            "marked_out": len(removed), "removed": sorted(removed)}


def reconcile_file(
    path: str | Path | None,
    as_of: str,
    db_path: str | Path | None = None,
) -> tuple[Roster, list[Finding], dict]:
    """Load, validate and diff in one call. Used by ``stocks universe``.

    ``db_path`` must be threaded through — defaulting to the production database
    would make a validation-only command silently read real state, and in tests
    would report drift caused by the developer's own data.
    """
    parsed = load(path)
    from . import db, paths

    paths.ensure_dirs()
    conn = db.connect(db_path)
    try:
        db.apply_schema(conn)
        findings = validate(parsed)
        drift = diff(conn, parsed)
        cov = coverage(conn, parsed)
    finally:
        conn.close()
    return parsed, findings, {**drift, "coverage": cov}


def format_report(roster: Roster, findings: list[Finding], detail: dict[str, Any]) -> str:
    """Human-readable roster report. Also used verbatim in the skill docs."""
    lines = [
        f"roster       {roster.index_code} ({roster.index_name})",
        f"source       {roster.source or '(none)'}",
        f"effective    {roster.effective or '(unverified)'}   "
        f"captured {roster.captured or '(unknown)'}",
        f"constituents {len(roster.constituents)}"
        + (f" of {roster.declared_size} declared" if roster.declared_size else ""),
        "",
    ]
    cov = detail.get("coverage") or {}
    if cov:
        missing_f = cov.get("missing_fundamentals", 0)
        lines += [
            f"coverage     {cov.get('with_profile', 0)}/{cov.get('members', 0)} profiles, "
            f"{cov.get('with_fundamentals', 0)}/{cov.get('members', 0)} fundamentals",
            f"missing      {missing_f} without fundamentals (cannot be screened)",
        ]
        syms = cov.get("missing_fundamentals_symbols") or []
        if syms:
            lines.append(f"              {', '.join(syms[:12])}")
        lines.append("")
    for label, key in (("entrants", "entrants"), ("removals", "removals")):
        items = detail.get(key) or []
        shown = ", ".join(items[:12]) + (f" (+{len(items) - 12} more)" if len(items) > 12 else "")
        lines.append(f"{label:<12} {len(items):>4}  {shown or '-'}")

    if findings:
        lines += ["", "findings:"]
        for f in findings:
            lines.append(f"  [{f.level}] {f.code}: {f.message}")
    else:
        lines += ["", "findings:     none"]
    return "\n".join(lines)


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.level == "error" for f in findings)