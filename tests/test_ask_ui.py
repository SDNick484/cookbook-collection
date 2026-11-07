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
    page, sent = make_page(browser, server, [ok(book)])
    page.goto(f"{server}/admin#/ask")
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
    page.close()


def test_ask_error_retry_and_new_question(browser, server, book):
    page, sent = make_page(browser, server, [(502, {"error": "Search is unavailable."}), ok(book)])
    page.goto(f"{server}/admin#/ask")
    page.fill("#ask-q", "stew")
    page.press("#ask-q", "Enter")
    page.locator(".ask-err", has_text="Search is unavailable.").wait_for()
    page.click("#ask-retry")
    page.locator('li[data-id="1"]').wait_for()
    assert sent[0] == sent[1] == {"thread": [{"q": "stew"}]}
    page.click("#ask-new")
    assert page.locator(".ask-q").count() == 0
    page.close()


def test_ask_disables_after_fifth_question(browser, server, book):
    page, sent = make_page(browser, server, [ok(book, turns_left=0)])
    page.goto(f"{server}/admin#/ask")
    page.fill("#ask-q", "stew")
    page.press("#ask-q", "Enter")
    page.locator(".ask-a").wait_for()
    assert page.locator("#ask-q").is_disabled()
    assert "Start a new question to keep going" in page.locator("#ask-q").get_attribute("placeholder")
    page.close()


def test_ask_instead_prefills_without_sending(browser, server, book):
    page, sent = make_page(browser, server, [ok(book)])
    page.goto(f"{server}/admin#/?q=zzzqqxnomatch")
    link = page.locator("a", has_text="Ask instead")
    link.wait_for()
    link.click()
    page.locator("#ask-q").wait_for()
    assert page.input_value("#ask-q") == "zzzqqxnomatch"
    assert sent == []
    page.close()


def test_ask_lives_in_more_and_nav_unchanged_at_360(browser, server, book):
    # Measured at 360 px: the editor nav already wraps to two lines (72 px vs 34 px per link) without
    # an Ask entry, so Ask ships at the top of More instead of the nav.
    page, _ = make_page(browser, server, [ok(book)], viewport={"width": 360, "height": 740})
    page.goto(f"{server}/admin#/more")
    link = page.locator("#view .panel a", has_text="Ask about your cookbooks")
    link.wait_for()
    assert page.locator("#view .panel").first.locator("a").get_attribute("href") == "#/ask"
    assert page.locator("#nav a", has_text="Ask").count() == 0
    nav_h = page.evaluate("document.querySelector('#nav').getBoundingClientRect().height")
    a_h = page.evaluate("document.querySelector('#nav a').getBoundingClientRect().height")
    print(f"nav height {nav_h}, link height {a_h}")
    link.click()
    page.locator("#ask-q").wait_for()
    page.close()
