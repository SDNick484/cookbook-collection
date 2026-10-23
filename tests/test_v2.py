"""v2 features: Dinner Log link, photo import, suggestions, stats, shelf, MCP."""
import io, json, os, sqlite3, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
import app as A  # noqa: E402
import extras as X  # noqa: E402

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}
DL_SCHEMA = """
CREATE TABLE meals (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, name TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'original', source_title TEXT NOT NULL DEFAULT '',
  source_detail TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, rating INTEGER CHECK (rating BETWEEN 1 AND 10));
"""


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture()
def dl(monkeypatch, tmp_path):
    path = tmp_path / "meals.db"
    con = sqlite3.connect(path)
    con.executescript(DL_SCHEMA)
    con.commit()
    monkeypatch.setattr(X, "DINNERLOG_DB", str(path))

    def add(day, name, stype="cookbook", title="", detail="", rating=None):
        k = sqlite3.connect(path)
        k.execute("INSERT INTO meals(day, name, source_type, source_title, source_detail, rating) VALUES (?,?,?,?,?,?)",
                  (day, name, stype, title, detail, rating))
        k.commit()
        mid = k.execute("SELECT max(id) FROM meals").fetchone()[0]
        k.close()
        return mid
    return add


def recipe_id(c, book_title, name):
    b = next(x for x in c.get("/admin/api/books?q=" + book_title).json["books"] if x["title"] == book_title)
    book = c.get(f"/admin/api/books/{b['id']}").json
    return b["id"], next(r for r in book["recipes"] if r["name"] == name)


# ------------------------------------------------------------ Dinner Log
def test_dinnerlog_matching(c, dl):
    today = date.today().isoformat()
    m1 = dl(today, "Zuni roast chicken", title="Zuni Cafe Cookbook", detail="p. 342", rating=9)       # page match
    dl(today, "Roast chicken again", title="the zuni cafe cookbook", detail="342", rating=7)        # case/article
    dl(today, "Fennel pork chops", title="Dining In", detail="", rating=8)                           # name match
    dl(today, "Something", title="Dining In", detail="999")                                         # book only
    dl(today, "Mystery", title="Grandma's binder", detail="12")                                     # no book
    dl(today, "Takeout", stype="restaurant", title="Zuni Cafe")                                     # ignored
    st = c.get("/admin/api/dinnerlog").json
    assert st["available"] and st["meals"] == 5
    assert st["recipe_matched"] == 3 and st["book_only"] == 1
    assert st["unmatched_titles"][0]["title"] == "Grandma's binder"

    bid, r = recipe_id(c, "The Zuni Cafe Cookbook", "Zuni Roast Chicken with Bread Salad")
    assert r["cooked_count"] == 2 and r["cooked_avg"] == 8.0 and r["cooked_last"] == today
    _, pork = recipe_id(c, "Dining In", "Fennel-Rubbed Pork Chops")
    assert pork["cooked_count"] == 1
    # public view never shows cooking history
    pub = c.get(f"/api/books/{bid}").json
    assert all("cooked_count" not in x for x in pub["recipes"])

    # alias an unknown title to a book, then manually link + create a recipe
    binder = c.post("/admin/api/books", json={"title": "Family Recipes"}).json["id"]
    c.post("/admin/api/dinnerlog/alias", json={"title": "Grandma's binder", "book_id": binder})
    dinners = c.get(f"/admin/api/books/{binder}/dinners").json["dinners"]
    assert len(dinners) == 1 and dinners[0]["how"] == "book"
    r = c.post("/admin/api/dinnerlog/link", json={"meal_id": dinners[0]["meal_id"],
                                                    "create": {"book_id": binder, "name": "Mystery stew", "page": "12"}}).json
    assert r["recipe_id"]
    d2 = c.get(f"/admin/api/books/{binder}/dinners").json["dinners"][0]
    assert d2["how"] == "manual" and d2["recipe_name"] == "Mystery stew"

    # manual override beats auto, and survives new Dinner Log writes
    _, other = recipe_id(c, "The Zuni Cafe Cookbook", "Zuni Caesar Salad")
    c.post("/admin/api/dinnerlog/link", json={"meal_id": m1, "recipe_id": other["id"]})
    dl(today, "New dinner", title="Dining In", detail="83")  # changes the file -> resync
    _, caesar = recipe_id(c, "The Zuni Cafe Cookbook", "Zuni Caesar Salad")
    assert caesar["cooked_count"] == 1


