"""Health tests.

The point of `health` is that absence stays visible, so these tests check that
each way of being wrongly absent is reported rather than smoothed over.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stocks import db, health, sync
from stocks.config import Config, _hash
from stocks.providers import PriceBar


def _cfg(**over) -> Config:
    data = {
        "sync": {"price_staleness_hours": 20, "fundamentals_staleness_days": 7,
                 "profile_staleness_days": 7, "full_screen_staleness_days": 7},
        "universe": {"benchmark": "NIFTY_SMALLCAP250", "benchmark_wire": ""},
        "valuation": {"inputs": {"risk_free_rate": 0.065, "stale_after_days": 180}},
        "gate5": {"integrity": {"required_checks": ["promoter_pledge", "auditor"]}},
        "sector_overrides": {"financials": {"requires_filing_analysis": True}},
        "unmeasurable": {"items": ["pricing_power", "moat_trend"]},
    }
    for path, value in over.items():
        node = data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data=data, source=type("P", (), {"__str__": lambda s: "t"})(),
                  config_hash=_hash(data), gating_hash=_hash(data))


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.apply_schema(c)
    c.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
              "VALUES ('AAA','AAA Ltd',1,'2026-01-01')")
    c.commit()
    yield c
    c.close()


def _named(h: health.Health, name: str) -> health.Check:
    return next(c for c in h.checks if c.name == name)


def test_empty_database_reports_bad_not_ok(conn):
    """A fresh database has never been fetched; that must not read as healthy."""
    h = health.build(cfg=_cfg(), conn=conn)
    assert h.worst == health.BAD
    assert _named(h, "prices").status == health.BAD
    assert _named(h, "fundamentals").status == health.BAD


def test_price_coverage_counts_symbols_not_rows(conn):
    """500 bars for one symbol must not look like 500 symbols covered."""
    sync.write_prices(conn, "AAA", [PriceBar(date=f"2026-01-{d:02d}", close=1.0)
                                    for d in range(1, 29)], "fake")
    h = health.build(cfg=_cfg(), conn=conn)
    assert _named(h, "price coverage").status == health.OK


def test_price_coverage_flags_missing_symbol(conn):
    conn.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                 "VALUES ('BBB','BBB Ltd',1,'2026-01-01')")
    sync.write_prices(conn, "AAA", [PriceBar(date="2026-01-01", close=1.0)], "fake")
    conn.commit()
    check = _named(health.build(cfg=_cfg(), conn=conn), "price coverage")
    assert check.status == health.WARN
    assert "1 symbol(s) have no bars" in check.detail


def test_stale_data_is_warn_not_bad(conn):
    """Stale data is recoverable by syncing; missing data is not."""
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).isoformat()
    conn.execute("INSERT INTO fetch_log (ts,source,kind,ok,n_items) VALUES (?,?,?,1,1)",
                 (old, "fake", "prices"))
    conn.commit()
    assert _named(health.build(cfg=_cfg(), conn=conn), "prices").status == health.WARN


def test_blocked_symbols_are_counted(conn):
    # AAA is already in the fixture; BBB needs adding.
    for code, sym in (("no_financials", "AAA"), ("insufficient_history", "BBB")):
        if sym != "AAA":
            conn.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                         "VALUES (?,'x',1,'2026-01-01')", (sym,))
        conn.execute("INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at) "
                     "VALUES (?,?,'block','detail','2026-01-01')", (sym, code))
    conn.commit()
    h = health.build(cfg=_cfg(), conn=conn)
    assert _named(h, "issue: no_financials").status == health.WARN
    assert "stocks health --explain no_financials" in _named(h, "issue: no_financials").fix


def test_resolved_issues_are_not_counted(conn):
    conn.execute("INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at,"
                 "resolved_at) VALUES ('AAA','no_financials','block','d','2026-01-01',"
                 "'2026-02-01')")
    conn.commit()
    h = health.build(cfg=_cfg(), conn=conn)
    assert not any(c.name.startswith("issue:") for c in h.checks)


def test_unconfirmed_benchmark_is_warned(conn):
    check = _named(health.build(cfg=_cfg(), conn=conn), "benchmark")
    assert check.status == health.WARN
    assert "unconfirmed" in check.detail
    assert "benchmark_wire" in (check.fix or "")


def test_confirmed_benchmark_with_bars_is_ok(conn):
    cfg = _cfg(**{"universe.benchmark_wire": "^CONFIRMED"})
    sync.write_benchmark(conn, "NIFTY_SMALLCAP250",
                         [PriceBar(date="2026-01-01", close=100.0)], "fake")
    conn.commit()
    assert _named(health.build(cfg=cfg, conn=conn), "benchmark").status == health.OK


def test_missing_risk_free_rate_is_bad(conn):
    """Without it ROIC>WACC cannot be judged, and that is a silent gate failure."""
    cfg = _cfg(**{"valuation.inputs.risk_free_rate": None})
    check = _named(health.build(cfg=cfg, conn=conn), "risk_free_rate")
    assert check.status == health.BAD


def test_stale_risk_free_rate_is_warned(conn, tmp_path, monkeypatch):
    cfg = _cfg(**{"valuation.inputs.stale_after_days": 1})
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
    monkeypatch.setattr(health.paths, "config_path",
                        lambda: tmp_path / "screen.toml")
    (tmp_path / "screen.toml").write_text("")
    import os
    os.utime(tmp_path / "screen.toml", (old.timestamp(), old.timestamp()))
    check = _named(health.build(cfg=cfg, conn=conn), "risk_free_rate")
    assert check.status == health.WARN
    assert "refresh every 1d" in check.detail


def test_unmeasurable_is_always_surfaced(conn):
    """Structurally unavailable inputs are reported on every run, not once."""
    for _ in range(2):
        h = health.build(cfg=_cfg(), conn=conn)
        check = _named(h, "unmeasurable")
        assert check.status == health.WARN
        assert "pricing_power" in check.detail


def test_unmeasurable_list_empty_is_warned(conn):
    cfg = _cfg(**{"unmeasurable.items": []})
    assert _named(health.build(cfg=cfg, conn=conn), "unmeasurable").status == health.WARN


def test_filing_analysis_flagged(conn):
    conn.execute("UPDATE universe SET requires_filing = 1")
    conn.commit()
    check = _named(health.build(cfg=_cfg(), conn=conn), "filing analysis")
    assert check.status == health.WARN
    assert "NPA/CET1" in check.detail


def test_integrity_unverified_is_warned(conn):
    conn.execute("INSERT INTO watchlist (symbol,added_on,status,updated_at) "
                 "VALUES ('AAA','2026-01-01','researching','2026-01-01')")
    conn.commit()
    check = _named(health.build(cfg=_cfg(), conn=conn), "integrity")
    assert check.status == health.WARN
    assert "promoter_pledge" in check.detail


def test_integrity_verified_is_ok(conn):
    conn.execute("INSERT INTO watchlist (symbol,added_on,status,updated_at) "
                 "VALUES ('AAA','2026-01-01','researching','2026-01-01')")
    for c in ("promoter_pledge", "auditor"):
        conn.execute("INSERT INTO integrity_check (symbol,check_name,status,verified_at) "
                     "VALUES ('AAA',?,'verified','2026-02-01')", (c,))
    conn.commit()
    assert _named(health.build(cfg=_cfg(), conn=conn), "integrity").status == health.OK


def test_integrity_empty_required_checks_is_bad(conn):
    cfg = _cfg(**{"gate5.integrity.required_checks": []})
    assert _named(health.build(cfg=cfg, conn=conn), "integrity").status == health.BAD


def test_never_screened_is_warned(conn):
    check = _named(health.build(cfg=_cfg(), conn=conn), "screen run")
    assert check.status == health.WARN and "never run" in check.detail


def test_recent_screen_is_ok(conn):
    conn.execute(
        "INSERT INTO screen_run (started_at,finished_at,config_hash,config_json) "
        "VALUES (?,?,?,?)",
        (dt.datetime.now(dt.UTC).isoformat(),
         dt.datetime.now(dt.UTC).isoformat(), "h", "{}"),
    )
    conn.commit()
    assert _named(health.build(cfg=_cfg(), conn=conn), "screen run").status == health.OK


def test_render_hides_ok_checks_and_gives_a_fix(conn):
    h = health.build(cfg=_cfg(), conn=conn)
    text = h.render()
    assert "[ ok ]" not in text
    assert "→" in text
    assert text.strip().splitlines()[-1].endswith("bad")


def test_explain_code_lists_symbols(conn):
    conn.execute("INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at) "
                 "VALUES ('AAA','no_financials','block','vendor empty','2026-01-01')")
    conn.commit()
    lines = health.explain_code(conn, "no_financials")
    assert lines == ["AAA: vendor empty"]
    assert health.explain_code(conn, "nothing") == []