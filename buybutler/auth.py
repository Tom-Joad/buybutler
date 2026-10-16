"""Passwords, users and login sessions.

Passwords are hashed with scrypt from the standard library. A session is a
random token in an HttpOnly cookie; the database only keeps its SHA-256, so
a copy of the database can't be used to log in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db

# scrypt cost: about 50 ms and 16 MB per hash on a small server.
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 10
USERNAME_MAX = 64

# A hash of a random password, checked when the user name is unknown, so a
# wrong name takes as long as a wrong password.
_DUMMY_HASH: str | None = None


@dataclass(frozen=True)
class User:
    id: int
    household_id: int
    username: str
    role: str


class AuthError(ValueError):
    """A user name or password that can't be used."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_username(username: str) -> str:
    username = username.strip()
    if not username or len(username) > USERNAME_MAX:
        raise AuthError(f"the user name must have 1 to {USERNAME_MAX} characters")
    if not all(c.isalnum() or c in "._-@" for c in username):
        raise AuthError("the user name may only contain letters, digits and . _ - @")
    return username


def check_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"the password must have at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > 1024:
        raise AuthError("the password is too long")
    return password


def create_user(conn: sqlite3.Connection, username: str, password: str) -> User:
    """Add a user. The first user creates the household and becomes its admin."""
    username = check_username(username)
    check_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError("a user with this name exists already")

    stamp = db.now()
    # One household for now; adding users to other households comes with
    # the user management.
    row = conn.execute("SELECT id FROM households ORDER BY id LIMIT 1").fetchone()
    if row is None:
        household_id = conn.execute(
            "INSERT INTO households (name, created_at, updated_at) VALUES ('Home', ?, ?)", (stamp, stamp)
        ).lastrowid
        role = "admin"
    else:
        household_id = row["id"]
        role = "member"

    user_id = conn.execute(
        "INSERT INTO users (household_id, username, password_hash, role, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (household_id, username, hash_password(password), role, stamp, stamp),
    ).lastrowid
    return User(user_id, household_id, username, role)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """Change a password and end every session of that user."""
    check_password(password)
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(password), db.now(), user_id),
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    global _DUMMY_HASH
    row = conn.execute(
        "SELECT id, household_id, username, role, password_hash FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()
    if row is None:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return User(row["id"], row["household_id"], row["username"], row["role"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def start_session(conn: sqlite3.Connection, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    stamp = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (_token_hash(token), user_id, _iso(stamp), _iso(stamp), _iso(stamp + timedelta(days=days))),
    )
    # Expired sessions are cleared here rather than by a timer.
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(stamp),))
    return token


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def session_user(conn: sqlite3.Connection, token: str, days: int) -> User | None:
    """The user a session token belongs to; extends the session while it is used."""
    row = conn.execute(
        "SELECT u.id, u.household_id, u.username, u.role, s.last_seen_at, s.expires_at"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    current = datetime.now(timezone.utc)
    if row["expires_at"] < _iso(current):
        end_session(conn, token)
        return None
    # A sliding expiry, written at most once an hour to keep reads cheap.
    if row["last_seen_at"] < _iso(current - timedelta(hours=1)):
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
            (_iso(current), _iso(current + timedelta(days=days)), _token_hash(token)),
        )
    return User(row["id"], row["household_id"], row["username"], row["role"])


class LoginThrottle:
    """Slows down password guessing: a client address that fails too often
    is locked out for a while. Kept in memory; a restart clears it."""

    def __init__(self, attempts: int = 5, window: float = 900.0) -> None:
        self.attempts = attempts
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, moment: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if moment - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def retry_after(self, key: str) -> int:
        """Seconds until the address may try again; 0 when it may now."""
        with self._lock:
            moment = time.monotonic()
            recent = self._recent(key, moment)
            if len(recent) < self.attempts:
                return 0
            return max(1, int(self.window - (moment - recent[0])))

    def failed(self, key: str) -> None:
        with self._lock:
            moment = time.monotonic()
            self._recent(key, moment)
            self._failures.setdefault(key, []).append(moment)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
