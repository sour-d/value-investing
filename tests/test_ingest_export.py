"""The MCP inbox contract: what an agent fetches must land intact.

These cover the seam most likely to break silently. A malformed payload does
not raise — it writes fewer rows, and a screen that quietly judges a name on
missing fundamentals is worse than one that crashed.
"""

from __future__ import annotations

import json

import pytest

from stocks import db, export, ingest, issues


@pytest.fixture
def conn(tmp_path):
    # A real file, not ":memory:": export_all() and ingest_all() open their own
    # connection, and each ":memory:" connection is a separate database.
    connection = db.connect(tmp_path / "stocks.db")
    db.apply_schema(connection)
    yield connection
    connection.close()


def _path(conn) -> str:
    """The file backing a connection, committing first.

    Helpers open their own connection, and SQLite will not show it rows another
    connection has written but not committed.
    """
    conn.commit()
    return str(conn.execute("PRAGMA database_list").fetchone()[2])


def write(tmp_path, name: str, payload: object) -> None:
    (tmp_path / name).write_text(json.dumps(payload))


# ── fundamentals: the MCP nests as {item: {fy_end: value}} ────────────────


def test_fundamentals_transpose_item_major_shape(conn, tmp_path):
    write(tmp_path, "fundamentals.json", {"ACC": {"income_statement": {
        "Total Revenue": {"2026-03-31": 2.5e11, "2025-03-31": 2.0e11},
        "Net Income": {"2026-03-31": 2.1e10, "2025-03-31": 1.8e10},
    }}})
    assert ingest.ingest_all(_path(conn), tmp_path)["fundamentals"] == 4

    rows = conn.execute(
        "SELECT fy_end, item, value FROM fundamentals_annual "
        "WHERE symbol='ACC' AND item='Total Revenue' ORDER BY fy_end"
    ).fetchall()
    # The item name is the outer key and the date is the inner one. Reading that
    # the other way round silently drops every row.
    assert [r["fy_end"] for r in rows] == ["2025-03-31", "2026-03-31"]
    assert rows[1]["value"] == 2.5e11


def test_nan_is_absent_not_zero(conn, tmp_path):
    # A mixed period, so the row survives and the NaN is stored as NULL. A period
    # that is entirely NaN is dropped instead; that is the next test.
    payload = {"ACC": {"income_statement": {
        "Net Income": {"2026-03-31": float("nan"), "2025-03-31": 1.8e10},
        "Total Revenue": {"2026-03-31": 2.5e11, "2025-03-31": 2.0e11},
    }}}
    assert ingest.ingest_fundamentals(conn, payload) == 4

    stored = dict(conn.execute(
        "SELECT item, value FROM fundamentals_annual WHERE symbol='ACC' "
        "AND fy_end='2026-03-31'").fetchall())
    assert stored["Net Income"] is None, (
        "NaN must not become 0; 0 would drag every average toward zero"
    )
    assert stored["Total Revenue"] == 2.5e11


def test_all_nan_period_is_skipped_entirely(conn, tmp_path):
    payload = {"ACC": {"income_statement": {"Net Income": {"2022-03-31": float("nan")}}}}
    assert ingest.ingest_fundamentals(conn, payload) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM fundamentals_annual").fetchone()[0] == 0


def test_statement_aliases_both_accepted(conn, tmp_path):
    payload = {"ACC": {
        "income_statement": {"Net Income": {"2026-03-31": 1.0}},
        "income":          {"Net Income": {"2025-03-31": 2.0}},
        "balance_sheet":   {"Total Debt": {"2026-03-31": 3.0}},
        "cashflow":        {"Free Cash Flow": {"2026-03-31": 4.0}},
    }}
    assert ingest.ingest_fundamentals(conn, payload) == 4
    statements = {r["statement"] for r in conn.execute(
        "SELECT DISTINCT statement FROM fundamentals_annual")}
    assert statements == {"income", "balance", "cashflow"}


# ── profile: nested across three vendor sections ──────────────────────────


def test_profile_flattens_nested_vendor_sections(conn, tmp_path):
    write(tmp_path, "profile.json", {"ACC": {
        "as_of": "2026-10-04",
        "financialData": {"currentPrice": 1182.8, "debtToEquity": 2.085},
        "keyStats": {"trailingEps": 101.37},
        "summaryDetail": {"marketCap": 2.2e11},
    }})
    assert ingest.ingest_profile(conn, json.loads(
        (tmp_path / "profile.json").read_text())) == 1

    row = conn.execute("SELECT * FROM profile_snapshot WHERE symbol='ACC'").fetchone()
    assert row["current_price"] == 1182.8
    assert row["trailing_eps"] == 101.37
    assert row["market_cap"] == 2.2e11
    assert row["trusted"] == 0, "an ingested vendor ratio is a claim, never a verified fact"


