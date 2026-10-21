"""Lists shared between users: roles, membership, admin user management,
and the migration from the single household."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from buybutler import auth, db

from .test_app import H, JPEG, PASSWORD, make_product, rate
from .test_app import client, fake_off, logged_in, settings  # noqa: F401  (fixtures)


def login(app, name: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/session", json={"username": name, "password": PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    return c


def add_user(settings, name: str, role: str | None = None) -> None:
    with db.session(settings.database) as conn:
        auth.create_user(conn, name, PASSWORD, role)


def use(c: TestClient, list_id: int) -> TestClient:
    c.headers["X-Buybutler-List"] = str(list_id)
    return c


@pytest.fixture
def home(logged_in):
    """The admin's first list, created with the first user."""
    lists = logged_in.get("/api/lists").json()["lists"]
    assert [(x["name"], x["role"]) for x in lists] == [("Home", "owner")]
    return lists[0]["id"]


def share(owner: TestClient, list_id: int, name: str, role: str = "editor"):
    return owner.post(f"/api/lists/{list_id}/members", json={"username": name, "role": role}, headers=H)


# ---- lists ---------------------------------------------------------------


def test_new_user_starts_without_a_list(logged_in, settings, home):
    add_user(settings, "sam")
    sam = login(logged_in.app, "sam")
    assert sam.get("/api/lists").json()["lists"] == []
    r = sam.get("/api/products")
    assert r.status_code == 409 and "create one" in r.json()["detail"]
    created = sam.post("/api/lists", json={"name": "Sam's"}, headers=H).json()
    assert created["role"] == "owner" and created["created_at"].endswith("Z")
    assert sam.get("/api/products").json()["products"] == []


def test_lists_keep_products_apart(logged_in, home):
    work = logged_in.post("/api/lists", json={"name": "Office"}, headers=H).json()["id"]
    make_product(use(logged_in, home), name="Milk", barcode="4000000000001")
    make_product(use(logged_in, work), name="Office milk", barcode="4000000000001")
    assert [p["name"] for p in use(logged_in, home).get("/api/products").json()["products"]] == ["Milk"]
    assert [p["name"] for p in use(logged_in, work).get("/api/products").json()["products"]] == ["Office milk"]
    found = use(logged_in, work).get("/api/lookup", params={"barcode": "4000000000001"}).json()
    assert found["product"]["name"] == "Office milk" and found["product"]["role"] == "owner"


def test_unknown_list_header(logged_in, home):
    assert use(logged_in, 999).get("/api/products").status_code == 404
    logged_in.headers["X-Buybutler-List"] = "abc"
    assert logged_in.get("/api/products").status_code == 422


def test_rename_and_delete_with_confirmation(logged_in, settings, home):
    p = make_product(use(logged_in, home))
    r = rate(logged_in, p["id"])
    logged_in.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H)
    assert logged_in.patch(f"/api/lists/{home}", json={"name": "Kitchen"}, headers=H).json()["name"] == "Kitchen"
    wrong = logged_in.request("DELETE", f"/api/lists/{home}", json={"confirm_name": "Home"}, headers=H)
    assert wrong.status_code == 400
    ok = logged_in.request("DELETE", f"/api/lists/{home}", json={"confirm_name": "Kitchen"}, headers=H)
    assert ok.status_code == 204
    assert logged_in.get("/api/lists").json()["lists"] == []
    assert list(settings.images.rglob("*.*")) == []
    with db.session(settings.database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 0


# ---- members and roles ---------------------------------------------------


def test_share_by_name(logged_in, settings, home):
    add_user(settings, "sam")
    assert share(logged_in, home, "nobody").json()["detail"] == "there is no user with this name"
    r = share(logged_in, home, "SAM", "viewer")
    assert r.status_code == 201
    assert [(m["username"], m["role"]) for m in r.json()["members"]] == [("tj", "owner"), ("sam", "viewer")]
    assert share(logged_in, home, "sam").status_code == 409
    sam = login(logged_in.app, "sam")
    assert [x["name"] for x in sam.get("/api/lists").json()["lists"]] == ["Home"]


def test_viewer_reads_but_cant_write(logged_in, settings, home):
    p = make_product(use(logged_in, home))
    r = rate(logged_in, p["id"])
    price = logged_in.post(f"/api/products/{p['id']}/prices", json={"amount_cents": 1}, headers=H).json()
    add_user(settings, "sam")
    share(logged_in, home, "sam", "viewer")
    sam = use(login(logged_in.app, "sam"), home)

    assert sam.get(f"/api/products/{p['id']}").json()["role"] == "viewer"
    assert len(sam.get("/api/products").json()["products"]) == 1
    refused = [
        sam.post("/api/products", json={"name": "x"}, headers=H),
        sam.post("/api/products/scan", json={"barcode": "4000000000002"}, headers=H),
        sam.patch(f"/api/products/{p['id']}", json={"name": "x"}, headers=H),
        sam.delete(f"/api/products/{p['id']}", headers=H),
        sam.put(f"/api/products/{p['id']}/image", content=JPEG, headers=H),
        sam.post(f"/api/products/{p['id']}/ratings", json={"buy_again": True}, headers=H),
        sam.patch(f"/api/ratings/{r['id']}", json={"reason": "x"}, headers=H),
        sam.delete(f"/api/ratings/{r['id']}", headers=H),
        sam.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H),
        sam.post(f"/api/products/{p['id']}/prices", json={"amount_cents": 1}, headers=H),
        sam.patch(f"/api/prices/{price['id']}", json={"amount_cents": 2}, headers=H),
        sam.delete(f"/api/prices/{price['id']}", headers=H),
        share(sam, home, "tj"),
    ]
    assert [x.status_code for x in refused] == [403] * len(refused)


