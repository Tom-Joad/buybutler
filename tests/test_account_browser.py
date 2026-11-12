"""The account page in a real browser at phone size; see
test_outbox_browser.py for the server and browser fixtures."""

from __future__ import annotations

import time

from buybutler import twofactor

from .test_app import PASSWORD
from .test_outbox_browser import api, browser, page, server  # noqa: F401  (fixtures)


def test_turning_on_two_factor_asks_to_check_the_access_keys(page, api):
    home = api.get("/api/lists").json()["lists"][0]["id"]
    r = api.post("/api/account/tokens", json={"name": "Recipe book", "list_id": home, "password": PASSWORD})
    assert r.status_code == 201
    page.goto("/#/account")
    page.wait_for_selector("#tk-keys li")
    assert page.is_hidden("#tf-keys")

    page.fill("#tf-password", PASSWORD)
    page.click("#tf-totp-start")
    page.wait_for_selector("#tf-totp-setup:not([hidden])")
    secret = page.text_content("#tf-secret").replace(" ", "")
    page.fill("#tf-code", twofactor.code_at(secret, int(time.time() // twofactor.STEP)))
    page.click("#tf-totp-confirm")
    page.wait_for_selector("#tf-keys:not([hidden])")
    assert page.text_content("#tf-key-list").startswith("Recipe book")
    assert "not used yet" in page.text_content("#tf-key-list")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

    page.click("#tf-keys-manage")
    page.wait_for_function("() => document.querySelector('#tk-section').getBoundingClientRect().top < 100")
    # Removing the key under Access keys empties the list to check.
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#tk-keys li button")
    page.wait_for_selector("#tf-keys", state="hidden")

    # Leaving the page forgets the list.
    page.goto("/#/shop")
    page.goto("/#/account")
    page.wait_for_selector("#tf-status:not(:empty)")
    assert page.is_hidden("#tf-keys")
