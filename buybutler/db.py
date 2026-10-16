"""SQLite storage: schema, migrations and the time stamps every row carries.

Every row records `created_at` and, where it can change, `updated_at`, as
UTC ISO 8601 with milliseconds. Households and users are in the schema from
the start, so several users can be added later without moving data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Each entry moves the schema one version up; PRAGMA user_version records
# how far a database is. Never edit a released step, append a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE households (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE users (
        id             INTEGER PRIMARY KEY,
        household_id   INTEGER NOT NULL REFERENCES households(id),
        username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash  TEXT NOT NULL,
        role           TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );

    CREATE TABLE sessions (
        token_hash    TEXT PRIMARY KEY,
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at    TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        expires_at    TEXT NOT NULL
    );
    CREATE INDEX sessions_user ON sessions(user_id);

    CREATE TABLE products (
        id            INTEGER PRIMARY KEY,
        household_id  INTEGER NOT NULL REFERENCES households(id),
        barcode       TEXT,
        name          TEXT NOT NULL,
        brand         TEXT NOT NULL DEFAULT '',
        category      TEXT NOT NULL DEFAULT '',
        quantity      TEXT NOT NULL DEFAULT '',
        image         TEXT,
        source        TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'openfoodfacts')),
        created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (household_id, barcode)
    );

    CREATE TABLE ratings (
        id          INTEGER PRIMARY KEY,
        product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
        buy_again   INTEGER NOT NULL CHECK (buy_again IN (0, 1)),
        stars       INTEGER CHECK (stars BETWEEN 1 AND 5),
        reason      TEXT NOT NULL DEFAULT '',
        store       TEXT NOT NULL DEFAULT '',
        rated_on    TEXT NOT NULL,
        photo       TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX ratings_product ON ratings(product_id, rated_on, created_at);

    CREATE TABLE off_cache (
        barcode     TEXT PRIMARY KEY,
        found       INTEGER NOT NULL,
        data        TEXT,
        fetched_at  TEXT NOT NULL
    );
    """,
]


def now() -> str:
    """The current time as stored: UTC, milliseconds, `Z`."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    # FastAPI opens a request's connection in one worker thread and may use
    # it in another; each connection still serves one request at a time.
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def migrate(path: Path) -> tuple[int, int]:
    """Bring the database to the newest schema. Returns (before, after)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        # WAL lets the web app read while a write is in progress; the setting
        # is stored in the file and survives restarts.
        conn.execute("PRAGMA journal_mode = WAL")
        before = conn.execute("PRAGMA user_version").fetchone()[0]
        if before > len(MIGRATIONS):
            raise RuntimeError(
                f"database schema {before} is newer than this version supports ({len(MIGRATIONS)})"
            )
        for version in range(before, len(MIGRATIONS)):
            # One transaction per step: a failing step leaves the database
            # at the previous version (closing the connection rolls back).
            conn.executescript(
                "BEGIN;" + MIGRATIONS[version] + f"PRAGMA user_version = {version + 1}; COMMIT;"
            )
        return before, len(MIGRATIONS)
    finally:
        conn.close()


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection whose writes are committed together, or not at all."""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def backup(path: Path, target: Path) -> None:
    """Write a consistent copy of the database, also while the app runs."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = connect(path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
