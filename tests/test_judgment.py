"""Watchlist, integrity attestation, and `stocks why`.

The recurring theme: absence must never read as a pass. An unattested check, an
unrecorded verdict, and an unreviewed framework dimension are all reported as
unknown, because a blank that looks fine is the failure that actually costs
money.
"""

from __future__ import annotations

import pytest

from stocks import db, judgment, portfolio
from stocks.config import Config, _hash

REQUIRED = ["promoter_pledge", "auditor"]


def _cfg(**over) -> Config:
    data = {
        "gate3": {"profitability": {"primary": "roic", "min_roic": 15.0,
                                    "min_roic_wacc_spread": 5.0, "min_roe": 13.0,
                                    "min_ebitda_margin": 9.0}},
        "gate4": {"value": {"max_pe": 16.0, "max_ev_ebitda": 8.0,
                            "min_fcf_yield_pct": 6.0}},
        "gate5": {"integrity": {"required_checks": REQUIRED}},
        "tiers": {"quality_score": {}, "tier2_max_failures": 1, "tier3_min_quality": 7},
        "valuation": {"inputs": {"risk_free_rate": 0.065, "equity_risk_premium": 0.05}},
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
                  "VALUES (?,'x Ltd',1,'2026-01-01')", (sym,))
    c.commit()
    yield c
    c.close()


def _price(c, symbol, close, date="2026-03-02"):
    c.execute("INSERT OR REPLACE INTO price_daily "
              "(symbol,date,open,high,low,close,volume,source,fetched_at) "
              "VALUES (?,?,?,?,?,?,0,'test','2026-03-02')",
              (symbol, date, close, close, close, close))


# ────────────────────────────────────────────────────────────── watchlist ────


