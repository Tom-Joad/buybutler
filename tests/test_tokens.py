"""Access keys for other programs: made with the password, shown once,
stored hashed, bound to one list's shopping list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from buybutler import db

from .test_app import H, PASSWORD, client, fake_off, logged_in, settings  # noqa: F401
from .test_lists import add_user, home, login, share  # noqa: F401


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
    assert r.status_code == 201 and r.json() == {"list": "Home", "added": 2}
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
