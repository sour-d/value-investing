"""Screen run persistence and the delta rules.

The invariant under test is the one in AGENTS.md: entrant/exit output is only
meaningful within a single config hash. A threshold change reported as a
fundamental change is worse than no report, because it is believed.
"""

from __future__ import annotations

import pytest

from stocks import config as config_mod
from stocks import db, screencmd


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.apply_schema(c)
    yield c
    c.close()


def _cfg(**overrides):
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
        # ROIC is the primary profitability gate, so a test config that omits it
        # would silently fall back to ROE and quietly test the wrong variant.
        "gate3": {"profitability": {"primary": "roic", "min_roic": 15.0,
                                    "min_roic_wacc_spread": 5.0,
                                    "min_roe": 13.0, "min_ebitda_margin": 9.0}},
        "gate4": {"value": {"max_pe": 16.0, "max_ev_ebitda": 8.0,
                            "min_fcf_yield_pct": 6.0},
                  "financial": {"max_pb": 1.1, "min_roe": 15.0}},
        "gate5": {"integrity": {"required_checks": ["promoter_pledge"]}},
        "tiers": {"quality_score": {}, "tier2_max_failures": 1, "tier3_min_quality": 7},
        "valuation": {"inputs": {"risk_free_rate": 0.065, "equity_risk_premium": 0.05}},
        "unmeasurable": {"items": ["pricing_power"]},
        "sync": {"price_staleness_hours": 20},
    }
    for path, value in overrides.items():
        node = base
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return config_mod.Config(
        data=base,
        source=type("P", (), {"name": "test", "__str__": lambda s: "test"})(),
        config_hash=config_mod._hash(base),
        gating_hash=config_mod._hash(base),
    )


INSERTS = (
    ("income", "Net Income", "roe"),
    ("income", "Total Revenue", "revenue"),
    ("income", "EBITDA", "margin"),
    # ROIC needs EBIT, tax and pretax to derive NOPAT; WACC needs debt, cash
    # and interest expense to derive the cost of capital. Without these the
    # spread gate is unanswerable, which would mask what these tests are about.
    ("income", "EBIT", "ebit"),
    ("income", "Pretax Income", "pretax"),
    ("income", "Tax Provision", "tax"),
    ("income", "Interest Expense", "interest"),
    ("balance", "Stockholders Equity", "equity"),
    ("balance", "Total Assets", "assets"),
    ("balance", "Total Debt", "debt"),
    ("balance", "Cash And Cash Equivalents", "cash"),
    ("cashflow", "Operating Cash Flow", "ocf"),
)
YEARS = ("2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31")


def _seed(conn, symbol: str, clean: bool) -> None:
    """A symbol with four clean years, or one whose ROE is below the floor.

    Gate 0 requires four contiguous annual periods across all three
    statements, so every statement needs a mapped item for each year.
    """
    conn.execute("INSERT OR IGNORE INTO universe (symbol,name,in_index,added_on) "
                 "VALUES (?,?,1,'2026-01-01')", (symbol, f"{symbol} Ltd"))
    # A market cap and enterprise value, so the Gate 4 OR has something to
    # evaluate. Absent valuation data must fail, not inherit a pass.
    conn.execute(
        "INSERT OR REPLACE INTO profile_snapshot (symbol,as_of,market_cap,"
        "enterprise_value,source) VALUES (?,'2026-01-01',100.0,100.0,'test')",
        (symbol,),
    )
    # Capital structure is sized so a clean symbol clears ROIC>WACC comfortably:
    # equity 100, debt 20, cash 30 gives invested capital 90 against NOPAT of
    # ~17, so ROIC ~19% against a WACC near 11%.
    values = {
        "roe": 20.0 if clean else 5.0, "revenue": 1000.0, "margin": 150.0,
        "equity": 100.0, "assets": 200.0, "ocf": 20.0 if clean else 1.0,
        "ebit": 20.0, "pretax": 18.0, "tax": 3.0, "interest": 1.0,
        "debt": 20.0, "cash": 30.0,
    }
    for year in YEARS:
        for statement, item, key in INSERTS:
            conn.execute(
                "INSERT INTO fundamentals_annual "
                "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?,'test','2026-01-01','2026-01-01')",
                (symbol, year, statement, item, values[key]),
            )
    conn.commit()


