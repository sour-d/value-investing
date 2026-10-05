"""Database connection, schema application and small query helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Bumped whenever a table's shape changes in a way ALTER TABLE cannot express
# (a changed primary key, a retyped column). Additive columns are handled
# automatically by `_ADDITIVE_COLUMNS` and must not bump this.
SCHEMA_EPOCH = 2


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and create) the database with foreign keys enabled.

    ``:memory:`` is honoured so tests can run without touching disk.
    """
    if db_path is None:
        paths.ensure_dirs()
        db_path = paths.db_path()
    elif str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the first schema revision. SQLite cannot add a column
# with `CREATE TABLE IF NOT EXISTS`, so existing databases are reconciled here
# instead of forcing a destructive re-init.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("profile_snapshot", "debt_to_equity", "REAL"),
    ("profile_snapshot", "vendor_invested_capital", "REAL"),
    # From the MCP ingest path. `grossMargins` and `targetMeanPrice` are
    # published by india-stock but were not in the original offline dump, so the
    # columns arrive late rather than invalidating every existing database.
    ("profile_snapshot", "gross_margin", "REAL"),
    # Phase 5. Rows written before this column existed carry the default of 1,
    # which matches the pre-Phase-5 behaviour: every check was blocking. Their
    # deltas remain as recorded rather than being silently reinterpreted.
    ("gate_result", "blocking", "INTEGER NOT NULL DEFAULT 1"),
    # Roster provenance. `added_on` records when *this database* first saw a
    # symbol, which is not the same fact as when the index added it. These two
    # carry the membership claim so the two can be told apart.
    ("universe", "membership_source", "TEXT"),
    ("universe", "membership_as_of", "TEXT"),
)


class SchemaOutOfDate(RuntimeError):
    """The database predates the current schema shape and cannot be migrated."""

    def __init__(self, path: str, found: int | None) -> None:
        super().__init__(
            f"{path} was built with schema epoch {found}, this build expects {SCHEMA_EPOCH}. "
            "It holds only derived data, so it is safe to rebuild: "
            f"rm {path} && stocks init && uv run python scripts/import_baseline.py /tmp/opencode"
        )
        self.path = path
        self.found = found


def _epoch(conn: sqlite3.Connection) -> int | None:
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if "universe" not in tables:
        return None  # fresh file, nothing to migrate
    if "meta_kv" not in tables:
        return 1  # predates the epoch marker; treat as the first revision
    row = conn.execute("SELECT value FROM meta_kv WHERE key = 'schema_epoch'").fetchone()
    return None if row is None else int(row["value"])


def apply_schema(conn: sqlite3.Connection, strict: bool = True) -> None:
    """Apply the schema and reconcile additive columns.

    Idempotent: every object is `IF NOT EXISTS`, and missing columns are appended
    in place. Safe to run on every command.
    """
    existing = _epoch(conn)
    if strict and existing is not None and existing != SCHEMA_EPOCH:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        raise SchemaOutOfDate(path, existing)

    conn.executescript(SCHEMA_PATH.read_text())
    for table, column, decl in _ADDITIVE_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute(
        "INSERT INTO meta_kv (key,value) VALUES ('schema_epoch',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_EPOCH),),
    )
    conn.commit()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def view_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql).fetchone()["n"])