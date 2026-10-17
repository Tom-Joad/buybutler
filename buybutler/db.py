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
    # 2: purchase prices with an optional quantity and an offer flag.
    """
    CREATE TABLE prices (
        id            INTEGER PRIMARY KEY,
        product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
        rating_id     INTEGER REFERENCES ratings(id) ON DELETE SET NULL,
        amount_cents  INTEGER NOT NULL CHECK (amount_cents >= 0),
        quantity      REAL CHECK (quantity IS NULL OR quantity > 0),
        unit          TEXT NOT NULL DEFAULT '',
        on_offer      INTEGER NOT NULL DEFAULT 0 CHECK (on_offer IN (0, 1)),
        store         TEXT NOT NULL DEFAULT '',
        bought_on     TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX prices_product ON prices(product_id, bought_on, created_at);
    """,
    # 3: an ID the phone gives each new record, so a request repeated after
    # a lost answer doesn't create the record twice.
    """
    ALTER TABLE products ADD COLUMN client_id TEXT;
    ALTER TABLE ratings ADD COLUMN client_id TEXT;
    ALTER TABLE prices ADD COLUMN client_id TEXT;
    CREATE UNIQUE INDEX products_client_id ON products(client_id) WHERE client_id IS NOT NULL;
    CREATE UNIQUE INDEX ratings_client_id ON ratings(client_id) WHERE client_id IS NOT NULL;
    CREATE UNIQUE INDEX prices_client_id ON prices(client_id) WHERE client_id IS NOT NULL;
    """,
    # 4: several lists shared between users with roles, instead of one
    # household per user. The household becomes the first list; its admin
    # its owner, everyone else an editor. users loses household_id (rebuilt,
    # as SQLite can't drop a foreign-key column).
    """
    ALTER TABLE households RENAME TO lists;
    ALTER TABLE lists ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    ALTER TABLE products RENAME COLUMN household_id TO list_id;

    CREATE TABLE list_members (
        list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role        TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (list_id, user_id)
    );
    CREATE INDEX list_members_user ON list_members(user_id);

    INSERT INTO list_members (list_id, user_id, role, created_at, updated_at)
        SELECT household_id, id, CASE role WHEN 'admin' THEN 'owner' ELSE 'editor' END, created_at, created_at
        FROM users;
    UPDATE lists SET created_by = (
        SELECT MIN(u.id) FROM users u WHERE u.household_id = lists.id AND u.role = 'admin');

    CREATE TABLE users_new (
        id             INTEGER PRIMARY KEY,
        username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash  TEXT NOT NULL,
        role           TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    INSERT INTO users_new (id, username, password_hash, role, created_at, updated_at)
        SELECT id, username, password_hash, role, created_at, updated_at FROM users;
    DROP TABLE users;
    ALTER TABLE users_new RENAME TO users;
    """,
    # 5: second factors. An authenticator app's secret (TOTP), passkeys,
    # one-time recovery codes, and the short-lived challenges of a login or
    # passkey registration in progress. webauthn_id is the random handle a
    # passkey stores for its user.
    """
    ALTER TABLE users ADD COLUMN webauthn_id TEXT;
    CREATE UNIQUE INDEX users_webauthn_id ON users(webauthn_id) WHERE webauthn_id IS NOT NULL;

    CREATE TABLE totp_secrets (
        user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        secret        TEXT NOT NULL,
        confirmed_at  TEXT,
        last_step     INTEGER NOT NULL DEFAULT 0,
        last_used_at  TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );

    CREATE TABLE passkeys (
        id             INTEGER PRIMARY KEY,
        user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        credential_id  TEXT NOT NULL UNIQUE,
        public_key     BLOB NOT NULL,
        sign_count     INTEGER NOT NULL DEFAULT 0,
        transports     TEXT NOT NULL DEFAULT '',
        name           TEXT NOT NULL,
        last_used_at   TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE INDEX passkeys_user ON passkeys(user_id);

    CREATE TABLE recovery_codes (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        code_hash   TEXT NOT NULL,
        used_at     TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX recovery_codes_user ON recovery_codes(user_id);

    CREATE TABLE auth_challenges (
        token_hash  TEXT PRIMARY KEY,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        purpose     TEXT NOT NULL CHECK (purpose IN ('second_factor', 'passkey_login', 'passkey_register')),
        challenge   TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL
    );
    """,
    # 6: one shopping list per list. An item is a product of the list or a
    # free text; client_id names it from the phone (also before the server
    # knew it). store_order remembers where an item goes in a store's walk,
    # per product ("p:<id>") or free text ("t:<text>").
    """
    CREATE TABLE shopping_items (
        id          INTEGER PRIMARY KEY,
        list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
        product_id  INTEGER REFERENCES products(id) ON DELETE CASCADE,
        text        TEXT NOT NULL DEFAULT '',
        quantity    TEXT NOT NULL DEFAULT '',
        store       TEXT NOT NULL DEFAULT '',
        checked_at  TEXT,
        checked_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        client_id   TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        CHECK (product_id IS NOT NULL OR text <> '')
    );
    CREATE INDEX shopping_items_list ON shopping_items(list_id);
    CREATE UNIQUE INDEX shopping_items_open_product ON shopping_items(list_id, product_id)
        WHERE product_id IS NOT NULL AND checked_at IS NULL;

    CREATE TABLE store_order (
        list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
        store       TEXT NOT NULL COLLATE NOCASE,
        item_key    TEXT NOT NULL,
        position    INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (list_id, store, item_key)
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
        # Steps that rebuild a table must not trip the foreign keys midway;
        # they are checked once all steps are done.
        conn.execute("PRAGMA foreign_keys = OFF")
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
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"database has {len(broken)} broken references after the migration")
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