def test_watch_add_and_list(conn):
    judgment.add_watch(conn, "AAA", buy_line=100.0)
    rows = judgment.watchlist(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAA" and rows[0]["status"] == "watch"


def test_watch_add_is_an_upsert(conn):
    """Revising a buy-line is routine maintenance, not a duplicate-key error."""
    judgment.add_watch(conn, "AAA", buy_line=100.0)
    judgment.add_watch(conn, "AAA", buy_line=90.0)
    rows = judgment.watchlist(conn)
    assert len(rows) == 1 and rows[0]["buy_line"] == 90.0


def test_margin_of_safety_derived_on_write(conn):
    judgment.add_watch(conn, "AAA", buy_line=150.0, fv_low=210.0, fv_high=240.0)
    d = judgment.watchlist(conn)[0]
    assert d["margin_of_safety"] == pytest.approx(28.57, abs=0.01)


def test_margin_of_safety_none_without_both_inputs(conn):
    judgment.add_watch(conn, "AAA", buy_line=150.0)
    assert judgment.watchlist(conn)[0]["margin_of_safety"] is None


def test_distance_to_buy_line_computed_on_read(conn):
    """Recomputed every read, so it cannot go stale against the live price."""
    judgment.add_watch(conn, "AAA", buy_line=100.0)
    _price(conn, "AAA", 80.0)
    assert judgment.watchlist(conn)[0]["to_buy_line_pct"] == pytest.approx(-20.0)
    _price(conn, "AAA", 120.0, "2026-03-03")
    assert judgment.watchlist(conn)[0]["to_buy_line_pct"] == pytest.approx(20.0)


def test_watchlist_ordered_held_before_avoid(conn):
    for sym, status in (("AAA", "avoid"), ("BBB", "held")):
        judgment.add_watch(conn, sym)
        judgment.set_status(conn, sym, status)
    assert [d["status"] for d in judgment.watchlist(conn)] == ["held", "avoid"]


def test_add_watch_resets_status_to_watch(conn):
    """Re-adding an entry is a fresh look, so it must not inherit 'avoid'.

    Otherwise a name marked avoid is unreachable by re-adding it, and the only
    way back is remembering the separate status command.
    """
    judgment.add_watch(conn, "AAA")
    judgment.set_status(conn, "AAA", "avoid")
    judgment.add_watch(conn, "AAA", buy_line=100.0)
    assert judgment.watchlist(conn)[0]["status"] == "watch"


def test_watch_status_change_requires_membership(conn):
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.set_status(conn, "AAA", "buy")
    assert "not on the watchlist" in str(e.value)


def test_watch_remove_missing_refuses(conn):
    with pytest.raises(portfolio.PortfolioError):
        judgment.remove_watch(conn, "AAA")


def test_unknown_status_refused(conn):
    judgment.add_watch(conn, "AAA")
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.set_status(conn, "AAA", "yolo")
    assert "must be one of" in str(e.value)


def test_watch_rejects_symbol_outside_universe(conn):
    with pytest.raises(portfolio.PortfolioError):
        judgment.add_watch(conn, "ZZZ")


# ────────────────────────────────────────────────────────────── integrity ────


def test_verified_requires_evidence(conn):
    """The whole point of Gate 5: a bare 'verified' is indistinguishable from
    a guess, so it must not be recordable."""
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.attest(conn, _cfg(), "AAA", "auditor", "verified")
    assert "evidence is required" in str(e.value)


def test_verified_with_evidence_is_recorded(conn):
    judgment.attest(conn, _cfg(), "AAA", "auditor", "verified",
                    "auditor opinion unqualified, FY26 annual report p.88")
    d = {x["check_name"]: x for x in judgment.portfolio.integrity_ledger(
        conn, _cfg(), "AAA")}
    assert d["auditor"]["status"] == "verified"
    assert "p.88" in d["auditor"]["evidence"]
    assert d["auditor"]["verified_at"]


def test_unknown_is_allowed_without_evidence(conn):
    """Not having checked is an honest answer, and it keeps the buy blocked."""
    judgment.attest(conn, _cfg(), "AAA", "auditor", "unknown")
    unverified, _ = portfolio.integrity_status(conn, _cfg(), "AAA")
    assert unverified == ["promoter_pledge", "auditor"]


def test_failed_status_stored_with_evidence(conn):
    judgment.attest(conn, _cfg(), "AAA", "auditor", "failed", "adverse opinion")
    d = {x["check_name"]: x for x in judgment.portfolio.integrity_ledger(
        conn, _cfg(), "AAA")}
    assert d["auditor"]["status"] == "failed"


def test_unconfigured_check_name_refused(conn):
    """Otherwise a typo creates a row that no gate ever reads."""
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.attest(conn, _cfg(), "AAA", "auditr", "verified", "evidence")
    assert "not a configured Gate 5 check" in str(e.value)


def test_integrity_render_says_buy_blocked(conn):
    judgment.attest(conn, _cfg(), "AAA", "auditor", "verified", "p.88")
    text = judgment.render_integrity(conn, _cfg(), "AAA")
    assert "buy blocked" in text
    assert "promoter_pledge" in text


def test_integrity_render_all_verified(conn):
    for c in REQUIRED:
        judgment.attest(conn, _cfg(), "AAA", c, "verified", "evidence")
    assert "all required checks verified" in judgment.render_integrity(conn, _cfg(), "AAA")


def test_re_attesting_updates_rather_than_duplicates(conn):
    judgment.attest(conn, _cfg(), "AAA", "auditor", "unknown")
    judgment.attest(conn, _cfg(), "AAA", "auditor", "verified", "p.88")
    rows = judgment.portfolio.integrity_ledger(conn, _cfg(), "AAA")
    assert len([r for r in rows if r["check_name"] == "auditor"]) == 1


# ───────────────────────────────────────────────────────── verdict/thesis ────


def test_verdict_requires_a_reason(conn):
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.record_verdict(conn, "AAA", "avoid", "   ")
    assert "needs a reason" in str(e.value)


def test_verdict_recorded_and_latest_wins(conn):
    judgment.record_verdict(conn, "AAA", "watch", "awaiting results")
    judgment.record_verdict(conn, "AAA", "avoid", "moat narrowing, prices up 40%")
    p = judgment.why_payload("AAA", conn=conn, cfg=_cfg())
    assert p["claimed"]["verdict"]["verdict"] == "avoid"


def test_invalid_verdict_refused(conn):
    with pytest.raises(portfolio.PortfolioError):
        judgment.record_verdict(conn, "AAA", "buy", "reason")


def test_assessment_requires_rationale(conn):
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.record_assessment(conn, "AAA", "moat_type", "network", "  ")
    assert "needs a rationale" in str(e.value)


def test_invalid_dimension_refused(conn):
    with pytest.raises(portfolio.PortfolioError) as e:
        judgment.record_assessment(conn, "AAA", "vibes", "good", "because")
    assert "must be one of" in str(e.value)


def test_unassessed_dimensions_are_listed(conn):
    judgment.record_assessment(conn, "AAA", "moat_type", "licence", "regulated")
    p = judgment.why_payload("AAA", conn=conn, cfg=_cfg())
    assert "moat_type" not in p["unassessed_dimensions"]
    assert "pricing_power" in p["unassessed_dimensions"]
    text = judgment.render_why(p)
    assert "unknown, not 'fine'" in text


# ─────────────────────────────────────────────────────────────── stocks why ────


def test_why_splits_measured_from_claimed(conn):
    """Provenance separation: the screen says one thing, the human another."""
    p = judgment.why_payload("AAA", conn=conn, cfg=_cfg())
    assert p["measured"]["clean"] is False          # no statements seeded
    assert p["claimed"]["verdict"] is None
    assert p["integrity"]["buy_allowed"] is False
    assert p["unassessed_dimensions"] == list(judgment.DIMENSIONS)


def test_why_reports_holding_from_the_ledger(conn):
    for c in REQUIRED:
        judgment.attest(conn, _cfg(), "AAA", c, "verified", "p.1")
    portfolio.record_transaction(conn, "AAA", "BUY", 10, 100.0, reason="r", cfg=_cfg())
    p = judgment.why_payload("AAA", conn=conn, cfg=_cfg())
    assert p["holding_qty"] == 10
    assert p["integrity"]["buy_allowed"] is True


def test_why_shows_override_history(conn):
    portfolio.record_transaction(
        conn, "AAA", "BUY", 10, 100.0, reason="r", cfg=_cfg(),
        override_integrity="checked the exchange filing")
    p = judgment.why_payload("AAA", conn=conn, cfg=_cfg())
    assert p["trades"][0]["integrity_override"] == "checked the exchange filing"
    assert "integrity override" in judgment.render_why(p)


def test_why_renders_blocked_symbol_stops_early(conn):
    c = conn
    c.execute("INSERT INTO symbol_issue (symbol,code,severity,detail,detected_at) "
              "VALUES ('AAA','no_financials','block','vendor empty','2026-01-01')")
    c.commit()
    p = judgment.why_payload("AAA", conn=conn, cfg=_cfg())
    text = judgment.render_why(p)
    # No profile snapshot was seeded, so the name is unknown, not invented.
    assert text.startswith("AAA — unknown")
    assert "BLOCKED" in text
    # No metric table for a symbol that cannot be measured.
    assert "measured:" not in text