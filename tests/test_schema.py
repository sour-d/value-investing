"""Schema and invariant tests.

These assert the guarantees the rest of the codebase relies on. If one of these
fails, the ledger can drift and every downstream number is suspect.
"""

from __future__ import annotations

import pytest

from stocks import db


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.apply_schema(c)
    yield c
    c.close()


def _seed_universe(c, *symbols: str) -> None:
    c.executemany(
        "INSERT INTO universe (symbol, name, added_on) VALUES (?, ?, '2026-01-01')",
        [(s, f"{s} Ltd") for s in symbols],
    )
    c.commit()


def test_schema_applies_clean(conn):
    names = db.table_names(conn)
    for required in (
        "universe", "price_daily", "fundamentals_annual", "fetch_log",
        "profile_snapshot", "symbol_issue", "screen_run", "gate_result",
        "transactions", "watchlist", "integrity_check",
        "valuation_assumption", "qualitative_assessment",
        "research_verdict", "alerts", "metric_cache", "quote_snapshot",
    ):
        assert required in names, f"missing table {required}"


def test_ledger_views_are_views_not_tables(conn):
    views = db.view_names(conn)
    for v in ("v_positions", "v_cashflows"):
        assert v in views, f"{v} must be a view so it cannot drift"
        assert v not in db.table_names(conn)


def test_apply_schema_is_idempotent(conn):
    db.apply_schema(conn)
    db.apply_schema(conn)
    assert "transactions" in db.table_names(conn)


def test_transactions_are_append_only(conn):
    _seed_universe(conn, "ACC")
    conn.execute(
        "INSERT INTO transactions (ts,symbol,side,qty,price,reason,recorded_at) "
        "VALUES ('2026-01-02','ACC','BUY',10,100.0,'test','2026-01-02')"
    )
    conn.commit()

    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE transactions SET price = 1.0")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM transactions")
    conn.rollback()
    assert db.count(conn, "transactions") == 1


def test_transaction_reason_is_required(conn):
    _seed_universe(conn, "ACC")
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO transactions (ts,symbol,side,qty,price,reason,recorded_at) "
            "VALUES ('2026-01-02','ACC','BUY',10,100.0,'   ','2026-01-02')"
        )


def test_transaction_checks_positive_qty_and_price(conn):
    _seed_universe(conn, "ACC")
    for qty, price in ((0, 100.0), (10, 0.0), (-5, 100.0)):
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO transactions (ts,symbol,side,qty,price,reason,recorded_at) "
                "VALUES ('2026-01-02','ACC','BUY',?,?,'x','2026-01-02')",
                (qty, price),
            )


def test_v_positions_reflects_ledger_and_excludes_closed(conn):
    _seed_universe(conn, "ACC", "IGL")
    conn.executemany(
        "INSERT INTO transactions (ts,symbol,side,qty,price,fees,reason,recorded_at) "
        "VALUES (?,?,?,?,?,?,'r','2026-01-02')",
        [
            ("2026-01-02", "ACC", "BUY", 100, 150.0, 10.0),
            ("2026-02-02", "ACC", "BUY", 50, 160.0, 5.0),
            ("2026-03-02", "IGL", "BUY", 20, 140.0, 1.0),
            ("2026-04-02", "IGL", "SELL", 20, 150.0, 1.0),
        ],
    )
    rows = {r["symbol"]: r for r in conn.execute("SELECT * FROM v_positions")}
    assert set(rows) == {"ACC"}
    assert rows["ACC"]["qty"] == 150
    # 100*150+10 + 50*160+5 = 15010 + 8005
    assert rows["ACC"]["gross_buy"] == pytest.approx(23015.0)


def test_v_cashflows_signs(conn):
    _seed_universe(conn, "ACC")
    conn.executemany(
        "INSERT INTO transactions (ts,symbol,side,qty,price,fees,reason,recorded_at) "
        "VALUES (?,?,?,?,?,?,'r','2026-01-02')",
        [
            ("2026-01-02", "ACC", "BUY", 10, 100.0, 5.0),
            ("2026-06-02", "ACC", "SELL", 4, 120.0, 2.0),
        ],
    )
    rows = {r["d"]: r["amount"] for r in conn.execute("SELECT * FROM v_cashflows")}
    assert rows["2026-01-02"] == pytest.approx(-1005.0)
    assert rows["2026-06-02"] == pytest.approx(478.0)


def test_fundamentals_period_is_a_column_not_a_position(conn):
    """A missing fiscal year must be representable without shifting neighbours."""
    _seed_universe(conn, "ACC")
    rows = [
        ("ACC", "2026-03-31", "income", "TotalRevenue", 252.0),
        ("ACC", "2025-03-31", "income", "TotalRevenue", 261.0),
        # FY2024 deliberately absent, exactly like the real ACC payload
        ("ACC", "2022-12-31", "income", "TotalRevenue", 190.0),
    ]
    conn.executemany(
        "INSERT INTO fundamentals_annual "
        "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
        "VALUES (?,?,?,?,?,'yahoo','2026-10-01','2026-10-01')",
        rows,
    )
    conn.commit()
    got = conn.execute(
        "SELECT fy_end, value FROM fundamentals_annual "
        "WHERE symbol='ACC' AND item='TotalRevenue' ORDER BY fy_end"
    ).fetchall()
    assert [r["fy_end"] for r in got] == ["2022-12-31", "2025-03-31", "2026-03-31"]
    assert got[1]["value"] == pytest.approx(261.0)


def test_symbol_issue_severity_is_constrained(conn):
    _seed_universe(conn, "BAGMANE")
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO symbol_issue (symbol,code,severity,detected_at) "
            "VALUES ('BAGMANE','no_financials','fatal','2026-10-01')"
        )
    conn.execute(
        "INSERT INTO symbol_issue (symbol,code,severity,detected_at) "
        "VALUES ('BAGMANE','no_financials','block','2026-10-01')"
    )
    conn.commit()


def test_profile_snapshot_untrusted_by_default(conn):
    _seed_universe(conn, "ACC")
    conn.execute(
        "INSERT INTO profile_snapshot (symbol,as_of,trailing_pe,source) "
        "VALUES ('ACC','2026-10-01',11.6,'yahoo')"
    )
    conn.commit()
    row = conn.execute("SELECT trusted FROM profile_snapshot WHERE symbol='ACC'").fetchone()
    assert row["trusted"] == 0