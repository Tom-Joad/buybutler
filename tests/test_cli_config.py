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
