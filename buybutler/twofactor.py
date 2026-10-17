"""Second factors: an authenticator app (TOTP), passkeys and recovery codes.

Two-factor login is optional per user. It is on as soon as a user has a
confirmed authenticator app or at least one passkey; the password alone is
then no longer enough. A passkey also logs in on its own: it is something
the user has, unlocked with a fingerprint, face or PIN.

TOTP follows RFC 6238 (SHA-1, 6 digits, 30 seconds) with the standard
library. A code is accepted once: the newest time step used is stored, and
older or equal ones are refused. The secret is stored as it is, next to the
rest of the data in /config; a key inside the same container would add
little.

Recovery codes are random, shown once, and stored only as SHA-256. Each
works one time. Login and registration challenges live in the database for
a few minutes, so they survive a restart and need no memory of their own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import webauthn
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import db

ISSUER = "Buybutler"
STEP = 30
DIGITS = 6
RECOVERY_CODES = 10
# Without 0/O and 1/I, so a code copied by hand can't be misread.
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CHALLENGE_MINUTES = 5
# Passkey login options are handed out without a login; this caps the rows
# someone can make the server keep.
MAX_OPEN_CHALLENGES = 1000
PASSKEY_NAME_MAX = 60


class TwoFactorError(ValueError):
    """A second-factor change that can't be made."""


class TooManyChallenges(RuntimeError):
    """Too many logins are in progress at the same time."""


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- status --------------------------------------------------------------


def has_totp(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM totp_secrets WHERE user_id = ? AND confirmed_at IS NOT NULL", (user_id,)
    ).fetchone() is not None


def passkey_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM passkeys WHERE user_id = ?", (user_id,)).fetchone()[0]


def enabled(conn: sqlite3.Connection, user_id: int) -> bool:
    """Whether the password alone is no longer enough for this user."""
    return has_totp(conn, user_id) or passkey_count(conn, user_id) > 0


def methods(conn: sqlite3.Connection, user_id: int, passkeys_on: bool) -> list[str]:
    """The second factors a login can continue with."""
    found = []
    if has_totp(conn, user_id):
        found.append("totp")
    if passkeys_on and passkey_count(conn, user_id):
        found.append("passkey")
    if recovery_left(conn, user_id):
        found.append("recovery")
    return found


def status(conn: sqlite3.Connection, user_id: int) -> dict:
    rows = conn.execute(
        "SELECT id, name, created_at, updated_at, last_used_at FROM passkeys WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return {
        "enabled": enabled(conn, user_id),
        "totp": has_totp(conn, user_id),
        "passkeys": [dict(r) for r in rows],
        "recovery_codes_left": recovery_left(conn, user_id),
    }


def _after_removal(conn: sqlite3.Connection, user_id: int) -> None:
    """Recovery codes only make sense while there is a second factor."""
    if not enabled(conn, user_id):
        conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))


