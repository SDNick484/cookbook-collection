"""The enforced Content-Security-Policy and Permissions-Policy don't break the app (Chromium, via Playwright).

Each test opens a page of the real app on a local server, uses the parts that load something unusual (cover
candidates from Open Library, which redirect to archive.org; blob: previews of index photos; the share target;
the bookmarklet link; Ask), and fails on any CSP violation, console error or page error. Network calls the page
would make to outside hosts, and the server calls that would reach Claude, are stubbed. Skipped when Playwright
or Chromium isn't installed (as in CI, like the other browser tests).
"""
import io
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as A  # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api")

WATCH = """
window.__csp = [];
document.addEventListener("securitypolicyviolation",
  (e) => window.__csp.push(`${e.effectiveDirective} blocked ${e.blockedURI || "inline"}`));
"""


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
    """A public cookbook with a few recipes (LAN_EDIT=1 in the tests: localhost may edit)."""
    c = A.app.test_client()
    bid = c.post("/admin/api/books", json={"title": "CSP Test Kitchen", "authors": "Tester"}).json["id"]
    for name, page in (("Roast Chicken", "12"), ("Lemon Tart", "40")):
        c.post(f"/admin/api/books/{bid}/recipes", json={"name": name, "page": page})
    return bid


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (40, 60), "#336699").save(b, "PNG")
    return b.getvalue()


class Watched:
    """A page that records CSP violations, console errors and page errors."""

    def __init__(self, browser):
        self.page = browser.new_page(viewport={"width": 390, "height": 844})
        self.console, self.errors = [], []
        self.page.add_init_script(WATCH)
        self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}")
                     if m.type in ("error", "warning") else None)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: d.dismiss())

    def goto(self, url):
        resp = self.page.goto(url)
        csp = resp.headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp, "page served without the CSP"
        assert "content-security-policy-report-only" not in resp.headers
        return resp

    def assert_clean(self, where):
        violations = self.page.evaluate("window.__csp || []")
        noisy = [m for m in self.console if "Content Security Policy" in m or "Permissions policy" in m
                 or "Refused to" in m or m.startswith("error:")]
        assert not violations, f"{where}: CSP violations {violations}"
        assert not noisy, f"{where}: console {noisy}"
        assert not self.errors, f"{where}: page errors {self.errors}"

    def close(self):
        self.page.close()


@pytest.fixture()
def w(browser):
    page = Watched(browser)
    yield page
    page.close()


def stub_features(page, **extra):
    """The real /admin/api/features answer, with some features switched on."""
    def handle(route):
        resp = route.fetch()
        body = {**resp.json(), **extra}
        route.fulfill(response=resp, body=json.dumps(body), content_type="application/json")
    page.route("**/admin/api/features", handle)


# ------------------------------------------------------------------ the policy is really enforced
def test_policy_is_enforced_not_report_only(w, server):
    w.goto(f"{server}/")
    w.page.locator("#view .cover").first.wait_for()
    ran = w.page.evaluate("""() => { const s = document.createElement("script");
        s.textContent = "window.__inline = 1"; document.head.append(s); return window.__inline === 1; }""")
    assert ran is False
    assert any("script-src" in v for v in w.page.evaluate("window.__csp"))
    allowed = w.page.evaluate("""() => { const p = document.permissionsPolicy || document.featurePolicy;
        return ["camera", "screen-wake-lock", "microphone", "geolocation"].map((f) => p.allowsFeature(f)); }""")
    assert allowed == [True, True, False, False]


# ------------------------------------------------------------------ public pages
def test_catalog(w, server):
    w.goto(f"{server}/")
    w.page.locator("#view .cover").first.wait_for()
    w.page.wait_for_load_state("networkidle")
    w.assert_clean("catalog")


def test_book_page(w, server, book):
    w.goto(f"{server}/#/book/{book}")
    w.page.get_by_text("Roast Chicken").first.wait_for()
    w.page.wait_for_load_state("networkidle")
    w.assert_clean("book page")


def test_photos_gallery(w, server):
    w.goto(f"{server}/#/photos")
    w.page.wait_for_load_state("networkidle")
    w.assert_clean("photos")


# ------------------------------------------------------------------ editor
def test_editor_catalog_and_book(w, server, book):
    w.goto(f"{server}/admin#/")
    w.page.locator("#view .cover").first.wait_for()
    w.page.goto(f"{server}/admin#/book/{book}")
    w.page.get_by_text("Lemon Tart").first.wait_for()
    w.page.wait_for_load_state("networkidle")
    w.assert_clean("editor book page")


