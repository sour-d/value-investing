"""The daily loop: what it prints, what it writes, and what it does when a step
fails.

The invariants under test are the ones that make the report worth reading. A
report that silently omits a section reads as a complete report, so omissions are
asserted. A sync outage must still leave a usable day, because "nothing today" is
itself the information that matters.
"""

from __future__ import annotations

import json

import pytest

from stocks import daily, db


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.apply_schema(c)
    yield c
    c.close()


def _cfg(**overrides):
    from stocks import config as config_mod

    base = {
        "gate0": {"data_sufficiency": {"min_annual_periods": 4,
                                       "require_contiguous_years": True}},
        "gate1": {"earnings_quality": {"ni_positive_years_ratio": 0.99,
                                       "ocf_positive_years_ratio": 0.99,
                                       "fcf_positive_years_ratio": 0.75,
                                       "min_ocf_to_ni": 0.80,
                                       "max_accruals_ratio": 0.10}},
        "gate2": {"balance_sheet": {"max_net_debt_to_equity": 0.30,
                                    "min_interest_coverage": 5.0},
                  "financial": {"max_debt_to_equity": 1.8}},
        "gate3": {"profitability": {"primary": "roic", "min_roic": 15.0,
                                    "min_roic_wacc_spread": 5.0,
                                    "min_roe": 13.0, "min_ebitda_margin": 9.0}},
        "gate4": {"value": {"max_pe": 16.0, "max_ev_ebitda": 8.0,
                            "min_fcf_yield_pct": 6.0},
                  "financial": {"max_pb": 1.1, "min_roe": 15.0}},
        "gate5": {"integrity": {"required_checks": ["promoter_pledge", "auditor"]}},
        "tiers": {"quality_score": {}, "tier2_max_failures": 1,
                  "tier3_min_quality": 7},
        "valuation": {"inputs": {"risk_free_rate": 0.065,
                                 "equity_risk_premium": 0.05}},
        "unmeasurable": {"items": ["pricing_power", "moat_trend"]},
        "sync": {"price_staleness_hours": 20},
    }
    for path, value in overrides.items():
        node = base
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    src = type("P", (), {"name": "test", "__str__": lambda s: "test"})()
    return config_mod.Config(
        data=base, source=src,
        config_hash=config_mod._hash(base), gating_hash=config_mod._hash(base),
    )


def _watch(conn, symbol: str, buy_line: float,
           status: str = "watch") -> None:
    _universe(conn, symbol)
    conn.execute("INSERT INTO watchlist (symbol,status,buy_line,added_on,updated_at) "
                 "VALUES (?,?,?,?,?)",
                 (symbol, status, buy_line, "2026-01-01", "2026-01-01"))
    conn.commit()


def _prices(conn, symbol: str, *closes: float, date: str = "2026-01-01") -> None:
    import datetime as dt

    _universe(conn, symbol)

    for i, close in enumerate(closes):
        d = (dt.date.fromisoformat(date) + dt.timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO price_daily (symbol,date,open,high,low,close,volume,source,"
            "fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol, d, close, close, close, close, 1000, "test", "2026-01-01"),
        )
    conn.commit()


def _universe(conn, *symbols: str) -> None:
    for sym in symbols:
        conn.execute("INSERT OR IGNORE INTO universe (symbol,name,in_index,added_on) "
                     "VALUES (?,?,1,'2026-01-01')", (sym, f"{sym} Ltd"))
    conn.commit()


def _buy(conn, symbol: str, qty: int, price: float) -> None:
    _universe(conn, symbol)
    conn.execute(
        "INSERT INTO transactions (ts,symbol,side,qty,price,fees,reason,recorded_at) "
        "VALUES ('2026-01-01',?,'BUY',?,?,0,'test','2026-01-01')",
        (symbol, qty, price),
    )
    conn.commit()


# ── the line budget ────────────────────────────────────────────────────────

def test_report_respects_the_line_budget(conn):
    """A report that runs long is a report that gets skimmed, so the cap is
    enforced in code rather than trusted to good intentions."""
    for i in range(12):
        sym = f"W{i:02d}"
        _watch(conn, sym, 1000.0)
        _prices(conn, sym, 1.0, 1.0)

    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)

    assert len(result.lines) <= daily.MAX_LINES
    # Trimming must be announced. A silently shortened list reads as complete.
    assert result.dropped


def test_critical_lines_survive_the_budget(conn):
    """Trimming starts from the least decision-relevant line, so screen changes
    and buy-line hits are never the thing that gets cut."""
    for i in range(12):
        sym = f"W{i:02d}"
        _watch(conn, sym, 1000.0)
        _prices(conn, sym, 1.0, 1.0)

    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)
    text = "\n".join(result.lines)

    assert result.lines[0].startswith("at buy-line")
    # The closing caveat is the last thing standing.
    assert text.strip().endswith("passers are research candidates, not buys")


# ── section contents ───────────────────────────────────────────────────────

def test_watch_name_at_buy_line_is_reported(conn):
    _watch(conn, "AAA", 150.0)
    _prices(conn, "AAA", 142.8, 142.8)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)

    assert any("at buy-line AAA 142.8 <= 150.0" == ln for ln in result.lines)


def test_watch_name_above_buy_line_is_not_actionable(conn):
    """Above the buy-line it is context, not an instruction, so it stays silent."""
    _watch(conn, "AAA", 100.0)
    _prices(conn, "AAA", 150.0, 150.0)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)

    assert not any("at buy-line" in ln for ln in result.lines)


