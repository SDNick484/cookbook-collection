"""Every visible control on the book page and the house recipe page is at least 44 x 44 CSS px on a phone.

Editors use the app on Android phones; a 26 px row button is a missed tap. This opens the pages at 360 px wide
(the narrowest common Android width) in Chromium, public and editor views, and fails listing every control that
is smaller than 44 px in either direction. Skipped when Playwright or Chromium isn't installed (CI's ui-tests job
runs it through .github/scripts/run-ui-tests.sh, which fails on a skip).
"""
import io
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

MIN = 44
# What a finger can hit: links, buttons, form fields, disclosure summaries, the rating slider. A checkbox inside a
# label, or a file input inside a button-styled label, is measured as its label (the label takes the tap).
UNDERSIZED = """(min) => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=slider], [role=button], [tabindex]:not([tabindex="-1"])';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    let t = el;
    if (el.matches('input[type=checkbox], input[type=radio], input[type=file]') && el.closest('label')) t = el.closest('label');
    const r = t.getBoundingClientRect(), cs = getComputedStyle(t);
    if (!r.width || !r.height || cs.visibility === 'hidden' || t.closest('[hidden], .hidden')) continue;
    if (t.closest('dialog:not([open])')) continue;
    if (r.width < min || r.height < min) {
      const name = (el.getAttribute('aria-label') || el.textContent || el.value || el.placeholder || '').trim().slice(0, 40);
      out.push(`${Math.round(r.width)}x${Math.round(r.height)} <${el.tagName.toLowerCase()} class="${el.className}"> ${name}`);
    }
  }
  return out;
}"""


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
def data():
    """A cookbook with everything its page can show (shelf, cuisine, tags, several authors, a photo, more than a
    dozen recipes so the filter box appears) and a public house recipe with sections, scaling and timers."""
    from PIL import Image

    c = A.app.test_client()   # LAN_EDIT=1 in the tests: localhost may edit
    shelf = c.post("/admin/api/locations", json={"name": "Tap pantry", "parent_id": None}).json["location"]["id"]
    bid = c.post("/admin/api/books", json={
        "title": "Tap Target Kitchen: a Very Long Title About Regional Cooking", "cuisine": "Italian",
        "authors": ["Annamaria Bartolomeo-Castellammare", "Giovanni Rossi"], "tags": ["pasta", "weeknight"],
        "publisher": "Small Press", "year": 2019, "location_id": shelf}).json["id"]
    for i in range(14):
        c.post(f"/admin/api/books/{bid}/recipes", json={"name": f"Recipe number {i}", "page": str(10 + i),
                                                       "heading": "Chicken" if i % 2 else ""})
    rid = c.get(f"/admin/api/books/{bid}").json["recipes"][0]["id"]
    img = io.BytesIO()
    Image.new("RGB", (300, 200), "#aa6633").save(img, "JPEG")
    c.post(f"/admin/api/recipes/{rid}/photos", data={"file": (io.BytesIO(img.getvalue()), "dish.jpg")},
           content_type="multipart/form-data")
    house = c.post("/admin/api/house/recipes", json={
        "name": "Tap test ragù", "servings": 4, "prep_minutes": 20, "cook_minutes": 90, "methods": ["braise"],
        "ingredients": [{"quantity_text": "500", "unit": "g", "item": "beef", "prep_note": "cubed"},
                        {"quantity_text": "1", "unit": "", "item": "onion", "section": "Soffritto"}],
        "steps": [{"text": "Brown the beef.", "timer_minutes": 10}, {"text": "Simmer.", "timer_minutes": 90}],
        "notes": "Better the next day."}).json
    assert c.put(f"/admin/api/books/{house['book']['id']}", json={"visibility": "public"}).status_code == 200
    return {"book": bid, "recipe": house["uid"]}


def undersized(browser, url, ready):
    page = browser.new_page(viewport={"width": 360, "height": 780}, is_mobile=True, has_touch=True)
    try:
        page.goto(url)
        page.locator(ready).first.wait_for()
        page.wait_for_load_state("networkidle")
        return page.evaluate(UNDERSIZED, MIN)
    finally:
        page.close()


@pytest.mark.parametrize("view", ["/", "/admin"])
def test_book_page_controls_are_44px(browser, server, data, view):
    ready = "#rl .acts .edit" if view == "/admin" else "#rl li"
    small = undersized(browser, f"{server}{view}#/book/{data['book']}", ready)
    assert not small, f"{view} book page: controls under {MIN}px at 360px wide:\n" + "\n".join(small)


@pytest.mark.parametrize("view", ["/", "/admin"])
def test_house_recipe_page_controls_are_44px(browser, server, data, view):
    small = undersized(browser, f"{server}{view}#/recipe/{data['recipe']}", ".rsteps .timer")
    assert not small, f"{view} recipe page: controls under {MIN}px at 360px wide:\n" + "\n".join(small)
