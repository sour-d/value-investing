"""The index roster: reference data that can silently invalidate a screen.

A wrong roster does not crash. It screens the wrong 251 companies and reports a
clean run, so every check here is about refusing an inconsistent roster before
it can reach the universe table.
"""

from __future__ import annotations

import json

import pytest

from stocks import db, roster

VALID_ISIN_A = "INE012A01025"
VALID_ISIN_B = "INE203G01027"


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "stocks.db")
    db.apply_schema(connection)
    yield connection
    connection.close()


def make(**overrides) -> dict:
    """A minimal roster that passes validation, for breaking one thing at a time."""
    payload = {
        "index_code": "NIFTY_SMALLCAP250",
        "index_name": "NIFTY SMALLCAP 250",
        "declared_size": 2,
        "effective": "2026-07-01",
        "captured": "2026-10-05",
        "source": "nse csv export",
        "constituents": [
            {"symbol": "ACC", "name": "ACC Ltd.", "isin": VALID_ISIN_A,
             "industry": "Construction Materials"},
            {"symbol": "IGL", "name": "Indraprastha Gas Ltd.", "isin": VALID_ISIN_B,
             "industry": "Oil Gas & Consumable Fuels"},
        ],
    }
    payload.update(overrides)
    return payload


def codes(payload: dict) -> set[str]:
    return {f.code for f in roster.validate(roster.parse(payload))}


def errors(payload: dict) -> set[str]:
    return {f.code for f in roster.validate(roster.parse(payload))
            if f.level == "error"}


# ── validation: what must block reconciliation ───────────────────────────


def test_valid_roster_has_no_findings():
    assert roster.validate(roster.parse(make())) == []


def test_size_mismatch_is_an_error():
    # The index is named 250; a roster of a different length is stale or corrupt.
    payload = make(declared_size=251)
    assert "size_mismatch" in errors(payload)


def test_missing_declared_size_is_only_a_warning():
    payload = make(declared_size=None)
    found = {f.code for f in roster.validate(roster.parse(payload))}
    assert "no_declared_size" in found
    assert "no_declared_size" not in errors(payload)


def test_duplicate_symbol_is_an_error():
    payload = make()
    payload["constituents"].append({"symbol": "ACC", "name": "ACC Ltd.",
                                     "isin": VALID_ISIN_A})
    assert "duplicate_symbol" in errors(payload)


def test_duplicate_company_name_is_an_error():
    # The same business listed twice inflates the universe without any
    # duplicate-symbol signal, because the symbols legitimately differ.
    payload = make()
    payload["constituents"].append({"symbol": "ACCL", "name": "ACC Ltd.",
                                     "isin": VALID_ISIN_B})
    assert "duplicate_name" in errors(payload)


def test_duplicate_isin_is_an_error():
    payload = make()
    payload["constituents"].append({"symbol": "ACCBEES", "name": "ACC Bees Ltd.",
                                     "isin": VALID_ISIN_A})
    assert "duplicate_isin" in errors(payload)


def test_missing_source_is_an_error():
    # Without provenance a membership claim cannot be checked later.
    assert "no_source" in errors(make(source=""))


def test_missing_effective_date_is_only_a_warning():
    # Honest about being unverified without refusing to work.
    found = {f.code for f in roster.validate(roster.parse(make(effective=None)))}
    assert "no_effective_date" in found
    assert "no_effective_date" not in errors(make(effective=None))


@pytest.mark.parametrize("bad", ["ACC.NS", "ACC-EQ", "M M", "ACC@CM"])
def test_symbols_must_be_bare_nse_style(bad):
    payload = make()
    payload["constituents"][0]["symbol"] = bad
    assert "bad_symbol_format" in errors(payload)


@pytest.mark.parametrize("raw", ["acc", " acc ", "Acc"])
def test_symbol_case_and_padding_are_normalised_not_flagged(raw):
    # Vendor files are inconsistent about case and padding; that is our problem
    # to absorb, not the roster author's.
    payload = make()
    payload["constituents"][0]["symbol"] = raw
    assert "bad_symbol_format" not in errors(payload)


def test_ampersand_in_a_real_symbol_is_allowed():
    # M&M is a genuine NSE symbol, so '&' must not be treated as malformed.
    payload = make()
    payload["constituents"][0]["symbol"] = "M&M"
    assert "bad_symbol_format" not in errors(payload)


def test_placeholder_row_by_non_indian_isin_is_an_error():
    # NSE pads index files with filler rows. An ISIN that does not start with
    # "IN" cannot be an Indian ISIN at all.
    payload = make()
    payload["constituents"].append({"symbol": "DUMMYHEG", "name": "Something Ltd.",
                                     "isin": "DUM545A01024"})
    assert "placeholder_row" in errors(payload)


def test_placeholder_row_by_dummy_name_is_an_error():
    payload = make()
    payload["constituents"].append({"symbol": "DUMMYXYZ", "name": "Dummy XYZ Ltd."})
    assert "placeholder_row" in errors(payload)


def test_real_company_is_not_flagged_as_placeholder():
    parsed = roster.parse(make())
    assert not any(roster._is_placeholder(c) for c in parsed.constituents)


def test_empty_roster_is_an_error():
    assert "empty" in errors(make(constituents=[]))


# ── accepted input shapes ─────────────────────────────────────────────────


def test_parse_accepts_a_bare_symbol_list():
    parsed = roster.parse(["acc", "igl"])
    assert parsed.symbols == ("ACC", "IGL")


def test_parse_accepts_a_symbol_keyed_map():
    parsed = roster.parse({"ACC": {"name": "ACC Ltd."}, "IGL": {}})
    assert parsed.symbols == ("ACC", "IGL")
    assert parsed.constituents[0]["name"] == "ACC Ltd."