def reset(conn: sqlite3.Connection, user_id: int) -> None:
    """Remove every second factor of a user (lost phone)."""
    for table in ("totp_secrets", "passkeys", "recovery_codes", "auth_challenges"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


# ---- TOTP ----------------------------------------------------------------


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def provisioning_uri(secret: str, username: str) -> str:
    """The otpauth:// address the authenticator app reads from the QR code."""
    label = quote(f"{ISSUER}:{username}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def start_totp(conn: sqlite3.Connection, user_id: int) -> str:
    """A new secret, waiting for the first code to confirm it."""
    if has_totp(conn, user_id):
        raise TwoFactorError("an authenticator app is set up already; remove it first")
    secret = new_secret()
    stamp = db.now()
    conn.execute(
        "INSERT INTO totp_secrets (user_id, secret, created_at, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (user_id) DO UPDATE SET secret = excluded.secret, last_step = 0,"
        " created_at = excluded.created_at, updated_at = excluded.updated_at",
        (user_id, secret, stamp, stamp),
    )
    return secret


def _normal_code(code: str) -> str:
    return "".join(code.split()).replace("-", "").upper()


def check_totp(conn: sqlite3.Connection, user_id: int, code: str, confirming: bool = False,
               now: float | None = None) -> bool:
    """Accept a code of the current, previous or next 30 seconds, once."""
    code = _normal_code(code)
    if len(code) != DIGITS or not code.isdigit():
        return False
    row = conn.execute(
        "SELECT secret, last_step, confirmed_at FROM totp_secrets WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None or (row["confirmed_at"] is None) != confirming:
        return False
    current = int((time.time() if now is None else now) // STEP)
    for step in (current - 1, current, current + 1):
        if step > row["last_step"] and hmac.compare_digest(code_at(row["secret"], step), code):
            stamp = db.now()
            # A parallel request with the same code finds the step taken.
            taken = conn.execute(
                "UPDATE totp_secrets SET last_step = ?, last_used_at = ?, updated_at = ?,"
                " confirmed_at = COALESCE(confirmed_at, ?) WHERE user_id = ? AND last_step < ?",
                (step, stamp, stamp, stamp, user_id, step),
            ).rowcount
            return taken == 1
    return False


def remove_totp(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM totp_secrets WHERE user_id = ?", (user_id,))
    _after_removal(conn, user_id)


# ---- recovery codes ------------------------------------------------------


def _code_hash(code: str) -> str:
    return hashlib.sha256(_normal_code(code).encode()).hexdigest()


def new_recovery_codes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Replace the user's recovery codes; the old ones stop working."""
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    stamp = db.now()
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
        conn.execute(
            "INSERT INTO recovery_codes (user_id, code_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, _code_hash(raw), stamp, stamp),
        )
    return codes


def recovery_left(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (user_id,)
    ).fetchone()[0]


def use_recovery_code(conn: sqlite3.Connection, user_id: int, code: str) -> bool:
    if len(_normal_code(code)) != 10:
        return False
    stamp = db.now()
    return conn.execute(
        "UPDATE recovery_codes SET used_at = ?, updated_at = ? WHERE id = ("
        " SELECT id FROM recovery_codes WHERE user_id = ? AND code_hash = ? AND used_at IS NULL LIMIT 1)",
        (stamp, stamp, user_id, _code_hash(code)),
    ).rowcount == 1


# ---- challenges ----------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_challenge(conn: sqlite3.Connection, purpose: str, user_id: int | None, challenge: bytes = b"") -> str:
    """A short-lived ticket for a login or registration in progress."""
    moment = datetime.now(timezone.utc)
    conn.execute("DELETE FROM auth_challenges WHERE expires_at < ?", (_iso(moment),))
    if conn.execute("SELECT COUNT(*) FROM auth_challenges").fetchone()[0] >= MAX_OPEN_CHALLENGES:
        raise TooManyChallenges("too many logins in progress")
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_challenges (token_hash, user_id, purpose, challenge, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_token_hash(token), user_id, purpose, bytes_to_base64url(challenge), _iso(moment),
         _iso(moment + timedelta(minutes=CHALLENGE_MINUTES))),
    )
    return token


def get_challenge(conn: sqlite3.Connection, token: str, *purposes: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM auth_challenges WHERE token_hash = ? AND expires_at >= ?",
        (_token_hash(token), db.now()),
    ).fetchone()
    if row is None or row["purpose"] not in purposes:
        return None
    return row


def challenge_of(row: sqlite3.Row) -> bytes:
    return webauthn.base64url_to_bytes(row["challenge"])


def set_challenge(conn: sqlite3.Connection, token: str, challenge: bytes) -> None:
    conn.execute(
        "UPDATE auth_challenges SET challenge = ? WHERE token_hash = ?",
        (bytes_to_base64url(challenge), _token_hash(token)),
    )


def drop_challenge(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM auth_challenges WHERE token_hash = ?", (_token_hash(token),))


# ---- passkeys ------------------------------------------------------------


def _user_handle(conn: sqlite3.Connection, user_id: int) -> bytes:
    """The random ID a passkey keeps for its user; never the user name."""
    row = conn.execute("SELECT webauthn_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row["webauthn_id"]:
        return webauthn.base64url_to_bytes(row["webauthn_id"])
    handle = secrets.token_bytes(32)
    conn.execute(
        "UPDATE users SET webauthn_id = ?, updated_at = ? WHERE id = ?",
        (bytes_to_base64url(handle), db.now(), user_id),
    )
    return handle


def _descriptors(conn: sqlite3.Connection, user_id: int) -> list[PublicKeyCredentialDescriptor]:
    found = []
    for row in conn.execute("SELECT credential_id, transports FROM passkeys WHERE user_id = ?", (user_id,)):
        transports = []
        for name in filter(None, row["transports"].split(",")):
            try:
                transports.append(AuthenticatorTransport(name))
            except ValueError:
                pass
        found.append(PublicKeyCredentialDescriptor(
            id=webauthn.base64url_to_bytes(row["credential_id"]), transports=transports or None))
    return found


def registration_options(conn: sqlite3.Connection, rp_id: str, user_id: int, username: str) -> tuple[dict, bytes]:
    """Options for navigator.credentials.create(), and the challenge to keep."""
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=ISSUER,
        user_name=username,
        user_id=_user_handle(conn, user_id),
        exclude_credentials=_descriptors(conn, user_id),
        # A discoverable credential, so it can log in without a user name,
        # and always unlocked by the user (fingerprint, face or PIN).
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    return json.loads(webauthn.options_to_json(options)), options.challenge


def register(conn: sqlite3.Connection, rp_id: str, origin: str, user_id: int, credential: dict,
             challenge: bytes, name: str) -> int:
    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise TwoFactorError("the passkey could not be registered") from exc
    credential_id = bytes_to_base64url(verified.credential_id)
    if conn.execute("SELECT 1 FROM passkeys WHERE credential_id = ?", (credential_id,)).fetchone():
        raise TwoFactorError("this passkey is registered already")
    response = credential.get("response") if isinstance(credential, dict) else None
    transports = response.get("transports") if isinstance(response, dict) else None
    transports = [t for t in transports if isinstance(t, str)][:8] if isinstance(transports, list) else []
    stamp = db.now()
    return conn.execute(
        "INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, transports, name,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, credential_id, verified.credential_public_key, verified.sign_count,
         ",".join(t[:20] for t in transports), name, stamp, stamp),
    ).lastrowid


def authentication_options(conn: sqlite3.Connection, rp_id: str, user_id: int | None) -> tuple[dict, bytes]:
    """Options for navigator.credentials.get(). Without a user, the browser
    offers every passkey it has for this site."""
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=_descriptors(conn, user_id) if user_id is not None else None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return json.loads(webauthn.options_to_json(options)), options.challenge


def authenticate(conn: sqlite3.Connection, rp_id: str, origin: str, credential: dict, challenge: bytes,
                 user_id: int | None = None) -> int | None:
    """The user a passkey response belongs to, or None when it doesn't hold."""
    if not isinstance(credential, dict) or not isinstance(credential.get("id"), str):
        return None
    row = conn.execute(
        "SELECT p.id, p.user_id, p.public_key, p.sign_count, u.webauthn_id FROM passkeys p"
        " JOIN users u ON u.id = p.user_id WHERE p.credential_id = ?",
        (credential["id"],),
    ).fetchone()
    if row is None or (user_id is not None and row["user_id"] != user_id):
        return None
    response = credential.get("response")
    handle = response.get("userHandle") if isinstance(response, dict) else None
    if handle and handle != row["webauthn_id"]:
        return None
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError):
        return None
    stamp = db.now()
    conn.execute(
        "UPDATE passkeys SET sign_count = ?, last_used_at = ?, updated_at = ? WHERE id = ?",
        (verified.new_sign_count, stamp, stamp, row["id"]),
    )
    return row["user_id"]


def remove_passkey(conn: sqlite3.Connection, user_id: int, passkey_id: int) -> bool:
    gone = conn.execute("DELETE FROM passkeys WHERE id = ? AND user_id = ?", (passkey_id, user_id)).rowcount
    _after_removal(conn, user_id)
    return gone == 1
