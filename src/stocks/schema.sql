-- Schema for the local stocks database.
--
-- Design rules enforced here:
--   * Raw provider values are stored; metrics are derived on read.
--   * `fundamentals_annual` is keyed by fiscal period so period misalignment in
--     the vendor feed is visible rather than silently absorbed by position.
--   * `transactions` is append-only, enforced by trigger, not by convention.
--   * Positions / P&L are views, so there is no second copy to drift.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────── universe ────

CREATE TABLE IF NOT EXISTS universe (
    symbol            TEXT PRIMARY KEY,          -- bare NSE symbol, e.g. 'ACC'
    name              TEXT,
    isin              TEXT,                      -- stable across symbol changes
    industry          TEXT,
    sector            TEXT,
    is_financial      INTEGER,                   -- nullable: NULL = not yet classified
    classification_src TEXT,                     -- 'heuristic' | 'manual'
    requires_filing   INTEGER NOT NULL DEFAULT 0,-- sector needs filings-based analysis
    in_index          INTEGER NOT NULL DEFAULT 1,
    added_on          TEXT NOT NULL,
    removed_on        TEXT                       -- set on exit; rows are never deleted
);

-- ──────────────────────────────────────────────────────────────── prices ────

CREATE TABLE IF NOT EXISTS price_daily (
    symbol     TEXT NOT NULL REFERENCES universe(symbol),
    date       TEXT NOT NULL,                    -- YYYY-MM-DD
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    adj_close  REAL,
    volume     INTEGER,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_price_date ON price_daily(date);

CREATE TABLE IF NOT EXISTS price_benchmark (
    index_code TEXT NOT NULL,                    -- 'NIFTY_SMALLCAP250'
    date       TEXT NOT NULL,
    open       REAL, high REAL, low REAL, close REAL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (index_code, date)
) WITHOUT ROWID;

-- Vendor-reported trailing statistics that a 1-year bar history cannot reproduce.
-- Doubles as a day-0 audit anchor for later disagreement checks.
CREATE TABLE IF NOT EXISTS quote_snapshot (
    symbol          TEXT NOT NULL REFERENCES universe(symbol),
    as_of           TEXT NOT NULL,
    all_time_high   REAL,
    all_time_low    REAL,
    vendor_52w_high REAL,
    vendor_52w_low  REAL,
    vendor_50d_avg  REAL,
    vendor_200d_avg REAL,
    source          TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of)
) WITHOUT ROWID;

-- ──────────────────────────────────────────────────────── fundamentals ────

-- Long format on purpose. Every value carries its own fiscal period, so a
-- missing FY or a changed fiscal year-end is a gap rather than a shift.
CREATE TABLE IF NOT EXISTS fundamentals_annual (
    symbol      TEXT NOT NULL REFERENCES universe(symbol),
    fy_end      TEXT NOT NULL,                   -- '2026-03-31'
    statement   TEXT NOT NULL CHECK (statement IN ('income','balance','cashflow')),
    item        TEXT NOT NULL,                   -- 'TotalRevenue', 'NetIncome', ...
    value       REAL,                            -- raw INR; NULL if vendor has none
    source      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    changed_at    TEXT,                          -- set only when the value changes
    PRIMARY KEY (symbol, fy_end, statement, item)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_fundamentals_item ON fundamentals_annual(item);

-- Provenance: exactly what the vendor returned, per fetch.
CREATE TABLE IF NOT EXISTS fetch_log (
    fetch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,               -- 'universe'|'profile'|'financials'|'prices'
    symbol          TEXT,
    ok              INTEGER NOT NULL,
    n_items         INTEGER,
    period_set_json TEXT,                        -- ['2026-03-31', ...] as returned
    error           TEXT,
    duration_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_fetch_symbol ON fetch_log(symbol, kind, ts);

-- ────────────────────────────────────────────────────────── profile ────

-- Vendor-computed ratios. `trusted` stays 0 until cross-checked against filings.
CREATE TABLE IF NOT EXISTS profile_snapshot (
    symbol                TEXT NOT NULL REFERENCES universe(symbol),
    as_of                 TEXT NOT NULL,
    current_price         REAL,
    market_cap            REAL,
    enterprise_value      REAL,
    trailing_pe           REAL,
    forward_pe            REAL,
    price_to_book         REAL,
    peg_ratio             REAL,
    ev_ebitda             REAL,
    ev_revenue            REAL,
    book_value            REAL,
    trailing_eps          REAL,
    forward_eps           REAL,
    dividend_yield        REAL,
    payout_ratio          REAL,
    beta                  REAL,
    shares_outstanding    REAL,
    float_shares          REAL,
    total_cash            REAL,
    total_debt            REAL,
    roe                   REAL,
    roa                   REAL,
    profit_margin         REAL,
    operating_margin      REAL,
    ebitda_margin         REAL,
    revenue_growth        REAL,
    earnings_growth       REAL,
    held_pct_insiders     REAL,
    held_pct_institutions REAL,
    employees             INTEGER,
    business_summary      TEXT,
    vendor_invested_capital REAL,
    source                TEXT NOT NULL,
    trusted               INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, as_of)
) WITHOUT ROWID;

-- ────────────────────────────────────────────────────── data quality ────

-- Makes the data-integrity rules enforceable: the screen reads these rows.
CREATE TABLE IF NOT EXISTS symbol_issue (
    symbol      TEXT NOT NULL REFERENCES universe(symbol),
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('block','warn')),
    detail      TEXT,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (symbol, code, detected_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_issue_open ON symbol_issue(symbol, resolved_at);

-- ───────────────────────────────────────────────────────── derived ────

-- Disposable cache. Source of truth is raw fundamentals + screen.toml.
CREATE TABLE IF NOT EXISTS metric_cache (
    symbol TEXT NOT NULL REFERENCES universe(symbol),
    as_of  TEXT NOT NULL,
    metric TEXT NOT NULL,
    value  REAL,
    PRIMARY KEY (symbol, as_of, metric)
) WITHOUT ROWID;

-- `config_json` snapshots the fully resolved config so a historical run stays
-- reproducible even after screen.toml changes.
CREATE TABLE IF NOT EXISTS screen_run (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    config_hash   TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    universe_size INTEGER,
    n_clean       INTEGER,
    notes         TEXT
);

-- Audit trail of a run, not the source of truth. Current verdicts re-evaluate
-- against the current screen.toml on every read.
CREATE TABLE IF NOT EXISTS gate_result (
    run_id     INTEGER NOT NULL REFERENCES screen_run(run_id),
    symbol     TEXT NOT NULL,
    gate       TEXT NOT NULL,
    passed     INTEGER NOT NULL,
    metric     TEXT,
    value      REAL,
    threshold  TEXT,
    fail_reason TEXT,
    PRIMARY KEY (run_id, symbol, gate)
) WITHOUT ROWID;

-- ────────────────────────────────────────────────────────── ledger ────

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL REFERENCES universe(symbol),
    side        TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    price       REAL NOT NULL CHECK (price > 0),
    fees        REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
    reason      TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    thesis_ref  TEXT,
    integrity_override TEXT,        -- recorded when Gate 5 was overridden
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_symbol ON transactions(symbol, ts);

-- Append-only enforced by the database, not by convention.
CREATE TRIGGER IF NOT EXISTS transactions_no_update
BEFORE UPDATE ON transactions
BEGIN SELECT RAISE(ABORT, 'transactions is append-only; insert a reversing entry'); END;

CREATE TRIGGER IF NOT EXISTS transactions_no_delete
BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'transactions is append-only; insert a reversing entry'); END;

-- Derived position state. Views, so they cannot drift from the ledger.
CREATE VIEW IF NOT EXISTS v_positions AS
SELECT
    symbol,
    SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END) AS qty,
    SUM(CASE WHEN side = 'BUY' THEN qty * price + fees ELSE 0 END) AS gross_buy,
    SUM(CASE WHEN side = 'BUY' THEN fees ELSE 0 END) AS buy_fees,
    MIN(ts) AS first_ts,
    MAX(ts) AS last_ts