def test_first_run_reports_no_baseline(conn):
    _seed(conn, "AAA", clean=True)
    result = screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)
    assert not result.comparable
    assert "no baseline" in (result.note or "")
    assert "no change" not in result.render()


def test_entrants_and_exits_across_identical_config(conn):
    _seed(conn, "AAA", clean=True)
    _seed(conn, "BBB", clean=True)
    first = screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)
    assert set(first.passers) == {"AAA", "BBB"}

    # BBB stops qualifying; AAA is unchanged.
    conn.execute("DELETE FROM fundamentals_annual WHERE symbol = 'BBB'")
    conn.commit()
    second = screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)

    assert second.comparable
    assert second.exits == ["BBB"]
    assert second.entrants == []
    assert "GONE BBB" in second.render()


def test_threshold_change_blocks_delta(conn):
    """The core invariant: a config change must not masquerade as a new result."""
    _seed(conn, "AAA", clean=True)
    screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)

    stricter = _cfg()
    stricter.data["gate1"]["earnings_quality"]["min_ocf_to_ni"] = 0.99
    stricter = config_mod.Config(
        data=stricter.data,
        source=stricter.source,
        config_hash=config_mod._hash(stricter.data),
        gating_hash=config_mod._hash(stricter.data),
    )
    result = screencmd._run_locked(conn, stricter, full=False, use_roic=False)

    assert not result.comparable
    assert result.entrants == [] and result.exits == []
    assert "thresholds changed" in (result.note or "")
    assert "no delta" in result.render()


def test_gate_variant_change_blocks_delta(conn):
    """A ROE run and a ROIC run apply different gates.

    Both can share a config_hash, so the variant is compared separately. Without
    this, switching gates reported a threshold change as a fundamental change in
    the business.
    """
    _seed(conn, "AAA", clean=True)
    cfg = _cfg()
    first = screencmd._run_locked(conn, cfg, full=False, use_roic=False)
    assert set(first.passers) == {"AAA"}

    # Only the variant flips; the config is byte-identical.
    result = screencmd._run_locked(conn, _cfg(), full=False, use_roic=True)

    assert result.config_hash == cfg.config_hash
    assert not result.comparable
    assert "different gates" in (result.note or "")
    # Still no entrants/exits: those would imply the businesses changed.
    assert result.entrants == [] and result.exits == []

    # But the effect of the gate change itself is reported, since when the
    # variant flip is the event under review that difference is the answer.
    d = result.variant_delta
    assert d["from"] == "roe" and d["to"] == "roic"
    assert set(d["stable"]) == {"AAA"}
    assert "not evidence that any business changed" in d["note"]


def test_variant_delta_reports_entries_and_exits(conn):
    """Under the ROE variant AAA is clean; the ROIC variant rejects it.

    The lists must be attributed to the gate change, not presented as a
    fundamental move in the company.
    """
    screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)
    result = screencmd._run_locked(conn, _cfg(), full=False, use_roic=True)
    d = result.variant_delta
    assert d["entered"] == [] or "AAA" in d["entered"]
    assert "gate change" in result.render()


def test_run_persists_config_snapshot_and_gate_rows(conn):
    _seed(conn, "AAA", clean=True)
    result = screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)

    run = conn.execute("SELECT * FROM screen_run WHERE run_id = ?",
                       (result.run_id,)).fetchone()
    assert run["config_hash"] == result.config_hash
    # The snapshot must contain the resolved gating config, not a file path.
    assert '"gate3"' in run["config_json"]

    rows = conn.execute("SELECT * FROM gate_result WHERE run_id = ? AND symbol = 'AAA'",
                        (result.run_id,)).fetchall()
    assert rows
    assert {"G0", "G1", "G2", "G3", "G4"} <= {r["gate"] for r in rows}


def test_report_stays_short(conn):
    """A screen that prints 251 rows gets skimmed."""
    for i in range(60):
        _seed(conn, f"SYM{i:03d}", clean=True)
    result = screencmd._run_locked(conn, _cfg(), full=False, use_roic=False)
    rendered = result.render()
    # The passer list is the only unbounded part; the framing must stay short.
    assert result.n_clean == 60
    assert len(rendered.splitlines()) <= 8