def test_editor_cover_candidates_from_open_library(w, server, book):
    """Cover candidates load straight from the lookup hosts (Open Library, archive.org, Google)."""
    image = png()
    for host in ("https://covers.openlibrary.org/**", "https://ia800100.us.archive.org/**",
                 "https://books.googleusercontent.com/**"):
        w.page.route(host, lambda r: r.fulfill(status=200, content_type="image/png", body=image))
    results = [{"title": f"Match {i}", "subtitle": None, "authors": ["Tester"], "year": 2020, "publishers": ["P"],
                "isbns": ["9780306406157"], "cover_url": url} for i, url in enumerate((
                    "https://covers.openlibrary.org/b/id/123-L.jpg",
                    "https://ia800100.us.archive.org/view/cover-L.jpg",
                    "https://books.googleusercontent.com/books/content?id=x-L.jpg"))]
    w.page.route("**/admin/api/lookup/search*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"results": results})))
    w.goto(f"{server}/admin#/book/{book}/edit")
    w.page.locator("#ol").click()
    w.page.wait_for_function("""() => { const im = [...document.querySelectorAll('.cand img')];
        return im.length === 3 && im.every((i) => i.complete && i.naturalWidth > 0); }""")
    w.page.locator(".cand button[data-i='0']").click()          # use it: the chosen cover is shown
    w.page.wait_for_function("() => { const i = document.querySelector('#newcover img'); return i && i.complete && i.naturalWidth > 0; }")
    w.assert_clean("editor cover candidates")


def test_open_library_redirects_stay_inside_the_policy(w, server):
    """Open Library answers cover URLs with a redirect to an archive.org server; CSP checks the redirect target.
    Playwright doesn't route the redirected request, so the archive.org fetch itself may fail offline: what this
    checks is that the policy allows it, and still blocks a redirect anywhere else."""
    w.page.route("https://covers.openlibrary.org/b/id/1-S.jpg", lambda r: r.fulfill(
        status=302, headers={"Location": "https://ia800100.us.archive.org/view/1.jpg"}))
    w.page.route("https://covers.openlibrary.org/b/id/2-S.jpg", lambda r: r.fulfill(
        status=302, headers={"Location": "https://evil.example/2.jpg"}))
    w.goto(f"{server}/")
    w.page.locator("#view .cover").first.wait_for()
    for n in (1, 2):
        w.page.evaluate("""(n) => new Promise((done) => { const i = new Image(); i.onload = i.onerror = done;
            i.src = `https://covers.openlibrary.org/b/id/${n}-S.jpg`; })""", n)
    w.page.wait_for_timeout(300)
    blocked = w.page.evaluate("window.__csp")   # Chromium reports the URL before the redirect
    assert any("/2-S.jpg" in v for v in blocked), blocked              # the redirect to evil.example is blocked...
    assert not any("/1-S.jpg" in v for v in blocked), blocked          # ...the one to archive.org is not


def test_editor_cover_fallback_without_inline_handler(w, server, book):
    """The 'no cover image' fallback used to be an inline onerror; it's a listener now and must still work."""
    w.page.route("https://covers.openlibrary.org/**", lambda r: r.fulfill(status=404, body=b""))
    results = [{"title": "Broken cover", "subtitle": None, "authors": ["Tester"], "year": None, "publishers": [],
                "isbns": [], "cover_url": "https://covers.openlibrary.org/b/id/404-L.jpg"}]
    w.page.route("**/admin/api/lookup/search*", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"results": results})))
    w.goto(f"{server}/admin#/book/{book}/edit")
    w.page.locator("#ol").click()
    w.page.locator(".cand button[data-i='0']").click()
    w.page.locator("#newcover", has_text="No cover image available").wait_for()
    w.console[:] = [m for m in w.console if "404" not in m]     # the missing image itself is expected
    w.assert_clean("editor cover fallback")


def test_index_import_with_photo_previews(w, server, book, tmp_path):
    """Index photos are previewed from blob: URLs before the server reads them."""
    stub_features(w.page, photo_import=True)
    w.page.route(f"**/admin/api/books/{book}/index/scan", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"scan": 7})))
    w.page.route("**/admin/api/index/scan/7/pages", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"pages": [1]})))
    w.page.route("**/admin/api/index/scan/7", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"status": "running", "pages": [{"page_no": 1, "status": "queued"}]})))
    photo = tmp_path / "index-page.png"
    photo.write_bytes(png())
    w.goto(f"{server}/admin#/book/{book}/index")
    w.page.locator("#photos").set_input_files(str(photo))
    w.page.wait_for_function("""() => [...document.querySelectorAll('#scanpages img')]
        .some((i) => i.src.startsWith('blob:') && i.complete && i.naturalWidth > 0)""")
    w.assert_clean("index import")


