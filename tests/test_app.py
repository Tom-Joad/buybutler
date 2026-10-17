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
    assert client.get("/api/session").json() == {"username": "tj", "role": "admin", "currency": "EUR"}
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


# ---- prices ----------------------------------------------------------------


def add_price(c, product_id: int, **kw) -> dict:
    body = {"amount_cents": 249} | kw
    r = c.post(f"/api/products/{product_id}/prices", json=body, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def test_price_is_optional_and_has_time_stamps(logged_in):
    p = make_product(logged_in)
    assert p["price"] is None
    pr = add_price(logged_in, p["id"], quantity=500, unit="g", store="Market A", bought_on="2026-10-01")
    assert pr["amount_cents"] == 249 and pr["unit_price"] == {"amount_cents": 498, "per": "kg"}
    assert pr["created_at"].endswith("Z") and pr["user"] == "tj" and pr["on_offer"] is False


def test_rating_can_carry_a_price(logged_in):
    p = make_product(logged_in)
    r = rate(logged_in, p["id"], store="Market B", rated_on="2026-10-02",
             price={"amount_cents": 199, "quantity": 1.5, "unit": "l"})
    detail = logged_in.get(f"/api/products/{p['id']}").json()
    assert len(detail["prices"]) == 1
    pr = detail["prices"][0]
    assert pr["rating_id"] == r["id"] and pr["store"] == "Market B" and pr["bought_on"] == "2026-10-02"
    assert pr["unit_price"] == {"amount_cents": 133, "per": "l"}
    # Without a price nothing is stored.
    rate(logged_in, p["id"])
    assert len(logged_in.get(f"/api/products/{p['id']}").json()["prices"]) == 1


def test_price_summary_skips_offers_and_converts_units(logged_in):
    p = make_product(logged_in)
    add_price(logged_in, p["id"], amount_cents=450, quantity=1, unit="kg", bought_on="2026-01-01")
    add_price(logged_in, p["id"], amount_cents=249, quantity=500, unit="g", bought_on="2026-05-01")
    add_price(logged_in, p["id"], amount_cents=99, quantity=500, unit="g", on_offer=True, bought_on="2026-06-01")
    add_price(logged_in, p["id"], amount_cents=279, quantity=500, unit="G", bought_on="2026-09-01")
    # Another unit basis doesn't count against per-kg prices.
    add_price(logged_in, p["id"], amount_cents=10, quantity=1, unit="piece", bought_on="2026-02-01")
    summary = logged_in.get(f"/api/products/{p['id']}").json()["price"]
    assert summary["last"]["amount_cents"] == 279
    assert summary["basis"] == "kg"
    assert summary["lowest"]["amount_cents"] == 450  # 4.50 per kg
    assert summary["highest"]["amount_cents"] == 279  # 5.58 per kg
    assert summary["count"] == 5


def test_unit_price_rounds_half_up(logged_in):
    p = make_product(logged_in)
    pr = add_price(logged_in, p["id"], amount_cents=349, quantity=400, unit="g")
    assert pr["unit_price"] == {"amount_cents": 873, "per": "kg"}


def test_price_summary_without_quantities(logged_in):
    p = make_product(logged_in)
    add_price(logged_in, p["id"], amount_cents=300, bought_on="2026-01-01")
    add_price(logged_in, p["id"], amount_cents=200, bought_on="2026-02-01")
    add_price(logged_in, p["id"], amount_cents=500, quantity=1, unit="kg", bought_on="2025-01-01")
    s = logged_in.get(f"/api/products/{p['id']}").json()["price"]
    assert s["basis"] is None and s["lowest"]["amount_cents"] == 200 and s["highest"]["amount_cents"] == 300


def test_price_validation(logged_in):
    p = make_product(logged_in)
    for body in ({"amount_cents": -1}, {"amount_cents": 100, "quantity": 0, "unit": "g"},
                 {"amount_cents": 100, "quantity": 5}, {"amount_cents": 10_000_001},
                 {"amount_cents": 100, "unit": "x" * 21}):
        r = logged_in.post(f"/api/products/{p['id']}/prices", json=body, headers=H)
        assert r.status_code == 422, body


def test_edit_and_delete_price(logged_in):
    p = make_product(logged_in)
    pr = add_price(logged_in, p["id"], quantity=500, unit="g")
    u = logged_in.patch(f"/api/prices/{pr['id']}", json={"amount_cents": 199, "on_offer": True}, headers=H).json()
    assert u["amount_cents"] == 199 and u["on_offer"] is True and u["created_at"] == pr["created_at"]
    u = logged_in.patch(f"/api/prices/{pr['id']}", json={"clear_quantity": True}, headers=H).json()
    assert u["quantity"] is None and u["unit"] == "" and u["unit_price"] is None
    bad = logged_in.patch(f"/api/prices/{pr['id']}", json={"quantity": 2}, headers=H)
    assert bad.status_code == 422
    assert logged_in.delete(f"/api/prices/{pr['id']}", headers=H).status_code == 204
    assert logged_in.get(f"/api/products/{p['id']}").json()["price"] is None


def test_list_shows_and_sorts_by_last_price(logged_in):
    a = make_product(logged_in, name="Dear")
    b = make_product(logged_in, name="Cheap")
    make_product(logged_in, name="No price")
    add_price(logged_in, a["id"], amount_cents=500)
    add_price(logged_in, b["id"], amount_cents=900, bought_on="2026-01-01")
    add_price(logged_in, b["id"], amount_cents=100, bought_on="2026-02-01")
    products = logged_in.get("/api/products", params={"sort": "price"}).json()["products"]
    assert [x["name"] for x in products] == ["Cheap", "Dear", "No price"]
    assert products[0]["price"]["last"]["amount_cents"] == 100 and products[2]["price"] is None


def test_prices_stay_in_their_household(logged_in, settings):
    p = make_product(logged_in)
    pr = add_price(logged_in, p["id"])
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
    assert other.patch(f"/api/prices/{pr['id']}", json={"amount_cents": 1}, headers=H).status_code == 404
    assert other.delete(f"/api/prices/{pr['id']}", headers=H).status_code == 404
    assert other.post(f"/api/products/{p['id']}/prices", json={"amount_cents": 1}, headers=H).status_code == 404


def test_migration_from_the_first_schema(settings):
    conn = sqlite3.connect(settings.database)
    conn.executescript(db.MIGRATIONS[0] + "PRAGMA user_version = 1;")
    conn.close()
    assert db.migrate(settings.database) == (1, len(db.MIGRATIONS))
    conn = sqlite3.connect(settings.database)
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    conn.close()


# ---- repeated requests (client_id) -----------------------------------------

CID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_repeated_product_create_returns_the_first(logged_in):
    body = {"name": "Oat milk", "barcode": "4000000000002", "client_id": CID}
    first = logged_in.post("/api/products", json=body, headers=H)
    again = logged_in.post("/api/products", json=body, headers=H)
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert len(logged_in.get("/api/products").json()["products"]) == 1


def test_repeated_rating_with_price_is_stored_once(logged_in):
    p = make_product(logged_in)
    body = {"buy_again": False, "stars": 1, "client_id": CID, "price": {"amount_cents": 199}}
    first = logged_in.post(f"/api/products/{p['id']}/ratings", json=body, headers=H)
    again = logged_in.post(f"/api/products/{p['id']}/ratings", json=body, headers=H)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    detail = logged_in.get(f"/api/products/{p['id']}").json()
    assert detail["rating_count"] == 1 and len(detail["prices"]) == 1


def test_repeated_price_is_stored_once(logged_in):
    p = make_product(logged_in)
    body = {"amount_cents": 249, "client_id": CID}
    a = logged_in.post(f"/api/products/{p['id']}/prices", json=body, headers=H)
    b = logged_in.post(f"/api/products/{p['id']}/prices", json=body, headers=H)
    assert (a.status_code, b.status_code) == (201, 200) and a.json()["id"] == b.json()["id"]


def test_client_id_for_another_product_is_refused(logged_in):
    p = make_product(logged_in, name="A")
    q = make_product(logged_in, name="B")
    logged_in.post(f"/api/products/{p['id']}/ratings", json={"buy_again": True, "client_id": CID}, headers=H)
    r = logged_in.post(f"/api/products/{q['id']}/ratings", json={"buy_again": True, "client_id": CID}, headers=H)
    assert r.status_code == 409


def test_client_id_format(logged_in):
    r = logged_in.post("/api/products", json={"name": "x", "client_id": "short"}, headers=H)
    assert r.status_code == 422
    r = logged_in.post("/api/products", json={"name": "x", "client_id": "a" * 65}, headers=H)
    assert r.status_code == 422


def test_concurrent_copy_returns_the_first(logged_in, settings, monkeypatch):
    # The first copy commits between the second copy's check and its insert.
    from buybutler import app as app_module

    p = make_product(logged_in)
    real = app_module._replayed
    calls = {"n": 0}

    def late(conn, user, table, client_id):
        calls["n"] += 1
        if table == "ratings" and calls["n"] == 1:
            with db.session(settings.database) as other:
                stamp = db.now()
                other.execute(
                    "INSERT INTO ratings (product_id, user_id, buy_again, rated_on, created_at, updated_at,"
                    " client_id) VALUES (?, 1, 1, '2026-10-17', ?, ?, ?)",
                    (p["id"], stamp, stamp, CID),
                )
            return None
        return real(conn, user, table, client_id)

    monkeypatch.setattr(app_module, "_replayed", late)
    r = logged_in.post(f"/api/products/{p['id']}/ratings", json={"buy_again": True, "client_id": CID}, headers=H)
    assert r.status_code == 200
    assert logged_in.get(f"/api/products/{p['id']}").json()["rating_count"] == 1


def test_households_cant_reuse_a_client_id(logged_in, settings):
    logged_in.post("/api/products", json={"name": "x", "client_id": CID}, headers=H)
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
    assert other.post("/api/products", json={"name": "y", "client_id": CID}, headers=H).status_code == 409
