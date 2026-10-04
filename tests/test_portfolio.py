"""Portfolio ledger: refusals, cost basis, and money-weighted return.

The invariants worth protecting, in order of how badly they bite when broken:

* an unverified Gate 5 check blocks a purchase, and an override is recorded,
* the ledger is the only source of position truth, so an unrecorded buy cannot
  be sold against,
* cost basis is average-cost including fees, matching broker convention,
* a function that cannot compute an honest number returns None instead of a
  plausible-looking one.
"""

from __future__ import annotations

import pytest

from stocks import db, portfolio
from stocks.config import Config, _hash

REQUIRED = ["promoter_pledge", "auditor", "regulatory"]


def _cfg(**over) -> Config:
    data = {
        "gate5": {"integrity": {"required_checks": REQUIRED}},
        "universe": {"benchmark": "NIFTY_SMALLCAP250", "benchmark_wire": ""},
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
    for sym in ("AAA", "BBB"):
        c.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                  "VALUES (?,'x',1,'2026-01-01')", (sym,))
    c.commit()
    yield c
    c.close()


def _verify(c, symbol, names=REQUIRED, status="verified", evidence="annual report"):
    for n in names:
        c.execute("INSERT OR REPLACE INTO integrity_check "
                  "(symbol,check_name,status,evidence,verified_at) "
                  "VALUES (?,?,?,?,'2026-02-01')", (symbol, n, status, evidence))
    c.commit()


def _price(c, symbol, close, date):
    c.execute("INSERT OR REPLACE INTO price_daily "
              "(symbol,date,open,high,low,close,volume,source,fetched_at) "
              "VALUES (?,?,?,?,?,?,0,'test','2026-03-02')",
              (symbol, date, close, close, close, close))


# ────────────────────────────────────────────────────────────── refusals ────


def test_buy_refused_when_integrity_unverified(conn):
    with pytest.raises(portfolio.IntegrityError) as e:
        portfolio.record_transaction(conn, "AAA", "BUY", 10, 100, reason="cheap",
                                     cfg=_cfg())
    msg = str(e.value)
    assert "promoter_pledge" in msg
    assert "--override-integrity" in msg
    assert conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 0


def test_missing_check_row_is_not_a_pass(conn):
    """Two of three verified is still unverified: absence is not an answer."""
    _verify(conn, "AAA", names=REQUIRED[:2])
    with pytest.raises(portfolio.IntegrityError) as e:
        portfolio.record_transaction(conn, "AAA", "BUY", 10, 100, reason="r", cfg=_cfg())
    assert "regulatory" in str(e.value)


def test_failed_check_blocks_the_buy(conn):
    _verify(conn, "AAA")
    conn.execute("UPDATE integrity_check SET status='failed' WHERE symbol='AAA' "
                 "AND check_name='auditor'")
    conn.commit()
    with pytest.raises(portfolio.IntegrityError):
        portfolio.record_transaction(conn, "AAA", "BUY", 10, 100, reason="r", cfg=_cfg())


def test_override_is_allowed_and_recorded(conn):
    tx = portfolio.record_transaction(
        conn, "AAA", "BUY", 10, 100, reason="thesis signed off",
        cfg=_cfg(), override_integrity="auditor opinion read in the filing")
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx,)).fetchone()
    assert row["integrity_override"] == "auditor opinion read in the filing"


def test_sell_does_not_require_integrity(conn):
    """Selling to escape an unresolved concern must not be blocked by Gate 5.

    The gate exists to stop money going in. Refusing an exit because paperwork
    is missing would trap capital in a name that is deteriorating.
    """
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 10, 100, reason="r", cfg=_cfg())
    conn.execute("UPDATE integrity_check SET status='unknown' WHERE symbol='AAA'")
    conn.commit()
    portfolio.record_transaction(conn, "AAA", "SELL", 5, 120, reason="exit", cfg=_cfg())
    assert portfolio.held_qty(conn, "AAA") == 5


def test_sell_more_than_held_refused(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 10, 100, reason="r", cfg=_cfg())
    with pytest.raises(portfolio.PortfolioError) as e:
        portfolio.record_transaction(conn, "AAA", "SELL", 11, 120, reason="r", cfg=_cfg())
    assert "holding 10" in str(e.value)


