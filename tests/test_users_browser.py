"""Managing users in a real browser at phone size; see
test_outbox_browser.py for the server and browser fixtures."""

from __future__ import annotations

import httpx

from .test_app import H, PASSWORD
from .test_outbox_browser import api, browser, page, server  # noqa: F401  (fixtures)


def test_deleting_a_user_asks_whether_their_name_stays(page, api, server):
    list_id = int(api.headers["X-Buybutler-List"])

    def user_with_a_rating(name: str) -> int:
        r = api.post("/api/users", json={"username": name, "password": PASSWORD, "admin_password": PASSWORD})
        assert r.status_code == 201, r.text
        assert api.post(f"/api/lists/{list_id}/members", json={"username": name, "role": "editor"}).status_code == 201
        with httpx.Client(base_url=server, headers=H | {"X-Buybutler-List": str(list_id)}) as c:
            assert c.post("/api/session", json={"username": name, "password": PASSWORD}).status_code == 200
            product = c.post("/api/products", json={"name": f"Bread of {name}"}).json()["id"]
            assert c.post(f"/api/products/{product}/ratings", json={"buy_again": True}).status_code == 201
        return product

    def delete(name: str, keep: bool) -> list[str]:
        """Deletes the user in the interface; returns the questions asked."""
        asked: list[str] = []

        def answer(dialog) -> None:
            asked.append(dialog.message)
            # The first question is whether to delete at all, the second whether the name stays.
            dialog.accept() if len(asked) == 1 or keep else dialog.dismiss()

        page.on("dialog", answer)
        page.fill("#u-password", PASSWORD)
        page.click(f"#a-users li:has(.word:text-is('{name}')) button:text-is('Delete')")
        page.wait_for_selector(f"#a-users li:has(.word:text-is('{name}'))", state="detached")
        page.remove_listener("dialog", answer)
        return asked

    kept, cleared = user_with_a_rating("kept"), user_with_a_rating("cleared")
    page.goto("/#/account")
    page.wait_for_selector("#a-users li:has(.word:text-is('kept'))")

    asked = delete("kept", keep=True)
    assert len(asked) == 2 and "Delete kept?" in asked[0] and "OK: keep the name" in asked[1]
    assert api.get(f"/api/products/{kept}").json()["rating"]["user"] == "kept"

    assert len(delete("cleared", keep=False)) == 2
    assert api.get(f"/api/products/{cleared}").json()["rating"]["user"] is None
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
