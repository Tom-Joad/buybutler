"""Two-factor login: authenticator app (TOTP), passkeys and recovery codes.

Passkeys are answered by a small software authenticator (P-256, attestation
"none"), so py_webauthn checks real signatures.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
import time

import cbor2
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import bytes_to_base64url

from buybutler import auth, config, db, twofactor
from buybutler.__main__ import main
from buybutler.app import create_app

H = {"X-Buybutler": "1"}
PASSWORD = "correct horse battery"
ORIGIN = "http://localhost:8080"


class Authenticator:
    """A passkey on a fake device."""

    def __init__(self, origin: str = ORIGIN, rp_id: str = "localhost") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.origin = origin
        self.rp_id = rp_id
        self.sign_count = 0
        self.user_handle: str | None = None

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin}).encode()

    def _rp_hash(self) -> bytes:
        return hashlib.sha256(self.rp_id.encode()).digest()

    def create(self, options: dict) -> dict:
        self.user_handle = options["user"]["id"]
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})
        # Flags: user present, user verified, attested credential data.
        auth_data = (self._rp_hash() + bytes([0x45]) + struct.pack(">I", self.sign_count) + bytes(16)
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id + cose)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "hybrid"],
            },
        }

    def get(self, options: dict, verified: bool = True) -> dict:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"])
        auth_data = self._rp_hash() + bytes([0x05 if verified else 0x01]) + struct.pack(">I", self.sign_count)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": self.user_handle,
            },
        }


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BUYBUTLER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUYBUTLER_OFF_LOOKUP", "false")
    monkeypatch.setenv("BUYBUTLER_PUBLIC_URL", ORIGIN)
    return config.load()


def new_client(settings) -> TestClient:
    app = create_app(settings, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    return TestClient(app)


@pytest.fixture
def client(settings):
    with new_client(settings) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
            auth.create_user(conn, "sam", PASSWORD)
        yield c


def login(c: TestClient, name: str = "tj") -> dict:
    r = c.post("/api/session", json={"username": name, "password": PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def logged_in(client):
    login(client)
    return client


def current_code(settings, user: str = "tj", offset: int = 0) -> str:
    with db.session(settings.database) as conn:
        secret = conn.execute(
            "SELECT t.secret FROM totp_secrets t JOIN users u ON u.id = t.user_id WHERE u.username = ?", (user,)
        ).fetchone()[0]
    return twofactor.code_at(secret, int(time.time() // twofactor.STEP) + offset)


def enable_totp(c: TestClient, settings, user: str = "tj") -> list[str]:
    r = c.post("/api/account/totp", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["uri"].startswith(f"otpauth://totp/Buybutler%3A{user}?secret=")
    r = c.post("/api/account/totp/confirm", json={"code": current_code(settings, user)}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["recovery_codes"]


def add_passkey(c: TestClient, device: Authenticator, name: str = "Phone") -> dict:
    r = c.post("/api/account/passkeys/options", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["options"]["authenticatorSelection"]["residentKey"] == "required"
    r = c.post("/api/account/passkeys", json={
        "ticket": start["ticket"], "credential": device.create(start["options"]), "name": name,
    }, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def wait_for_next_step(settings) -> None:
    """A code used in this test is refused until the next 30 seconds; move
    the stored step back instead of waiting."""
    with db.session(settings.database) as conn:
        conn.execute("UPDATE totp_secrets SET last_step = last_step - 3")


# ---- TOTP ------------------------------------------------------------------


def test_totp_matches_rfc_6238():
    # RFC 6238, appendix B, SHA-1 (8 digits there; the last 6 here).
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890", RFC test value  # gitleaks:allow
    assert twofactor.code_at(secret, 59 // 30) == "287082"
    assert twofactor.code_at(secret, 1111111109 // 30) == "081804"
    assert twofactor.code_at(secret, 2000000000 // 30) == "279037"


def test_totp_setup_needs_the_password(logged_in):
    r = logged_in.post("/api/account/totp", json={"password": "wrong password!"}, headers=H)
    assert r.status_code == 400
    assert logged_in.get("/api/account/security").json()["enabled"] is False


def test_unconfirmed_totp_is_not_required(logged_in, settings):
    logged_in.post("/api/account/totp", json={"password": PASSWORD}, headers=H)
    r = logged_in.post("/api/account/totp/confirm", json={"code": "000000"}, headers=H)
    assert r.status_code == 400
    assert "username" in login(new_client(settings))


def test_totp_login(logged_in, settings):
    other = new_client(settings)
    login(other)
    codes = enable_totp(logged_in, settings)
    assert len(codes) == 10 and len(set(codes)) == 10
    # Turning it on ends every other session, this one continues.
    assert other.get("/api/session").status_code == 401
    assert logged_in.get("/api/session").status_code == 200

    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    assert step["methods"] == ["totp", "recovery"]
    assert "buybutler_session" not in fresh.cookies
    assert fresh.get("/api/session").status_code == 401
    wait_for_next_step(settings)
    r = fresh.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": current_code(settings)},
                   headers=H)
    assert r.status_code == 200 and r.json()["username"] == "tj"
    assert fresh.get("/api/session").status_code == 200


def test_a_totp_code_works_once(logged_in, settings):
    enable_totp(logged_in, settings)
    wait_for_next_step(settings)
    code = current_code(settings)
    first = new_client(settings)
    step = login(first)["second_factor"]
    assert first.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": code},
                      headers=H).status_code == 200
    second = new_client(settings)
    step = login(second)["second_factor"]
    r = second.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": code}, headers=H)
    assert r.status_code == 401


def test_wrong_codes_are_throttled(logged_in, settings):
    enable_totp(logged_in, settings)
    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    statuses = [
        fresh.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": "000000"},
                   headers=H).status_code
        for _ in range(6)
    ]
    assert statuses[-1] == 429 and 401 in statuses
    # The lock is on the account: the password alone can't get around it.
    assert fresh.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H).status_code == 429


def test_ticket_expires_and_belongs_to_the_login(logged_in, settings):
    enable_totp(logged_in, settings)
    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    r = fresh.post("/api/session/second-factor", json={"ticket": "x" * 43, "code": current_code(settings)},
                   headers=H)
    assert r.status_code == 401
    with db.session(settings.database) as conn:
        conn.execute("UPDATE auth_challenges SET expires_at = '2000-01-01T00:00:00.000Z'")
    r = fresh.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": current_code(settings)},
                   headers=H)
    assert r.status_code == 401 and "start again" in r.json()["detail"]


def test_recovery_code_works_once(logged_in, settings):
    codes = enable_totp(logged_in, settings)
    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    r = fresh.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": codes[0].lower()},
                   headers=H)
    assert r.status_code == 200 and r.json()["recovery_codes_left"] == 9
    again = new_client(settings)
    step = login(again)["second_factor"]
    r = again.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": codes[0]}, headers=H)
    assert r.status_code == 401
    with db.session(settings.database) as conn:
        stored = [r[0] for r in conn.execute("SELECT code_hash FROM recovery_codes")]
    assert codes[1] not in stored and codes[1].replace("-", "") not in stored


def test_new_recovery_codes_replace_the_old(logged_in, settings):
    old = enable_totp(logged_in, settings)
    r = logged_in.post("/api/account/recovery-codes", json={"password": PASSWORD}, headers=H)
    new = r.json()["recovery_codes"]
    assert set(old).isdisjoint(new)
    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    assert fresh.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": old[1]},
                      headers=H).status_code == 401


def test_removing_the_last_factor_turns_it_off(logged_in, settings):
    enable_totp(logged_in, settings)
    assert logged_in.request("DELETE", "/api/account/totp", json={"password": "wrong password!"},
                             headers=H).status_code == 400
    r = logged_in.request("DELETE", "/api/account/totp", json={"password": PASSWORD}, headers=H)
    assert r.json() | {"passkey_origin": None} == {
        "enabled": False, "totp": False, "passkeys": [], "recovery_codes_left": 0, "passkey_origin": None,
    }
    assert "username" in login(new_client(settings))


def test_second_factor_tables_have_time_stamps(logged_in, settings):
    enable_totp(logged_in, settings)
    add_passkey(logged_in, Authenticator())
    with db.session(settings.database) as conn:
        for table in ("totp_secrets", "passkeys", "recovery_codes"):
            row = conn.execute(f"SELECT created_at, updated_at FROM {table}").fetchone()
            assert row["created_at"].endswith("Z") and row["updated_at"].endswith("Z")


# ---- passkeys --------------------------------------------------------------


def test_passkey_login_without_a_user_name(logged_in, settings):
    device = Authenticator()
    result = add_passkey(logged_in, device)
    assert result["enabled"] is True and len(result["recovery_codes"]) == 10
    assert result["passkeys"][0]["name"] == "Phone"

    fresh = new_client(settings)
    start = fresh.post("/api/session/passkey/options", json={}, headers=H).json()
    assert start["options"].get("allowCredentials", []) == []
    r = fresh.post("/api/session/passkey", json={"ticket": start["ticket"], "credential": device.get(start["options"])},
                   headers=H)
    assert r.status_code == 200 and r.json()["username"] == "tj"
    assert fresh.get("/api/session").status_code == 200
    passkey = logged_in.get("/api/account/security").json()["passkeys"][0]
    assert passkey["last_used_at"] is not None


def test_passkey_needs_user_verification_and_a_fresh_challenge(logged_in, settings):
    device = Authenticator()
    add_passkey(logged_in, device)
    fresh = new_client(settings)
    start = fresh.post("/api/session/passkey/options", json={}, headers=H).json()
    r = fresh.post("/api/session/passkey", json={
        "ticket": start["ticket"], "credential": device.get(start["options"], verified=False)}, headers=H)
    assert r.status_code == 401
    # The challenge is gone after one answer, right or wrong.
    start = fresh.post("/api/session/passkey/options", json={}, headers=H).json()
    answer = device.get(start["options"])
    assert fresh.post("/api/session/passkey", json={"ticket": start["ticket"], "credential": answer},
                      headers=H).status_code == 200
    again = new_client(settings)
    assert again.post("/api/session/passkey", json={"ticket": start["ticket"], "credential": answer},
                      headers=H).status_code == 401


def test_passkey_from_another_site_is_refused(logged_in, settings):
    device = Authenticator(origin="https://evil.example")
    r = logged_in.post("/api/account/passkeys/options", json={"password": PASSWORD}, headers=H)
    start = r.json()
    r = logged_in.post("/api/account/passkeys", json={"ticket": start["ticket"],
                                                      "credential": device.create(start["options"])}, headers=H)
    assert r.status_code == 400
    assert logged_in.get("/api/account/security").json()["passkeys"] == []


def test_passkey_as_second_factor(logged_in, settings):
    phone = Authenticator()
    add_passkey(logged_in, phone)
    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    assert step["methods"] == ["passkey", "recovery"]
    start = fresh.post("/api/session/passkey/options", json={"ticket": step["ticket"]}, headers=H).json()
    assert start["ticket"] == step["ticket"]
    assert [c["id"] for c in start["options"]["allowCredentials"]] == [bytes_to_base64url(phone.credential_id)]
    r = fresh.post("/api/session/passkey", json={"ticket": step["ticket"], "credential": phone.get(start["options"])},
                   headers=H)
    assert r.status_code == 200 and r.json()["username"] == "tj"


def test_passkey_of_another_user_doesnt_finish_a_login(logged_in, settings):
    add_passkey(logged_in, Authenticator())
    sam = new_client(settings)
    login(sam, "sam")
    sams_key = Authenticator()
    add_passkey(sam, sams_key)
    # Someone with tj's password and sam's passkey.
    fresh = new_client(settings)
    step = login(fresh, "tj")["second_factor"]
    start = fresh.post("/api/session/passkey/options", json={"ticket": step["ticket"]}, headers=H).json()
    r = fresh.post("/api/session/passkey", json={"ticket": step["ticket"],
                                                 "credential": sams_key.get(start["options"])}, headers=H)
    assert r.status_code == 401


def test_passkey_registration_needs_the_password(logged_in):
    r = logged_in.post("/api/account/passkeys/options", json={"password": "wrong password!"}, headers=H)
    assert r.status_code == 400


def test_registration_ticket_is_not_a_login(logged_in, settings):
    device = Authenticator()
    add_passkey(logged_in, device)
    start = logged_in.post("/api/account/passkeys/options", json={"password": PASSWORD}, headers=H).json()
    fresh = new_client(settings)
    r = fresh.post("/api/session/passkey", json={"ticket": start["ticket"], "credential": device.get(start["options"])},
                   headers=H)
    assert r.status_code == 401


def test_remove_passkey(logged_in, settings):
    result = add_passkey(logged_in, Authenticator())
    passkey_id = result["passkeys"][0]["id"]
    sam = new_client(settings)
    login(sam, "sam")
    assert sam.request("DELETE", f"/api/account/passkeys/{passkey_id}", json={"password": PASSWORD},
                       headers=H).status_code == 404
    r = logged_in.request("DELETE", f"/api/account/passkeys/{passkey_id}", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["recovery_codes_left"] == 0


def test_second_passkey_keeps_the_recovery_codes(logged_in):
    first = add_passkey(logged_in, Authenticator(), "Phone")
    second = add_passkey(logged_in, Authenticator(), "Laptop")
    assert "recovery_codes" in first and "recovery_codes" not in second
    assert [p["name"] for p in second["passkeys"]] == ["Phone", "Laptop"]


def test_passkeys_are_off_without_a_public_url(tmp_path, monkeypatch):
    monkeypatch.setenv("BUYBUTLER_DATA_DIR", str(tmp_path))
    settings = config.load()
    with new_client(settings) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
        assert c.get("/api/session/options").json() == {"passkey_origin": None}
        assert c.post("/api/session/passkey/options", json={}, headers=H).status_code == 404
        login(c)
        assert c.post("/api/account/passkeys/options", json={"password": PASSWORD}, headers=H).status_code == 404
        # The authenticator app works without it.
        enable_totp(c, settings)


def test_too_many_open_challenges(client, monkeypatch):
    monkeypatch.setattr(twofactor, "MAX_OPEN_CHALLENGES", 3)
    statuses = [client.post("/api/session/passkey/options", json={}, headers=H).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]


# ---- admins and the command line -----------------------------------------


def test_admin_resets_two_factor(logged_in, settings):
    sam = new_client(settings)
    login(sam, "sam")
    enable_totp(sam, settings, "sam")
    users = {u["username"]: u for u in logged_in.get("/api/users").json()["users"]}
    assert users["sam"]["two_factor"] is True and users["tj"]["two_factor"] is False
    assert sam.post(f"/api/users/{users['tj']['id']}/reset-2fa", headers=H).status_code == 403
    r = logged_in.post(f"/api/users/{users['sam']['id']}/reset-2fa", headers=H)
    assert {u["username"]: u["two_factor"] for u in r.json()["users"]}["sam"] is False
    assert "username" in login(new_client(settings), "sam")


def test_cli_reset_2fa(logged_in, settings, monkeypatch, capsys):
    enable_totp(logged_in, settings)
    add_passkey(logged_in, Authenticator())
    assert main(["user", "list"]) == 0
    assert "tj\tadmin\t2fa" in capsys.readouterr().out
    assert main(["user", "reset-2fa", "tj"]) == 0
    with db.session(settings.database) as conn:
        for table in ("totp_secrets", "passkeys", "recovery_codes"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert "username" in login(new_client(settings))
    assert main(["user", "reset-2fa", "nobody"]) == 1


@pytest.mark.parametrize("value, error", [
    ("buybutler.example.com", "address in the browser"),
    ("https://buybutler.example.com/app", "address in the browser"),
    ("https://192.168.1.10:8080", "IP address"),
    ("http://buybutler.example.com", "https://"),
    ("https://buybutler.example.com:99999", "port"),
])
def test_public_url_is_checked(monkeypatch, value, error):
    monkeypatch.setenv("BUYBUTLER_PUBLIC_URL", value)
    with pytest.raises(config.ConfigError, match=error):
        config.load()


def test_public_url_gives_origin_and_rp_id(monkeypatch):
    monkeypatch.setenv("BUYBUTLER_PUBLIC_URL", "https://Buybutler.Example.com:8443/")
    settings = config.load()
    assert settings.public_url == "https://buybutler.example.com:8443"
    assert settings.passkey_rp_id == "buybutler.example.com"


def test_secrets_stay_out_of_the_log(logged_in, settings, caplog):
    caplog.set_level("INFO", logger="buybutler")
    codes = enable_totp(logged_in, settings)
    with db.session(settings.database) as conn:
        secret = conn.execute("SELECT secret FROM totp_secrets").fetchone()[0]
    fresh = new_client(settings)
    step = login(fresh)["second_factor"]
    fresh.post("/api/session/second-factor", json={"ticket": step["ticket"], "code": codes[0]}, headers=H)
    text = " ".join(str(r.__dict__) for r in caplog.records)
    assert "two_factor_enabled" in text and "login_succeeded" in text
    for value in (secret, codes[0], step["ticket"], PASSWORD, "'tj'"):
        assert value not in text
