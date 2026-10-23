"""Recipes from websites (NYT Cooking and others), saved by their links and kept for editors only."""
import json, os, sqlite3, sys, tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import extras as X  # noqa: E402
import online as O  # noqa: E402

NYT = "https://cooking.nytimes.com/recipes/1017256-french-onion-soup"


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture()
def clean_online():
    yield
    con = A.connect()
    for (bid,) in con.execute("SELECT id FROM books WHERE kind='online'").fetchall():
        con.execute("DELETE FROM books WHERE id=?", (bid,))
        con.execute("DELETE FROM books_fts WHERE rowid=?", (bid,))
    con.commit()
    con.close()


def test_parse_link():
    p = O.parse_link(NYT + "?action=click&module=RecipeBox#comments")
    assert p == {"url": NYT, "site": "cooking.nytimes.com", "site_name": "NYT Cooking", "ext_id": "1017256",
                 "title": "French Onion Soup"}
    assert O.parse_link("https://cooking.nytimes.com/recipes/1023456-spicy-chili-with-black-beans")["title"] == \
        "Spicy Chili with Black Beans"
    assert O.parse_link("https://cooking.nytimes.com/recipes/1017256")["ext_id"] == "1017256"   # no slug: same recipe
    for not_recipe in ("https://cooking.nytimes.com/recipe-box", "https://cooking.nytimes.com/68861692-nyt-cooking/1",
                       "https://cooking.nytimes.com/search?q=chili", "ftp://x.com/a", "not a link", "https://example.com/"):
        assert O.parse_link(not_recipe) is None, not_recipe
    se = O.parse_link("https://www.seriouseats.com/best-chili-recipe-11729/")
    assert se["site_name"] == "Serious Eats" and se["title"] == "Best Chili" and se["ext_id"] == "seriouseats.com/best-chili-recipe-11729"
    assert O.parse_link("https://m.bonappetit.com/recipe/bas-best-chocolate-chip-cookies")["title"] == "Bas Best Chocolate Chip Cookies"
    other = O.parse_link("https://someblog.example/2024/05/grandmas-pie.html")
    assert other["site_name"] == "someblog.example" and other["title"] == "Grandmas Pie"


def test_links_in_text():
    text = f"Saved: {NYT}, and again ({NYT}?smid=share) plus https://cooking.nytimes.com/recipe-box and\n" \
           "https://www.seriouseats.com/best-chili-recipe-11729"
    found, skipped = O.links_in(text)
    assert [f["ext_id"] for f in found] == ["1017256", "seriouseats.com/best-chili-recipe-11729"]
    assert skipped == ["https://cooking.nytimes.com/recipe-box"]