def test_new_recipe_from_photo_fills_the_form_and_attaches_on_save(w, server, tmp_path):
    """From photo: a blob: preview, the polled job, then a draft in the form with the model's text shown as text
    (never markup), and the photo attached once the (real) save succeeds."""
    stub_features(w.page, photo_import=True)
    draft = {"name": "<b>Card</b> cake", "detail": {"description": None, "yield_text": None, "servings": 8, "prep_minutes": None,
             "cook_minutes": 30, "total_minutes": None, "methods": [], "equipment": [], "notes": None, "inspiration": None},
             "ingredients": [{"section": "For the cake", "quantity_text": "1 1/2", "quantity_num": 1.5, "quantity_max": None,
                              "unit": "cups", "item": "flour", "prep_note": None, "optional": 0}],
             "steps": [{"section": None, "text": "Bake.", "timer_minutes": None}]}
    done = {"status": "done", "source": "photo", "photos": [{"page_no": 1, "status": "done", "error": None}],
            "attachable": 1, "draft": draft, "missing": [], "warnings": ["The reply: <img src=x> cut off"]}
    attached = []
    w.page.route("**/admin/api/house/capture/photos", lambda r: r.fulfill(
        status=202, content_type="application/json", body=json.dumps({"job": "j1", "photos": 1})))
    w.page.route("**/admin/api/house/capture/j1", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(done)))
    w.page.route("**/admin/api/house/capture/j1/attach", lambda r: (attached.append(r.request.post_data_json), r.fulfill(
        status=201, content_type="application/json", body=json.dumps({"photos": []})))[-1])
    photo = tmp_path / "card.png"
    photo.write_bytes(png())
    w.goto(f"{server}/admin#/recipe/new")
    w.page.locator("#cap-photo").click()
    w.page.locator("#cap-pick").set_input_files(str(photo))
    w.page.wait_for_function("""() => [...document.querySelectorAll('#cap-pages img')]
        .some((i) => i.src.startsWith('blob:') && i.complete && i.naturalWidth > 0)""")
    w.page.locator("#cap-read").click()
    w.page.locator("#cap-note").wait_for(timeout=15000)
    assert w.page.input_value("input[name=name]") == "<b>Card</b> cake"
    assert w.page.input_value("[data-k=item]") == "flour" and w.page.input_value("[data-k=quantity_text]") == "1 1/2"
    assert w.page.locator("#cap-note b, #cap-note img, form b").count() == 0  # model text stays text
    assert "<img src=x> cut off" in w.page.inner_text("#cap-note")
    w.page.locator("#cap-attach").check()
    w.page.locator("#rsave").click()
    w.page.wait_for_function("() => /^#\\/recipe\\/[0-9a-f]{32}$/.test(location.hash)", timeout=10000)
    assert len(attached) == 1 and isinstance(attached[0]["recipe_id"], int)
    w.assert_clean("new recipe from photo")


def test_new_recipe_from_pasted_text(w, server):
    w.goto(f"{server}/admin#/recipe/new")
    w.page.locator("#cap-text").click()
    w.page.fill("#cap-txt", "Pasted Soup\nIngredients\n2-3 carrots, diced\na pinch of salt\nMethod\n1. Simmer.\n")
    w.page.locator("#cap-go").click()
    w.page.locator("#cap-note").wait_for()
    assert w.page.input_value("input[name=name]") == "Pasted Soup"
    assert [w.page.input_value(f"#ing-rows .erow:not(.sec) >> nth={i} >> [data-k=item]") for i in range(2)] == ["carrots", "salt"]
    assert w.page.locator("#cap-attach").count() == 0  # nothing to attach from text
    w.assert_clean("new recipe from text")


def test_index_import_paste(w, server, book):
    w.goto(f"{server}/admin#/book/{book}/index")
    w.page.locator("#pastebox[open] #txt").wait_for()            # photo import is off in the tests: paste is open
    w.page.fill("#txt", "Chicken\n  braised, 50\nBraised Fennel, 255")
    w.page.locator("#parse").click()
    w.page.locator("#prev input[name=name][value='Braised Fennel']").wait_for()
    w.assert_clean("index import (paste)")