def test_parse_accepts_a_data_envelope():
    parsed = roster.parse({"data": [{"symbol": "ACC"}]})
    assert parsed.symbols == ("ACC",)


# ── reconciliation ───────────────────────────────────────────────────────


def test_apply_marks_members_in_with_provenance(conn):
    result = roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    assert result["members"] == 2

    row = conn.execute("SELECT * FROM universe WHERE symbol='ACC'").fetchone()
    assert row["in_index"] == 1
    assert row["isin"] == VALID_ISIN_A
    assert row["membership_source"] == "nse csv export"
    # Provenance carries the roster's claim; added_on carries when we recorded it.
    assert row["membership_as_of"] == "2026-07-01"
    assert row["added_on"] == "2026-10-05"


def test_apply_demotes_names_the_roster_omits(conn):
    roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    # A later rebalance drops IGL.
    shrunk = make(declared_size=1, constituents=[make()["constituents"][0]])

    result = roster.apply_roster(conn, roster.parse(shrunk), "2026-11-01")
    assert result["removed"] == ["IGL"]

    # Rows are never deleted; membership is a flag so the timeline survives.
    row = conn.execute(
        "SELECT in_index, removed_on, membership_as_of FROM universe WHERE symbol='IGL'"
    ).fetchone()
    assert row["in_index"] == 0
    # The operational timeline says when we noticed...
    assert row["removed_on"] == "2026-11-01"
    # ...and the roster claimed it was effective earlier.
    assert row["membership_as_of"] == "2026-07-01"


def test_apply_refuses_an_invalid_roster(conn):
    roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    with pytest.raises(ValueError, match="size_mismatch"):
        roster.apply_roster(conn, roster.parse(make(declared_size=99)), "2026-10-05")
    # Membership must be untouched by the rejected roster.
    assert conn.execute(
        "SELECT COUNT(*) FROM universe WHERE in_index=1"
    ).fetchone()[0] == 2


def test_readding_a_removed_symbol_refreshes_added_on(conn):
    roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    shrunk = make(declared_size=1, constituents=[make()["constituents"][0]])
    roster.apply_roster(conn, roster.parse(shrunk), "2026-11-01")
    roster.apply_roster(conn, roster.parse(make()), "2026-12-01")

    row = conn.execute("SELECT in_index, added_on FROM universe WHERE symbol='IGL'").fetchone()
    assert row["in_index"] == 1
    # Re-stamped on re-entry, because this database noticed it then.
    assert row["added_on"] == "2026-12-01"
    acc = conn.execute("SELECT added_on FROM universe WHERE symbol='ACC'").fetchone()
    assert acc["added_on"] == "2026-10-05"


def test_added_on_is_not_overwritten_while_still_a_member(conn):
    roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    roster.apply_roster(conn, roster.parse(make()), "2026-12-01")
    # Still a member throughout, so the first-noticed date must not drift forward.
    row = conn.execute("SELECT added_on FROM universe WHERE symbol='ACC'").fetchone()
    assert row["added_on"] == "2026-10-05"


# ── drift and coverage ───────────────────────────────────────────────────


def test_diff_reports_entrants_and_removals(conn):
    roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    moved = make(declared_size=2, constituents=[
        make()["constituents"][0],
        {"symbol": "NEWCO", "name": "Newco Ltd.", "isin": "INE999A01099",
         "industry": "Industrials"},
    ])
    drift = roster.diff(conn, roster.parse(moved))
    assert drift["entrants"] == ["NEWCO"]
    assert drift["removals"] == ["IGL"]
    assert drift["unchanged"] == ["ACC"]


def test_coverage_reports_symbols_missing_fundamentals(conn):
    roster.apply_roster(conn, roster.parse(make()), "2026-10-05")
    conn.execute(
        "INSERT INTO fundamentals_annual "
        "(symbol, fy_end, statement, item, value, source, first_seen_at, last_seen_at) "
        "VALUES ('ACC','2026-03-31','income','Total Revenue',1.0,'test',"
        "'2026-10-05','2026-10-05')"
    )
    conn.commit()

    cov = roster.coverage(conn, roster.parse(make()))
    assert cov["members"] == 2
    assert cov["with_fundamentals"] == 1
    assert cov["missing_fundamentals"] == 1
    assert cov["missing_fundamentals_symbols"] == ["IGL"]


# ── the committed roster ─────────────────────────────────────────────────


def test_committed_roster_is_valid():
    # The file every run depends on. If this fails, stop and reconcile it.
    parsed = roster.load()
    assert not roster.has_errors(roster.validate(parsed))
    assert len(parsed.constituents) == parsed.declared_size


def test_committed_roster_has_no_placeholder_rows():
    parsed = roster.load()
    assert not any(roster._is_placeholder(c) for c in parsed.constituents)


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_universe_exits_nonzero_on_errors(tmp_path, capsys):
    from stocks.cli import main

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make(declared_size=7)))
    assert main(["universe", "--roster", str(bad)]) == 1


def test_cli_universe_exits_zero_on_a_clean_roster(tmp_path, capsys):
    from stocks.cli import main

    good = tmp_path / "good.json"
    good.write_text(json.dumps(make()))
    assert main(["universe", "--roster", str(good)]) == 0


def test_cli_universe_refuses_apply_while_invalid(tmp_path):
    from stocks import db as db_mod
    from stocks.cli import main

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make(declared_size=7)))
    assert main(["--db", str(tmp_path / "x.db"), "universe",
                 "--roster", str(bad), "--apply"]) == 1
    conn = db_mod.connect(tmp_path / "x.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0] == 0
    finally:
        conn.close()