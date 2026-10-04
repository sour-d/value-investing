"""The pilot run, reproduced from stored raw data.

This file is the contract for Phase 2: the same raw vendor values under the same
thresholds must yield the same nine passers as the pilot pipeline. If a metric
formula drifts, this fails loudly instead of the screen quietly changing meaning.

``min_roic`` became the primary profitability gate in Phase 5. The exact nine
pilot passers below are recorded with the ROE variant, so the two tests that
assert on them pass ``use_roic=False``: they are a regression contract for the
metric formulas and Gate 4 semantics, not a claim about which variant is
currently configured. ``TestRoicPrimary`` covers the active gate.
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
    """The nine pilot passers, under the ROE variant that produced them."""
    passers = {
        v.symbol
        for v in screen.evaluate_universe(populated, cfg, use_roic=False)
        if v.clean
    }
    assert passers == PILOT_PASSERS, (
        f"expected {sorted(PILOT_PASSERS)}, got {sorted(passers)}"
    )


def test_roic_variant_delta_against_the_pilot(populated, cfg):
    """The Phase 5 switch, measured rather than assumed.

    Recorded explicitly because the change was small and the reasoning matters
    more than the count: NAVA enters on returns-versus-cost-of-capital while
    SUNTV leaves, and neither company's fundamentals moved.
    """
    roe = {v.symbol for v in screen.evaluate_universe(populated, cfg, use_roic=False)
           if v.clean}
    roic = {v.symbol for v in screen.evaluate_universe(populated, cfg) if v.clean}
    assert roic - roe == {"NAVA"}
    assert roe - roic == {"SUNTV"}


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


def test_config_gates_roic(cfg):
    # Phase 5 landed: ROIC is the gate and its thresholds are required, so a
    # config that omits them is a warning rather than a silent default.
    assert cfg.get("gate3.profitability.primary") == "roic"
    assert cfg.get("gate3.profitability.min_roic") is not None
    assert cfg.get("gate3.profitability.min_roic_wacc_spread") is not None
    assert not any("min_roic" in w for w in cfg.warnings)
    # Integrity must be configured, or Gate 5 would not block.
    assert not any("required_checks" in w for w in cfg.warnings)


class TestRoicPrimary:
    """Phase 5: ROIC-over-WACC grades profitability; ROE is context only.

    The motivation is that ROE measures return on *equity*, so it improves when
    a company adds debt even if the business earns nothing extra. Gate 2 already
    constrains leverage, so grading profitability on ROE too would let the
    capital structure flatter the operating result.
    """

    @staticmethod
    def _metrics(**kw):
        """A Metrics instance with only the profitability fields overridden.

        Attributes are assigned after construction, so this works because the
        dataclass is not frozen. Anything left unset stays as computed (None),
        which is the honest value for an absent metric.
        """
        c = db.connect(":memory:")
        db.apply_schema(c)
        c.execute("INSERT INTO universe (symbol,name,in_index,added_on) "
                  "VALUES ('RC','Roic Co',1,'2026-01-01')")
        m = M.compute(c, "RC")
        for key, value in kw.items():
            assert hasattr(m, key), f"Metrics has no field {key!r}"
            setattr(m, key, value)
        return c, m

    def test_roic_is_primary_and_roe_is_not_a_gate(self, cfg):
        c, m = self._metrics(roic=20.0, wacc=11.0, roic_spread=9.0, roe=14.0)
        try:
            names = [g.name for g in screen.gate3(m, cfg)]
            assert "roic" in names and "roic_wacc_spread" in names
            assert "roe" not in names
            assert "roe_secondary" in names
        finally:
            c.close()

    def test_high_roe_low_roic_now_fails(self, cfg):
        """The heart of the switch: leverage-inflated ROE no longer passes."""
        c, m = self._metrics(roe=30.0, roic=4.0, wacc=12.0, roic_spread=-8.0,
                             ebitda_margin=20.0)
        try:
            gates = screen.gate3(m, cfg)
            assert any(g.name == "roic" and not g.passed for g in gates)
            assert next(g for g in gates if g.name == "roe_secondary").passed
        finally:
            c.close()

    def test_high_roic_low_roe_no_longer_rejected(self, cfg):
        c, m = self._metrics(roe=4.0, roic=25.0, wacc=11.0, roic_spread=14.0,
                             ebitda_margin=20.0)
        try:
            gates = screen.gate3(m, cfg)
            assert [g.name for g in gates if not g.passed and g.blocking] == []
        finally:
            c.close()

    def test_low_roe_is_recorded_but_non_blocking(self, cfg):
        c, m = self._metrics(roe=2.0, roic=25.0, wacc=11.0, roic_spread=14.0,
                             ebitda_margin=20.0)
        try:
            g = next(x for x in screen.gate3(m, cfg) if x.name == "roe_secondary")
            assert not g.passed and g.blocking is False
        finally:
            c.close()

    def test_spread_failure_names_both_sides(self, cfg):
        """A bare "spread too small" hides which side is responsible."""
        c, m = self._metrics(roic=14.0, wacc=11.0, roic_spread=3.0, ebitda_margin=20.0)
        try:
            g = next(x for x in screen.gate3(m, cfg) if x.name == "roic_wacc_spread")
            assert not g.passed
            assert "ROIC 14.00" in (g.reason or "") and "WACC 11.00" in (g.reason or "")
        finally:
            c.close()

    def test_missing_risk_free_rate_fails_the_spread_explicitly(self, cfg):
        """No WACC means the harder question is unanswerable, not passed."""
        c, m = self._metrics(roic=30.0, wacc=None, roic_spread=None, ebitda_margin=20.0)
        try:
            g = next(x for x in screen.gate3(m, cfg) if x.name == "roic_wacc_spread")
            assert not g.passed
            assert "risk_free_rate" in (g.reason or "")
        finally:
            c.close()

    def test_roe_variant_still_selectable_for_comparison(self, cfg):
        c, m = self._metrics(roe=20.0)
        try:
            names = [g.name for g in screen.gate3(m, cfg, use_roic=False)]
            assert "roe" in names and "roic" not in names
        finally:
            c.close()

    def test_non_blocking_failure_does_not_count_as_rejection(self, cfg):
        c, m = self._metrics(roe=2.0, roic=25.0, wacc=11.0, roic_spread=14.0,
                             ebitda_margin=20.0)
        try:
            v = screen.SymbolVerdict(symbol="RC", name="Roic Co")
            v.gates = screen.gate3(m, cfg)
            v.n_failed = sum(1 for g in v.gates if not g.passed and g.blocking)
            assert v.n_failed == 0 and v.clean
        finally:
            c.close()


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