def test_editor_writes_and_is_named(logged_in, settings, home):
    add_user(settings, "sam")
    share(logged_in, home, "sam", "editor")
    sam = use(login(logged_in.app, "sam"), home)
    p = make_product(sam, name="Sam's find")
    rate(sam, p["id"], reason="Good")
    detail = logged_in.get(f"/api/products/{p['id']}").json()
    assert detail["ratings"][0]["user"] == "sam"
    assert share(sam, home, "tj", "viewer").status_code == 403


def test_outsiders_see_nothing(logged_in, settings, home):
    p = make_product(use(logged_in, home))
    r = rate(logged_in, p["id"])
    photo = logged_in.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H).json()["photo"]
    add_user(settings, "eve")
    eve = login(logged_in.app, "eve")
    assert eve.get(f"/api/products/{p['id']}").status_code == 404
    assert eve.get(photo).status_code == 404
    assert eve.get(f"/api/lists/{home}/members").status_code == 404
    assert use(eve, home).get("/api/products").status_code == 404


def test_owner_rules(logged_in, settings, home):
    add_user(settings, "sam")
    share(logged_in, home, "sam", "editor")
    me = next(m for m in logged_in.get(f"/api/lists/{home}/members").json()["members"] if m["you"])
    sam_id = next(m["user_id"] for m in logged_in.get(f"/api/lists/{home}/members").json()["members"] if not m["you"])
    # The only owner can neither step down nor leave.
    assert logged_in.patch(f"/api/lists/{home}/members/{me['user_id']}", json={"role": "editor"},
                           headers=H).status_code == 409
    assert logged_in.delete(f"/api/lists/{home}/members/{me['user_id']}", headers=H).status_code == 409
    # With a second owner they can.
    logged_in.patch(f"/api/lists/{home}/members/{sam_id}", json={"role": "owner"}, headers=H)
    assert logged_in.delete(f"/api/lists/{home}/members/{me['user_id']}", headers=H).status_code == 204
    assert logged_in.get("/api/lists").json()["lists"] == []


def test_member_can_leave(logged_in, settings, home):
    add_user(settings, "sam")
    share(logged_in, home, "sam", "viewer")
    sam = login(logged_in.app, "sam")
    sam_id = next(m["user_id"] for m in sam.get(f"/api/lists/{home}/members").json()["members"] if m["you"])
    assert sam.delete(f"/api/lists/{home}/members/{sam_id}", headers=H).status_code == 204
    assert sam.get("/api/lists").json()["lists"] == []


# ---- users (admins) ------------------------------------------------------

# Changes to users carry the admin's own password.
ADMIN = {"admin_password": PASSWORD}


def test_only_admins_manage_users(logged_in, settings, home):
    add_user(settings, "sam")
    sam = login(logged_in.app, "sam")
    assert sam.get("/api/users").status_code == 403
    assert sam.post("/api/users", json={"username": "x", "password": PASSWORD, **ADMIN}, headers=H).status_code == 403


