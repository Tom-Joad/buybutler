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


def test_user_delete_keeps_or_clears_the_name(data, monkeypatch, capsys):
    run(monkeypatch, ["user", "add", "tj", "--password-stdin"], "a long password\n")
    settings = config.load()

    def user_with_a_rating(name: str) -> None:
        run(monkeypatch, ["user", "add", name, "--password-stdin"], "another password\n")
        with db.session(settings.database) as conn:
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
            now = db.now()
            conn.execute("INSERT INTO list_members VALUES (1, ?, 'editor', ?, ?)", (uid, now, now))
            pid = conn.execute("INSERT INTO products (list_id, name, created_by, created_at, updated_at)"
                               " VALUES (1, ?, ?, ?, ?)", (name, uid, now, now)).lastrowid
            conn.execute("INSERT INTO ratings (product_id, user_id, buy_again, rated_on, created_at, updated_at)"
                         " VALUES (?, ?, 1, '2026-01-01', ?, ?)", (pid, uid, now, now))

    def names() -> list[tuple]:
        with db.session(settings.database) as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT p.name, p.created_by_name, r.user_name FROM products p JOIN ratings r ON r.product_id = p.id"
                " ORDER BY p.id")]

    for name in ("kept", "cleared", "script"):
        user_with_a_rating(name)
    capsys.readouterr()
    assert run(monkeypatch, ["user", "delete", "kept", "--keep-name"]) == 0
    assert "name was kept" in capsys.readouterr().out
    assert run(monkeypatch, ["user", "delete", "cleared", "--clear-name"]) == 0
    # Without a terminal nobody can be asked: as before, the name goes.
    assert run(monkeypatch, ["user", "delete", "script"]) == 0
    assert "name was removed" in capsys.readouterr().out
    assert names() == [("kept", "kept", "kept"), ("cleared", "", ""), ("script", "", "")]
    with pytest.raises(SystemExit):
        run(monkeypatch, ["user", "delete", "tj", "--keep-name", "--clear-name"])


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
        ("BUYBUTLER_EXPORT", "sometimes"),
        ("BUYBUTLER_EXPORT_SECONDS", "5"),
        ("BUYBUTLER_TRASH_DAYS", "-1"),
    ],
)
def test_bad_settings(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(config.ConfigError):
        config.load()


def test_defaults(monkeypatch):
    for name in ("BUYBUTLER_PORT", "BUYBUTLER_OFF_LOOKUP", "BUYBUTLER_DATA_DIR", "BUYBUTLER_OFF_LANGUAGE",
                 "BUYBUTLER_EXPORT", "BUYBUTLER_EXPORT_SECONDS", "BUYBUTLER_TRASH_DAYS"):
        monkeypatch.delenv(name, raising=False)
    s = config.load()
    assert s.port == 8080 and s.off_lookup is True and s.off_language == "en"
    assert str(s.database) == "/config/buybutler.sqlite3"
    assert (s.export, s.export_seconds, s.trash_days) == (True, 60, 30)
    assert (str(s.export_dir), str(s.trash)) == ("/config/export", "/config/trash")


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


def test_flooding_the_throttle_does_not_free_a_locked_account(monkeypatch):
    t = auth.LoginThrottle()
    monkeypatch.setattr(auth.LoginThrottle, "MAX_KEYS", 100)
    for _ in range(5):
        t.begin("alice", "1.1.1.1")
    assert t.begin("alice", "1.1.1.1")[1] is None
    for n in range(1000):
        t.begin(f"x{n}", f"10.0.{n // 256}.{n % 256}")
    assert t.begin("alice", "2.2.2.2")[1] is None
    assert len(t._attempts) <= 100


def test_a_full_throttle_refuses_new_keys_instead_of_dropping_locked_ones(monkeypatch):
    t = auth.LoginThrottle(per_account=1, per_client=1)
    monkeypatch.setattr(auth.LoginThrottle, "MAX_KEYS", 10)
    for n in range(5):
        assert t.begin(f"user{n}", f"198.51.100.{n}")[1] is not None  # 10 locked keys
    wait, attempt = t.begin("newcomer", "203.0.113.1")
    assert attempt is None and wait > 0
    assert len(t._attempts) == 10
    assert t.begin("user0", "203.0.113.2")[1] is None  # still locked


def test_log_time_is_local_with_offset(monkeypatch):
    import json
    import logging
    import time

    from buybutler.logging_setup import JsonFormatter

    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        record = logging.LogRecord("buybutler", logging.INFO, "", 0, "starting", None, None)
        record.created = 1767225600.0  # 2026-01-01 00:00 UTC
        line = json.loads(JsonFormatter().format(record))
        assert line["ts"] == "2026-01-01T01:00:00.000+01:00"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
