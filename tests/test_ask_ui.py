"""Browser smoke test for the Ask view (#/ask) with /admin/api/ask stubbed.

Kept apart from test_ask.py so a missing Playwright only skips these checks, not the API suite.
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as A  # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api")

FEATURES = {"ask": True}


@pytest.fixture(scope="module")
def server():
    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", 0, A.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def book():
    c = A.app.test_client()
    return c.post("/admin/api/books", json={"title": "Ask UI Book", "authors": "Tester"}).json["id"]


def make_page(browser, base, answers, viewport=None):
    """Open /admin with features and /admin/api/ask stubbed; `answers` is a list of (status, body) served in order."""
    page = browser.new_page(viewport=viewport or {"width": 390, "height": 800})
    sent = []
    queue = list(answers)
    page.route("**/admin/api/features", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(FEATURES)))

    def ask(route, request):
        sent.append(request.post_data_json)
        status, body = queue.pop(0) if queue else answers[-1]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/admin/api/ask", ask)
    return page, sent


def ok(book, turns_left=4):
    return (200, {"answer": "Two <b>ideas</b>.", "turns_left": turns_left, "items": [
        {"kind": "recipe", "id": 1, "name": "Test Stew", "heading": None, "page": "3", "book_id": book,
         "book_title": "Ask UI Book", "url": None, "want_to_try": 0}]})


def test_ask_flow(browser, server, book):
    page, sent = make_page(browser, server, [ok(book), ok(book)], viewport={"width": 360, "height": 740})
    puts = []

    def put(route, request):
        puts.append(request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")
    page.route("**/admin/api/recipes/1", put)
    # Ask is a button on the header's brand row (not a nav tab, and no longer listed under More).
    page.goto(f"{server}/admin#/more")
    page.locator("#view .panel").first.wait_for()
    assert page.locator("#view a", has_text="Ask").count() == 0
    assert page.locator("#nav a", has_text="Ask").count() == 0
    btn = page.locator(".top-row #ask-top")
    assert btn.is_visible()
    # At phone width it shares the brand's row instead of adding a line.
    brand_top = page.locator(".brand").bounding_box()["y"]
    assert abs(btn.bounding_box()["y"] - brand_top) < 12
    btn.click()
    page.locator("#ask-q").wait_for()
    assert "on" in (btn.get_attribute("class") or "")
    page.fill("#ask-q", "something with stew")
    page.press("#ask-q", "Enter")
    ans = page.locator(".ask-a .txt")
    ans.wait_for()
    assert ans.inner_text() == "Two <b>ideas</b>."
    assert page.locator(".ask-a b").count() == 0
    row = page.locator('li[data-id="1"]')
    assert "Test Stew" in row.inner_text()
    assert row.locator("button.want").count() == 1
    assert page.locator(".grp", has_text="Ask UI Book").count() == 1
    assert sent == [{"thread": [{"q": "something with stew"}]}]
    # The same recipe in a follow-up: each row's star toggles its own turn's item.
    page.fill("#ask-q", "more stew")
    page.press("#ask-q", "Enter")
    page.locator(".ask-a").nth(1).wait_for()
    second = page.locator('.ask-a[data-turn="1"] li[data-id="1"] button.want')
    second.click()
    page.locator('.ask-a[data-turn="1"] li[data-id="1"] button.want.on').wait_for()
    assert puts == [{"want_to_try": True}]
    assert page.locator('.ask-a[data-turn="0"] li[data-id="1"] button.want.on').count() == 0
    second.click()
    page.locator('.ask-a[data-turn="1"] li[data-id="1"] button.want:not(.on)').wait_for()
    assert puts == [{"want_to_try": True}, {"want_to_try": False}]
    # "Ask instead" from an empty keyword search starts a fresh thread and prefills the input without sending.
    page.goto(f"{server}/admin#/?q=zzzqqxnomatch")
    page.locator("a", has_text="Ask instead").click()
    page.locator("#ask-q").wait_for()
    assert page.input_value("#ask-q") == "zzzqqxnomatch"
    assert page.locator(".ask-q").count() == 0
    assert len(sent) == 2
    page.press("#ask-q", "Enter")
    page.locator('li[data-id="1"]').wait_for()
    assert sent[2] == {"thread": [{"q": "zzzqqxnomatch"}]}
    page.close()


def test_no_ask_button_when_off(browser, server):
    page = browser.new_page(viewport={"width": 360, "height": 740})
    page.route("**/admin/api/features", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"ask": False})))
    page.goto(f"{server}/admin#/")
    page.locator("#nav a").first.wait_for()
    assert page.locator("#ask-top").count() == 0
    page.close()


def test_ask_error_retry_and_retyped_question(browser, server, book):
    page, sent = make_page(
        browser, server,
        [(502, {"error": "Search is unavailable."}), (502, {"error": "Search is unavailable."}), ok(book, turns_left=0),
         ok(book)])
    page.goto(f"{server}/admin#/ask")
    page.fill("#ask-q", "stew")
    page.press("#ask-q", "Enter")
    page.locator(".ask-err", has_text="Search is unavailable.").wait_for()
    page.click("#ask-retry")  # thread kept, same question resent
    page.locator(".ask-err").wait_for()
    assert sent[0] == sent[1] == {"thread": [{"q": "stew"}]}
    # Typing a different question after an error replaces the failed turn instead of adding a second q.
    page.fill("#ask-q", "soup instead")
    page.press("#ask-q", "Enter")
    page.locator('li[data-id="1"]').wait_for()
    assert sent[2] == {"thread": [{"q": "soup instead"}]}
    assert page.locator(".ask-q").count() == 1
    assert page.locator("#ask-q").is_disabled()  # turns_left 0: start a new question
    page.click("#ask-new")
    assert page.locator(".ask-q").count() == 0
    # New question keeps what was typed; it is hidden while a question is being answered.
    page.fill("#ask-q", "keep this")
    page.press("#ask-q", "Enter")
    page.locator('li[data-id="1"]').wait_for()
    page.fill("#ask-q", "draft text")
    page.click("#ask-new")
    assert page.input_value("#ask-q") == "draft text" and page.locator(".ask-q").count() == 0
    page.close()
