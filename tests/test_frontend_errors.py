"""Browser checks for error handling in static/app.js mutation handlers.

Each test serves the real app on localhost, makes one API call fail (HTTP 500 or a
slow response), and checks that the user sees a toast with the server's message and
that the control is usable again afterwards. Skipped when Playwright or Chromium
isn't installed.
"""
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
import app as A  # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api")


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
    """A fresh book with one recipe, created through the test client."""
    c = A.app.test_client()
    bid = c.post("/admin/api/books", json={"title": "Frontend Error Test Book", "authors": "Tester"}).json["id"]
    c.post(f"/admin/api/books/{bid}/recipes", json={"name": "Test Stew", "page": "3"})
    return bid


def fail(status, message):
    return {"status": status, "body": {"error": message}}


def open_admin(browser, base, hash_, rules):
    """Open /admin/<hash_> with the given {(method, url_glob): response} rules; returns (page, calls)."""
    page = browser.new_page()
    calls = {}
    for key, spec in rules.items():
        method, glob = key

        def handle(route, request, key=key, spec=spec, method=method):
            if request.method != method:
                return route.fallback()  # hand other methods to the next matching handler
            calls[key] = calls.get(key, 0) + 1
            route.fulfill(status=spec["status"], content_type="application/json", body=json.dumps(spec["body"]))

        page.route(glob, handle)
    page.on("dialog", lambda d: d.accept())  # confirm() prompts
    page.goto(f"{base}/admin/{hash_}")
    return page, calls


def toast_text(page):
    return page.locator("#toast.show").inner_text(timeout=5000)


def test_index_commit_error_shows_toast_and_reenables(browser, server, book):
    page, calls = open_admin(browser, server, f"#/book/{book}/index", {
        ("POST", "**/admin/api/books/*/index/parse"): {"status": 200, "body": {"rows": [
            {"heading": "Chicken", "name": "Roast Chicken", "page": "12"}]}},
        ("POST", "**/admin/api/books/*/index/commit"): fail(500, "Index commit exploded"),
    })
    page.locator("#txt").fill("Chicken\nRoast Chicken, 12")
    page.click("#parse")
    page.wait_for_selector("#commit")
    page.click("#commit")
    assert "Index commit exploded" in toast_text(page)
    assert page.locator("#commit").is_enabled()


def test_index_commit_double_click_sends_one_request(browser, server, book):
    page, calls = open_admin(browser, server, f"#/book/{book}/index", {
        ("POST", "**/admin/api/books/*/index/parse"): {"status": 200, "body": {"rows": [
            {"heading": "Beef", "name": "Beef Stew", "page": "7"}]}},
        ("POST", "**/admin/api/books/*/index/commit"): {"status": 200, "body": {"added": 1, "skipped": 0}},
    })
    page.locator("#txt").fill("Beef\nBeef Stew, 7")
    page.click("#parse")
    page.wait_for_selector("#commit")
    page.dblclick("#commit")
    page.wait_for_timeout(800)
    assert calls.get(("POST", "**/admin/api/books/*/index/commit"), 0) == 1


def test_eyb_recipe_import_error_shows_toast_and_reenables(browser, server, book):
    preview = {"status": 200, "body": {"kind": "recipes", "total": 2, "books": [], "groups": [
        {"eyb_title": "Test Book", "author": "A", "count": 2, "with_pages": 2, "new": 2, "match": None, "method": ""}]}}
    page, _ = open_admin(browser, server, "#/eyb", {
        ("POST", "**/admin/api/eyb/preview"): preview,
        ("POST", "**/admin/api/eyb/commit"): fail(500, "EYB commit exploded"),
    })
    page.set_input_files("#eybfile", files=[{"name": "recipes.csv", "mimeType": "text/csv", "buffer": b"a,b\n1,2\n"}])
    page.wait_for_selector("#eybgo")
    page.click("#eybgo")
    assert "EYB commit exploded" in toast_text(page)
    assert page.locator("#eybgo").is_enabled()


def test_covers_fetch_error_shows_toast_and_reenables(browser, server, book):
    page, _ = open_admin(browser, server, "#/eyb", {
        ("GET", "**/admin/api/enrich"): {"status": 200, "body": {"remaining": 3, "results": []}},
        ("POST", "**/admin/api/enrich"): fail(500, "Covers lookup exploded"),
    })
    page.wait_for_selector("#enrich:not([disabled])")
    page.click("#enrich")
    assert "Covers lookup exploded" in toast_text(page)
    assert page.locator("#enrich").is_enabled()


def test_want_to_try_toggle_error_shows_toast(browser, server, book):
    page, _ = open_admin(browser, server, f"#/book/{book}", {
        ("PUT", "**/admin/api/recipes/*"): fail(500, "Star toggle exploded"),
    })
    page.wait_for_selector("#rl li .want")
    page.click("#rl li .want")
    assert "Star toggle exploded" in toast_text(page)


def test_book_delete_error_shows_toast(browser, server, book):
    page, _ = open_admin(browser, server, f"#/book/{book}/edit", {
        ("DELETE", f"**/admin/api/books/{book}"): fail(500, "Book delete exploded"),
    })
    page.wait_for_selector("#delete")
    page.click("#delete")
    assert "Book delete exploded" in toast_text(page)


def test_recipe_delete_error_shows_toast(browser, server, book):
    page, _ = open_admin(browser, server, f"#/book/{book}", {
        ("DELETE", "**/admin/api/recipes/*"): fail(500, "Recipe delete exploded"),
    })
    page.wait_for_selector("#rl li .edit")
    page.click("#rl li .edit")
    page.wait_for_selector("dialog[open] #del")
    page.click("dialog[open] #del")
    assert "Recipe delete exploded" in toast_text(page)