def test_unknown_symbol_suggests_alternatives(conn):
    with pytest.raises(portfolio.PortfolioError) as e:
        portfolio.record_transaction(conn, "AA", "BUY", 1, 1, reason="r", cfg=_cfg())
    assert "not in the tracked universe" in str(e.value)
    assert "AAA" in str(e.value)


@pytest.mark.parametrize("kwargs,needle", [
    ({"qty": 0}, "qty must be positive"),
    ({"price": 0}, "price must be positive"),
    ({"fees": -1}, "fees cannot be negative"),
    ({"reason": "   "}, "reason is required"),
])
def test_bad_input_refused(conn, kwargs, needle):
    _verify(conn, "AAA")
    args = {"qty": 10, "price": 100.0, "fees": 0.0, "reason": "ok"}
    args.update(kwargs)
    with pytest.raises(portfolio.PortfolioError) as e:
        portfolio.record_transaction(conn, "AAA", "BUY", cfg=_cfg(), **args)
    assert needle in str(e.value)


# ─────────────────────────────────────────────────────── cost basis ────


def test_average_cost_includes_fees(conn):
    """Fees capitalise into basis, so a round trip is never flattered."""
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, fees=50.0,
                                 reason="r", cfg=_cfg())
    p = portfolio.positions(conn)[0]
    assert p.avg_cost == pytest.approx(10.5)


def test_sale_does_not_change_average_cost(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 20.0, reason="r", cfg=_cfg())
    portfolio.record_transaction(conn, "AAA", "SELL", 100, 30.0, reason="r", cfg=_cfg())
    p = portfolio.positions(conn)[0]
    assert p.qty == 100
    assert p.avg_cost == pytest.approx(15.0)


def test_realised_pnl_uses_average_cost_at_time_of_sale(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 20.0, reason="r", cfg=_cfg())
    # Sell half of a 100+100 holding bought at an average of 15.
    portfolio.record_transaction(conn, "AAA", "SELL", 100, 30.0, reason="r", cfg=_cfg())
    assert portfolio.realised_pnl(conn) == pytest.approx(1500.0)


