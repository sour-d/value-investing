"""Sync tests.

The network is never touched. What is under test is the decision logic: what is
fetched, what is skipped, and what happens when a provider returns nothing for
a symbol it was asked about.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stocks import db, sync
from stocks.config import Config
from stocks.providers import Financials, PriceBar, Quote


def _cfg(**over) -> Config:
    data = {
        "sync": {
            "price_staleness_hours": 20,
            "fundamentals_staleness_days": 7,
            "profile_staleness_days": 7,
        },
        # Default to a confirmed wire code; tests that care about the unconfirmed
        # case pass benchmark_wire="".
        "universe": {"benchmark": "NIFTY_SMALLCAP250", "benchmark_wire": "^CONFIRMED"},
        "sector_overrides": {"financials": {"requires_filing_analysis": True}},
    }
    for path, value in over.items():
        node = data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    from stocks.config import _hash
    return Config(data=data, source=type("P", (), {"__str__": lambda s: "test"})(),
                  config_hash=_hash(data), gating_hash=_hash(data))


class FakeProvider:
    """A provider that records calls and returns whatever it was primed with."""

    name = "fake"

    def __init__(self, *, prices=None, quotes=None, financials=None, benchmark=None):
        self._prices = prices or {}
        self._quotes = quotes if quotes is not None else {}
        self._financials = financials or {}
        self._benchmark = benchmark or []
        self.calls: list[tuple[str, int]] = []

    def prices(self, symbols, start, end):
        self.calls.append(("prices", len(symbols)))
        return {s: self._prices.get(s, []) for s in symbols}

    def quotes(self, symbols):
        self.calls.append(("quotes", len(symbols)))
        return {s: self._quotes.get(s) for s in symbols if s in self._quotes}

    def financials(self, symbols):
        self.calls.append(("financials", len(symbols)))
        return {s: self._financials.get(s) for s in symbols}

    def benchmark(self, index_code, start, end):
        self.calls.append(("benchmark", 1))
        return self._benchmark


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.apply_schema(c)
    for s in ("AAA", "BBB"):
        c.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                  "VALUES (?,?,1,'2026-01-01')", (s, f"{s} Ltd"))
    c.commit()
    yield c
    c.close()


def _bars(n: int = 3) -> list[PriceBar]:
    return [PriceBar(date=f"2026-01-0{i + 1}", open=1.0, high=2.0, low=0.5,
                     close=1.5, adj_close=1.4, volume=100) for i in range(n)]


# ───────────────────────────────────────────────────────────── planning ────


def test_fresh_database_plans_everything(conn):
    p = sync.plan(conn, _cfg())
    assert p.prices == ["AAA", "BBB"]
    assert p.financials == ["AAA", "BBB"]
    assert p.quotes == ["AAA", "BBB"]
    assert p.benchmark


def test_recent_fetch_makes_data_fresh(conn):
    sync._log(conn, "fake", "prices", None, True, n_items=1)
    sync._log(conn, "fake", "financials", None, True, n_items=1)
    sync._log(conn, "fake", "profile", None, True, n_items=1)
    conn.commit()
    p = sync.plan(conn, _cfg())
    assert p.prices == [] and p.financials == [] and p.quotes == []
    # The benchmark still needs a first fetch.
    assert p.benchmark


def test_failed_fetch_does_not_count_as_fresh(conn):
    """A sync that returned nothing must be retried, not remembered as done."""
    sync._log(conn, "fake", "prices", None, False, error="boom")
    conn.commit()
    assert sync.plan(conn, _cfg()).prices == ["AAA", "BBB"]


def test_stale_threshold_is_respected(conn):
    """Prices go stale after 20h; fundamentals after 7 days."""
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
    conn.execute("INSERT INTO fetch_log (ts,source,kind,ok,n_items) VALUES (?,?,?,1,1)",
                 (old, "fake", "prices"))
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute("INSERT INTO fetch_log (ts,source,kind,ok,n_items) VALUES (?,?,?,1,1)",
                 (recent, "fake", "financials"))
    conn.commit()
    p = sync.plan(conn, _cfg())
    assert p.prices, "30h old prices should be refetched"
    assert not p.financials, "fresh fundamentals should be skipped"


def test_stale_days_boundary(conn):
    just_inside = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)).isoformat()
    conn.execute("INSERT INTO fetch_log (ts,source,kind,ok,n_items) VALUES (?,?,?,1,1)",
                 (just_inside, "fake", "financials"))
    conn.commit()
    assert not sync.plan(conn, _cfg()).financials

    just_outside = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
    conn.execute("UPDATE fetch_log SET ts = ? WHERE kind = 'financials'", (just_outside,))
    conn.commit()
    assert sync.plan(conn, _cfg()).financials


def test_full_sync_overrides_staleness(conn):
    sync._log(conn, "fake", "prices", None, True, n_items=1)
    conn.commit()
    p = sync.plan(conn, _cfg(), full=True)
    assert p.prices == ["AAA", "BBB"] and p.benchmark


def test_removed_symbols_are_not_fetched(conn):
    conn.execute("UPDATE universe SET in_index = 0 WHERE symbol = 'BBB'")
    conn.commit()
    assert sync.plan(conn, _cfg()).prices == ["AAA"]


# ─────────────────────────────────────────────────────────────── writes ────


def test_write_prices_is_idempotent(conn):
    bars = _bars()
    assert sync.write_prices(conn, "AAA", bars, "fake") == 3
    assert sync.write_prices(conn, "AAA", bars, "fake") == 3
    assert db.count(conn, "price_daily", "symbol = 'AAA'") == 3


def test_price_restatement_updates_in_place(conn):
    sync.write_prices(conn, "AAA", _bars(), "fake")
    revised = [PriceBar(date="2026-01-01", close=9.9, adj_close=9.9)]
    sync.write_prices(conn, "AAA", revised, "fake")
    row = conn.execute("SELECT close FROM price_daily WHERE symbol='AAA' "
                       "AND date='2026-01-01'").fetchone()
    assert row["close"] == 9.9


def test_fundamental_change_is_dated(conn):
    fin = Financials("AAA", {"income": {"Net Income": {"2025-03-31": 100.0}}})
    sync.write_financials(conn, fin, "fake")
    row = conn.execute("SELECT value, changed_at FROM fundamentals_annual").fetchone()
    assert row["value"] == 100.0
    assert row["changed_at"] is None, "first sighting is not a change"

    sync.write_financials(conn, Financials("AAA", {"income": {"Net Income": {"2025-03-31": 90.0}}}), "fake")
    row = conn.execute("SELECT value, changed_at FROM fundamentals_annual").fetchone()
    assert row["value"] == 90.0
    assert row["changed_at"] is not None, "a restatement must be dated"


def test_profile_stays_untrusted(conn):
    q = Quote("AAA", "2026-02-01", price=100.0, market_cap=1000.0,
              profile={"marketCap": 1000.0, "trailingPE": 12.5,
                       "longBusinessSummary": "does things"})
    sync.write_quotes(conn, {"AAA": q}, "fake")
    row = conn.execute("SELECT * FROM profile_snapshot WHERE symbol='AAA'").fetchone()
    assert row["market_cap"] == 1000.0
    assert row["trailing_pe"] == 12.5
    assert row["business_summary"] == "does things"
    assert row["trusted"] == 0, "a vendor claim is not a verified fact"


def test_analyst_targets_are_not_stored(conn):
    """Targets would anchor the valuation judgement the screen is making."""
    q = Quote("AAA", "2026-02-01", profile={
        "targetMeanPrice": 999.0, "recommendationKey": "buy",
        "marketCap": 10.0,
    })
    sync.write_quotes(conn, {"AAA": q}, "fake")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile_snapshot)")}
    assert "target_mean_price" not in cols
    assert "recommendation_key" not in cols


# ────────────────────────────────────────────────────────────── driver ────


def test_run_records_every_symbol(conn, monkeypatch):
    prov = FakeProvider(prices={"AAA": _bars(), "BBB": _bars()},
                        quotes={"AAA": Quote("AAA", "2026-02-01")},
                        financials={"AAA": Financials("AAA", {"income": {"Net Income": {"2025-03-31": 1.0}}})},
                        benchmark=_bars())
    monkeypatch.setattr(sync, "plan", lambda *a, **k: sync.SyncPlan(
        prices=["AAA", "BBB"], quotes=["AAA"], financials=["AAA"], benchmark=True))
    result = sync.run(provider=prov, cfg=_cfg(), conn=conn)

    assert result.n_prices == 6
    assert result.n_quotes == 1
    assert result.n_financials == 1
    assert result.n_benchmark == 3
    kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM fetch_log")}
    assert {"prices", "profile", "financials", "benchmark"} <= kinds


def test_empty_symbol_response_is_reported_not_hidden(conn):
    """A provider that returns nothing for a symbol must be visible."""
    prov = FakeProvider(prices={}, quotes={}, financials={}, benchmark=[])
    result = sync.run(provider=prov, cfg=_cfg(), full=True, conn=conn)
    assert result.n_prices == 0
    assert result.errors
    assert any("prices" in e for e in result.errors)
    # And the failure is logged, so the next run retries instead of trusting it.
    assert conn.execute("SELECT COUNT(*) AS n FROM fetch_log WHERE ok = 0"
                        ).fetchone()["n"] > 0


def test_silent_provider_returns_nothing_at_all(conn):
    prov = FakeProvider(prices={"AAA": _bars()}, benchmark=[])
    result = sync.run(provider=prov, cfg=_cfg(), full=True, conn=conn)
    assert result.n_prices == 3
    assert any("returned nothing" in e for e in result.errors)


def test_provider_exception_is_caught_not_raised(conn):
    class Broken:
        name = "broken"

        def prices(self, *a):
            raise RuntimeError("network down")

        def quotes(self, *a):
            raise RuntimeError("network down")

        def financials(self, *a):
            raise RuntimeError("network down")

        def benchmark(self, *a):
            raise RuntimeError("network down")

    result = sync.run(provider=Broken(), cfg=_cfg(), full=True, conn=conn)
    assert result.n_prices == 0
    assert len(result.errors) == 4
    assert all("network down" in e for e in result.errors)


def test_summary_is_short(conn):
    result = sync.SyncResult(plan=sync.SyncPlan(), n_prices=5, n_quotes=251)
    summary = result.summary()
    assert len(summary.splitlines()) == 1
    assert "5 price bars" in summary and "251 quotes" in summary


def test_nothing_to_fetch_says_nothing_to_fetch(conn):
    result = sync.run(provider=FakeProvider(), cfg=_cfg(), conn=conn)
    assert "nothing to fetch" in result.summary()


def test_benchmark_not_fetched_when_wire_unconfirmed(conn):
    """No confirmed wire code means no benchmark fetch, not a wrong index."""
    p = sync.plan(conn, _cfg(**{"universe.benchmark_wire": ""}))
    assert not p.benchmark
    assert "unconfirmed" in p.reasons["benchmark"]


def test_benchmark_resolved_from_config(conn):
    cfg = _cfg(**{"universe.benchmark_wire": "^CONFIRMED"})
    index_code, wire = sync.resolve_benchmark(cfg)
    assert index_code == "NIFTY_SMALLCAP250" and wire == "^CONFIRMED"
    assert sync.plan(conn, cfg).benchmark


def test_unconfirmed_benchmark_is_reported_not_silently_skipped(conn):
    prov = FakeProvider(benchmark=[])
    cfg = _cfg(**{"universe.benchmark_wire": ""})
    result = sync.run(provider=prov, cfg=cfg, full=True, conn=conn)
    assert result.n_benchmark == 0
    # No error either: an unconfirmed benchmark is not a failed fetch, it is a
    # configuration gap that `stocks health` owns.
    assert not any("benchmark" in e for e in result.errors)


def test_benchmark_error_does_not_abort_other_kinds(conn):
    """A benchmark failure must not cost us the price and quote fetch."""
    prov = FakeProvider(prices={"AAA": _bars(), "BBB": _bars()},
                        quotes={"AAA": Quote("AAA", "2026-02-01")})
    cfg = _cfg(**{"universe.benchmark_wire": "^CONFIRMED"})
    result = sync.run(provider=prov, cfg=cfg, full=True, conn=conn)
    assert result.n_prices == 6
    assert result.n_quotes == 1
    assert result.n_benchmark == 0


def test_financials_flagged_for_filing_analysis(conn):
    conn.execute("UPDATE universe SET is_financial = 1 WHERE symbol = 'BBB'")
    conn.commit()
    n = sync.mark_filing_required(conn, _cfg())
    assert n == 1
    assert conn.execute("SELECT requires_filing FROM universe WHERE symbol='BBB'"
                        ).fetchone()["requires_filing"] == 1


def test_filing_analysis_not_flagged_when_config_disables_it(conn):
    conn.execute("UPDATE universe SET is_financial = 1 WHERE symbol = 'BBB'")
    conn.commit()
    cfg = _cfg(**{"sector_overrides.financials.requires_filing_analysis": False})
    assert sync.mark_filing_required(conn, cfg) == 0