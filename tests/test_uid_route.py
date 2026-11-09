"""Browser checks for the #/uid/<book uid>[/<recipe uid>] deep link in static/app.js.

Dinner Log stores only uids, so its links land here. The route resolves the uid through the by-uid API and
replaces itself with #/book/<id>[?recipe=<id>]. Skipped when Playwright or Chromium isn't installed.
"""
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

UNKNOWN = "f" * 32


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
def catalog():
    """A cookbook with two recipes, and a recipe website (editor-only), with their ids and uids."""
    con = A.connect()
    bid = con.execute("INSERT INTO books(title) VALUES ('Uid Route Cookbook')").lastrowid
    rids = [con.execute("INSERT INTO recipes(book_id, name, page) VALUES (?, ?, ?)", (bid, n, p)).lastrowid
            for n, p in (("First Dish", "10"), ("Second Dish", "20"))]
    oid = con.execute("INSERT INTO books(title, kind) VALUES ('Uid Route Website', 'online')").lastrowid
    con.commit()
    out = {
        "book": bid, "book_uid": con.execute("SELECT uid FROM books WHERE id=?", (bid,)).fetchone()[0],
        "recipe": rids[1], "recipe_uid": con.execute("SELECT uid FROM recipes WHERE id=?", (rids[1],)).fetchone()[0],
        "online": oid, "online_uid": con.execute("SELECT uid FROM books WHERE id=?", (oid,)).fetchone()[0],
    }
    con.close()
    return out


def open_at(browser, url):
    page = browser.new_page()
    page.goto(url)
    return page


def test_book_uid_opens_the_book_and_replaces_the_hash(browser, server, catalog):
    page = open_at(browser, f"{server}/#/uid/{catalog['book_uid']}")
    page.wait_for_selector("h1")
    assert page.locator("h1").inner_text() == "Uid Route Cookbook"
    assert page.url.endswith(f"#/book/{catalog['book']}")


def test_recipe_uid_highlights_that_recipe(browser, server, catalog):
    page = open_at(browser, f"{server}/#/uid/{catalog['book_uid']}/{catalog['recipe_uid']}")
    page.wait_for_selector("#rl li.hl")
    assert page.url.endswith(f"#/book/{catalog['book']}?recipe={catalog['recipe']}")
    assert "Second Dish" in page.locator("#rl li.hl").inner_text()
    assert page.locator("#rl li.hl").count() == 1


def test_unknown_recipe_uid_still_opens_the_book(browser, server, catalog):
    page = open_at(browser, f"{server}/#/uid/{catalog['book_uid']}/{UNKNOWN}")
    page.wait_for_selector("h1")
    assert page.locator("h1").inner_text() == "Uid Route Cookbook"
    assert page.url.endswith(f"#/book/{catalog['book']}")
    assert page.locator("#rl li.hl").count() == 0


def test_back_skips_the_redirect(browser, server, catalog):
    page = open_at(browser, f"{server}/#/photos")
    page.wait_for_selector("#view")
    page.goto(f"{server}/#/uid/{catalog['book_uid']}")
    page.wait_for_selector("h1")
    page.go_back()
    page.wait_for_function("location.hash === '#/photos'")


def test_unknown_book_uid_says_so(browser, server):
    page = open_at(browser, f"{server}/#/uid/{UNKNOWN}")
    assert "isn't in the public catalog" in page.locator(".empty").inner_text(timeout=5000)
    page = open_at(browser, f"{server}/admin#/uid/{UNKNOWN}")
    assert "isn't in this catalog" in page.locator(".empty").inner_text(timeout=5000)


def test_recipe_website_is_editor_only(browser, server, catalog):
    """The public catalog hides kind='online' books, so the link must go through /admin."""
    page = open_at(browser, f"{server}/#/uid/{catalog['online_uid']}")
    assert "editor-only" in page.locator(".empty").inner_text(timeout=5000)
    page = open_at(browser, f"{server}/admin#/uid/{catalog['online_uid']}")
    page.wait_for_selector("h1")
    assert page.locator("h1").inner_text() == "Uid Route Website"
    assert page.url.endswith(f"/admin#/book/{catalog['online']}")


def test_malformed_uid_falls_through_to_the_home_view(browser, server):
    page = open_at(browser, f"{server}/#/uid/not-a-uid")
    page.wait_for_selector("#q")  # the catalog's search box: no uid route matched
