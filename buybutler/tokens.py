"""Access keys for other programs, such as a recipe book putting a recipe's
ingredients on the shopping list.

A key belongs to a user and reaches the shopping list of one list, and only
while the user may still edit that list. It is shown once when it is made;
the database keeps its SHA-256, so a copy of the database can't be used to
call the API. Programs send it as `Authorization: Bearer bbt_…`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db

PREFIX = "bbt_"
NAME_MAX = 60
TOKENS_MAX = 20


class TokenError(ValueError):
    """A key that can't be made; the message says why."""


@dataclass(frozen=True)
class Access:
    token_id: int
    user_id: int
    list_id: int
    list_name: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(conn: sqlite3.Connection, user_id: int, list_id: int, name: str) -> str:
    """A new key for the list's shopping list. The caller checked the role."""
    name = " ".join(name.split())
    if not name or len(name) > NAME_MAX:
        raise TokenError(f"the name must have 1 to {NAME_MAX} characters")
    if conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = ?", (user_id,)).fetchone()[0] >= TOKENS_MAX:
        raise TokenError(f"at most {TOKENS_MAX} access keys per user")
    token = PREFIX + secrets.token_urlsafe(32)
    stamp = db.now()
    conn.execute(
        "INSERT INTO api_tokens (user_id, list_id, name, token_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, list_id, name, _hash(token), stamp, stamp),
    )
    return token


def listing(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.name, t.list_id, l.name AS list_name, t.created_at, t.last_used_at"
        " FROM api_tokens t JOIN lists l ON l.id = t.list_id WHERE t.user_id = ? ORDER BY t.id",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke(conn: sqlite3.Connection, user_id: int, token_id: int) -> bool:
    return conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id)).rowcount == 1


def check(conn: sqlite3.Connection, token: str) -> Access | None:
    """The access a key gives, or None. A key whose user lost the right to
    edit its list (left it, became a viewer) gives none."""
    if not token.startswith(PREFIX) or len(token) > 100:
        return None
    row = conn.execute(
        "SELECT t.id, t.user_id, t.list_id, t.last_used_at, l.name AS list_name FROM api_tokens t"
        " JOIN lists l ON l.id = t.list_id"
        " JOIN list_members m ON m.list_id = t.list_id AND m.user_id = t.user_id AND m.role IN ('owner', 'editor')"
        " WHERE t.token_hash = ?",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    # The last use, written at most once an hour to keep reads cheap.
    current = datetime.now(timezone.utc)
    hour_ago = (current - timedelta(hours=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if row["last_used_at"] is None or row["last_used_at"] < hour_ago:
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (db.now(), row["id"]))
    return Access(row["id"], row["user_id"], row["list_id"], row["list_name"])