FROM transactions
GROUP BY symbol
HAVING SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END) <> 0;

-- Cash flows for XIRR: negative on buy, positive on sell.
CREATE VIEW IF NOT EXISTS v_cashflows AS
SELECT
    ts AS d,
    SUM(CASE WHEN side = 'BUY' THEN -(qty * price + fees)
             ELSE (qty * price - fees) END) AS amount
FROM transactions
GROUP BY ts;

-- ──────────────────────────────────────────── judgment (watchlist etc.) ────

CREATE TABLE IF NOT EXISTS watchlist (
    symbol        TEXT PRIMARY KEY REFERENCES universe(symbol),
    added_on      TEXT NOT NULL,
    buy_line      REAL,
    fv_low        REAL,
    fv_high       REAL,
    margin_of_safety REAL,
    status        TEXT NOT NULL DEFAULT 'watch'
                  CHECK (status IN ('watch','researching','buy','held','avoid','done')),
    thesis_path   TEXT,
    entry_reason  TEXT,
    updated_at    TEXT NOT NULL
);

-- Required by Gate 5: no provider supplies promoter pledge, auditor, RPT or
-- regulatory data, so these are attested by a human and never auto-verified.
CREATE TABLE IF NOT EXISTS integrity_check (
    symbol      TEXT NOT NULL REFERENCES universe(symbol),
    check_name  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('verified','failed','unknown')),
    evidence    TEXT,
    verified_at TEXT,
    PRIMARY KEY (symbol, check_name)
) WITHOUT ROWID;

-- Provenance for fair value. Without this, a buy-line is an unreviewable number.
CREATE TABLE IF NOT EXISTS valuation_assumption (
    symbol    TEXT NOT NULL REFERENCES universe(symbol),
    as_of     TEXT NOT NULL,
    method    TEXT NOT NULL,
    param     TEXT NOT NULL,
    value     TEXT NOT NULL,
    rationale TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, method, param)
) WITHOUT ROWID;

-- Machine-readable layer of a thesis, so the four sell criteria become a query.
CREATE TABLE IF NOT EXISTS qualitative_assessment (
    symbol     TEXT NOT NULL REFERENCES universe(symbol),
    dimension  TEXT NOT NULL,      -- 'moat_type'|'moat_trend'|'pricing_power'|'business_model'|'mgmt_integrity'|'capital_allocation'
    assessment TEXT NOT NULL,
    rationale  TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    PRIMARY KEY (symbol, dimension, as_of)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS research_verdict (
    symbol   TEXT NOT NULL REFERENCES universe(symbol),
    as_of    TEXT NOT NULL,
    verdict  TEXT NOT NULL CHECK (verdict IN ('preferred','watch','avoid')),
    reason   TEXT NOT NULL,
    evidence TEXT,
    reviewer TEXT NOT NULL DEFAULT 'sour-d',
    PRIMARY KEY (symbol, as_of)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL REFERENCES universe(symbol),
    kind            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    acknowledged_at TEXT       -- NULL = still firing; this is the dedupe key
);

CREATE INDEX IF NOT EXISTS idx_alert_open ON alerts(symbol, kind, acknowledged_at);