def test_realised_pnl_net_of_selling_fees(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    portfolio.record_transaction(conn, "AAA", "SELL", 100, 12.0, fees=25.0,
                                 reason="r", cfg=_cfg())
    assert portfolio.realised_pnl(conn) == pytest.approx(175.0)


def test_closed_position_is_not_a_position(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    portfolio.record_transaction(conn, "AAA", "SELL", 100, 12.0, reason="r", cfg=_cfg())
    assert portfolio.held_qty(conn, "AAA") == 0
    assert portfolio.positions(conn) == []


def test_mark_to_market_uses_latest_close(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    _price(conn, "AAA", 9.0, "2026-03-01")
    _price(conn, "AAA", 12.0, "2026-03-02")
    p = portfolio.positions(conn)[0]
    assert p.price == 12.0
    assert p.market_value == 1200.0
    assert p.unrealised == pytest.approx(200.0)
    assert p.unrealised_pct == pytest.approx(20.0)
    assert p.day_change_pct == pytest.approx((12 - 9) / 9 * 100)


def test_unpriced_holding_is_excluded_not_assumed_unchanged(conn):
    """A holding with no price must not be valued at cost by default."""
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    report = portfolio.pnl(conn=conn, cfg=_cfg())
    assert report.market_value == 0.0
    assert any("no price for AAA" in n for n in report.notes)


def test_pnl_totals(conn):
    _verify(conn, "AAA")
    _verify(conn, "BBB")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg())
    portfolio.record_transaction(conn, "BBB", "BUY", 50, 20.0, reason="r", cfg=_cfg())
    _price(conn, "AAA", 15.0, "2026-03-02")   # 1000 -> 1500, +500
    _price(conn, "BBB", 25.0, "2026-03-02")   # 1000 -> 1250, +250
    r = portfolio.pnl(conn=conn, cfg=_cfg())
    assert r.invested == pytest.approx(2000.0)
    assert r.market_value == pytest.approx(2750.0)
    assert r.unrealised == pytest.approx(750.0)
    assert r.unrealised_pct == pytest.approx(37.5)


def test_since_filters_realised_only(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg(),
                                 ts="2026-01-05T00:00:00+00:00")
    portfolio.record_transaction(conn, "AAA", "SELL", 50, 12.0, reason="r", cfg=_cfg(),
                                 ts="2026-02-05T00:00:00+00:00")
    assert portfolio.realised_pnl(conn, since="2026-03-01") == 0.0
    assert portfolio.realised_pnl(conn, since="2026-01-01") == pytest.approx(100.0)


# ──────────────────────────────────────────────────────────────── XIRR ────


def test_xirr_known_answer():
    # -1000 then +1210 a year later is 21% money-weighted.
    r = portfolio.xirr([("2026-01-01", -1000.0), ("2027-01-01", 1210.0)])
    assert r == pytest.approx(0.21, abs=1e-4)


def test_xirr_is_not_naive_average_of_returns():
    """Money-weighted, so a large early loss dominates a small late gain."""
    r = portfolio.xirr([("2026-01-01", -1000.0), ("2027-01-01", 1050.0)])
    assert r == pytest.approx(0.05, abs=1e-4)


def test_xirr_returns_none_rather_than_a_guess():
    assert portfolio.xirr([]) is None
    assert portfolio.xirr([("2026-01-01", 100.0)]) is None
    # All one sign: no root exists, so no rate is reported.
    assert portfolio.xirr([("2026-01-01", -100.0), ("2027-01-01", -50.0)]) is None


def test_xirr_from_ledger_cashflows(conn):
    _verify(conn, "AAA")
    portfolio.record_transaction(conn, "AAA", "BUY", 100, 10.0, reason="r", cfg=_cfg(),
                                 ts="2026-01-01T00:00:00+00:00")
    portfolio.record_transaction(conn, "AAA", "SELL", 100, 11.0, reason="r", cfg=_cfg(),
                                 ts="2027-01-01T00:00:00+00:00")
    flows = [(r["d"], r["amount"]) for r in conn.execute(
        "SELECT d, amount FROM v_cashflows ORDER BY d")]
    assert portfolio.xirr(flows) == pytest.approx(0.10, abs=1e-3)


# ───────────────────────────────────────────────────────────── benchmark ────


def test_benchmark_refuses_when_wire_unconfirmed(conn):
    """No confirmed identity means no relative number, even if asked for."""
    b = portfolio.benchmark_performance(conn, _cfg())
    assert not b["available"]
    assert "unconfirmed" in b["reason"]


def test_benchmark_reports_window_when_confirmed(conn):
    conn.execute("INSERT INTO price_benchmark (index_code,date,close,source,fetched_at) "
              "VALUES ('NIFTY_SMALLCAP250','2026-01-01',100.0,'test','2026-03-02')")
    conn.execute("INSERT INTO price_benchmark (index_code,date,close,source,fetched_at) "
              "VALUES ('NIFTY_SMALLCAP250','2026-03-01',110.0,'test','2026-03-02')")
    conn.commit()
    cfg = _cfg(**{"universe.benchmark_wire": "^CONFIRMED"})
    b = portfolio.benchmark_performance(conn, cfg)
    assert b["available"]
    assert b["first_date"] == "2026-01-01" and b["last_date"] == "2026-03-01"


def test_benchmark_confirmed_but_no_bars_says_so(conn):
    b = portfolio.benchmark_performance(conn, _cfg(**{"universe.benchmark_wire": "^X"}))
    assert not b["available"]
    assert "no benchmark bars" in b["reason"]


def test_integrity_ledger_lists_every_required_check(conn):
    _verify(conn, "AAA", names=REQUIRED[:1])
    ledger = {d["check_name"]: d for d in
              portfolio.integrity_ledger(conn, _cfg(), "AAA")}
    assert set(ledger) == set(REQUIRED)
    assert ledger["auditor"]["status"] == "unknown"
    assert ledger["auditor"]["required"] is True


def test_render_reports_no_positions(conn):
    assert "0 open position(s)" in portfolio.render_pnl(conn=conn, cfg=_cfg())


def test_render_shows_benchmark_unavailable(conn):
    text = portfolio.render_pnl(conn=conn, cfg=_cfg(), benchmark=True)
    assert "benchmark unavailable" in text