def test_dinnerlog_missing_is_harmless(c, monkeypatch):
    monkeypatch.setattr(X, "DINNERLOG_DB", "/nonexistent/meals.db")
    assert c.get("/admin/api/dinnerlog").json["available"] is False
    assert c.get("/admin/api/books/1").status_code == 200


def test_match_recipe_unit():
    recs = [(1, "roast, with bread salad", "Chicken", 342, "342–344"), (2, "Braised fennel", None, 255, "255"),
            (3, "Rosemary-Roasted Potatoes", None, 235, "235"), (4, "Salt Roasties", None, 237, "237")]
    assert X.match_recipe(recs, "343", "chicken") == 1          # inside a page range
    assert X.match_recipe(recs, "p255", "anything") == 2        # single recipe on page
    assert X.match_recipe(recs, "", "rosemary roasted potatoes") == 3
    assert X.match_recipe(recs, "", "pizza") is None


# ------------------------------------------------------------ suggestions / stats / shelf
def test_suggest_modes(c, dl):
    old = (date.today() - timedelta(days=200)).isoformat()
    dl(old, "Leeks", title="Downtime", detail="80", rating=9)
    for mode in X.SUGGEST_MODES:
        r = c.get(f"/admin/api/suggest?mode={mode}&n=4").json
        assert r["mode"] == mode and len(r["recipes"]) <= 4
    want = c.get("/admin/api/suggest?mode=want&n=20").json
    assert all(x["want_to_try"] == 1 and not x.get("cooked_count") for x in want["recipes"])
    books = [x["book_id"] for x in want["recipes"]]
    assert len(set(books[:4])) == min(4, len(set(books)))  # spread across books first
    fav = c.get("/admin/api/suggest?mode=favorites&n=20").json["recipes"]
    assert any(x["name"].startswith("Leeks") for x in fav)
    assert c.get("/admin/api/suggest?mode=bogus").status_code == 400
    assert c.get("/admin/api/suggest", headers=CF).status_code == 401


def test_stats_and_shelf(c, dl):
    dl(date.today().isoformat(), "Pork", title="Dining In", detail="213", rating=8)
    s = c.get("/admin/api/stats").json
    assert s["books"] >= 153 and s["dinners_from_books"] >= 1 and s["top_books"]
    assert any(b["title"] == "Dining In" for b in s["top_books"])
    bid = s["top_books"][0]["id"]
    out = c.post(f"/admin/api/books/{bid}/shelf", json={"out": True}).json
    assert out["status"] == "out" and out["out_since"]
    assert any(b["id"] == bid for b in c.get("/admin/api/stats").json["off_shelf"])
    assert [b["id"] for b in c.get("/admin/api/books?status=out").json["books"]] == [bid]
    back = c.post(f"/admin/api/books/{bid}/shelf", json={"out": False}).json
    assert back["status"] == "owned" and back["out_since"] is None
    assert "status" not in c.get(f"/api/books/{bid}").json


def test_collection_stats(c):
    before = c.get("/admin/api/stats").json
    pepin = next(a for a in before["by_author"] if a["name"] == "Jacques Pépin")
    assert pepin["books"] >= 7 and before["by_author"][0]["books"] >= pepin["books"]  # most books first
    ten = next(p for p in before["by_publisher"] if p["name"] == "Ten Speed Press")
    new = [c.post("/admin/api/books", json=b).json["id"] for b in (
        {"title": "Stats A", "authors": ["Jacques Pépin"], "publisher": "ten speed press", "year": 1994,
         "format": "Hardcover", "rating": 4.5, "tags": ["Stats tag"]},
        {"title": "Stats B", "publisher": "Ten Speed Press", "year": 2003, "format": "hardcover", "rating": 4.5},
        {"title": "Stats C", "publisher": "Ten Speed Press", "status": "wanted", "rating": 1},  # not owned: ignored
    )]
    s = c.get("/admin/api/stats").json
    assert next(a for a in s["by_author"] if a["name"] == "Jacques Pépin")["books"] == 8
    # spellings that differ only by case are one publisher, under the most common spelling
    assert [p["books"] for p in s["by_publisher"] if p["name"].lower() == "ten speed press"] == [ten["books"] + 2]
    assert {"name": "1990s", "books": 1} in s["by_decade"] and {"name": "2000s", "books": 1} in s["by_decade"]
    assert [f for f in s["by_format"] if f["name"].lower() == "hardcover"][0]["books"] >= 2
    assert {"stars": 4.5, "books": 2} in s["ratings"] and all(r["stars"] != 1 for r in s["ratings"])
    assert {"name": "Stats tag", "books": 1} in s["by_tag"]
    assert s["missing"]["year"] == before["missing"]["year"]
    # the publisher filter the bars link to matches every spelling
    titles = {b["title"] for b in c.get("/api/books?publisher=TEN SPEED PRESS").json["books"]}
    assert {"Stats A", "Stats B", "Stats C"} <= titles and len(titles) == ten["books"] + 3
    for bid in new:
        c.delete(f"/admin/api/books/{bid}")


