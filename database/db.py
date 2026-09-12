"""Thin sqlite3 data-access layer, WAL mode, no ORM.

Deliberately not SQLAlchemy: the controller (Python 3.9, venv-ctrl) and the
ML/RL/backend services (Python 3.11, venv-ml) both talk to the same SQLite
file, and pinning one ORM version across two independent virtualenvs is not
worth the coupling (see docs/decisions.md). stdlib sqlite3 has no such
version-skew risk.

Table-specific convenience methods are added in the phases that first need
them (e.g. telemetry_collector adds telemetry inserts in Phase 5); this
module only provides the generic primitives every one of those will sit on
top of.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: str | Path, wal: bool = True) -> sqlite3.Connection:
    """Open a sqlite3 connection configured per docs/architecture.md.

    Creates parent directories for db_path if needed (fresh checkouts don't
    ship a database/ file, only the schema).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added to a table after databases already existed in the wild.
# CREATE TABLE IF NOT EXISTS cannot introduce one, and the working tree
# must keep running against databases written by earlier versions, so
# each is added here when absent. Kept as data rather than a migration
# framework: the approved stack is stdlib sqlite3 with no ORM.
_ADDED_COLUMNS = (
    ("route_changes", "decision_inputs_json", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_schema(conn: sqlite3.Connection, schema_path: str | Path | None = None) -> None:
    """Execute schema.sql against an open connection. Idempotent."""
    path = Path(schema_path) if schema_path is not None else _SCHEMA_PATH
    with open(path, "r") as f:
        conn.executescript(f.read())
    _add_missing_columns(conn)
    conn.commit()


class Database:
    """Context-manager wrapper around a connected, schema-initialized DB."""

    def __init__(self, db_path: str | Path, wal: bool = True, init: bool = True):
        self.db_path = Path(db_path)
        self.conn = connect(self.db_path, wal=wal)
        if init:
            init_schema(self.conn)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        cur = self.conn.executemany(sql, seq_of_params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def insert(self, table: str, fields: Mapping[str, Any]) -> int:
        """Generic single-row insert. Returns the new row's rowid.

        `table` is only ever passed as a Python literal by internal callers
        (never derived from external/user input), so an f-string identifier
        is safe here; values are always bound as parameters.
        """
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
        cur = self.conn.execute(sql, tuple(fields.values()))
        self.conn.commit()
        return cur.lastrowid

    def table_names(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return [r["name"] for r in rows]

    def index_names(self, table: str) -> list[str]:
        rows = self.query(f"PRAGMA index_list({table})")
        return [r["name"] for r in rows]

    def journal_mode(self) -> str:
        row = self.query_one("PRAGMA journal_mode")
        return row[0] if row else ""
