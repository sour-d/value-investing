"""Gate evaluation.

Gates are read from ``screen.toml`` on every run, never baked in here. A gate
failure always carries the metric, the observed value and the threshold, so
``stocks screen --explain`` can show why without recomputing anything.

Missing-data policy, which the pilot pipeline established:

* Most metrics are skipped when absent — the gate is *not applied* and the
  symbol passes. Requiring every metric would reject sound companies because a
  vendor omits a line item.
* Gate 3 profitability is the deliberate exception. ROE and EBITDA margin are
  the profitability floor itself; with neither present there is no basis to
  call the business profitable, so absence fails.
* Gate 4 is an OR, and a symbol with *no* value metric at all fails rather than
  inheriting a pass. The pilot had this hole: absent data read as "cheap".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import metrics as M
from .config import Config


@dataclass
class GateResult:
    gate: str
    name: str
    passed: bool
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None
    reason: str | None = None
    note: str | None = None
    op: str = ""          # ">=", "<=" or "" when the check has no single direction

    def as_row(self) -> tuple[str, str, int, str | None, float | None, float | None, str | None, str | None]:
        """Column values for ``gate_result`` (run_id and symbol are added by the caller)."""
        return (self.gate, self.name, int(self.passed), self.metric,
                self.value, self.threshold, self.reason, self.note)


@dataclass
class SymbolVerdict:
    symbol: str
    name: str | None
    gates: list[GateResult] = field(default_factory=list)
    tier: int = 3
    quality_score: int = 0
    n_failed: int = 0
    blocked: str | None = None      # set when a data issue prevents evaluation

    @property
    def clean(self) -> bool:
        return self.blocked is None and self.n_failed == 0

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]


def _skip(metric: str, threshold: float, gate: str, name: str, op: str) -> GateResult:
    """Metric absent: the gate is not applied, so it passes with a note."""
    return GateResult(gate, name, True, metric, None, threshold,
                      note="metric unavailable; gate not applied", op=op)


def _higher_is_better(metric: str, value: float | None, threshold: float,
                      gate: str, name: str) -> GateResult:
    if value is None:
        return _skip(metric, threshold, gate, name, ">=")
    ok = value >= threshold
    return GateResult(gate, name, ok, metric, value, threshold,
                      None if ok else f"{name} {value:.2f} < {threshold:g}", op=">=")


def _lower_is_better(metric: str, value: float | None, threshold: float,
                     gate: str, name: str) -> GateResult:
    if value is None:
        return _skip(metric, threshold, gate, name, "<=")
    ok = value <= threshold
    return GateResult(gate, name, ok, metric, value, threshold,
                      None if ok else f"{name} {value:.2f} > {threshold:g}", op="<=")


def _required_higher(metric: str, value: float | None, threshold: float,
                     gate: str, name: str) -> GateResult:
    """Like `_higher_is_better`, but absence fails: this metric is the floor."""
    if value is None:
        return GateResult(gate, name, False, metric, None, threshold,
                          f"{name} unavailable; cannot demonstrate profitability",
                          op=">=")
    ok = value >= threshold
    return GateResult(gate, name, ok, metric, value, threshold,
                      None if ok else f"{name} {value:.2f} < {threshold:g}", op=">=")


# ────────────────────────────────────────────────────────── gate 0 ────


def gate0(m: M.Metrics, cfg: Config) -> list[GateResult]:
    """Data sufficiency. Runs before any judgement.

    Depth is the MINIMUM period count across all three statements: 121 of 249
    pilot symbols had mismatched depth, so testing income alone would pass
    names with no usable balance sheet.
    """
    g0 = cfg.get("gate0.data_sufficiency", {}) or {}
    out: list[GateResult] = []

    _, depth = m.periods()
    need = int(g0.get("min_annual_periods", 4))
    out.append(GateResult(
        "G0", "data_sufficiency", depth >= need, "min_statement_depth", float(depth),
        float(need),
        None if depth >= need else
        f"only {depth} annual periods (min across statements); need {need}",
        op=">=",
    ))

    if g0.get("require_contiguous_years", True):
        gaps = _year_gaps(m)
        out.append(GateResult(
            "G0", "contiguous_years", not gaps, "year_gaps", float(len(gaps)), 0.0,
            None if not gaps else f"non-contiguous fiscal years: {'; '.join(gaps)}",
            op="<=",
        ))
    return out


def _year_gaps(m: M.Metrics) -> list[str]:
    periods, _ = m.periods()
    if len(periods) < 2:
        return []
    years = sorted({int(p[:4]) for p in periods})
    missing = [str(y) for y in range(years[0], years[-1] + 1) if y not in years]
    return [f"{p} missing FY{y}" for p, y in zip(periods, missing)] or []


# ────────────────────────────────────────────────────────── gates 1-4 ────


def gate1(m: M.Metrics, cfg: Config) -> list[GateResult]:
    c = cfg.get("gate1.earnings_quality", {}) or {}
    out = [
        _higher_is_better("ni_pos", m.ni_pos, float(c.get("ni_positive_years_ratio", 0.99)),
                          "G1", "ni_positive_years"),
        _higher_is_better("ocf_pos", m.ocf_pos, float(c.get("ocf_positive_years_ratio", 0.99)),
                          "G1", "ocf_positive_years"),
    ]
    # FCF is optional: many Indian smallcaps report no free-cash-flow line, and
    # requiring it would silently drop otherwise-sound names.
    fcf_thr = float(c.get("fcf_positive_years_ratio", 0.75))
    if m.fcf_pos is None:
        out.append(GateResult("G1", "fcf_positive_years", True, "fcf_pos", None,
                              fcf_thr, note="no FCF line reported; gate not applied",
                              op=">="))
    else:
        out.append(_higher_is_better("fcf_pos", m.fcf_pos, fcf_thr, "G1",
                                     "fcf_positive_years"))

    out += [
        _higher_is_better("ocf_conv", m.ocf_conv, float(c.get("min_ocf_to_ni", 0.8)),
                          "G1", "cash_conversion"),
        _lower_is_better("accruals", m.accruals, float(c.get("max_accruals_ratio", 0.10)),
                         "G1", "accruals"),
    ]
    return out


def gate2(m: M.Metrics, cfg: Config) -> list[GateResult]:
    if m.is_financial:
        c = cfg.get("gate2.financial", {}) or {}
        limit = float(c.get("max_debt_to_equity", 1.8))
        value = m.vendor_debt_to_equity if m.vendor_debt_to_equity is not None else m.debt_to_equity
        return [_lower_is_better("debt_to_equity", value, limit, "G2",
                                 "financial_debt_to_equity")]

    c = cfg.get("gate2.balance_sheet", {}) or {}
    return [
        _lower_is_better("net_debt_to_equity", m.net_debt_to_equity,
                         float(c.get("max_net_debt_to_equity", 0.30)), "G2", "net_debt_to_equity"),
        _higher_is_better("interest_coverage", m.interest_coverage,
                          float(c.get("min_interest_coverage", 5.0)), "G2", "interest_coverage"),
    ]


def gate3(m: M.Metrics, cfg: Config, use_roic: bool = False) -> list[GateResult]:
    c = cfg.get("gate3.profitability", {}) or {}

    # ROIC is the primary profitability gate from Phase 5: it removes
    # capital-structure distortion. ROE stays as a secondary check.
    out: list[GateResult] = []
    if use_roic:
        out.append(_required_higher("roic", m.roic, float(c.get("min_roic", 15.0)),
                                    "G3", "roic"))
        spread_min = float(c.get("min_roic_wacc_spread", 5.0))
        spread_ok = m.roic_spread is not None and m.roic_spread >= spread_min
        out.append(GateResult(
            "G3", "roic_wacc_spread", spread_ok, "roic_spread", m.roic_spread, spread_min,
            None if spread_ok else "ROIC does not clear WACC by the required margin",
            op=">=",
        ))

    out.append(_required_higher("ebitda_margin", m.ebitda_margin,
                                float(c.get("min_ebitda_margin", 9.0)), "G3", "ebitda_margin"))
    out.append(_required_higher("roe", m.roe, float(c.get("min_roe", 13.0)), "G3", "roe"))
    return out


def gate4(m: M.Metrics, cfg: Config) -> list[GateResult]:
    """Value gate. Any ONE cheap metric earns the slot — this is an OR.

    Deliberately loose by design: the qualitative layer is what rejects what
    survives, and a strict AND here would throw away the cheap names that are
    precisely worth researching.

    A symbol with no value metric at all fails, rather than inheriting a pass
    from absent data.
    """
    c = cfg.get("gate4.value", {}) or {}
    # Each option is (label, satisfied, observed_text, threshold). Satisfaction is
    # a real boolean — a composite condition like "PB cheap AND ROE high" has no
    # single metric value and must not be faked into one.
    options: list[tuple[str, bool, str, float]] = []
    available = 0

    pe, ev, fy = m.pe_avg, m.ev_ebitda, m.fcf_yield
    for name, val, thr, higher in (
        ("pe_avg", pe, float(c.get("max_pe", 16.0)), False),
        ("ev_ebitda", ev, float(c.get("max_ev_ebitda", 8.0)), False),
        ("fcf_yield", fy, float(c.get("min_fcf_yield_pct", 6.0)), True),
    ):
        if val is None:
            continue
        available += 1
        ok = (val >= thr) if higher else (0 < val <= thr)
        options.append((name, ok, f"{name}={val:.2f}", thr))

    if m.is_financial:
        fc = cfg.get("gate4.financial", {}) or {}
        pb_max, roe_min = float(fc.get("max_pb", 1.1)), float(fc.get("min_roe", 15.0))
        if m.pb is not None and m.roe is not None:
            available += 1
            ok = m.pb <= pb_max and m.roe >= roe_min
            options.append((
                "pb_and_roe", ok,
                f"PB={m.pb:.2f} (<= {pb_max:g}) AND ROE={m.roe:.2f} (>= {roe_min:g})",
                pb_max,
            ))

    passing = [o for o in options if o[1]]
    if passing:
        label, _, text, thr = passing[0]
        return [GateResult("G4", "value", True, label, None, thr,
                           note=f"cheap on {text}")]
    if available == 0:
        return [GateResult("G4", "value", False, None, None, None,
                           reason="no valuation metric available; cannot show it is cheap")]
    detail = ", ".join(t for _, _, t, _ in options)
    return [GateResult("G4", "value", False, None, None, None,
                       reason=f"not cheap on any metric ({detail})")]


# ────────────────────────────────────────────────────────── tiers ────


def quality_score(m: M.Metrics, cfg: Config) -> int:
    """Rank on business quality independently of price."""
    t = cfg.get("tiers.quality_score", {}) or {}
    s = 0
    if m.roe is not None:
        s += int(t.get("roe_at_least_15", 2)) if m.roe >= 15 else (
            int(t.get("roe_at_least_12", 1)) if m.roe >= 12 else 0)
    if m.ebitda_margin is not None:
        s += int(t.get("ebitda_margin_at_least_15", 2)) if m.ebitda_margin >= 15 else (
            int(t.get("ebitda_margin_at_least_10", 1)) if m.ebitda_margin >= 10 else 0)
    if m.ocf_conv is not None:
        s += int(t.get("ocf_conv_at_least_12", 2)) if m.ocf_conv >= 1.2 else (
            int(t.get("ocf_conv_at_least_09", 1)) if m.ocf_conv >= 0.9 else 0)
    if m.net_debt_to_equity is not None:
        s += int(t.get("net_cash", 2)) if m.net_debt_to_equity <= 0 else (
            int(t.get("low_leverage", 1)) if m.net_debt_to_equity <= 0.3 else 0)
    if m.accruals is not None and m.accruals < 0:
        s += int(t.get("negative_accruals", 1))
    if m.recv_rev_ratio is not None and m.recv_rev_ratio < 1.15:
        s += int(t.get("receivables_disciplined", 1))
    if m.intangibles_pct is not None and m.intangibles_pct < 0.20:
        s += int(t.get("low_intangibles", 1))
    return s


def classify_tier(n_failed: int, score: int, cfg: Config) -> int:
    """Research priority, as a mutually exclusive ladder.

    1 passes everything. 3 is a strong business that missed one gate — the
    cheapest thing on the list to research, and the tier the pilot's report
    buried inside its own tier 2. 2 is a near-miss with weaker quality. 4 failed
    several gates and is not worth the time.

    Ordering by research value rather than by failure count is deliberate:
    "how many gates did it miss" is the wrong question when one gate is a data
    artefact and another is a broken balance sheet.
    """
    t = cfg.get("tiers", {}) or {}
    t2_max = int(t.get("tier2_max_failures", 1))
    t3_min = int(t.get("tier3_min_quality", 7))
    if n_failed == 0:
        return 1
    if n_failed <= t2_max and score >= t3_min:
        return 3
    if n_failed <= t2_max:
        return 2
    return 4


# ───────────────────────────────────────────────────── orchestration ────


def evaluate_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    cfg: Config,
    risk_free: float | None = None,
    erp: float | None = None,
    use_roic: bool = False,
) -> SymbolVerdict:
    m = M.compute(conn, symbol, risk_free_rate=risk_free, equity_risk_premium=erp)
    v = SymbolVerdict(symbol=symbol, name=m.name or symbol)

    blocked = conn.execute(
        "SELECT code, detail FROM symbol_issue "
        "WHERE symbol = ? AND severity = 'block' AND resolved_at IS NULL "
        "ORDER BY detected_at DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if blocked is not None:
        v.blocked = f"{blocked['code']}: {blocked['detail'] or 'see stocks health'}"
        return v

    v.gates = (gate0(m, cfg) + gate1(m, cfg) + gate2(m, cfg)
               + gate3(m, cfg, use_roic=use_roic) + gate4(m, cfg))
    v.n_failed = sum(1 for g in v.gates if not g.passed)
    v.quality_score = quality_score(m, cfg)
    v.tier = classify_tier(v.n_failed, v.quality_score, cfg)
    return v


def evaluate_universe(
    conn: sqlite3.Connection, cfg: Config, use_roic: bool = False
) -> list[SymbolVerdict]:
    symbols = [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM universe WHERE in_index = 1 ORDER BY symbol"
    )]
    risk_free = cfg.get("valuation.inputs.risk_free_rate")
    erp = cfg.get("valuation.inputs.equity_risk_premium")
    return [
        evaluate_symbol(conn, s, cfg, risk_free=risk_free, erp=erp, use_roic=use_roic)
        for s in symbols
    ]