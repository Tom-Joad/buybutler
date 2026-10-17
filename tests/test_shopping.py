"""The shopping list: adding, ticking, the per-store order, roles."""

from __future__ import annotations

import uuid

from buybutler import db

from .test_app import H, client, fake_off, logged_in, make_product, other_user_with_list, rate, settings  # noqa: F401
from .test_lists import add_user, home, login, share, use  # noqa: F401


def add(c, **kw):
    body = {"client_id": str(uuid.uuid4())} | kw
    return c.post("/api/shopping", json=body, headers=H)


def items(c, store: str = "") -> list[dict]:
    r = c.get("/api/shopping", params={"store": store})
    assert r.status_code == 200
    return r.json()["items"]


def test_add_product_and_free_text(logged_in):
    p = make_product(logged_in, name="Oat milk", brand="Oaty", category="Drinks")
    rate(logged_in, p["id"], buy_again=False, reason="Too thin")
    r = add(logged_in, product_id=p["id"], quantity="2x", store="Corner shop")
    assert r.status_code == 201
    item = r.json()
    assert item["name"] == "Oat milk" and item["verdict"] is False and item["reason"] == "Too thin"
    assert item["quantity"] == "2x" and item["store"] == "Corner shop" and item["checked"] is False
    assert item["added_by"] == "tj" and item["key"] == f"p:{p['id']}"
    assert item["created_at"].endswith("Z") and item["updated_at"].endswith("Z")
    r = add(logged_in, text="  Bread   rolls ")
    assert r.status_code == 201 and r.json()["name"] == "Bread   rolls" and r.json()["key"] == "t:bread rolls"
    assert [i["name"] for i in items(logged_in)] == ["Oat milk", "Bread   rolls"]


def test_add_needs_product_or_text(logged_in):
    assert add(logged_in).status_code == 422
    assert add(logged_in, text="   ").status_code == 422
    assert add(logged_in, product_id=9999).status_code == 404
    assert add(logged_in, text="x" * 101).status_code == 422


def test_open_product_is_not_added_twice(logged_in):
    p = make_product(logged_in)
    first = add(logged_in, product_id=p["id"]).json()
    again = add(logged_in, product_id=p["id"])
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    # Once ticked, it can go on the list again.
    logged_in.patch(f"/api/shopping/{first['id']}", json={"checked": True}, headers=H)
    assert add(logged_in, product_id=p["id"]).status_code == 201


def test_repeated_add_returns_the_first(logged_in):
    cid = str(uuid.uuid4())
    assert add(logged_in, client_id=cid, text="Milk").status_code == 201
    r = add(logged_in, client_id=cid, text="Milk")
    assert r.status_code == 200 and r.json()["id"] == cid
    assert len(items(logged_in)) == 1


def test_tick_keeps_the_first_and_untick(logged_in, settings):
    item = add(logged_in, text="Milk").json()
    r = logged_in.patch(f"/api/shopping/{item['id']}", json={"checked": True}, headers=H)
    first = r.json()
    assert first["checked"] is True and first["checked_by"] == "tj"
    # A second phone ticks the same item: it stays as it was.
    again = logged_in.patch(f"/api/shopping/{item['id']}", json={"checked": True}, headers=H).json()
    assert again["checked_at"] == first["checked_at"]
    r = logged_in.patch(f"/api/shopping/{item['id']}", json={"checked": False}, headers=H)
    assert r.json()["checked"] is False and r.json()["checked_by"] is None


def test_untick_when_the_product_is_on_the_list_again(logged_in):
    p = make_product(logged_in)
    old = add(logged_in, product_id=p["id"]).json()
    logged_in.patch(f"/api/shopping/{old['id']}", json={"checked": True}, headers=H)
    new = add(logged_in, product_id=p["id"]).json()
    r = logged_in.patch(f"/api/shopping/{old['id']}", json={"checked": False}, headers=H)
    assert r.status_code == 200 and r.json() is None
    assert [i["id"] for i in items(logged_in)] == [new["id"]]


def test_edit_quantity_store_and_text(logged_in):
    item = add(logged_in, text="Milk").json()
    r = logged_in.patch(f"/api/shopping/{item['id']}", json={"quantity": "3", "store": "Market", "text": "Oat milk"},
                        headers=H)
    changed = r.json()
    assert (changed["quantity"], changed["store"], changed["name"]) == ("3", "Market", "Oat milk")
    assert changed["updated_at"] >= item["updated_at"]
    assert logged_in.patch("/api/shopping/" + "0" * 36, json={"checked": True}, headers=H).status_code == 404


