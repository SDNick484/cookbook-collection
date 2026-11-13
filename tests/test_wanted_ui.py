"""Browser checks for wanted books in static/app.js: Add with "Don't own it yet", the Wanted screen (More -> Wanted),
staying off the catalog, and Bought it. Skipped when Playwright or Chromium isn't installed."""
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
ROOT = Path(__file__).resolve().parents[1]
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


@pytest.fixture()
def page(browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    pg.set_default_timeout(8000)
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    pg.close()


def add_wanted(title, **cols):
    con = A.connect()
    fields = {"title": title, "status": "wanted", **cols}
    bid = con.execute(f"INSERT INTO books({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                      list(fields.values())).lastrowid
    A.reindex_book(con, bid)
    con.commit()
    con.close()
    return bid


def test_add_wanted_then_see_it_and_buy_it(server, page):
    page.goto(f"{server}/admin#/add")
    page.fill("#bf input[name=title]", "Browser Wanted Book")
    assert not page.is_visible("#want-fields")
    page.check("#dont-own")
    assert page.is_visible("#want-fields") and not page.is_visible("#loc-pick")
    page.fill("#bf input[name=release_date]", "2099-05")
    page.fill("#bf input[name=price]", "35")
    page.click("#save")
    page.wait_for_url("**#/book/*")
    page.locator("#bought").wait_for()

    page.goto(f"{server}/admin#/wanted")
    page.locator("#wanted-coming", has_text="Browser Wanted Book").wait_for()
    assert "May 2099" in page.inner_text("#wanted-coming") and "$35.00" in page.inner_text("#wanted-coming")

    page.goto(f"{server}/admin#/?q=browser wanted")
    page.wait_for_load_state("networkidle")
    assert "Browser Wanted Book" not in page.inner_text("#view")

    page.goto(f"{server}/admin#/wanted")
    page.click("#wanted-coming a:has-text('Browser Wanted Book')")
    page.click("#bought")
    page.click("dialog button.primary")
    page.locator("#toast", has_text="On the shelf").wait_for()
    assert not page.is_visible("#bought")
    page.goto(f"{server}/admin#/?q=browser wanted")
    page.locator("#view", has_text="Browser Wanted Book").wait_for()
    assert page.errors == []


def test_wanted_screen_groups_sorts_and_links_safely(server, page):
    add_wanted("Ui Out Now Book", release_date="2001-01", price_cents=2450, price_currency="EUR", want_priority=2)
    add_wanted("Ui Junk Link Book", buy_url="javascript:alert(1)", want_priority=1)
    add_wanted("Ui Linked Book", buy_url="https://publisher.example/b", want_priority=3)
    page.goto(f"{server}/admin#/more")
    page.click("a[href='#/wanted']")
    out = page.locator("#wanted-out")
    out.locator("text=Ui Out Now Book").wait_for()
    assert "€24.50" in out.inner_text()
    assert page.locator("#wanted-out a[href^='javascript']").count() == 0
    link = page.locator("#wanted-out a.buy").first
    assert link.get_attribute("href") == "https://publisher.example/b" and link.get_attribute("rel") == "noopener noreferrer"
    titles = [t for t in page.locator("#wanted-out .wtitle").all_inner_texts() if t.startswith("Ui ")]
    assert titles == ["Ui Junk Link Book", "Ui Out Now Book", "Ui Linked Book"]  # priority 1, 2, 3
    page.select_option("#wanted-sort", "added")
    titles = [t for t in page.locator("#wanted-out .wtitle").all_inner_texts() if t.startswith("Ui ")]
    assert titles == ["Ui Linked Book", "Ui Junk Link Book", "Ui Out Now Book"]  # newest first
    page.reload()
    page.locator("#wanted-out .wtitle").first.wait_for()
    assert page.input_value("#wanted-sort") == "added"  # remembered in this browser
    assert page.errors == []
