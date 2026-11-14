"""The readable copy under export/: what it holds, when it is written and
that the database marks every change."""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from buybutler import auth, db, export, files
from buybutler.__main__ import main
from buybutler.app import create_app

from .test_app import H, JPEG, PASSWORD, make_product, other_user_with_list, rate
from .test_app import client, fake_off, logged_in, settings  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def by_hand(monkeypatch):
    """No export on its own: the tests run it when they want it."""
    monkeypatch.setenv("BUYBUTLER_EXPORT", "false")


def run(settings, **kw) -> dict:
    return export.Export(settings).run(**kw)


def pending(settings) -> tuple[list[int], list[int]]:
    with db.session(settings.database) as conn:
        return (
            [r[0] for r in conn.execute("SELECT id FROM products WHERE export_rev > exported_rev ORDER BY id")],
            [r[0] for r in conn.execute("SELECT id FROM lists WHERE export_rev > exported_rev ORDER BY id")],
        )


def header(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n") and text.endswith("---\n")
    return yaml.safe_load(text[4:-4])


def product_file(settings, product_id: int) -> Path:
    found = list(settings.export_dir.glob(f"*/products/{product_id}-*/product.md"))
    found += list(settings.export_dir.glob(f"*/products/{product_id}/product.md"))
    assert len(found) == 1, found
    return found[0]


def price(c, product_id: int, **kw) -> dict:
    r = c.post(f"/api/products/{product_id}/prices", json={"amount_cents": 199} | kw, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


# ---- what the files say ----------------------------------------------------


def test_a_product_reads_back_as_the_database_has_it(logged_in, settings):
    p = make_product(logged_in, name="Käse-Spätzle \"fein\"", brand="Älpler", barcode="0012345678905",
                     category="Teigwaren", quantity="500 g")
    logged_in.put(f"/api/products/{p['id']}/image", content=JPEG, headers=H)
    first = rate(logged_in, p["id"], buy_again=False, stars=2, store="Rewe", reason="zu salzig\nund teuer: 2,49 €",
                 rated_on="2026-03-02")
    rate(logged_in, p["id"], rated_on="2026-05-01")
    price(logged_in, p["id"], amount_cents=249, quantity=500, unit="g", store="Rewe", bought_on="2026-03-02")
    price(logged_in, p["id"], amount_cents=5, quantity=0.5, unit="l", on_offer=True, bought_on="2026-04-01")

    assert run(settings) == {"lists": 1, "products": 1, "removed": 0}
    path = product_file(settings, p["id"])
    assert path.parent.name == f"{p['id']}-kaese-spaetzle-fein"
    assert path.parent.parent.parent.name == "1-home"
    data = header(path)
    detail = logged_in.get(f"/api/products/{p['id']}").json()

    assert data["format"] == "buybutler/1" and data["id"] == p["id"] and data["list"] == "Home"
    assert data["name"] == "Käse-Spätzle \"fein\"" and data["brand"] == "Älpler"
    # Text, not the number 12345678905.
    assert data["barcode"] == "0012345678905"
    assert data["category"] == "Teigwaren" and data["quantity"] == "500 g" and data["source"] == "manual"
    assert data["created"] == detail["created_at"] and data["updated"] == detail["updated_at"]
    assert data["created_by"] == "tj"
    # The path leads from the product's folder to the picture.
    assert (path.parent / data["photo"]).resolve().read_bytes() == JPEG

    assert [r["date"] for r in data["ratings"]] == ["2026-03-02", "2026-05-01"]  # oldest first, as text
    r = data["ratings"][0]
    assert r == {"id": first["id"], "date": "2026-03-02", "by": "tj", "buy_again": False, "stars": 2, "store": "Rewe",
                 "reason": "zu salzig\nund teuer: 2,49 €", "created": first["created_at"],
                 "updated": first["updated_at"]}
    assert data["ratings"][1]["buy_again"] is True and "stars" not in data["ratings"][1]

    a, b = data["prices"]
    assert (a["amount"], a["quantity"], a["unit"], a["store"], a["date"]) == (2.49, 500, "g", "Rewe", "2026-03-02")
    assert "offer" not in a and isinstance(a["quantity"], int)
    assert (b["amount"], b["quantity"], b["unit"], b["offer"]) == (0.05, 0.5, "l", True)
    # No key a YAML 1.1 reader turns into true or false (`on`, `no`, `y` …).
    assert all(isinstance(k, str) for row in (data, r, a) for k in row)


def test_texts_yaml_would_misread_stay_texts(logged_in, settings):
    odd = "yes: [no] #x \\ 'q'   \x85 ﻿ 🧀 – true"
    p = make_product(logged_in, name=odd, brand="null", category="2026-03-02", quantity="1e3")
    run(settings)
    data = header(product_file(settings, p["id"]))
    assert (data["name"], data["brand"], data["category"], data["quantity"]) == (odd, "null", "2026-03-02", "1e3")


def test_list_file_names_members_and_currency(logged_in, settings, monkeypatch):
    with db.session(settings.database) as conn:
        auth.create_user(conn, "sam", PASSWORD)
    logged_in.post("/api/lists/1/members", json={"username": "sam", "role": "viewer"}, headers=H)
    run(settings)
    data = header(settings.export_dir / "1-home" / "list.md")
    assert data["format"] == "buybutler-list/1" and data["name"] == "Home" and data["currency"] == "EUR"
    assert [(m["user"], m["role"]) for m in data["members"]] == [("tj", "owner"), ("sam", "viewer")]

    # Another currency reaches the file at the next start, without a change in the database.
    chf = dataclasses.replace(settings, currency="CHF")
    assert export.Export(chf).run(check=True)["lists"] == 1
    assert header(settings.export_dir / "1-home" / "list.md")["currency"] == "CHF"


def test_the_format_description_is_written_and_its_examples_parse(settings):
    copy = export.Export(settings)
    copy.prepare()
    text = (settings.export_dir / "FORMAT.md").read_text(encoding="utf-8")
    assert text == Path(export.__file__).with_name("FORMAT.md").read_text(encoding="utf-8")
    blocks = [b.split("```")[0] for b in text.split("```markdown\n")[1:]]
    product, lst = (yaml.safe_load(b[4:-4]) for b in blocks)
    assert product["format"] == export.PRODUCT_FORMAT and product["barcode"] == "4000000000001"
    assert lst["format"] == export.LIST_FORMAT and len(lst["members"]) == 2


def test_one_list_holds_nothing_of_another(logged_in, settings):
    mine = make_product(logged_in, name="Mine")
    other = other_user_with_list(logged_in, settings)
    theirs = make_product(other, name="Theirs")
    run(settings)
    assert product_file(settings, mine["id"]).parent.parent.parent.name == "1-home"
    assert product_file(settings, theirs["id"]).parent.parent.parent.name == "2-other-s-list"
    assert "Theirs" not in "".join(p.read_text() for p in (settings.export_dir / "1-home").rglob("*.md"))


# ---- every change is marked --------------------------------------------------


def test_every_change_to_a_product_marks_it(logged_in, settings):
    p = make_product(logged_in)
    pid = p["id"]

    def marked(what: str) -> dict:
        assert pending(settings)[0] == [pid], what
        assert run(settings)["products"] == 1, what
        assert pending(settings) == ([], []), what
        return header(product_file(settings, pid))

    marked("created")
    assert logged_in.patch(f"/api/products/{pid}", json={"brand": "Mill"}, headers=H).status_code == 200
    assert marked("product changed")["brand"] == "Mill"
    logged_in.put(f"/api/products/{pid}/image", content=JPEG, headers=H)
    assert "photo" in marked("picture set")
    logged_in.delete(f"/api/products/{pid}/image", headers=H)
    assert "photo" not in marked("picture removed")

    r = rate(logged_in, pid, stars=3)
    assert marked("rated")["ratings"][0]["stars"] == 3
    assert logged_in.patch(f"/api/ratings/{r['id']}", json={"stars": 5}, headers=H).status_code == 200
    assert marked("rating changed")["ratings"][0]["stars"] == 5
    logged_in.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H)
    assert "photo" in marked("rating photo set")["ratings"][0]
    logged_in.delete(f"/api/ratings/{r['id']}/photo", headers=H)
    assert "photo" not in marked("rating photo removed")["ratings"][0]

    pr = price(logged_in, pid)
    assert marked("price added")["prices"][0]["amount"] == 1.99
    assert logged_in.patch(f"/api/prices/{pr['id']}", json={"amount_cents": 250}, headers=H).status_code == 200
    assert marked("price changed")["prices"][0]["amount"] == 2.5
    assert logged_in.delete(f"/api/prices/{pr['id']}", headers=H).status_code == 204
    assert "prices" not in marked("price deleted")
    assert logged_in.delete(f"/api/ratings/{r['id']}", headers=H).status_code == 204
    assert "ratings" not in marked("rating deleted")


def test_every_change_to_a_list_marks_it(logged_in, settings):
    with db.session(settings.database) as conn:
        auth.create_user(conn, "sam", PASSWORD)
    run(settings)

    def marked(what: str) -> dict:
        assert pending(settings)[1] == [1], what
        assert run(settings)["lists"] == 1, what
        assert pending(settings) == ([], []), what
        return header(next(settings.export_dir.glob("1-*/list.md")))

    logged_in.post("/api/lists/1/members", json={"username": "sam", "role": "viewer"}, headers=H)
    assert len(marked("member added")["members"]) == 2
    sam = logged_in.get("/api/lists/1/members").json()["members"][1]["user_id"]
    logged_in.patch(f"/api/lists/1/members/{sam}", json={"role": "editor"}, headers=H)
    assert marked("role changed")["members"][1]["role"] == "editor"
    logged_in.delete(f"/api/lists/1/members/{sam}", headers=H)
    assert len(marked("member removed")["members"]) == 1
    assert logged_in.patch("/api/lists/1", json={"name": "Zuhause"}, headers=H).status_code == 200
    assert marked("renamed")["name"] == "Zuhause"


def test_shopping_and_logins_mark_nothing(logged_in, settings):
    p = make_product(logged_in)
    run(settings)
    item = logged_in.post("/api/shopping", json={"client_id": "item-0000000000001", "product_id": p["id"]},
                          headers=H)
    assert item.status_code == 201, item.text
    logged_in.patch(f"/api/shopping/{item.json()['id']}", json={"checked": True}, headers=H)
    logged_in.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
    assert pending(settings) == ([], [])
    assert run(settings) == {"lists": 0, "products": 0, "removed": 0}


def test_a_deleted_user_leaves_the_records_without_a_name(logged_in, settings):
    with db.session(settings.database) as conn:
        auth.create_user(conn, "sam", PASSWORD)
        sam = conn.execute("SELECT id FROM users WHERE username = 'sam'").fetchone()[0]
    logged_in.post("/api/lists/1/members", json={"username": "sam", "role": "editor"}, headers=H)
    c = TestClient(logged_in.app)
    c.post("/api/session", json={"username": "sam", "password": PASSWORD}, headers=H)
    c.headers["X-Buybutler-List"] = "1"
    p = make_product(c)
    rate(c, p["id"])
    price(c, p["id"])
    run(settings)
    assert header(product_file(settings, p["id"]))["ratings"][0]["by"] == "sam"

    r = logged_in.request("DELETE", f"/api/users/{sam}", json={"admin_password": PASSWORD}, headers=H)
    assert r.status_code == 204, r.text
    assert pending(settings) == ([p["id"]], [1])
    run(settings)
    data = header(product_file(settings, p["id"]))
    assert "created_by" not in data and "by" not in data["ratings"][0] and "by" not in data["prices"][0]
    assert [m["user"] for m in header(settings.export_dir / "1-home" / "list.md")["members"]] == ["tj"]


def test_a_deleted_user_keeps_the_name_when_asked(logged_in, settings):
    with db.session(settings.database) as conn:
        auth.create_user(conn, "sam", PASSWORD)
        sam = conn.execute("SELECT id FROM users WHERE username = 'sam'").fetchone()[0]
    logged_in.post("/api/lists/1/members", json={"username": "sam", "role": "editor"}, headers=H)
    c = TestClient(logged_in.app)
    c.post("/api/session", json={"username": "sam", "password": PASSWORD}, headers=H)
    c.headers["X-Buybutler-List"] = "1"
    p = make_product(c)
    rate(c, p["id"])
    price(c, p["id"])
    before = logged_in.get(f"/api/products/{p['id']}").json()
    run(settings)

    r = logged_in.request("DELETE", f"/api/users/{sam}", json={"admin_password": PASSWORD, "keep_name": True},
                          headers=H)
    assert r.status_code == 204, r.text
    after = logged_in.get(f"/api/products/{p['id']}").json()
    assert after["rating"]["user"] == "sam" and after["prices"][0]["user"] == "sam"
    # Keeping the name is no change to the record: its time stamps stay.
    assert after["rating"]["updated_at"] == before["rating"]["updated_at"]
    assert after["updated_at"] == before["updated_at"]
    assert pending(settings)[0] == [p["id"]]
    run(settings)
    data = header(product_file(settings, p["id"]))
    assert data["created_by"] == "sam" and data["ratings"][0]["by"] == "sam" and data["prices"][0]["by"] == "sam"
    # A new user of that name is someone else; the old records keep the name, the new ones are theirs.
    with db.session(settings.database) as conn:
        assert conn.execute("SELECT user_id, user_name FROM ratings").fetchone()[:] == (None, "sam")


# ---- when files are written --------------------------------------------------


def test_several_changes_are_one_write_and_no_change_is_none(logged_in, settings, monkeypatch):
    p = make_product(logged_in)
    written: list[str] = []
    real = files.write_if_changed

    def spy(path, text, tmp):
        done = real(path, text, tmp)
        if done:
            written.append(path.name)
        return done

    monkeypatch.setattr(files, "write_if_changed", spy)
    for stars in (1, 2, 3):
        rate(logged_in, p["id"], stars=stars)
    price(logged_in, p["id"])
    run(settings)
    assert written == ["list.md", "product.md"]

    # Nothing pending: not a file, not a commit.
    synced: list[Path] = []
    monkeypatch.setattr(files, "sync_tree", synced.append)
    with db.session(settings.database) as conn:
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        assert run(settings) == {"lists": 0, "products": 0, "removed": 0}
        assert conn.execute("PRAGMA data_version").fetchone()[0] == version
    assert written == ["list.md", "product.md"] and synced == []

    # A change that leaves the text as it is (the same value again) writes no file.
    logged_in.patch(f"/api/products/{p['id']}", json={"name": p["name"]}, headers=H)
    before = product_file(settings, p["id"]).stat()
    run(settings)
    after = product_file(settings, p["id"]).stat()
    if header(product_file(settings, p["id"]))["updated"] == p["updated_at"]:
        assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert pending(settings) == ([], [])


def test_a_run_that_dies_before_the_flush_is_repeated(logged_in, settings, monkeypatch):
    p = make_product(logged_in)

    def dies(path):
        raise OSError("power cut")

    monkeypatch.setattr(files, "sync_tree", dies)
    with pytest.raises(OSError):
        run(settings)
    assert pending(settings) == ([p["id"]], [1])
    monkeypatch.undo()
    # The files are there already, so nothing is written, but now it counts.
    assert run(settings) == {"lists": 0, "products": 0, "removed": 0}
    assert pending(settings) == ([], [])
    assert not list((settings.export_dir / ".tmp").iterdir())


def test_a_change_during_a_run_stays_pending(logged_in, settings, monkeypatch):
    p = make_product(logged_in)
    real = files.sync_tree

    def meanwhile(path):
        rate(logged_in, p["id"], stars=1)
        real(path)

    monkeypatch.setattr(files, "sync_tree", meanwhile)
    run(settings)
    monkeypatch.undo()
    assert pending(settings)[0] == [p["id"]]
    assert "ratings" not in header(product_file(settings, p["id"]))
    run(settings)
    assert header(product_file(settings, p["id"]))["ratings"][0]["stars"] == 1


def test_a_renamed_product_or_list_moves_its_folder(logged_in, settings):
    p = make_product(logged_in, name="Rye bread")
    run(settings)
    old = product_file(settings, p["id"]).parent
    (old / "note.txt").write_text("mine")
    logged_in.patch(f"/api/products/{p['id']}", json={"name": "Roggenbrot"}, headers=H)
    logged_in.patch("/api/lists/1", json={"name": "Daheim"}, headers=H)
    run(settings)
    new = product_file(settings, p["id"]).parent
    assert not old.exists() and new == settings.export_dir / "1-daheim" / "products" / f"{p['id']}-roggenbrot"
    assert (new / "note.txt").read_text() == "mine"
    assert header(new / "product.md")["list"] == "Daheim"
    assert [d.name for d in settings.export_dir.iterdir() if d.is_dir() and d.name != ".tmp"] == ["1-daheim"]


def test_a_name_without_letters_and_a_copied_folder(logged_in, settings):
    p = make_product(logged_in, name="🧀")
    run(settings)
    folder = settings.export_dir / "1-home" / "products" / str(p["id"])
    assert folder.is_dir()
    # A copy made in a file manager carries the same number; the export goes on.
    copy = folder.with_name(f"{p['id']}-kopie")
    copy.mkdir()
    (copy / "product.md").write_text("old")
    logged_in.patch(f"/api/products/{p['id']}", json={"name": "Cheese"}, headers=H)
    run(settings)
    run(settings, everything=True)
    assert header(folder.with_name(f"{p['id']}-cheese") / "product.md")["name"] == "Cheese"


# ---- deleting ----------------------------------------------------------------


def test_deleted_products_and_lists_go_to_the_trash(logged_in, settings):
    keep = make_product(logged_in, name="Keep")
    gone = make_product(logged_in, name="Gone")
    other = other_user_with_list(logged_in, settings)
    theirs = make_product(other, name="Theirs")
    run(settings)

    assert logged_in.delete(f"/api/products/{gone['id']}", headers=H).status_code == 204
    assert other.request("DELETE", "/api/lists/2", json={"confirm_name": "other's list"}, headers=H).status_code == 204
    assert run(settings) == {"lists": 0, "products": 0, "removed": 2}
    trashed = sorted(d.name for d in settings.trash.iterdir())
    assert len(trashed) == 2
    assert trashed[0].startswith(f"{gone['id']}-gone-") and trashed[1].startswith("2-other-s-list-")
    # The list went as one folder, its product in it.
    assert (settings.trash / trashed[1] / "products" / f"{theirs['id']}-theirs" / "product.md").exists()
    assert product_file(settings, keep["id"]).exists()
    assert not list(settings.export_dir.glob(f"*/products/{gone['id']}-*"))
    with db.session(settings.database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM export_gone").fetchone()[0] == 0


def test_a_number_given_again_gets_a_fresh_folder(logged_in, settings):
    old = make_product(logged_in, name="Old")
    run(settings)
    logged_in.delete(f"/api/products/{old['id']}", headers=H)
    new = make_product(logged_in, name="New")
    assert new["id"] == old["id"]  # SQLite hands out the newest row's number again
    assert run(settings) == {"lists": 0, "products": 1, "removed": 1}
    assert product_file(settings, new["id"]).parent.name == f"{new['id']}-new"
    assert [d.name.rsplit("-", 2)[0] for d in settings.trash.iterdir()] == [f"{old['id']}-old"]


def test_deleted_before_it_was_ever_written(logged_in, settings):
    p = make_product(logged_in)
    logged_in.delete(f"/api/products/{p['id']}", headers=H)
    assert run(settings) == {"lists": 1, "products": 0, "removed": 0}
    assert not settings.trash.exists()


def test_the_trash_is_emptied_after_the_days_set(settings):
    copy = export.Export(dataclasses.replace(settings, trash_days=30))
    settings.trash.mkdir()
    for name in ("7-old-20200101-120000", "8-old-20200101-120000~2", "9-new-" + time.strftime("%Y%m%d-%H%M%S"),
                 "not-ours"):
        (settings.trash / name).mkdir()
    (settings.trash / "file-20200101-120000").write_text("x")
    assert copy.empty_trash() == 2
    assert sorted(p.name for p in settings.trash.iterdir())[:2] == [
        "9-new-" + sorted(p.name for p in settings.trash.iterdir())[0][6:], "file-20200101-120000"]
    assert (settings.trash / "not-ours").is_dir()
    (settings.trash / "7-old-20200101-120000").mkdir()
    assert export.Export(dataclasses.replace(settings, trash_days=0)).empty_trash() == 0


# ---- start, commands, switch -------------------------------------------------


def test_at_start_missing_files_come_back_and_leftovers_go(logged_in, settings):
    p = make_product(logged_in)
    run(settings)
    product_file(settings, p["id"]).unlink()
    product_file_dir = settings.export_dir / "1-home" / "products" / f"{p['id']}-rye-bread"
    product_file_dir.rmdir()
    (settings.export_dir / ".tmp" / "left.tmp").write_text("half")
    assert run(settings) == {"lists": 0, "products": 0, "removed": 0}  # nothing changed, nothing looked at
    copy = export.Export(settings)
    copy.prepare()
    assert copy.run(check=True)["products"] == 1
    assert product_file(settings, p["id"]).exists()
    assert not list((settings.export_dir / ".tmp").iterdir())


def test_a_database_from_before_is_written_whole(tmp_path, settings):
    """Schema 8 (1.3.3) with data, then this version starts on it."""
    conn = sqlite3.connect(settings.database)
    for version in range(8):
        conn.executescript("BEGIN;" + db.MIGRATIONS[version] + f"PRAGMA user_version = {version + 1}; COMMIT;")
    now = db.now()
    conn.execute("INSERT INTO users (id, username, password_hash, role, created_at, updated_at)"
                 " VALUES (1, 'tj', 'x', 'admin', ?, ?)", (now, now))
    conn.execute("INSERT INTO lists (id, name, created_at, updated_at, created_by) VALUES (1, 'Home', ?, ?, 1)",
                 (now, now))
    conn.execute("INSERT INTO list_members VALUES (1, 1, 'owner', ?, ?)", (now, now))
    for i in (1, 2, 3):
        conn.execute("INSERT INTO products (id, list_id, name, created_at, updated_at) VALUES (?, 1, ?, ?, ?)",
                     (i, f"Product {i}", now, now))
    conn.execute("INSERT INTO ratings (product_id, user_id, buy_again, rated_on, created_at, updated_at)"
                 " VALUES (2, 1, 1, '2026-01-01', ?, ?)", (now, now))
    conn.commit()
    conn.close()

    assert db.migrate(settings.database) == (8, len(db.MIGRATIONS))
    assert pending(settings) == ([1, 2, 3], [1])
    assert run(settings) == {"lists": 1, "products": 3, "removed": 0}
    assert header(product_file(settings, 2))["ratings"][0]["by"] == "tj"


def test_export_command_and_backup(logged_in, settings, capsys):
    p = make_product(logged_in)
    assert main(["export"]) == 0
    assert "1 products and 1 lists written" in capsys.readouterr().out
    assert (settings.export_dir / "FORMAT.md").exists()
    # A file changed by hand is put right by the command, not by a normal run.
    path = product_file(settings, p["id"])
    path.write_text("scribble")
    assert run(settings)["products"] == 0 and path.read_text() == "scribble"
    assert main(["export"]) == 0
    assert header(path)["name"] == "Rye bread"

    # With the export switched off, a backup leaves the copy alone.
    rate(logged_in, p["id"])
    assert main(["backup"]) == 0
    assert "ratings" not in header(path)


def test_backup_writes_the_copy_when_the_export_is_on(logged_in, settings, monkeypatch):
    p = make_product(logged_in)
    monkeypatch.setenv("BUYBUTLER_EXPORT", "true")
    assert main(["backup"]) == 0
    assert product_file(settings, p["id"]).exists()


def test_switched_off_nothing_is_written_until_it_is_on_again(logged_in, settings):
    p = make_product(logged_in)
    rate(logged_in, p["id"])
    time.sleep(0.2)
    assert not settings.export_dir.exists()
    # The counts were kept: switching it on catches up.
    assert pending(settings) == ([p["id"]], [1])
    on = dataclasses.replace(settings, export=True, export_seconds=0)
    with TestClient(create_app(on)):
        deadline = time.monotonic() + 5
        while pending(settings) != ([], []) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert header(product_file(settings, p["id"]))["ratings"][0]["buy_again"] is True
    assert (settings.export_dir / "FORMAT.md").exists()


def test_the_running_app_writes_after_a_change(settings, fake_off):
    on = dataclasses.replace(settings, export=True, export_seconds=0)
    with TestClient(create_app(on, transport=httpx.MockTransport(fake_off.handler))) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
        c.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
        # Scanned: the picture arrives after the answer, in the background.
        r = c.post("/api/products/scan", json={"barcode": "4000000000001"}, headers=H)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = list(settings.export_dir.glob(f"*/products/{pid}-*/product.md"))
            if found and "photo" in header(found[0]) and pending(settings) == ([], []):
                break
            time.sleep(0.05)
        data = header(product_file(settings, pid))
        assert data["source"] == "openfoodfacts" and data["photo"].startswith("../../../../images/products/")
        # Reading changes nothing.
        before = product_file(settings, pid).stat().st_mtime_ns
        c.get("/api/products")
        time.sleep(0.3)
        assert product_file(settings, pid).stat().st_mtime_ns == before


def test_files_follow_the_umask(logged_in, settings, monkeypatch):
    p = make_product(logged_in)
    monkeypatch.setattr(files, "MODE", 0o664)
    run(settings)
    assert product_file(settings, p["id"]).stat().st_mode & 0o777 == 0o664
