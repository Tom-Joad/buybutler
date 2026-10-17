"""Command line, settings and password handling."""

from __future__ import annotations

import io
import sqlite3

import pytest

from buybutler import auth, config, db
from buybutler.__main__ import main


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("BUYBUTLER_DATA_DIR", str(tmp_path))
    return tmp_path


def run(monkeypatch, args, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(args)


def test_user_add_list_passwd_delete(data, monkeypatch, capsys):
    assert run(monkeypatch, ["user", "add", "tj", "--password-stdin"], "a long password\n") == 0
    assert run(monkeypatch, ["user", "add", "sam", "--password-stdin"], "another password\n") == 0
    assert run(monkeypatch, ["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "tj\tadmin" in out and "sam\tmember" in out
    assert run(monkeypatch, ["user", "passwd", "sam", "--password-stdin"], "a third password\n") == 0
    with db.session(data / "buybutler.sqlite3") as conn:
        assert auth.authenticate(conn, "sam", "a third password") is not None
    assert run(monkeypatch, ["user", "delete", "tj"]) == 1
    assert "last admin" in capsys.readouterr().err
    assert run(monkeypatch, ["user", "delete", "sam"]) == 0


def test_user_add_rejects_short_password_and_duplicates(data, monkeypatch, capsys):
    assert run(monkeypatch, ["user", "add", "tj", "--password-stdin"], "short\n") == 1
    assert "at least" in capsys.readouterr().err
    assert run(monkeypatch, ["user", "add", "tj", "--password-stdin"], "a long password\n") == 0
    assert run(monkeypatch, ["user", "add", "TJ", "--password-stdin"], "a long password\n") == 1
    assert run(monkeypatch, ["user", "add", "bad name", "--password-stdin"], "a long password\n") == 1


def test_password_hash_is_not_the_password(data):
    stored = auth.hash_password("a long password")
    assert "a long password" not in stored and stored.startswith("scrypt$")
    assert auth.verify_password("a long password", stored)
    assert not auth.verify_password("a long passworD", stored)
    assert not auth.verify_password("x", "garbage")


def test_backup(data, monkeypatch, capsys):
    run(monkeypatch, ["user", "add", "tj", "--password-stdin"], "a long password\n")
    assert run(monkeypatch, ["backup"]) == 0
    copies = list((data / "backups").glob("buybutler-*.sqlite3"))
    assert len(copies) == 1
    conn = sqlite3.connect(copies[0])
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "tj"
    conn.close()


@pytest.mark.parametrize(
    "name,value",
    [
        ("BUYBUTLER_PORT", "eighty"),
        ("BUYBUTLER_PORT", "0"),
        ("BUYBUTLER_SESSION_DAYS", "-1"),
        ("BUYBUTLER_OFF_LOOKUP", "maybe"),
        ("BUYBUTLER_OFF_LANGUAGE", "deu"),
        ("BUYBUTLER_LOG_LEVEL", "LOUD"),
        ("BUYBUTLER_MAX_UPLOAD_MB", "1000"),
        ("BUYBUTLER_CURRENCY", "EURO"),
        ("BUYBUTLER_CURRENCY", "€"),
    ],
)
def test_bad_settings(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(config.ConfigError):
        config.load()


def test_defaults(monkeypatch):
    for name in ("BUYBUTLER_PORT", "BUYBUTLER_OFF_LOOKUP", "BUYBUTLER_DATA_DIR", "BUYBUTLER_OFF_LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    s = config.load()
    assert s.port == 8080 and s.off_lookup is True and s.off_language == "en"
    assert str(s.database) == "/config/buybutler.sqlite3"


# ---- login throttle --------------------------------------------------------


def test_throttle_holds_when_the_client_address_changes():
    t = auth.LoginThrottle()
    for n in range(5):
        wait, attempt = t.begin("tj", f"203.0.113.{n}")
        assert wait == 0 and attempt is not None
    wait, attempt = t.begin("TJ ", "203.0.113.99")
    assert attempt is None and wait > 0


def test_throttle_counts_attempts_before_the_check():
    # Parallel requests each reserve an attempt, so the sixth is refused
    # even when none of the first five has finished yet.
    t = auth.LoginThrottle()
    attempts = [t.begin("tj", "198.51.100.1")[1] for _ in range(5)]
    assert all(a is not None for a in attempts)
    assert t.begin("tj", "198.51.100.1")[1] is None


def test_throttle_success_takes_back_only_its_attempt():
    t = auth.LoginThrottle()
    for _ in range(4):
        t.begin("tj", "198.51.100.1")
    _, ok = t.begin("tj", "198.51.100.1")
    t.succeeded("tj", "198.51.100.1", ok)
    _, fifth = t.begin("tj", "198.51.100.1")
    assert fifth is not None
    assert t.begin("tj", "198.51.100.1")[1] is None


def test_throttle_per_client_limit():
    t = auth.LoginThrottle(per_client=3)
    for n in range(3):
        assert t.begin(f"user{n}", "198.51.100.1")[1] is not None
    assert t.begin("user9", "198.51.100.1")[1] is None


def test_throttle_memory_is_bounded(monkeypatch):
    t = auth.LoginThrottle()
    monkeypatch.setattr(auth.LoginThrottle, "MAX_KEYS", 100)
    for n in range(1000):
        t.begin(f"user{n}", f"client{n}")
    assert len(t._attempts) <= 100
