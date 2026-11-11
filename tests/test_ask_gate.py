"""Ask button on the public view, for signed-in family only.

The page decides from /admin/api/me and /admin/api/features, both behind Access. These tests pin down what an
anonymous visitor can and can't reach (the real gate), and that the SPA asks once per session and shows the
button only to an editor with AI search switched on. test_ask.py::test_no_identity_401 covers POST /admin/api/ask
with no identity; here it is again together with the other routes the button touches."""
import json, os, sys, tempfile, threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
import app as A  # noqa: E402
import ask  # noqa: E402
import scan as SC  # noqa: E402

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}   # came through Cloudflare, no Access sign-in
ASK = {"thread": [{"q": "chicken thighs"}]}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(ask, "SEARCH_KEY", "test-key")


@pytest.fixture()
def c():
    return A.app.test_client()


def test_anonymous_gets_nothing(c):
    for path in ("/admin/api/me", "/admin/api/features"):
        r = c.get(path, headers=CF)
        assert r.status_code == 401, path
        assert "ask" not in (r.get_json() or {}), path          # not even the flag
    assert c.post("/admin/api/ask", json=ASK, headers=CF).status_code == 401
    # and there is no public twin of any of them
    assert c.get("/api/features").status_code == 404
    assert c.post("/api/ask", json=ASK).status_code in (404, 405)
    assert c.get("/api/me", headers=CF).get_json() == {"editor": False}


def test_anonymous_cannot_ask_even_on_the_lan(c, monkeypatch):
    monkeypatch.setattr(A, "LAN_EDIT", False)                    # no identity of any kind
    assert c.post("/admin/api/ask", json=ASK).status_code == 401
    assert c.get("/admin/api/features").status_code == 401


def test_editor_gets_what_the_button_needs(c, monkeypatch):
    me = c.get("/admin/api/me")                                  # LAN_EDIT=1 in the tests: localhost may edit
    assert me.status_code == 200 and me.get_json()["editor"] is True
    assert c.get("/admin/api/features").get_json()["ask"] is True
    monkeypatch.setattr(ask, "SEARCH_KEY", "")
    monkeypatch.setattr(SC, "ANTHROPIC_API_KEY", "")
    assert c.get("/admin/api/features").get_json()["ask"] is False   # switched off: no button


# ----------------------------------------------------------------- the page
@pytest.fixture(scope="module")
def server():
    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", 0, A.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    sa = pytest.importorskip("playwright.sync_api")
    with sa.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def public_page(browser, me, features=None):
    """The public catalog with /admin/api/me and /admin/api/features stubbed; returns (page, calls)."""
    page = browser.new_page(viewport={"width": 390, "height": 800})
    calls = {"me": 0, "features": 0}

    def stub(name, status, body):
        def handler(route, request):
            calls[name] += 1
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        page.route(f"**/admin/api/{name}", handler)

    stub("me", *me)
    stub("features", *(features or (200, {"ask": True})))
    return page, calls


def test_page_hides_ask_from_visitors_and_asks_once(browser, server):
    page, calls = public_page(browser, (401, {"error": "sign in"}))
    page.goto(server + "/")
    page.wait_for_load_state("networkidle")
    assert page.locator("#ask-top").count() == 0
    assert "Sign in" in page.inner_text("#nav")
    assert calls == {"me": 1, "features": 0}                     # a visitor never reaches the features call
    page.reload()
    page.wait_for_load_state("networkidle")
    page.goto(server + "/#/photos")
    assert calls["me"] == 1                                      # remembered for the session, not asked per page load
    page.close()


def test_page_shows_ask_to_a_signed_in_editor(browser, server):
    page, calls = public_page(browser, (200, {"editor": True, "email": "someone@example.com"}))
    page.goto(server + "/")
    page.wait_for_selector("#ask-top", timeout=8000)
    assert page.get_attribute("#ask-top", "href") == "/admin#/ask"   # into the editor, behind Access
    assert "Edit" in page.inner_text("#nav")
    page.reload()
    page.wait_for_selector("#ask-top", timeout=8000)             # straight from the session cache
    assert calls == {"me": 1, "features": 1}
    page.close()


def test_page_hides_ask_when_ai_search_is_off(browser, server):
    page, calls = public_page(browser, (200, {"editor": True}), features=(200, {"ask": False}))
    page.goto(server + "/")
    page.wait_for_load_state("networkidle")
    assert page.locator("#ask-top").count() == 0 and "Edit" in page.inner_text("#nav")
    assert calls == {"me": 1, "features": 1}
    page.close()