def test_save_from_links(c, clean_online):
    pv = c.post("/admin/api/online/preview", json={"text": f"{NYT}\nhttps://cooking.nytimes.com/recipes/1019905-ground-beef-chili"}).json
    assert [x["title"] for x in pv["items"]] == ["French Onion Soup", "Ground Beef Chili"]
    assert all(x["recipe_id"] is None for x in pv["items"])
    items = [{"url": NYT, "title": "Classic French Onion Soup", "want_to_try": True},
             {"url": "https://cooking.nytimes.com/recipes/1019905-ground-beef-chili"},
             {"url": "https://cooking.nytimes.com/recipe-box"}]                                # ignored
    r = c.post("/admin/api/online/commit", json={"items": items}).json
    assert r["added"] == 2 and r["duplicates"] == 0 and [b["title"] for b in r["books"]] == ["NYT Cooking"]
    bid = r["books"][0]["id"]
    book = c.get(f"/admin/api/books/{bid}").json
    assert book["kind"] == "online" and book["site"] == "cooking.nytimes.com"
    got = {x["name"]: x for x in book["recipes"]}
    assert got["Classic French Onion Soup"]["url"] == NYT and got["Classic French Onion Soup"]["want_to_try"] == 1
    assert got["Classic French Onion Soup"]["page"] is None
    # saving again, or the same recipe by another form of its link, adds nothing
    again = c.post("/admin/api/online/commit", json={"items": [{"url": NYT + "?smid=ck-recipe-iOS-share"},
                                                               {"url": "https://cooking.nytimes.com/recipes/1019905"}]}).json
    assert again["added"] == 0 and again["duplicates"] == 2
    assert all(x["recipe_id"] for x in c.post("/admin/api/online/preview", json={"text": NYT}).json["items"])
    assert c.post("/admin/api/online/commit", json={"items": []}).status_code == 400
    # a second site gets its own entry
    se = c.post("/admin/api/online/commit", json={"items": [{"url": "https://www.seriouseats.com/best-chili-recipe-11729"}]}).json
    assert se["books"][0]["title"] == "Serious Eats" and se["books"][0]["id"] != bid
    # found by search, ingredient (title) and want-to-try, with the link
    hit = c.get("/admin/api/recipes?q=onion soup").json["recipes"]
    assert any(h["url"] == NYT and h["book_title"] == "NYT Cooking" for h in hit)
    assert any(h["book_id"] == bid for h in c.get("/admin/api/recipes?ingredient=beef&limit=500").json["recipes"])
    assert any(h["url"] == NYT for h in c.get("/admin/api/recipes?want=1&limit=500").json["recipes"])
    # editors only
    assert c.post("/admin/api/online/preview", json={"text": NYT}, headers={"Cf-Ray": "x"}).status_code == 401
    assert c.get(f"/api/books/{bid}").status_code == 404
    assert all(b["id"] != bid for b in c.get("/api/books").json["books"])
    assert any(b["id"] == bid for b in c.get("/admin/api/books").json["books"])
    assert not any(h.get("book_id") == bid for h in c.get("/api/recipes?q=onion soup").json["recipes"])
    assert all(b["id"] != bid for b in c.get("/api/recipes/books?ingredient=beef").json["books"])
    pub_recipes = c.get("/api/facets").json["stats"]["recipes"]
    assert c.get("/admin/api/facets").json["stats"]["recipes"] == pub_recipes + 3
    # not counted as cookbooks
    s = c.get("/admin/api/stats").json
    assert all(b["name"] != "NYT Cooking" for b in s["by_publisher"])
    assert {o["title"]: o["recipes"] for o in s["online"]} == {"NYT Cooking": 2, "Serious Eats": 1}
    assert c.get("/admin/api/facets").json["stats"]["books"] == c.get("/api/facets").json["stats"]["books"]


def test_mcp(c, clean_online, monkeypatch):
    c.post("/admin/api/online/commit", json={"items": [{"url": NYT}]})
    call = lambda path, args, name="search_recipes": A.app.test_client().post(path, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).json["result"]
    token = "t0ken"
    monkeypatch.setattr(X, "MCP_TOKEN", token)
    priv = call(f"/mcp/{token}", {"query": "french onion soup"})["structuredContent"]["recipes"]
    assert any(r.get("url") == NYT and r["book_title"] == "NYT Cooking" for r in priv)
    pub = call("/mcp", {"query": "french onion soup"})["structuredContent"]["recipes"]
    assert not any(r.get("url") for r in pub)
    assert call("/mcp", {"title": "NYT Cooking"}, "get_book").get("isError")
    init = A.app.test_client().post(f"/mcp/{token}", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).json
    assert "url instead of a page" in init["result"]["instructions"]


def test_dinner_log_credits_saved_links(c, clean_online, monkeypatch, tmp_path):
    r = c.post("/admin/api/online/commit", json={"items": [{"url": NYT}]}).json
    bid = r["books"][0]["id"]
    rid = c.get(f"/admin/api/books/{bid}").json["recipes"][0]["id"]
    path = tmp_path / "meals.db"
    dl = sqlite3.connect(path)
    dl.executescript("""CREATE TABLE meals (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, name TEXT NOT NULL,
      source_type TEXT NOT NULL DEFAULT 'original', source_title TEXT NOT NULL DEFAULT '',
      source_detail TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, rating INTEGER);""")
    today = date.today().isoformat()
    dl.execute("INSERT INTO meals(day, name, source_type, source_detail, rating) VALUES (?, 'Onion soup', 'website', ?, 9)",
               (today, NYT + "?smid=share"))
    dl.execute("INSERT INTO meals(day, name, source_type, source_detail) VALUES (?, 'Takeout', 'website', 'https://example.com/x')",
               (today,))
    dl.commit()
    dl.close()
    monkeypatch.setattr(X, "DINNERLOG_DB", str(path))
    con = A.connect()
    X.sync_dinnerlog(con, force=True)
    rows = con.execute("SELECT book_id, recipe_id, how FROM cooked").fetchall()
    con.close()
    assert [tuple(x) for x in rows] == [(bid, rid, "auto")]          # the unrelated website dinner is left out
    rec = c.get(f"/admin/api/books/{bid}").json["recipes"][0]
    assert rec["cooked_count"] == 1
