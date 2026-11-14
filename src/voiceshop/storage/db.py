"""SQLite connection management and the schema.

Each operation opens its own connection (cheap for SQLite) so the store is safe to use from the simulator
thread, the HTTP server threads and the CLI at the same time. WAL mode lets the monitor read while a suite
writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            actor     TEXT NOT NULL,
            action    TEXT NOT NULL,
            entity_id TEXT,
            detail    TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runs (
            id          TEXT PRIMARY KEY,
            batch_id    TEXT,
            scenario_id TEXT NOT NULL,
            mode        TEXT NOT NULL,
            source      TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'error')),
            created_at  TEXT NOT NULL,
            finished_at TEXT,
            summary     TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turns (
            run_id       TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            seq          INTEGER NOT NULL,
            speaker      TEXT NOT NULL CHECK (speaker IN ('agent', 'customer')),
            language     TEXT NOT NULL,
            text_spoken  TEXT NOT NULL,
            text_heard   TEXT NOT NULL,
            act          TEXT,
            intent       TEXT,
            latency_ms   REAL,
            created_at   TEXT NOT NULL,
            agent        TEXT,
            shop_version INTEGER,
            PRIMARY KEY (run_id, seq)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_runs_created ON runs (created_at)",
    ],
    2: [
        # Per-step timing of each agent reply (understand, route, dispatch), as JSON.
        "ALTER TABLE turns ADD COLUMN stages TEXT",
    ],
    3: [
        # Approved refunds: one row per order within a scope. The primary key is what makes a second
        # approval impossible, even across processes and restarts.
        """
        CREATE TABLE IF NOT EXISTS refunds (
            scope          TEXT NOT NULL,
            order_id       TEXT NOT NULL,
            amount         INTEGER NOT NULL,
            policy_version TEXT NOT NULL,
            status         TEXT NOT NULL,
            actor          TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            PRIMARY KEY (scope, order_id)
        )
        """,
    ],
}


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def migrate(path: str | Path) -> int:
    """Create the schema if needed. Idempotent; returns the schema version."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with session(path) as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            # Refuse rather than run against a schema this code does not know: writes would fail or lose data.
            raise RuntimeError(f"database schema {current} is newer than this code ({SCHEMA_VERSION}); "
                               "point VOICESHOP_DB at a new file")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _MIGRATIONS[version]:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return conn.execute("PRAGMA user_version").fetchone()[0]
