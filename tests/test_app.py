"""API tests against a temporary database; Open Food Facts is mocked."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from buybutler import auth, config, db
from buybutler.app import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
H = {"X-Buybutler": "1"}
PASSWORD = "correct horse battery"

OFF_PRODUCT = {
    "status": 1,
    "product": {
        "product_name": "Natural Yoghurt",
        "product_name_de": "Naturjoghurt",
        "brands": "Example Dairy, Other",
        "categories": "Dairies, Fermented foods, Yoghurts",
        "quantity": "500 g",
        "image_front_url": "https://images.openfoodfacts.org/images/products/400/front.400.jpg",
    },
}


class FakeOff:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.agents: list[str] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        self.agents.append(request.headers.get("user-agent", ""))
        if self.down:
            raise httpx.ConnectError("down")
        if request.url.host == "images.openfoodfacts.org":
            return httpx.Response(200, content=JPEG)
        if "/4000000000001" in request.url.path:
            return httpx.Response(200, json=OFF_PRODUCT)
        return httpx.Response(404, json={"status": 0})


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BUYBUTLER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUYBUTLER_OFF_LANGUAGE", "de")
    return config.load()


@pytest.fixture
def fake_off():
    return FakeOff()


@pytest.fixture
def client(settings, fake_off):
    app = create_app(settings, transport=httpx.MockTransport(fake_off.handler))
    with TestClient(app) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
        yield c


@pytest.fixture
def logged_in(client):
    r = client.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
    assert r.status_code == 200
    return client


def make_product(c, **kw) -> dict:
    body = {"name": "Rye bread"} | kw
    r = c.post("/api/products", json=body, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def rate(c, product_id: int, **kw) -> dict:
    body = {"buy_again": True} | kw
    r = c.post(f"/api/products/{product_id}/ratings", json=body, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


# ---- login ---------------------------------------------------------------


def test_health_needs_no_login(client):
    assert client.get("/health").json()["status"] == "ok"


def test_api_needs_login(client):
    assert client.get("/api/products").status_code == 401
    assert client.get("/api/session").status_code == 401


def test_login_and_logout(client):
    r = client.post("/api/session", json={"username": "TJ", "password": PASSWORD}, headers=H)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/session").json() == {"username": "tj", "role": "admin"}
    assert client.delete("/api/session", headers=H).status_code == 204
    assert client.get("/api/session").status_code == 401


def test_session_token_is_stored_hashed(client, settings):
    client.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
    token = client.cookies.get("buybutler_session")
    with db.session(settings.database) as conn:
        stored = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert token and token not in stored and len(stored) == 1


def test_wrong_password_and_throttle(client):
    for _ in range(5):
        r = client.post("/api/session", json={"username": "tj", "password": "wrong password"}, headers=H)
        assert r.status_code == 401
    r = client.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_lockout_is_per_account(client, settings):
    with db.session(settings.database) as conn:
        auth.create_user(conn, "sam", PASSWORD)
    for _ in range(4):
        client.post("/api/session", json={"username": "tj", "password": "wrong password"}, headers=H)
    # A correct login with another account must not reset the count for tj.
    other = TestClient(client.app)
    assert other.post("/api/session", json={"username": "sam", "password": PASSWORD}, headers=H).status_code == 200
    client.post("/api/session", json={"username": "tj", "password": "wrong password"}, headers=H)
    r = client.post("/api/session", json={"username": "TJ", "password": PASSWORD}, headers=H)
    assert r.status_code == 429
    # sam is not affected by tj's lockout.
    assert other.post("/api/session", json={"username": "sam", "password": PASSWORD}, headers=H).status_code == 200


def test_password_change_is_throttled(logged_in):
    for _ in range(5):
        r = logged_in.post("/api/session/password", json={"current": "wrong password", "new": "x" * 12}, headers=H)
        assert r.status_code == 400
    r = logged_in.post("/api/session/password", json={"current": PASSWORD, "new": "x" * 12}, headers=H)
    assert r.status_code == 429


def test_used_session_renews_the_cookie(logged_in, settings):
    assert "set-cookie" not in logged_in.get("/api/session").headers
    with db.session(settings.database) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = '2000-01-01T00:00:00.000Z'")
    r = logged_in.get("/api/session")
    assert r.status_code == 200
    assert "buybutler_session=" in r.headers["set-cookie"] and "Max-Age=" in r.headers["set-cookie"]


def test_unknown_user_is_like_wrong_password(client):
    r = client.post("/api/session", json={"username": "nobody", "password": PASSWORD}, headers=H)
    assert r.status_code == 401
    assert r.json()["detail"] == "wrong user name or password"


def test_changes_need_the_csrf_header(logged_in):
    assert logged_in.post("/api/products", json={"name": "x"}).status_code == 403
    assert logged_in.post("/api/session", json={"username": "tj", "password": PASSWORD}).status_code == 403


def test_change_password_keeps_this_session_only(logged_in, settings):
    other = TestClient(logged_in.app)
    other.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
    assert other.get("/api/session").status_code == 200
    r = logged_in.post(
        "/api/session/password", json={"current": PASSWORD, "new": "another long password"}, headers=H
    )
    assert r.status_code == 204
    assert logged_in.get("/api/session").status_code == 200
    assert other.get("/api/session").status_code == 401


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


# ---- products and ratings --------------------------------------------------


def test_every_row_has_time_stamps(logged_in):
    p = make_product(logged_in)
    r = rate(logged_in, p["id"], stars=4, reason="Good crust", store="Bakery")
    for row in (p, r):
        assert row["created_at"].endswith("Z") and row["updated_at"].endswith("Z")
    assert len(r["rated_on"]) == 10


def test_rating_holds_verdict_and_stars(logged_in):
    p = make_product(logged_in)
    r = rate(logged_in, p["id"], buy_again=False, stars=2, reason="Too sweet")
    assert r["buy_again"] is False and r["stars"] == 2 and r["user"] == "tj"
    bad = logged_in.post(f"/api/products/{p['id']}/ratings", json={"buy_again": True, "stars": 6}, headers=H)
    assert bad.status_code == 422


def test_newest_rating_counts_and_history_stays(logged_in):
    p = make_product(logged_in)
    rate(logged_in, p["id"], buy_again=True, stars=5, rated_on="2026-01-10")
    rate(logged_in, p["id"], buy_again=False, stars=1, reason="Recipe changed", rated_on="2026-09-01")
    # Entered later, but for an earlier day: doesn't replace the verdict.
    rate(logged_in, p["id"], buy_again=True, stars=4, rated_on="2026-05-01")
    detail = logged_in.get(f"/api/products/{p['id']}").json()
    assert detail["rating"]["buy_again"] is False
    assert detail["rating"]["reason"] == "Recipe changed"
    assert detail["rating_count"] == 3
    assert [r["rated_on"] for r in detail["ratings"]] == ["2026-09-01", "2026-05-01", "2026-01-10"]


def test_lists_and_filters(logged_in):
    a = make_product(logged_in, name="Oat milk", category="Drinks")
    b = make_product(logged_in, name="Gouda", category="Cheese")
    make_product(logged_in, name="Unrated thing")
    rate(logged_in, a["id"], buy_again=True, store="Market A", stars=5)
    rate(logged_in, b["id"], buy_again=False, store="Market B", reason="Rubbery", stars=1)

    def names(**params):
        r = logged_in.get("/api/products", params=params)
        assert r.status_code == 200
        return sorted(p["name"] for p in r.json()["products"])

    assert names(verdict="buy") == ["Oat milk"]
    assert names(verdict="skip") == ["Gouda"]
    assert names(verdict="unrated") == ["Unrated thing"]
    assert names(q="rubber") == ["Gouda"]
    assert names(q="%") == []
    assert names(store="market a") == ["Oat milk"]
    assert names(category="cheese") == ["Gouda"]
    stars = logged_in.get("/api/products", params={"sort": "stars"}).json()["products"]
    assert [p["name"] for p in stars] == ["Oat milk", "Gouda", "Unrated thing"]
    facets = logged_in.get("/api/facets").json()
    assert facets == {"stores": ["Market A", "Market B"], "categories": ["Cheese", "Drinks"]}


def test_edit_rating_sets_updated_at(logged_in):
    p = make_product(logged_in)
    r = rate(logged_in, p["id"], stars=3)
    u = logged_in.patch(f"/api/ratings/{r['id']}", json={"reason": "Fixed typo", "clear_stars": True}, headers=H)
    assert u.status_code == 200
    body = u.json()
    assert body["reason"] == "Fixed typo" and body["stars"] is None
    assert body["created_at"] == r["created_at"] and body["updated_at"] >= r["updated_at"]


def test_barcode_unique_and_valid(logged_in):
    make_product(logged_in, barcode="4000000000002")
    r = logged_in.post("/api/products", json={"name": "x", "barcode": "4000000000002"}, headers=H)
    assert r.status_code == 409
    r = logged_in.post("/api/products", json={"name": "x", "barcode": "12ab"}, headers=H)
    assert r.status_code == 422
    # Products without a barcode don't collide.
    make_product(logged_in, name="Bakery roll")
    make_product(logged_in, name="Bakery bun")


def test_patch_product(logged_in):
    p = make_product(logged_in, barcode="4000000000002")
    r = logged_in.patch(f"/api/products/{p['id']}", json={"name": "Dark rye", "barcode": ""}, headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "Dark rye" and r.json()["barcode"] is None
    assert logged_in.patch(f"/api/products/{p['id']}", json={"name": " "}, headers=H).status_code == 422


def test_delete_product_removes_ratings_and_pictures(logged_in, settings):
    p = make_product(logged_in)
    r = rate(logged_in, p["id"])
    logged_in.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H)
    logged_in.put(f"/api/products/{p['id']}/image", content=PNG, headers=H)
    assert len(list((settings.images).rglob("*.*"))) == 2
    assert logged_in.delete(f"/api/products/{p['id']}", headers=H).status_code == 204
    assert logged_in.get(f"/api/products/{p['id']}").status_code == 404
    assert list(settings.images.rglob("*.*")) == []


# ---- pictures --------------------------------------------------------------


def test_photo_upload_and_download(logged_in):
    p = make_product(logged_in)
    r = rate(logged_in, p["id"])
    up = logged_in.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H | {"Content-Type": "image/jpeg"})
    assert up.status_code == 200
    url = up.json()["photo"]
    got = logged_in.get(url)
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg"


def test_photo_rejects_other_files(logged_in):
    p = make_product(logged_in)
    r = logged_in.put(f"/api/products/{p['id']}/image", content=b"<svg></svg>", headers=H)
    assert r.status_code == 415


def test_photo_size_limit(logged_in, settings):
    p = make_product(logged_in)
    big = JPEG + b"\x00" * (settings.max_upload_mb * 1024 * 1024)
    assert logged_in.put(f"/api/products/{p['id']}/image", content=big, headers=H).status_code == 413


def test_upload_needs_login(client):
    assert client.put("/api/products/1/image", content=JPEG, headers=H).status_code == 401


def test_picture_names_are_checked(logged_in):
    assert logged_in.get("/images/products/..%2Fbuybutler.sqlite3").status_code == 404
    assert logged_in.get("/images/other/abc.jpg").status_code == 404


# ---- households ------------------------------------------------------------


def test_households_are_separate(logged_in, settings):
    p = make_product(logged_in, barcode="4000000000002")
    r = rate(logged_in, p["id"])
    up = logged_in.put(f"/api/ratings/{r['id']}/photo", content=JPEG, headers=H).json()
    # A second household as the user management will create it.
    with db.session(settings.database) as conn:
        stamp = db.now()
        hid = conn.execute(
            "INSERT INTO households (name, created_at, updated_at) VALUES ('Other', ?, ?)", (stamp, stamp)
        ).lastrowid
        conn.execute(
            "INSERT INTO users (household_id, username, password_hash, role, created_at, updated_at)"
            " VALUES (?, 'other', ?, 'admin', ?, ?)",
            (hid, auth.hash_password(PASSWORD), stamp, stamp),
        )
    other = TestClient(logged_in.app)
    other.post("/api/session", json={"username": "other", "password": PASSWORD}, headers=H)
    assert other.get("/api/products").json()["products"] == []
    assert other.get(f"/api/products/{p['id']}").status_code == 404
    assert other.patch(f"/api/ratings/{r['id']}", json={"reason": "x"}, headers=H).status_code == 404
    assert other.get(up["photo"]).status_code == 404
    # The same barcode can exist once per household.
    assert other.post("/api/products", json={"name": "x", "barcode": "4000000000002"}, headers=H).status_code == 201
    assert other.get("/api/lookup", params={"barcode": "4000000000002"}).json()["product"]["name"] == "x"


# ---- Open Food Facts -------------------------------------------------------


def test_lookup_known_product_skips_open_food_facts(logged_in, fake_off):
    make_product(logged_in, barcode="4000000000002", name="Mine")
    r = logged_in.get("/api/lookup", params={"barcode": "4000000000002"}).json()
    assert r["lookup"] == "known" and r["product"]["name"] == "Mine"
    assert fake_off.calls == []


def test_lookup_suggestion_is_cached(logged_in, fake_off):
    r = logged_in.get("/api/lookup", params={"barcode": "4000000000001"}).json()
    assert r["lookup"] == "found"
    s = r["suggestion"]
    assert s["name"] == "Naturjoghurt" and s["brand"] == "Example Dairy"
    assert s["category"] == "Yoghurts" and s["quantity"] == "500 g" and s["has_image"] is True
    assert "image_url" not in s
    logged_in.get("/api/lookup", params={"barcode": "4000000000001"})
    assert len(fake_off.calls) == 1
    assert fake_off.agents[0].startswith("Buybutler/")


def test_lookup_not_found_and_failure(logged_in, fake_off):
    assert logged_in.get("/api/lookup", params={"barcode": "4000000000099"}).json()["lookup"] == "not_found"
    fake_off.down = True
    assert logged_in.get("/api/lookup", params={"barcode": "4000000000098"}).json()["lookup"] == "failed"
    assert logged_in.get("/api/lookup", params={"barcode": "abc"}).status_code == 422


def test_lookup_disabled(settings, monkeypatch, fake_off):
    monkeypatch.setenv("BUYBUTLER_OFF_LOOKUP", "false")
    app = create_app(config.load(), transport=httpx.MockTransport(fake_off.handler))
    with TestClient(app) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
        c.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
        assert c.get("/api/lookup", params={"barcode": "4000000000001"}).json()["lookup"] == "disabled"
    assert fake_off.calls == []


def test_lookup_switch_also_covers_cached_pictures(settings, monkeypatch, fake_off):
    app = create_app(settings, transport=httpx.MockTransport(fake_off.handler))
    with TestClient(app) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
        c.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
        c.get("/api/lookup", params={"barcode": "4000000000001"})
    calls = len(fake_off.calls)
    monkeypatch.setenv("BUYBUTLER_OFF_LOOKUP", "false")
    app = create_app(config.load(), transport=httpx.MockTransport(fake_off.handler))
    with TestClient(app) as c:
        c.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
        p = make_product(c, barcode="4000000000001", name="x", use_suggested_image=True)
    assert p["image"] is None and p["source"] == "manual"
    assert len(fake_off.calls) == calls


def test_create_from_suggestion_downloads_image(logged_in, fake_off):
    logged_in.get("/api/lookup", params={"barcode": "4000000000001"})
    p = make_product(logged_in, barcode="4000000000001", name="Naturjoghurt", use_suggested_image=True)
    # Downloaded in the background after the response.
    p = logged_in.get(f"/api/products/{p['id']}").json()
    assert p["source"] == "openfoodfacts" and p["image"].startswith("/images/products/")
    assert fake_off.calls[-1].startswith("https://images.openfoodfacts.org/")
    assert logged_in.get(p["image"]).content == JPEG


def test_suggested_image_only_from_open_food_facts(settings, fake_off):
    with db.session(settings.database) as conn:
        db.migrate(settings.database)
        conn.execute(
            "INSERT INTO off_cache (barcode, found, data, fetched_at) VALUES (?, 1, ?, ?)",
            (
                "4000000000003",
                json.dumps({"barcode": "4000000000003", "name": "x", "brand": "", "category": "",
                            "quantity": "", "image_url": "https://example.org/evil.jpg"}),
                db.now(),
            ),
        )
    app = create_app(settings, transport=httpx.MockTransport(fake_off.handler))
    with TestClient(app) as c:
        with db.session(settings.database) as conn:
            auth.create_user(conn, "tj", PASSWORD)
        c.post("/api/session", json={"username": "tj", "password": PASSWORD}, headers=H)
        p = make_product(c, barcode="4000000000003", name="x", use_suggested_image=True)
    assert p["image"] is None
    assert fake_off.calls == []


# ---- database --------------------------------------------------------------


def test_migrate_is_idempotent(settings):
    assert db.migrate(settings.database) == (0, len(db.MIGRATIONS))
    assert db.migrate(settings.database) == (len(db.MIGRATIONS), len(db.MIGRATIONS))


def test_refuses_newer_schema(settings):
    db.migrate(settings.database)
    conn = sqlite3.connect(settings.database)
    conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError):
        db.migrate(settings.database)
