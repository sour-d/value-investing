"""The pilot run, reproduced from stored raw data.

This file is the contract for Phase 2: the same raw vendor values under the same
thresholds must yield the same nine passers as the pilot pipeline. If a metric
formula drifts, this fails loudly instead of the screen quietly changing meaning.

``min_roic`` is intentionally absent from ``screen.toml`` right now, so the
config validator emits a warning for it. That warning is the flag for Phase 5.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from stocks import config, db, metrics as M, screen

PILOT_PASSERS = {
    "BSOFT", "CHAMBLFERT", "GESHIP", "IGL", "KPITTECH",
    "MGL", "NATCOPHARM", "SUNTV", "ZENSARTECH",
}
PILOT_NO_FINANCIALS = {"BAGMANE", "DUMMYHEG"}
PILOT_INSUFFICIENT = {"CANHLIFE", "RUBICON", "VISL"}

SRC = Path("/tmp/opencode")
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg():
    return config.load()


@pytest.fixture(scope="module")
def populated(cfg):
    """The real database, imported once if it is not already populated."""
    db_path = REPO / "data" / "stocks.db"
    if not db_path.exists() or db_path.stat().st_size < 1_000_000:
        subprocess.run(
            [sys.executable, str(REPO / "scripts" / "import_baseline.py"), str(SRC)],
            check=True, capture_output=True,
        )
    c = db.connect()
    db.apply_schema(c)
    yield c
    c.close()


def test_pilot_universe_and_issues(populated):
    assert db.count(populated, "universe", "in_index = 1") == 251
    assert db.count(populated, "profile_snapshot") == 251

    blocked = {r["symbol"] for r in populated.execute(
        "SELECT symbol FROM symbol_issue WHERE code = 'no_financials'"
    )}
    assert blocked == PILOT_NO_FINANCIALS

    thin = {r["symbol"] for r in populated.execute(
        "SELECT symbol FROM symbol_issue WHERE code = 'insufficient_history'"
    )}
    assert thin == PILOT_INSUFFICIENT


def test_pilot_passers_reproduced(populated, cfg):
    passers = {v.symbol for v in screen.evaluate_universe(populated, cfg) if v.clean}
    assert passers == PILOT_PASSERS, (
        f"expected {sorted(PILOT_PASSERS)}, got {sorted(passers)}"
    )


def test_known_false_positives_still_pass_gates(populated, cfg):
    """BSOFT, KPITTECH and ZENSARTECH clear every gate and are still bad buys.

    This is the load-bearing reason the gates are labelled necessary but not
    sufficient. If a future change makes them fail, the qualitative layer may
    have started doing the quantitative layer's job.
    """
    verdicts = {v.symbol: v for v in screen.evaluate_universe(populated, cfg)}
    for symbol in ("BSOFT", "KPITTECH", "ZENSARTECH"):
        assert verdicts[symbol].clean, (
            f"{symbol} no longer passes; the gates are not the only filter, "
            "but this should be a deliberate decision"
        )


def test_gate0_uses_min_depth_across_statements(cfg):
    """A deep income statement must not carry a shallow balance sheet.

    Gate 0 floors on MIN(statement depths). 121 of 249 pilot symbols had
    mismatched raw statement depth, so a gate reading income alone passes names
    with no usable balance sheet to compute ROIC from.
    """
    c = db.connect(":memory:")
    db.apply_schema(c)
    try:
        c.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                  "VALUES ('TESTCO','Test Co',1,'2026-01-01')")
        # A mapped item per statement, so each one resolves to a real period set.
        for stmt, n, item in (("income", 5, "Total Revenue"),
                              ("balance", 3, "Stockholders Equity"),
                              ("cashflow", 5, "Operating Cash Flow")):
            for y in range(2021, 2021 + n):
                c.execute(
                    "INSERT INTO fundamentals_annual "
                    "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,'test','2026-01-01','2026-01-01')",
                    ("TESTCO", f"{y}-03-31", stmt, item, 1000.0),
                )

        results = screen.gate0(M.compute(c, "TESTCO"), cfg)
        depth = next(r for r in results if r.name == "data_sufficiency")
        assert not depth.passed
        assert "only 3 annual periods" in (depth.reason or "")
        assert "min across statements" in (depth.reason or "")
    finally:
        c.close()


def test_gate4_is_or_not_and(cfg):
    """Gate 4 is an OR: one cheap metric earns the slot.

    Regression guard — implementing it as AND silently dropped IGL, SUNTV and
    MGL, which are cheap on FCF yield alone.
    """
    c = db.connect(":memory:")
    db.apply_schema(c)
    try:
        c.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                  "VALUES ('ORCO','Or Co',1,'2026-01-01')")
        m = M.compute(c, "ORCO")

        m.pe_avg, m.ev_ebitda, m.fcf_yield = 40.0, 20.0, 9.0
        assert screen.gate4(m, cfg)[0].passed

        m.pe_avg, m.ev_ebitda, m.fcf_yield = 40.0, 20.0, 1.0
        assert not screen.gate4(m, cfg)[0].passed

        # No value metric at all must fail rather than inherit a pass.
        m.pe_avg = m.ev_ebitda = m.fcf_yield = m.pb = None
        m.is_financial = False
        assert not screen.gate4(m, cfg)[0].passed
    finally:
        c.close()


def test_gate3_fails_on_missing_profitability(cfg):
    """Gate 3 is the deliberate exception to the skip-missing policy."""
    c = db.connect(":memory:")
    db.apply_schema(c)
    try:
        m = M.compute(c, "NOPE")
        m.ebitda_margin = None
        m.roe = None
        results = screen.gate3(m, cfg)
        assert all(not r.passed for r in results)
        assert any("cannot demonstrate profitability" in (r.reason or "") for r in results)
    finally:
        c.close()


def test_missing_metric_policy(cfg):
    """Most gates skip absent metrics rather than failing them."""
    hi = screen._higher_is_better("m", None, 5.0, "G1", "n")
    lo = screen._lower_is_better("m", None, 5.0, "G2", "n")
    assert hi.passed and hi.note
    assert lo.passed and lo.note


def test_config_hash_ignores_formatting(tmp_path):
    a = tmp_path / "a.toml"
    a.write_text("[gate1]\nx = 1\n")
    b = tmp_path / "b.toml"
    b.write_text("# a comment\n[gate1]\n  x = 1\n")
    assert config.load(a).config_hash == config.load(b).config_hash

    c = tmp_path / "c.toml"
    c.write_text("[gate1]\nx = 2\n")
    assert config.load(a).config_hash != config.load(c).config_hash


def test_config_flags_missing_roic_threshold(cfg):
    # Phase 5 has not landed: ROIC is computed but not yet a gate threshold.
    assert any("min_roic" in w for w in cfg.warnings)
    # Integrity must be configured, or Gate 5 would not block.
    assert not any("required_checks" in w for w in cfg.warnings)


def test_config_hash_differs_between_thresholds(tmp_path):
    """Entrant/exit comparison is only valid within one config hash."""
    a = tmp_path / "a.toml"
    a.write_text("[gate1.earnings_quality]\nmin_ocf_to_ni = 0.80\n")
    b = tmp_path / "b.toml"
    b.write_text("[gate1.earnings_quality]\nmin_ocf_to_ni = 0.95\n")
    assert config.load(a).config_hash != config.load(b).config_hash


def test_gating_hash_ignores_sync_cadence(tmp_path):
    """Sync cadence must not change what a run means, so it stays out of the
    gating hash — otherwise every 'stocks sync' invalidates comparisons."""
    a = tmp_path / "a.toml"
    a.write_text(
        "[gate3.profitability]\nmin_roe = 13.0\n"
        "[sync]\nprice_staleness_hours = 20\n"
    )
    b = tmp_path / "b.toml"
    b.write_text(
        "[gate3.profitability]\nmin_roe = 13.0\n"
        "[sync]\nprice_staleness_hours = 4\n"
    )
    ca, cb = config.load(a), config.load(b)
    assert ca.gating_hash == cb.gating_hash
    assert ca.config_hash != cb.config_hash