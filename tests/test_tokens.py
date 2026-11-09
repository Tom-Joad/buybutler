"""Access keys for other programs: made with the password, shown once,
stored hashed, bound to one list's shopping list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from buybutler import db, shopping

from .test_app import H, PASSWORD, client, fake_off, logged_in, settings  # noqa: F401
from .test_lists import ADMIN, add_user, home, login, share  # noqa: F401


def make_token(c, list_id: int, password: str = PASSWORD, name: str = "Recipe book"):
    return c.post("/api/account/tokens", json={"name": name, "list_id": list_id, "password": password}, headers=H)


def program(app, token: str) -> TestClient:
    """A client without cookies and without the browser header."""
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_key_needs_the_password_and_is_shown_once(logged_in, settings, home):
    assert make_token(logged_in, home, password="wrong password").status_code == 400
    r = make_token(logged_in, home)
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("bbt_")
    listed = r.json()["tokens"]
    assert [(t["name"], t["list_name"]) for t in listed] == [("Recipe book", "Home")]
    assert "token" not in listed[0] and token not in logged_in.get("/api/account/tokens").text
    with db.session(settings.database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens WHERE token_hash = ?", (token,)).fetchone()[0] == 0


def test_program_adds_to_the_shopping_list(logged_in, home):
    token = make_token(logged_in, home).json()["token"]
    p = program(logged_in.app, token)
    r = p.post("/api/v1/shopping", json={"items": [{"text": "Mehl", "quantity": "400 g"}, {"text": "Eier", "quantity": "4"}]})
    assert r.status_code == 201 and r.json() == {"list": "Home", "added": 2, "existing": 0}
    seen = p.get("/api/v1/shopping").json()
    assert seen["list"] == "Home"
    assert [(i["name"], i["quantity"], i["checked"]) for i in seen["items"]] == [("Mehl", "400 g", False), ("Eier", "4", False)]
    shop = logged_in.get("/api/shopping").json()["items"]
    assert [i["added_by"] for i in shop] == ["tj", "tj"]
    assert logged_in.get("/api/account/tokens").json()["tokens"][0]["last_used_at"]


def test_program_api_refuses_bad_keys_and_bad_items(logged_in, home):
    assert TestClient(logged_in.app).get("/api/v1/shopping").status_code == 401
    assert program(logged_in.app, "bbt_nothing").get("/api/v1/shopping").status_code == 401
    # A browser session alone doesn't reach the program API.
    assert logged_in.get("/api/v1/shopping").status_code == 401
    p = program(logged_in.app, make_token(logged_in, home).json()["token"])
    assert p.post("/api/v1/shopping", json={"items": []}).status_code == 422
    assert p.post("/api/v1/shopping", json={"items": [{"text": "x" * 101}]}).status_code == 422
    assert p.post("/api/v1/shopping", json={"items": [{"text": "  "}]}).status_code == 422


def test_revoked_key_stops_working(logged_in, home):
    r = make_token(logged_in, home)
    p = program(logged_in.app, r.json()["token"])
    token_id = r.json()["tokens"][0]["id"]
    assert logged_in.delete(f"/api/account/tokens/{token_id}", headers=H).json() == {"tokens": []}
    assert p.get("/api/v1/shopping").status_code == 401
    assert logged_in.delete(f"/api/account/tokens/{token_id}", headers=H).status_code == 404


def test_key_follows_the_role_in_the_list(logged_in, settings, home):
    add_user(settings, "sam")
    share(logged_in, home, "sam", "editor")
    sam = login(logged_in.app, "sam")
    p = program(logged_in.app, make_token(sam, home).json()["token"])
    assert p.get("/api/v1/shopping").status_code == 200
    # As a viewer, sam may no longer change the list, and the key neither.
    members = logged_in.get(f"/api/lists/{home}/members").json()["members"]
    sam_id = next(m["user_id"] for m in members if m["username"] == "sam")
    logged_in.patch(f"/api/lists/{home}/members/{sam_id}", json={"role": "viewer"}, headers=H)
    assert p.get("/api/v1/shopping").status_code == 401
    # A viewer can't make a key for the list at all.
    assert make_token(sam, home).status_code == 404


def test_keys_of_other_users_stay_apart(logged_in, settings, home):
    r = make_token(logged_in, home)
    add_user(settings, "sam")
    sam = login(logged_in.app, "sam")
    assert sam.get("/api/account/tokens").json() == {"tokens": []}
    assert sam.delete(f"/api/account/tokens/{r.json()['tokens'][0]['id']}", headers=H).status_code == 404
    assert make_token(sam, home).status_code == 404


def test_password_change_removes_the_keys(logged_in, settings, home):
    p = program(logged_in.app, make_token(logged_in, home).json()["token"])
    r = logged_in.post("/api/session/password", json={"current": PASSWORD, "new": "another long password"}, headers=H)
    assert r.status_code == 204
    assert p.get("/api/v1/shopping").status_code == 401
    assert logged_in.get("/api/account/tokens").json() == {"tokens": []}


def test_admin_password_reset_removes_the_keys(logged_in, settings, home):
    add_user(settings, "sam")
    share(logged_in, home, "sam", "editor")
    sam = login(logged_in.app, "sam")
    p = program(logged_in.app, make_token(sam, home).json()["token"])
    mine = program(logged_in.app, make_token(logged_in, home).json()["token"])
    users = logged_in.get("/api/users").json()["users"]
    sam_id = next(u["id"] for u in users if u["username"] == "sam")
    r = logged_in.post(f"/api/users/{sam_id}/password", json={"password": "a brand new one", **ADMIN}, headers=H)
    assert r.status_code == 204
    assert p.get("/api/v1/shopping").status_code == 401
    # The admin's own key stays.
    assert mine.get("/api/v1/shopping").status_code == 200


def test_keys_do_not_survive_leaving_or_removal(logged_in, settings, home):
    add_user(settings, "sam")
    share(logged_in, home, "sam", "editor")
    sam = login(logged_in.app, "sam")
    other = logged_in.post("/api/lists", json={"name": "Other"}, headers=H).json()["id"]
    p = program(logged_in.app, make_token(sam, home).json()["token"])
    assert p.get("/api/v1/shopping").status_code == 200
    sam_id = next(u["id"] for u in logged_in.get("/api/users").json()["users"] if u["username"] == "sam")
    # An owner removes sam: the key goes with the membership.
    assert logged_in.delete(f"/api/lists/{home}/members/{sam_id}", headers=H).status_code == 204
    assert sam.get("/api/account/tokens").json() == {"tokens": []}
    share(logged_in, home, "sam", "editor")
    assert p.get("/api/v1/shopping").status_code == 401
    # Leaving on their own does the same; keys for other lists stay.
    share(logged_in, other, "sam", "editor")
    left = program(logged_in.app, make_token(sam, home).json()["token"])
    kept = program(logged_in.app, make_token(sam, other).json()["token"])
    assert sam.delete(f"/api/lists/{home}/members/{sam_id}", headers=H).status_code == 204
    share(logged_in, home, "sam", "editor")
    assert left.get("/api/v1/shopping").status_code == 401
    assert kept.get("/api/v1/shopping").status_code == 200
    assert [t["list_name"] for t in sam.get("/api/account/tokens").json()["tokens"]] == ["Other"]


def test_repeated_request_adds_nothing(logged_in, home):
    p = program(logged_in.app, make_token(logged_in, home).json()["token"])
    body = {"items": [{"text": "Mehl", "quantity": "400 g"}, {"text": "Eier", "quantity": "4",
                                                              "client_id": "recipe-17-eggs-0001"}]}
    assert p.post("/api/v1/shopping", json=body).json() == {"list": "Home", "added": 2, "existing": 0}
    assert p.post("/api/v1/shopping", json=body).json() == {"list": "Home", "added": 0, "existing": 2}
    assert len(p.get("/api/v1/shopping").json()["items"]) == 2


def test_same_text_goes_on_the_list_once(logged_in, home):
    p = program(logged_in.app, make_token(logged_in, home).json()["token"])
    p.post("/api/v1/shopping", json={"items": [{"text": "Milch"}]})
    # The same text in other case and spacing, now with a quantity: filled in.
    r = p.post("/api/v1/shopping", json={"items": [{"text": " milch ", "quantity": "1 l"}, {"text": "MILCH"}]})
    assert r.json() == {"list": "Home", "added": 0, "existing": 2}
    # Another quantity gets its own line, so it isn't lost.
    assert p.post("/api/v1/shopping", json={"items": [{"text": "Milch", "quantity": "500 ml"}]}).json()["added"] == 1
    items = p.get("/api/v1/shopping").json()["items"]
    assert [(i["name"], i["quantity"]) for i in items] == [("Milch", "1 l"), ("Milch", "500 ml")]
    # A ticked line doesn't count: it is bought already.
    first = logged_in.get("/api/shopping").json()["items"][0]["id"]
    logged_in.patch(f"/api/shopping/{first}", json={"checked": True}, headers=H)
    assert p.post("/api/v1/shopping", json={"items": [{"text": "Milch", "quantity": "1 l"}]}).json()["added"] == 1


def test_item_id_of_another_list_is_refused(logged_in, home):
    other = logged_in.post("/api/lists", json={"name": "Office"}, headers=H).json()["id"]
    p = program(logged_in.app, make_token(logged_in, home).json()["token"])
    q = program(logged_in.app, make_token(logged_in, other).json()["token"])
    item = {"text": "Kaffee", "client_id": "recipe-17-coffee-001"}
    assert p.post("/api/v1/shopping", json={"items": [item]}).status_code == 201
    assert q.post("/api/v1/shopping", json={"items": [item]}).status_code == 409


def test_full_list_takes_nothing_more(logged_in, home, monkeypatch):
    monkeypatch.setattr(shopping, "ITEMS_MAX", 3)
    p = program(logged_in.app, make_token(logged_in, home).json()["token"])
    p.post("/api/v1/shopping", json={"items": [{"text": "a"}, {"text": "b"}]})
    # All or none: c would fit, d not, so neither is added.
    r = p.post("/api/v1/shopping", json={"items": [{"text": "c"}, {"text": "d"}]})
    assert r.status_code == 409 and "full" in r.json()["detail"]
    assert len(p.get("/api/v1/shopping").json()["items"]) == 2
    # What is on the list already can still be sent.
    assert p.post("/api/v1/shopping", json={"items": [{"text": "a"}, {"text": "c"}]}).json()["added"] == 1


def test_unknown_keys_are_throttled(logged_in, home):
    good = program(logged_in.app, make_token(logged_in, home).json()["token"])
    for _ in range(5):
        assert good.get("/api/v1/shopping").status_code == 200
    bad = program(logged_in.app, "bbt_nothing")
    for _ in range(20):
        assert bad.get("/api/v1/shopping").status_code == 401
    r = bad.get("/api/v1/shopping")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # From the same address, a good key waits too.
    assert good.get("/api/v1/shopping").status_code == 429


def test_cookie_requests_keep_the_browser_header_check(logged_in, home):
    token = make_token(logged_in, home).json()["token"]
    items = {"items": [{"text": "Mehl"}]}
    # With the session cookie, a key doesn't excuse the missing header,
    # on the program API or anywhere else.
    bearer = {"Authorization": f"Bearer {token}"}
    assert logged_in.post("/api/v1/shopping", json=items, headers=bearer).status_code == 403
    assert logged_in.post("/api/lists", json={"name": "Office"}, headers=bearer).status_code == 403
    # Without the cookie, a key passes the check, but a cookie route still
    # wants a session.
    p = program(logged_in.app, token)
    assert p.post("/api/v1/shopping", json=items).status_code == 201
    assert p.post("/api/lists", json={"name": "Office"}).status_code == 401


def test_key_name_is_measured_after_collapsing_spaces(logged_in, home):
    r = make_token(logged_in, home, name="Recipe" + " " * 60 + "book")
    assert r.status_code == 201 and r.json()["tokens"][0]["name"] == "Recipe book"
    r = make_token(logged_in, home, name="x" * 61)
    assert r.status_code == 400 and r.json()["detail"] == "the name must have 1 to 60 characters"
