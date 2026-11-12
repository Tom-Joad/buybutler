"""The outbox in app.js, in a real browser: jobs are sent once, and changes
made offline reach the server. Needs Playwright with Chromium
(`python -m playwright install chromium`); skipped without it."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from buybutler import auth, config, db
from buybutler.app import create_app

sync_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "correct horse battery"
H = {"X-Buybutler": "1"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("config")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BUYBUTLER_DATA_DIR", str(data))
        mp.setenv("BUYBUTLER_OFF_LOOKUP", "false")
        settings = config.load()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    with db.session(settings.database) as conn:
        auth.create_user(conn, "tj", PASSWORD)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def api(server):
    """Another phone: the same user through the API."""
    with httpx.Client(base_url=server, headers=H) as c:
        assert c.post("/api/session", json={"username": "tj", "password": PASSWORD}).status_code == 200
        c.headers["X-Buybutler-List"] = str(c.get("/api/lists").json()["lists"][0]["id"])
        for item in c.get("/api/shopping").json()["items"]:
            c.delete(f"/api/shopping/{item['id']}")
        yield c


@pytest.fixture
def page(server, browser, api):
    context = browser.new_context(base_url=server, viewport={"width": 390, "height": 844},
                                  locale="en-US")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    assert context.request.post("/api/session", data={"username": "tj", "password": PASSWORD},
                                headers=H).ok
    page.goto("/#/shop")
    page.wait_for_function("() => state.user && state.listId")
    yield page
    context.close()
    assert errors == []


def outbox(page) -> list[dict]:
    return page.evaluate("async () => (await Outbox.all()).map((j) => ({ label: j.label, error: j.error }))")


def sent(page) -> list[dict]:
    """The outbox once it has nothing left to send (or after 5 seconds)."""
    for _ in range(50):
        jobs = outbox(page)
        if all(j["error"] for j in jobs):
            return jobs
        page.evaluate("syncOutbox()")
        time.sleep(0.1)
    return jobs


def test_a_job_is_sent_once_when_the_outbox_syncs_meanwhile(page, api):
    item = api.post("/api/shopping", json={"text": "Bread"}).json()
    deletes: list[str] = []

    def slow_delete(route, request):
        if request.method == "DELETE":
            deletes.append(request.url)
            # The outbox runs while this answer is still on its way.
            page.evaluate("syncOutbox()")
            time.sleep(0.3)
        route.continue_()

    page.route("**/api/shopping/*", slow_delete)
    page.evaluate("async (id) => { shopSend('remove', { method: 'DELETE', path: `/api/shopping/${id}` });"
                  " await shopChain; await syncOutbox(); }", item["id"])
    assert len(deletes) == 1
    assert outbox(page) == []
    assert api.get("/api/shopping").json()["items"] == []


def test_a_delete_of_an_item_gone_already_counts_as_done(page, api):
    item = api.post("/api/shopping", json={"text": "Milk"}).json()
    page.context.set_offline(True)
    page.evaluate("async (id) => { shopSend('remove', { method: 'DELETE', path: `/api/shopping/${id}` });"
                  " await shopChain; }", item["id"])
    assert len(outbox(page)) == 1
    # Another phone removed it meanwhile.
    assert api.delete(f"/api/shopping/{item['id']}").status_code == 204
    page.context.set_offline(False)
    assert sent(page) == []


def test_a_tick_made_offline_follows_another_phones_entry(page, api):
    product = api.post("/api/products", json={"name": "Oat milk"}).json()
    page.context.set_offline(True)
    page.evaluate("""async (product) => {
        addToShopping({ product });
        await shopChain;
        const id = (await Outbox.all())[0].steps[0].body.client_id;
        shopChange({ id }, { checked: true }, "tick");
        await shopChain;
    }""", product)
    assert len(outbox(page)) == 2
    # Meanwhile another phone puts the same product on the list.
    theirs = api.post("/api/shopping", json={"product_id": product["id"]}).json()
    page.context.set_offline(False)
    assert sent(page) == []
    items = api.get("/api/shopping").json()["items"]
    assert [(i["id"], i["checked"]) for i in items] == [(theirs["id"], True)]
