"""Database connection, schema application and small query helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


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


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema. Idempotent — every object is IF NOT EXISTS."""
    conn.executescript(SCHEMA_PATH.read_text())
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
    sql = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - table names are literals
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql).fetchone()["n"])