# ------------------------------------------------------------ photo import
def jpeg():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (3000, 4000), "white").save(b, "JPEG")
    return b.getvalue()


def test_photo_import(c, monkeypatch):
    bid = c.post("/admin/api/books", json={"title": "Photo Book"}).json["id"]
    up = lambda: c.post(f"/admin/api/books/{bid}/index/photo", data={"file": (io.BytesIO(jpeg()), "p.jpg")},
                        content_type="multipart/form-data")
    monkeypatch.setattr(X, "ANTHROPIC_API_KEY", "")
    assert up().status_code == 503
    monkeypatch.setattr(X, "ANTHROPIC_API_KEY", "sk-test")
    seen = {}

    def fake(img, media_type="image/jpeg"):
        from PIL import Image
        seen["size"] = Image.open(io.BytesIO(img)).size
        return 'Here you go:\n```json\n{"entries": [' \
               '{"heading": "Chicken", "name": "roast, with bread salad", "page": "342 - 44"},' \
               '{"heading": null, "name": "Braised Fennel", "page": "255"},' \
               '{"heading": null, "name": "Braised Fennel", "page": "255"},' \
               '{"heading": null, "name": "See also Potatoes", "page": ""}]}\n```'
    monkeypatch.setattr(X, "call_claude", fake)
    rows = up().json["rows"]
    assert max(seen["size"]) == 1568
    assert rows[0] == {"heading": "Chicken", "name": "roast, with bread salad", "page": "342–44", "duplicate": False}
    assert len(rows) == 3 and rows[2]["duplicate"] is True   # repeated within the same photo
    monkeypatch.setattr(X, "call_claude", lambda *a, **k: "sorry, can't read that")
    assert up().status_code == 502