def test_analyst_target_is_not_stored(conn, tmp_path):
    """A sell-side target must not anchor the repo's own valuation."""
    payload = {"ACC": {"summaryDetail": {"targetMeanPrice": 1518.0, "marketCap": 1.0}}}
    ingest.ingest_profile(conn, payload)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile_snapshot)")}
    assert "target_mean_price" not in cols


# ── prices ───────────────────────────────────────────────────────────────


def test_prices_accept_data_envelope_and_bare_list(conn, tmp_path):
    bars = [{"date": "2026-10-01", "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.5, "volume": 100}]
    write(tmp_path, "prices.json", {"ACC": {"data": bars}, "INFY": bars})
    assert ingest.ingest_prices(conn, json.loads(
        (tmp_path / "prices.json").read_text())) == 2

    row = conn.execute("SELECT * FROM price_daily WHERE symbol='ACC'").fetchone()
    assert row["close"] == 1.5
    assert row["adj_close"] == 1.5, "adj_close falls back to close when the vendor omits it"


def test_prices_skip_bars_without_a_close(conn, tmp_path):
    write(tmp_path, "prices.json", {"ACC": [{"date": "2026-10-01", "close": None}]})
    assert ingest.ingest_prices(conn, json.loads(
        (tmp_path / "prices.json").read_text())) == 0


# ── idempotency and roster ───────────────────────────────────────────────


def test_reingesting_does_not_duplicate(conn, tmp_path):
    write(tmp_path, "fundamentals.json",
          {"ACC": {"income_statement": {"Net Income": {"2026-03-31": 1.0}}}})
    write(tmp_path, "prices.json",
          {"ACC": {"data": [{"date": "2026-10-01", "close": 1.5}]}})

    ingest.ingest_all(_path(conn), tmp_path)
    first = conn.execute("SELECT COUNT(*) FROM fundamentals_annual").fetchone()[0]
    ingest.ingest_all(_path(conn), tmp_path)
    second = conn.execute("SELECT COUNT(*) FROM fundamentals_annual").fetchone()[0]
    assert first == second == 1


def test_data_before_roster_is_still_stored(conn, tmp_path):
    """An agent that fetched a quote before writing universe.json must not lose it."""
    write(tmp_path, "fundamentals.json",
          {"ACC": {"income_statement": {"Net Income": {"2026-03-31": 1.0}}}})
    ingest.ingest_all(_path(conn), tmp_path)

    assert conn.execute(
        "SELECT COUNT(*) FROM fundamentals_annual WHERE symbol='ACC'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT in_index FROM universe WHERE symbol='ACC'").fetchone()[0] == 1


# ── research routes into the Buffett tables ───────────────────────────────


def test_research_lands_in_qualitative_and_valuation(conn, tmp_path):
    write(tmp_path, "research.json", {"ACC": {
        "assessments": {"moat_type": {"assessment": "narrow", "rationale": "scale"}},
        "valuation": {"method": "dcf", "rationale": "owner earnings",
                      "params": {"growth_5y": "0.12"}},
        "verdict": "watch", "reason": "not yet verified",
    }})
    assert ingest.ingest_research(conn, json.loads(
        (tmp_path / "research.json").read_text())) == 3

    assert conn.execute(
        "SELECT assessment FROM qualitative_assessment WHERE symbol='ACC'"
    ).fetchone()["assessment"] == "narrow"
    assert conn.execute(
        "SELECT value FROM valuation_assumption WHERE symbol='ACC'"
    ).fetchone()["value"] == "0.12"
    assert conn.execute(
        "SELECT verdict FROM research_verdict WHERE symbol='ACC'"
    ).fetchone()["verdict"] == "watch"


def test_assessment_without_rationale_is_rejected(conn, tmp_path):
    """A verdict with no reasoning is an opinion, and is dropped."""
    write(tmp_path, "research.json",
          {"ACC": {"assessments": {"moat_type": {"assessment": "wide"}}}})
    assert ingest.ingest_research(conn, json.loads(
        (tmp_path / "research.json").read_text())) == 0


# ── issues ───────────────────────────────────────────────────────────────


def test_issues_flag_a_symbol_with_no_statements(conn):
    conn.execute("INSERT INTO universe (symbol, in_index, added_on) VALUES ('ACC',1,'2026-10-04')")
    counts = issues.recompute_issues(conn)
    assert counts["no_financials"] == 1
    assert conn.execute(
        "SELECT severity FROM symbol_issue WHERE symbol='ACC'").fetchone()["severity"] == "block"


def test_recompute_clears_a_flag_once_data_arrives(conn):
    conn.execute("INSERT INTO universe (symbol, in_index, added_on) VALUES ('ACC',1,'2026-10-04')")
    issues.recompute_issues(conn)
    assert conn.execute("SELECT COUNT(*) FROM symbol_issue").fetchone()[0] == 1

    conn.executemany(
        "INSERT INTO fundamentals_annual "
        "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
        "VALUES ('ACC',?,'income','Net Income',1.0,'mcp','2026-10-04','2026-10-04')",
        [(f"{year}-03-31",) for year in (2023, 2024, 2025, 2026)],
    )
    issues.recompute_issues(conn)
    assert conn.execute("SELECT COUNT(*) FROM symbol_issue").fetchone()[0] == 0, (
        "a corrected import must actually clear the error"
    )


# ── export ───────────────────────────────────────────────────────────────


def test_export_uses_the_shared_clean_definition(conn, tmp_path):
    """`blocking = 1` describes every check, so selecting on it lists the universe.

    A passer is the absence of `passed = 0 AND blocking = 1`. This pins the
    export to the same helper the screen uses so the two cannot drift.
    """
    from stocks import screencmd

    conn.executemany(
        "INSERT INTO universe (symbol, in_index, added_on) VALUES (?,1,'2026-10-04')",
        [("GOOD",), ("BAD",)],
    )
    conn.execute(
        "INSERT INTO screen_run "
        "(run_id,started_at,finished_at,config_hash,config_json,universe_size,n_clean,notes) "
        "VALUES (1,'2026-10-04','2026-10-04','cfg','{}',2,1,'roic')")
    conn.executemany(
        "INSERT INTO gate_result (run_id,symbol,gate,check_name,passed,blocking) "
        "VALUES (1,?,'gate1','x',?,?)",
        [("GOOD", 1, 1), ("BAD", 0, 1)],
    )

    assert screencmd.clean_symbols(conn, 1) == {"GOOD"}
    export.export_all(_path(conn), tmp_path / "out")
    body = (tmp_path / "out" / "passers.md").read_text()
    assert "GOOD" in body
    assert "| BAD" not in body, "a blocked symbol must not be exported as a passer"


def test_first_run_does_not_claim_everyone_entered(conn, tmp_path):
    conn.execute(
        "INSERT INTO screen_run "
        "(run_id,started_at,finished_at,config_hash,config_json,universe_size,n_clean,notes) "
        "VALUES (1,'2026-10-04','2026-10-04','cfg','{}',250,9,'roic')")
    export.export_all(_path(conn), tmp_path / "out")
    body = (tmp_path / "out" / "runs.md").read_text()
    assert "no earlier run to compare" in body, (
        "on the first run every passer is trivially new; recording 250 entrants "
        "would bury the 9 that matter"
    )


def test_export_ratios_are_not_doubled(conn, tmp_path):
    """metrics returns percentages already; formatting must not multiply again."""
    conn.execute(
        "INSERT INTO universe (symbol, in_index, added_on) VALUES ('ACC',1,'2026-10-04')")
    conn.execute(
        "INSERT INTO screen_run "
        "(run_id,started_at,finished_at,config_hash,config_json,universe_size,n_clean,notes) "
        "VALUES (1,'2026-10-04','2026-10-04','cfg','{}',1,1,'roic')")
    conn.execute(
        "INSERT INTO gate_result (run_id,symbol,gate,check_name,passed,blocking) "
        "VALUES (1,'ACC','gate1','x',1,1)")

    conn.executemany(
        "INSERT INTO fundamentals_annual "
        "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
        "VALUES ('ACC',?,'income','Net Income',100.0,'mcp','2026-10-04','2026-10-04')",
        [(f"{year}-03-31",) for year in (2023, 2024, 2025, 2026)],
    )
    conn.executemany(
        "INSERT INTO fundamentals_annual "
        "(symbol,fy_end,statement,item,value,source,first_seen_at,last_seen_at) "
        "VALUES ('ACC',?,'balance','Stockholders Equity',1000.0,'mcp','2026-10-04','2026-10-04')",
        [(f"{year}-03-31",) for year in (2023, 2024, 2025, 2026)],
    )

    export.export_all(_path(conn), tmp_path / "out")
    body = (tmp_path / "out" / "passers.md").read_text()
    assert "10.0%" in body, "ROE of 100/1000 should print as 10.0%, not 1000%"