def test_admin_creates_resets_and_deletes(logged_in, settings, home):
    r = logged_in.post("/api/users", json={"username": "kim", "password": PASSWORD, **ADMIN}, headers=H)
    assert r.status_code == 201
    kim = next(u for u in r.json()["users"] if u["username"] == "kim")
    assert kim["role"] == "member" and kim["lists"] == 0 and kim["created_at"].endswith("Z")
    assert logged_in.post("/api/users", json={"username": "kim", "password": PASSWORD, **ADMIN}, headers=H).status_code == 400
    assert logged_in.post("/api/users", json={"username": "x", "password": "short", **ADMIN}, headers=H).status_code == 400

    session = login(logged_in.app, "kim")
    assert logged_in.post(f"/api/users/{kim['id']}/password", json={"password": "a brand new one", **ADMIN},
                          headers=H).status_code == 204
    assert session.get("/api/session").status_code == 401

    # Kim's own list goes with Kim.
    kim_client = TestClient(logged_in.app)
    kim_client.post("/api/session", json={"username": "kim", "password": "a brand new one"}, headers=H)
    own = kim_client.post("/api/lists", json={"name": "Kim's"}, headers=H).json()["id"]
    make_product(use(kim_client, own), name="Kim's thing")
    assert logged_in.request("DELETE", f"/api/users/{kim['id']}", json=ADMIN, headers=H).status_code == 204
    with db.session(settings.database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lists WHERE id = ?", (own,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_shared_list_needs_another_owner_before_delete(logged_in, settings, home):
    add_user(settings, "kim")
    kim = login(logged_in.app, "kim")
    shared = kim.post("/api/lists", json={"name": "Shared"}, headers=H).json()["id"]
    share(kim, shared, "tj", "editor")
    kim_id = next(u["id"] for u in logged_in.get("/api/users").json()["users"] if u["username"] == "kim")
    r = logged_in.request("DELETE", f"/api/users/{kim_id}", json=ADMIN, headers=H)
    assert r.status_code == 409 and "only owner" in r.json()["detail"]


def test_last_admin_is_protected(logged_in, settings, home):
    me = next(u for u in logged_in.get("/api/users").json()["users"] if u["username"] == "tj")
    assert logged_in.patch(f"/api/users/{me['id']}", json={"role": "member", **ADMIN}, headers=H).status_code == 409
    assert logged_in.request("DELETE", f"/api/users/{me['id']}", json=ADMIN, headers=H).status_code == 409
    add_user(settings, "sam", "admin")
    sam_id = next(u["id"] for u in logged_in.get("/api/users").json()["users"] if u["username"] == "sam")
    assert logged_in.patch(f"/api/users/{me['id']}", json={"role": "member", **ADMIN}, headers=H).status_code == 200
    assert logged_in.get("/api/users").status_code == 403
    assert sam_id


def test_user_changes_need_the_admins_password(logged_in, settings, home):
    add_user(settings, "kim")
    kim_id = next(u["id"] for u in logged_in.get("/api/users").json()["users"] if u["username"] == "kim")
    wrong = {"admin_password": "not my password"}
    calls = [
        ("POST", "/api/users", {"username": "eve", "password": PASSWORD, "role": "admin"}),
        ("PATCH", f"/api/users/{kim_id}", {"role": "admin"}),
        ("POST", f"/api/users/{kim_id}/password", {"password": "taken over now"}),
        ("POST", f"/api/users/{kim_id}/reset-2fa", {}),
        ("DELETE", f"/api/users/{kim_id}", {}),
    ]
    for method, path, body in calls:
        # A session alone isn't enough: without the password the change is refused.
        assert logged_in.request(method, path, json=body, headers=H).status_code == 422, path
        assert logged_in.request(method, path, json={**body, **wrong}, headers=H).status_code in (400, 429), path
    users = {u["username"]: u for u in logged_in.get("/api/users").json()["users"]}
    assert "eve" not in users and users["kim"]["role"] == "member"
    assert "username" in login(logged_in.app, "kim").get("/api/session").json()


# ---- migration -----------------------------------------------------------


def test_migration_turns_the_household_into_a_list(settings):
    conn = sqlite3.connect(settings.database)
    for step in db.MIGRATIONS[:3]:
        conn.executescript(step)
    conn.executescript("PRAGMA user_version = 3;")
    stamp = "2026-10-01T10:00:00.000Z"
    conn.execute("INSERT INTO households (id, name, created_at, updated_at) VALUES (1, 'Home', ?, ?)", (stamp, stamp))
    conn.execute(
        "INSERT INTO users (id, household_id, username, password_hash, role, created_at, updated_at)"
        " VALUES (1, 1, 'tj', ?, 'admin', ?, ?), (2, 1, 'sam', ?, 'member', ?, ?)",
        (auth.hash_password(PASSWORD), stamp, stamp, auth.hash_password(PASSWORD), stamp, stamp),
    )
    conn.execute("INSERT INTO sessions VALUES ('x', 2, ?, ?, '2099-01-01T00:00:00.000Z')", (stamp, stamp))
    conn.execute(
        "INSERT INTO products (id, household_id, barcode, name, created_by, created_at, updated_at)"
        " VALUES (1, 1, '4000000000001', 'Milk', 2, ?, ?)",
        (stamp, stamp),
    )
    conn.execute(
        "INSERT INTO ratings (product_id, user_id, buy_again, rated_on, created_at, updated_at)"
        " VALUES (1, 2, 1, '2026-10-01', ?, ?)",
        (stamp, stamp),
    )
    conn.commit()
    conn.close()

    assert db.migrate(settings.database) == (3, len(db.MIGRATIONS))

    with db.session(settings.database) as c:
        assert [tuple(r) for r in c.execute("SELECT id, name, created_by FROM lists")] == [(1, "Home", 1)]
        assert [tuple(r) for r in c.execute("SELECT list_id, user_id, role FROM list_members ORDER BY user_id")] == [
            (1, 1, "owner"), (1, 2, "editor")]
        assert "household_id" not in [r[1] for r in c.execute("PRAGMA table_info(users)")]
        assert c.execute("SELECT list_id FROM products").fetchone()[0] == 1
        # References into the rebuilt users table still hold.
        assert c.execute("SELECT COUNT(*) FROM sessions WHERE user_id = 2").fetchone()[0] == 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        c.execute("DELETE FROM users WHERE id = 2")
        assert c.execute("SELECT user_id FROM ratings").fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
