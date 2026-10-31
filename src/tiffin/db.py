"""SQLite access: WAL connection, schema migration, append-only triggers, latest-row reads.

Table classes (schema.sql):
  ledgers     every table not listed below; migrate() gives each a BEFORE UPDATE and a
              BEFORE DELETE trigger that RAISE(ABORT). Corrections are new rows.
  projections rebuilt by the icu sync each run: icu_wellness, icu_activity.
  state       mutable by design: bot_state, conversation_state, run_ledger, icu_sync_state.
Erasure (admin.py) is the one carve-out: it drops the triggers inside one transaction,
deletes by person_id, and recreates them.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .clock import DEFAULT_CLOCK, Clock

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
SCHEMA_VERSION = 5

PROJECTION_TABLES = frozenset({"icu_wellness", "icu_activity"})
STATE_TABLES = frozenset(
    {"bot_state", "conversation_state", "run_ledger", "icu_sync_state", "schema_version"}
)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _check_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"bad identifier: {name!r}")
    return name


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database in autocommit mode (transactions are explicit via Db.tx())."""
    p = str(path)
    if readonly:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    else:
        con = sqlite3.connect(p, isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    if not readonly and p != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    return con


def tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def ledger_tables(con: sqlite3.Connection) -> list[str]:
    return [t for t in tables(con) if t not in PROJECTION_TABLES and t not in STATE_TABLES]


def trigger_sql(table: str) -> str:
    t = _check_ident(table)
    return (
        f"CREATE TRIGGER IF NOT EXISTS trg_{t}_noupd BEFORE UPDATE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;\n"
        f"CREATE TRIGGER IF NOT EXISTS trg_{t}_nodel BEFORE DELETE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;\n"
    )


def drop_trigger_sql(table: str) -> str:
    t = _check_ident(table)
    return f"DROP TRIGGER IF EXISTS trg_{t}_noupd;\nDROP TRIGGER IF EXISTS trg_{t}_nodel;\n"


def current_version(con: sqlite3.Connection) -> int:
    if "schema_version" not in tables(con):
        return 0
    row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def _apply_migration(con: sqlite3.Connection, path: Path) -> None:
    """Apply a numbered change; migration errors must be visible."""
    con.executescript(path.read_text(encoding="utf-8"))


def migrate(con: sqlite3.Connection, clock: Clock = DEFAULT_CLOCK) -> int:
    """Install the baseline schema or apply later numbered changes."""
    before = current_version(con)
    if before > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {before} is newer than this tiffin ({SCHEMA_VERSION})"
        )
    if before == 0:
        con.executescript(schema_sql())
    else:
        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            number = int(path.name[:4])
            if before < number <= SCHEMA_VERSION:
                _apply_migration(con, path)
    con.executescript("".join(trigger_sql(t) for t in ledger_tables(con)))
    if current_version(con) < SCHEMA_VERSION:
        con.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, clock.utc_stamp()),
        )
    return SCHEMA_VERSION


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    t = _check_ident(table)
    return [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]


def insert(con: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> int:
    t = _check_ident(table)
    cols = [_check_ident(c) for c in row]
    sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = con.execute(sql, tuple(row[c] for c in cols))
    return int(cur.lastrowid or 0)


def latest(con: sqlite3.Connection, table: str, **keys: Any) -> sqlite3.Row | None:
    """The newest row (highest id) matching the given equality keys."""
    t = _check_ident(table)
    where = " AND ".join(f"{_check_ident(k)} = ?" for k in keys)
    sql = f"SELECT * FROM {t}" + (f" WHERE {where}" if where else "") + " ORDER BY id DESC LIMIT 1"
    return con.execute(sql, tuple(keys.values())).fetchone()


class Db:
    """One connection per thread. Inserts fill created_at when the table has that column."""

    def __init__(self, path: Path | str, *, clock: Clock = DEFAULT_CLOCK, readonly: bool = False):
        self.path = Path(path) if str(path) != ":memory:" else path
        self.clock = clock
        self.con = connect(path, readonly=readonly)
        self._columns: dict[str, list[str]] = {}
        self._tx_depth = 0

    @classmethod
    def open(
        cls, path: Path | str, *, clock: Clock = DEFAULT_CLOCK, migrate_first: bool = True
    ) -> Db:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = cls(path, clock=clock)
        if migrate_first:
            migrate(db.con, clock)
        return db

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Db:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def columns(self, table: str) -> list[str]:
        if table not in self._columns:
            self._columns[table] = columns(self.con, table)
        return self._columns[table]

    def insert(self, table: str, **row: Any) -> int:
        if "created_at" in self.columns(table) and "created_at" not in row:
            row["created_at"] = self.clock.utc_stamp()
        return insert(self.con, table, row)

    def latest(self, table: str, **keys: Any) -> sqlite3.Row | None:
        return latest(self.con, table, **keys)

    def one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Row | None:
        return self.con.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        row = self.con.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        return self.con.execute(sql, params)

    @contextmanager
    def tx(self) -> Iterator[Db]:
        """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception. Re-entrant: nested
        tx() blocks join the outer transaction."""
        if self._tx_depth == 0:
            self.con.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.con.execute("ROLLBACK")
            raise
        else:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.con.execute("COMMIT")

    # bot_state helpers (state table, mutable)
    def state_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT v FROM bot_state WHERE k = ?", (key,))
        return default if row is None else str(row[0])

    def state_set(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO bot_state(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )

    def integrity_ok(self) -> bool:
        return self.scalar("PRAGMA integrity_check") == "ok"