def test_delete_and_clear_checked(logged_in):
    a = add(logged_in, text="A").json()
    b = add(logged_in, text="B").json()
    add(logged_in, text="C")
    assert logged_in.delete(f"/api/shopping/{a['id']}", headers=H).status_code == 204
    assert logged_in.delete(f"/api/shopping/{a['id']}", headers=H).status_code == 404
    logged_in.patch(f"/api/shopping/{b['id']}", json={"checked": True}, headers=H)
    assert logged_in.post("/api/shopping/clear-checked", headers=H).json() == {"removed": 1}
    assert [i["name"] for i in items(logged_in)] == ["C"]


def test_order_is_remembered_per_store(logged_in):
    milk = add(logged_in, text="Milk").json()
    bread = add(logged_in, text="Bread").json()
    cheese = add(logged_in, text="Cheese").json()
    r = logged_in.put("/api/shopping/order", json={"store": "Corner shop",
                                                   "keys": [cheese["key"], bread["key"], milk["key"]]}, headers=H)
    assert r.status_code == 204
    places = {i["name"]: i["position"] for i in items(logged_in, "corner SHOP")}
    assert places == {"Cheese": 0, "Bread": 1, "Milk": 2}
    # Another store has its own order; none yet.
    assert {i["position"] for i in items(logged_in, "Market")} == {None}

    # Next week only milk and cheese are on the list, plus apples; milk is
    # moved first. Bread keeps its place for later.
    for item in (milk, bread, cheese):
        logged_in.delete(f"/api/shopping/{item['id']}", headers=H)
    add(logged_in, text="milk")
    add(logged_in, text="Cheese")
    add(logged_in, text="Apples")
    logged_in.put("/api/shopping/order", json={"store": "Corner shop",
                                               "keys": ["t:milk", "t:apples", "t:cheese"]}, headers=H)
    with db.session(logged_in.app.state.settings.database) as conn:
        order = [r[0] for r in conn.execute("SELECT item_key FROM store_order ORDER BY position")]
    assert order == ["t:milk", "t:bread", "t:apples", "t:cheese"]
    places = {i["name"]: i["position"] for i in items(logged_in, "Corner shop")}
    assert places["milk"] < places["Apples"] < places["Cheese"]


def test_stores_come_from_ratings_prices_and_items(logged_in):
    p = make_product(logged_in)
    rate(logged_in, p["id"], store="Market")
    add(logged_in, text="Milk", store="Corner shop")
    add(logged_in, text="Eggs", store="corner shop")
    assert logged_in.get("/api/shopping").json()["stores"] == ["Corner shop", "Market"]


def test_lists_keep_their_shopping_apart(logged_in, settings):
    add(logged_in, text="Milk")
    mine = items(logged_in)[0]
    other = other_user_with_list(logged_in, settings)
    assert items(other) == []
    assert other.patch(f"/api/shopping/{mine['id']}", json={"checked": True}, headers=H).status_code == 404
    assert other.delete(f"/api/shopping/{mine['id']}", headers=H).status_code == 404
    # The same client_id can't be taken over from another list.
    assert add(other, client_id=mine["id"], text="Mine now").status_code == 409
    # Products of another list can't be put on this one.
    theirs = make_product(other, name="Their bread")
    assert add(logged_in, product_id=theirs["id"]).status_code == 404


def test_roles_on_the_shopping_list(logged_in, settings, home):
    add(logged_in, text="Milk")
    item = items(logged_in)[0]
    add_user(settings, "sam")
    add_user(settings, "eve")
    share(logged_in, home, "sam", "editor")
    share(logged_in, home, "eve", "viewer")
    sam = use(login(logged_in.app, "sam"), home)
    eve = use(login(logged_in.app, "eve"), home)
    assert [i["name"] for i in items(eve)] == ["Milk"]
    assert add(eve, text="Eggs").status_code == 403
    assert eve.patch(f"/api/shopping/{item['id']}", json={"checked": True}, headers=H).status_code == 403
    assert eve.delete(f"/api/shopping/{item['id']}", headers=H).status_code == 403
    assert eve.post("/api/shopping/clear-checked", headers=H).status_code == 403
    assert eve.put("/api/shopping/order", json={"keys": []}, headers=H).status_code == 403
    r = sam.patch(f"/api/shopping/{item['id']}", json={"checked": True}, headers=H)
    assert r.json()["checked_by"] == "sam"
    assert add(sam, text="Eggs").json()["added_by"] == "sam"


def test_deleting_the_product_or_list_removes_its_items(logged_in, settings):
    p = make_product(logged_in)
    add(logged_in, product_id=p["id"])
    logged_in.delete(f"/api/products/{p['id']}", headers=H)
    assert items(logged_in) == []