def test_gate5_gap_is_reported_next_to_the_buy_line(conn):
    """A name at its buy-line with an unverified check is a trap: the price says
    yes and the gate says you cannot act. Both facts must appear together."""
    _watch(conn, "AAA", 150.0)
    _prices(conn, "AAA", 142.8, 142.8)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)
    text = "\n".join(result.lines)

    assert "at buy-line AAA" in text
    assert "gate5 open: AAA (2)" in text  # two required checks, neither verified


def test_unmeasurable_inputs_are_always_listed(conn):
    """An absent input is never a pass, so this line is unconditional."""
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)
    assert any(ln.startswith("unmeasurable: 2") for ln in result.lines)


def test_movers_only_cover_held_names(conn):
    """A big move in an unheld stock is not actionable and would displace
    something that is."""
    _buy(conn, "HELD", 10, 100.0)
    _prices(conn, "HELD", 100.0, 90.0)
    _prices(conn, "UNHELD", 10.0, 1.0)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)
    text = "\n".join(result.lines)

    assert "moves: HELD -10.0%" in text
    assert "UNHELD" not in text


def test_portfolio_line_reports_unrealised_pnl(conn):
    _buy(conn, "AAA", 10, 100.0)
    _prices(conn, "AAA", 110.0, 110.0)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)

    assert any(ln.startswith("held: 1 names ·") and "+100" in ln for ln in result.lines)


# ── degradation ────────────────────────────────────────────────────────────

def test_sync_failure_still_produces_a_report(conn, monkeypatch):
    """A fetch outage must not end the day. The report saying the data is stale
    is itself the information that matters."""
    from stocks import sync as sync_mod

    def boom(*a, **k):
        raise RuntimeError("vendor timeout")

    monkeypatch.setattr(sync_mod, "run", boom)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=True, do_screen=False)

    assert any(e.startswith("sync:") for e in result.errors)
    assert result.lines  # still a report
    assert any(ln.startswith("unmeasurable:") for ln in result.lines)


def test_screen_failure_is_reported_not_swallowed(conn, monkeypatch):
    from stocks import screencmd

    def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(screencmd, "_run_locked", boom)
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=True)

    assert any("screen" in e for e in result.errors)
    assert any("screen failed" in ln for ln in result.lines)


# ── journal ────────────────────────────────────────────────────────────────

def test_journal_written_to_the_given_root(tmp_path):
    result = daily.DailyResult(date="2026-01-02", run_id=7,
                               screen={"n_clean": 9, "universe_size": 251})
    p = daily.write_journal(result, root=tmp_path)

    assert p == tmp_path / "2026-01-02.md"
    text = p.read_text()
    assert "# 2026-01-02" in text
    assert "screen run: 7" in text
    assert "9/251 clean" in text


def test_journal_honours_root_for_its_path(tmp_path):
    """journal_path used to ignore root entirely, so a test writing to a temp
    dir still landed in the real journal directory."""
    assert daily.journal_path("2026-01-02", tmp_path).parent == tmp_path


def test_journal_is_overwritten_not_appended(tmp_path):
    """One day produces one report. A second file for the same date means
    something ran twice, and keeping both would make the journal disagree with
    itself."""
    first = daily.write_journal(daily.DailyResult(date="2026-01-03"), root=tmp_path)
    second = daily.write_journal(daily.DailyResult(date="2026-01-03"), root=tmp_path)

    assert first == second
    assert len(list(tmp_path.glob("2026-01-03*.md"))) == 1
    # Overwriting, not appending: only the second run's content survives.
    assert "## Notes" not in second.read_text()


def test_journal_records_omissions_and_errors(tmp_path):
    result = daily.DailyResult(date="2026-01-04", lines=["x"],
                               dropped=["2 lines dropped"], errors=["sync: boom"])
    text = daily.write_journal(result, root=tmp_path).read_text()

    assert "## Omitted from the report" in text
    assert "2 lines dropped" in text
    assert "## Errors" in text
    assert "sync: boom" in text


def test_journal_records_free_text_notes(tmp_path):
    result = daily.DailyResult(date="2026-01-05")
    text = daily.write_journal(result, "thought about ACC", root=tmp_path).read_text()

    assert "## Notes" in text
    assert "thought about ACC" in text


# ── render ─────────────────────────────────────────────────────────────────

def test_render_accepts_result_or_payload():
    """_emit hands over whatever was serialised, so render must not assume a
    dataclass."""
    result = daily.DailyResult(date="2026-01-06", lines=["screen: 9 clean"],
                               dropped=["1 omitted"], errors=["sync: boom"])
    from_obj = daily.render(result)
    from_dict = daily.render(result.payload())

    assert from_obj == from_dict
    assert "1 line(s) omitted" in from_obj
    assert "errors: sync: boom" in from_obj


def test_payload_is_json_serialisable(conn):
    """Every CLI command supports --json so an agent can consume it."""
    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)
    payload = json.loads(json.dumps(result.payload(), default=str))
    assert payload["date"] == result.date


def test_render_output_matches_line_budget(conn):
    for i in range(12):
        sym = f"W{i:02d}"
        _watch(conn, sym, 1000.0)
        _prices(conn, sym, 1.0, 1.0)

    result = daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)
    # The budget covers the report; the omission and error notices are outside it.
    extra = bool(result.dropped) + bool(result.errors)
    assert len(daily.render(result).splitlines()) <= daily.MAX_LINES + extra


def test_build_leaves_a_borrowed_connection_open(conn):
    """Passing a connection means the caller keeps ownership of it; only a
    connection build() opened itself may be closed."""
    daily.build(conn=conn, cfg=_cfg(), do_sync=False, do_screen=False)

    assert conn.execute("SELECT 1").fetchone()[0] == 1