def test_ask(w, server, book):
    stub_features(w.page, ask=True)
    answer = {"answer": "Try the <b>roast chicken</b>.", "turns_left": 4, "items": [
        {"kind": "recipe", "id": 1, "name": "Roast Chicken", "heading": None, "page": "12", "book_id": book,
         "book_title": "CSP Test Kitchen", "url": None, "want_to_try": 0}]}
    w.page.route("**/admin/api/ask", lambda r: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps(answer)))
    w.goto(f"{server}/admin#/ask")
    w.page.fill("#ask-q", "chicken for tonight")
    w.page.press("#ask-q", "Enter")
    w.page.locator(".ask-a").first.wait_for()
    w.assert_clean("ask")


def test_share_target_and_bookmarklet(w, server):
    """Android's share sheet opens /admin?url=...; the bookmarklet link is for dragging, so clicking does nothing."""
    w.page.route("**/admin/api/online/preview", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"items": [], "skipped": []})))
    w.goto(f"{server}/admin?title=Soup&text=Look&url=https%3A%2F%2Fcooking.nytimes.com%2Frecipes%2F1-soup")
    w.page.locator("#lt").wait_for()
    assert "cooking.nytimes.com" in w.page.input_value("#lt")
    before = w.page.url
    w.page.locator("#bml").click()
    w.page.wait_for_timeout(200)
    assert w.page.url == before
    w.assert_clean("share target")


def test_recent_changes_and_undo(w, server, book):
    """More -> Recent changes: the list, its filters, and an undo, with no CSP violation."""
    c = A.app.test_client()
    rid = c.get(f"/admin/api/books/{book}").json["recipes"][0]["id"]
    c.put(f"/admin/api/recipes/{rid}", json={"rating": 9, "want_to_try": True})
    c.post(f"/admin/api/books/{book}/index/commit", json={"rows": [{"name": "Batch one", "page": "50"},
                                                                   {"name": "Batch two", "page": "51"}]})
    w.goto(f"{server}/admin#/more")
    w.page.locator("a[href='#/changes']").click()
    w.page.locator("#ch-list .rlist li").first.wait_for()
    assert "Undo all" in w.page.inner_text("#ch-list")
    assert "Added 2 recipes" in w.page.inner_text("#ch-list")
    w.page.select_option("#ch-book", label="CSP Test Kitchen")
    w.page.wait_for_url("**#/changes?book=*")
    w.page.locator("#ch-list .rlist li").first.wait_for()
    w.page.evaluate("window.confirm = () => true")
    w.page.locator(".ch-undo-all").first.click()
    w.page.locator("#toast", has_text="Undone").wait_for()
    w.page.locator("#ch-list em", has_text="undone").first.wait_for()  # the batch now reads "undone"
    names = [r["name"] for r in c.get(f"/admin/api/books/{book}").json["recipes"]]
    assert "Batch one" not in names and "Batch two" not in names
    w.assert_clean("recent changes")


def test_editor_insights_screens(w, server, book):
    """Progress on the card and book page, the made filter, Stats -> Shelf check and the neglected Cook mode render
    for an editor with no CSP violation or script error."""
    c = A.app.test_client()
    rid = c.get(f"/admin/api/books/{book}").json["recipes"][0]["id"]
    assert c.put(f"/admin/api/recipes/{rid}", json={"last_made": "2026-01-05"}).status_code == 200   # one of two made
    w.goto(f"{server}/admin#/")
    card = w.page.locator("a.card", has_text="CSP Test Kitchen")
    card.locator(".prog .pt", has_text="made 1 of 2").wait_for()
    w.page.goto(f"{server}/admin#/book/{book}")
    w.page.locator(".book .prog .pt", has_text="made 1 of 2").wait_for()
    shown = lambda: w.page.locator("#rl li:not(.hidden)")
    assert shown().count() == 2
    w.page.locator(".mf button", has_text="Not yet made").click()
    assert shown().count() == 1 and "Lemon Tart" in shown().inner_text()     # the first recipe is the made one
    w.page.locator(".mf button", has_text="Made").first.click()
    assert shown().count() == 1 and "Roast Chicken" in shown().inner_text()
    w.page.locator(".mf button", has_text="All").click()
    assert shown().count() == 2
    w.page.goto(f"{server}/admin#/stats")
    w.page.get_by_role("heading", name="Shelf check").wait_for()
    for panel in ("Never cooked from", "Not cooked from in 12 months", "Cooked a lot, never rated", "Index coverage"):
        assert w.page.get_by_role("heading", name=panel).count() == 1, panel
    w.page.goto(f"{server}/admin#/cook")
    w.page.locator(".cookbar .seg button", has_text="From a neglected book").click()
    w.page.locator("#cook-list li").first.wait_for()
    w.page.wait_for_load_state("networkidle")
    assert w.page.locator(".cookbar .seg button.on").inner_text() == "From a neglected book"
    w.assert_clean("insights screens")
