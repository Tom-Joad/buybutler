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


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str | None = None) -> User:
    """Add a user. The very first one becomes admin and gets a list "Home";
    later users start without a list and create or are given their own."""
    username = check_username(username)
    check_password(password)
    if role not in (None, "admin", "member"):
        raise AuthError("the role is admin or member")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError("a user with this name exists already")

    stamp = db.now()
    first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    role = "admin" if first else (role or "member")
    user_id = conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (username, hash_password(password), role, stamp, stamp),
    ).lastrowid
    if first:
        create_list(conn, user_id, "Home")
    return User(user_id, username, role)


def create_list(conn: sqlite3.Connection, owner_id: int, name: str) -> int:
    stamp = db.now()
    list_id = conn.execute(
        "INSERT INTO lists (name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, owner_id, stamp, stamp),
    ).lastrowid
    conn.execute(
        "INSERT INTO list_members (list_id, user_id, role, created_at, updated_at) VALUES (?, ?, 'owner', ?, ?)",
        (list_id, owner_id, stamp, stamp),
    )
    return list_id


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
        "SELECT id, username, role, password_hash FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()
    if row is None:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return User(row["id"], row["username"], row["role"])


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


def session_user(conn: sqlite3.Connection, token: str, days: int) -> tuple[User | None, bool]:
    """The user a session token belongs to, and whether the session was just
    extended (the caller then renews the cookie, so both expire together)."""
    row = conn.execute(
        "SELECT u.id, u.username, u.role, s.last_seen_at, s.expires_at"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None, False
    current = datetime.now(timezone.utc)
    if row["expires_at"] < _iso(current):
        end_session(conn, token)
        return None, False
    user = User(row["id"], row["username"], row["role"])
    # A sliding expiry, written at most once an hour to keep reads cheap.
    if row["last_seen_at"] < _iso(current - timedelta(hours=1)):
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
            (_iso(current), _iso(current + timedelta(days=days)), _token_hash(token)),
        )
        return user, True
    return user, False


class LoginThrottle:
    """Limits password guesses per account and per client address.

    The account limit is what holds: the client address can be forged
    through X-Forwarded-For, the user name being guessed can't be changed.
    The price is that someone can lock an account for a while by guessing
    wrong on purpose. Every attempt is counted before the password is
    checked, so parallel requests can't all slip under the limit; a
    successful one is taken back. Kept in memory; a restart clears it.
    """

    MAX_KEYS = 10_000

    def __init__(self, per_account: int = 5, per_client: int = 20, window: float = 900.0) -> None:
        self.per_account = per_account
        self.per_client = per_client
        self.window = window
        self._attempts: dict[str, list[float]] = {}
        self._swept = 0.0
        self._lock = threading.Lock()

    def _keys(self, username: str, client: str) -> list[tuple[str, int]]:
        return [
            ("user:" + username.strip().casefold(), self.per_account),
            ("client:" + client, self.per_client),
        ]

    def _recent(self, key: str, moment: float) -> list[float]:
        recent = [t for t in self._attempts.get(key, []) if moment - t < self.window]
        if recent:
            self._attempts[key] = recent
        else:
            self._attempts.pop(key, None)
        return recent

    def _sweep(self, moment: float) -> None:
        """Drop keys that were never seen again, so rotating addresses can't
        fill the memory."""
        if moment - self._swept < 60 and len(self._attempts) < self.MAX_KEYS:
            return
        self._swept = moment
        for key in list(self._attempts):
            self._recent(key, moment)
        if len(self._attempts) >= self.MAX_KEYS:
            newest_first = sorted(self._attempts, key=lambda k: self._attempts[k][-1], reverse=True)
            for key in newest_first[self.MAX_KEYS // 2 :]:
                del self._attempts[key]

    def begin(self, username: str, client: str) -> tuple[int, float | None]:
        """Count an attempt. Returns (seconds to wait, None) when it is refused,
        or (0, token) when it may go ahead; pass the token to succeeded()."""
        with self._lock:
            moment = time.monotonic()
            self._sweep(moment)
            keys = self._keys(username, client)
            wait = 0
            for key, limit in keys:
                recent = self._recent(key, moment)
                if len(recent) >= limit:
                    wait = max(wait, max(1, int(self.window - (moment - recent[-limit]))))
            if wait:
                return wait, None
            for key, _ in keys:
                self._attempts.setdefault(key, []).append(moment)
            return 0, moment

    def succeeded(self, username: str, client: str, token: float) -> None:
        """Take back the attempt of a correct password."""
        with self._lock:
            for key, _ in self._keys(username, client):
                entries = self._attempts.get(key)
                if entries and token in entries:
                    entries.remove(token)
                    if not entries:
                        del self._attempts[key]