# ------------------------------------------------------------ MCP
def rpc(c, method, params=None, url="/mcp", headers=None, mid=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        body["id"] = mid
    return c.post(url, json=body, headers=headers or {})


def test_mcp_public(c):
    init = rpc(c, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}}).json["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert rpc(c, "notifications/initialized", mid=None).status_code == 202
    names = {t["name"] for t in rpc(c, "tools/list").json["result"]["tools"]}
    assert names == {"search_recipes", "search_books", "get_book", "list_categories"}
    res = rpc(c, "tools/call", {"name": "search_recipes", "arguments": {"query": "chicken"}}).json["result"]
    data = json.loads(res["content"][0]["text"])
    assert data["count"] > 0 and all("rating" not in r and "notes" not in r for r in data["recipes"])
    assert rpc(c, "tools/call", {"name": "suggest_recipes", "arguments": {}}).json["error"]["code"] == -32602
    err = rpc(c, "tools/call", {"name": "get_book", "arguments": {}}).json["result"]
    assert err["isError"]
    book = json.loads(rpc(c, "tools/call", {"name": "get_book", "arguments": {"title": "zuni cafe"}}).json
                      ["result"]["content"][0]["text"])
    assert book["title"] == "The Zuni Cafe Cookbook" and len(book["recipes"]) >= 20
    assert c.get("/mcp").status_code == 405
    batch = c.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                 {"jsonrpc": "2.0", "method": "notifications/initialized"}]).json
    assert batch == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_mcp_private(c, monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", "s3cret-token")
    names = {t["name"] for t in rpc(c, "tools/list", url="/mcp/s3cret-token").json["result"]["tools"]}
    assert {"suggest_recipes", "cooking_stats", "set_want_to_try"} <= names
    hdr = {"Authorization": "Bearer s3cret-token"}
    assert len(rpc(c, "tools/list", headers=hdr).json["result"]["tools"]) == len(names)
    assert rpc(c, "tools/list", url="/mcp/wrong").status_code == 404
    res = json.loads(rpc(c, "tools/call", {"name": "suggest_recipes", "arguments": {"mode": "want", "count": 3}},
                         headers=hdr).json["result"]["content"][0]["text"])
    assert len(res["recipes"]) == 3
    rid = res["recipes"][0]["id"]
    rpc(c, "tools/call", {"name": "set_want_to_try", "arguments": {"recipe_id": rid, "want": False}}, headers=hdr)
    assert all(r["id"] != rid for r in c.get("/admin/api/recipes?want=1&limit=500").json["recipes"])
    monkeypatch.setattr(X, "MCP_PUBLIC", False)
    assert rpc(c, "tools/list").status_code == 401
    assert rpc(c, "tools/list", headers=hdr).status_code == 200


def test_match_book_prefers_word_overlap():
    books = [(1, X.norm_title("Simple"), X.tokens("Simple")),
             (2, X.norm_title("Ottolenghi Simple"), X.tokens("Ottolenghi Simple")),
             (3, X.norm_title("The Zuni Cafe Cookbook"), X.tokens("The Zuni Cafe Cookbook"))]
    assert X.match_book("Simple (Ottolenghi)", books, {}) == 2
    assert X.match_book("Simple", books, {}) == 1
    assert X.match_book("Zuni", books, {}) == 3
    assert X.match_book("zuni cafe", books, {}) == 3
    assert X.match_book("Grandma's binder", books, {}) is None


# ------------------------------------------------------------ wish list (against the real app)
WISHLIST_SRC = Path(os.environ.get("WISHLIST_SRC", "/home/claude/src/wishlist/app.py"))


@pytest.mark.skipif(not WISHLIST_SRC.exists(), reason="wish list source not available")
def test_wishlist_integration(c, monkeypatch, tmp_path):
    import importlib.util
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEED_PEOPLE", "Alice,Bob")
    spec = importlib.util.spec_from_file_location("wishlist_app", WISHLIST_SRC)
    wl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wl)
    wc = wl.app.test_client()

    def via_test_client(method, path, body=None):  # same contract as the HTTP call, over loopback
        r = wc.open(path, method=method, json=body, environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    headers={"X-Wishlist": "1"})
        if r.status_code >= 400:
            raise RuntimeError((r.get_json() or {}).get("error") or f"wish list answered {r.status_code}")
        return r.get_json()
    monkeypatch.setattr(X, "wishlist_call", via_test_client)

    people = c.get("/admin/api/wishlist/people").json["people"]
    assert [p["name"] for p in people] == ["Alice", "Bob"]
    bid = c.post("/admin/api/books", json={"title": "Nopalito", "authors": ["Gonzalo Guzmán"],
                                           "isbn": "9780131103627", "status": "wanted"}).json["id"]
    r = c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": people[1]["id"], "price": "32.50"})
    assert r.status_code == 200 and r.json["person"] == "Bob"
    items = via_test_client("GET", f"/api/people/{people[1]['id']}/items")["items"]
    assert items[0]["name"] == "Nopalito by Gonzalo Guzmán" and items[0]["price"] == 32.5
    assert "9780131103627" in items[0]["url"] and "ISBN 9780131103627" in items[0]["notes"]
    assert c.get(f"/admin/api/books/{bid}").json["wishes"][0]["person_name"] == "Bob"
    assert c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 999}).status_code == 400
    monkeypatch.setattr(X, "wishlist_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert c.get("/admin/api/wishlist/people").status_code == 502


def test_books_not_indexed_yet(c):
    empty = c.post("/admin/api/books", json={"title": "Unindexed test book"}).json["id"]
    gone = c.post("/admin/api/books", json={"title": "Unindexed but gone", "status": "gone"}).json["id"]
    some = c.post("/admin/api/books", json={"title": "Indexed test book"}).json["id"]
    c.post(f"/admin/api/books/{some}/recipes", json={"name": "Only recipe", "page": "1"})
    listed = {b["id"] for b in c.get("/admin/api/books?unindexed=1").json["books"]}
    assert empty in listed and gone not in listed and some not in listed
    assert all(b["recipe_count"] == 0 for b in c.get("/admin/api/books?unindexed=1").json["books"])
    call = lambda args: A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "search_books", "arguments": args}}).json["result"]["structuredContent"]["books"]
    none = {b["id"] for b in call({"indexed": False, "limit": 200})}
    assert empty in none and some not in none and all(b["recipe_count"] == 0 for b in call({"indexed": False, "limit": 200}))
    assert none == {b["id"] for b in call({"indexed": "false", "limit": 200})}
    with_recipes = {b["id"] for b in call({"indexed": True, "limit": 200})}
    assert some in with_recipes and empty not in with_recipes
    for bid in (empty, gone, some):
        c.delete(f"/admin/api/books/{bid}")
