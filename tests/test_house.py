"""House recipes: the house book, visibility, recipe details, quantity parsing, return URLs, and every read path's
visibility check (pages/JSON API, MCP, Ask's tools, search, counts)."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import extras as X  # noqa: E402
import house as H  # noqa: E402

ANON = {"REMOTE_ADDR": "203.0.113.9"}  # not in LAN_CIDRS and no Access token: not an editor


@pytest.fixture()
def c():
    return A.app.test_client()


def q(sql, *args):
    con = A.connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def house_id():
    return q("SELECT id FROM books WHERE kind='house'")[0][0]


def add_house_recipe(name, items=(), steps=()):
    """A house recipe written straight to the database (connect() registers fold() for the FTS triggers)."""
    con = A.connect()
    rid = con.execute("INSERT INTO recipes(book_id, name, source) VALUES (?, ?, 'house')", (house_id(), name)).lastrowid
    con.execute("INSERT INTO recipe_details(recipe_id, description) VALUES (?, 'ours')", (rid,))
    for i, item in enumerate(items):
        con.execute("INSERT INTO recipe_ingredient_lines(recipe_id, position, item) VALUES (?,?,?)", (rid, i, item))
    for i, text in enumerate(steps):
        con.execute("INSERT INTO recipe_steps(recipe_id, position, text) VALUES (?,?,?)", (rid, i, text))
    con.commit()
    uid = con.execute("SELECT uid FROM recipes WHERE id=?", (rid,)).fetchone()[0]
    con.close()
    return rid, uid


def set_visibility(bid, v):
    con = A.connect()
    con.execute("UPDATE books SET visibility=? WHERE id=?", (v, bid))
    con.commit()
    con.close()


# ------------------------------------------------------------------ the house book
def test_house_book_exists_once_and_is_members_only():
    rows = q("SELECT title, status, visibility, uid FROM books WHERE kind='house'")
    assert len(rows) == 1
    title, status, vis, uid = rows[0]
    assert title == "House Recipes" and status == "owned" and vis == "members" and len(uid) == 32


def test_house_book_is_idempotent():
    bid = house_id()
    con = A.connect()
    assert H.ensure_house_book(con) == bid and H.ensure_house_book(con) == bid
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):  # the partial unique index: a second one can't exist at all
        con.execute("INSERT INTO books(title, kind) VALUES ('Another', 'house')")
    con.rollback()
    con.close()
    A.init_db()  # every start runs it again
    A.init_db()
    assert q("SELECT count(*) FROM books WHERE kind='house'")[0][0] == 1 and house_id() == bid


def test_house_book_keeps_its_title_and_cannot_be_deleted(c, monkeypatch):
    bid = house_id()
    assert c.put(f"/admin/api/books/{bid}", json={"title": "Our Kitchen"}).status_code == 200
    monkeypatch.setattr(H, "HOUSE_BOOK_TITLE", "Something Else")
    A.init_db()
    assert q("SELECT title FROM books WHERE id=?", bid)[0][0] == "Our Kitchen"  # the env var only names a new one
    c.put(f"/admin/api/books/{bid}", json={"title": "House Recipes"})
    r = c.delete(f"/admin/api/books/{bid}")
    assert r.status_code == 409 and q("SELECT count(*) FROM books WHERE id=?", bid)[0][0] == 1
    assert c.put(f"/admin/api/books/{bid}", json={"location_id": 1}).status_code == 400  # not on a shelf


def test_visibility_is_validated(c):
    bid = house_id()
    assert c.put(f"/admin/api/books/{bid}", json={"visibility": "everyone"}).status_code == 400
    assert q("SELECT visibility FROM books WHERE id=?", bid)[0][0] == "members"
    with pytest.raises(sqlite3.IntegrityError):  # and the column's CHECK catches what the API doesn't see
        con = A.connect()
        try:
            con.execute("UPDATE books SET visibility='nobody' WHERE id=?", (bid,))
        finally:
            con.close()


def test_house_title_from_env_on_a_new_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "HOUSE_BOOK_TITLE", "Recettes de la maison")
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    A.init_db()
    con = sqlite3.connect(tmp_path / "cookbooks.db")
    assert con.execute("SELECT title FROM books WHERE kind='house'").fetchall() == [("Recettes de la maison",)]
    assert con.execute("SELECT count(*) FROM books WHERE kind IS NULL").fetchone()[0] >= 153  # still seeded
    con.close()


# ------------------------------------------------------------------ migration
def test_migration_on_a_populated_database(monkeypatch, tmp_path):
    """A catalog from the release before house recipes: seeded, plus an edited book, a recipe website and a shelf.
    Upgrading adds the column, tables and house book, and changes nothing that was there."""
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    real_migrate, real_ensure = H.migrate, H.ensure_house_book
    monkeypatch.setattr(H, "migrate", lambda con: None)
    monkeypatch.setattr(H, "ensure_house_book", lambda con: None)
    monkeypatch.setattr(A, "PUBLIC_BOOKS", "b.kind IS NULL")  # the old release's rule, while it builds its catalog
    A.init_db()
    con = A.connect()
    assert "visibility" not in [r[1] for r in con.execute("PRAGMA table_info(books)")]
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='recipe_details'").fetchone()
    con.execute("INSERT INTO books(title, kind, site, status) VALUES ('NYT Cooking', 'online', 'cooking.nytimes.com', 'owned')")
    con.execute("UPDATE books SET private_notes='kept' WHERE id=1")
    con.commit()
    snap = lambda: (con.execute("SELECT id, uid, title, kind, status, private_notes FROM books ORDER BY id").fetchall(),
                    con.execute("SELECT id, uid, book_id, name, page FROM recipes ORDER BY id").fetchall())
    books_before, recipes_before = [list(map(tuple, x)) for x in snap()]
    con.close()

    monkeypatch.setattr(H, "migrate", real_migrate)
    monkeypatch.setattr(H, "ensure_house_book", real_ensure)
    monkeypatch.setattr(A, "PUBLIC_BOOKS", "((b.kind IS NULL OR b.kind = 'house') AND b.visibility = 'public')")
    A.init_db()
    A.init_db()  # and again: nothing more happens
    con = A.connect()
    books_after, recipes_after = [list(map(tuple, x)) for x in snap()]
    house = [b for b in books_after if b[3] == "house"]
    assert len(house) == 1 and [b for b in books_after if b[3] != "house"] == books_before
    assert recipes_after == recipes_before
    assert con.execute("SELECT count(*) FROM books WHERE visibility <> 'public'").fetchone()[0] == 1  # the house book
    for t in ("recipe_details", "recipe_ingredient_lines", "recipe_steps"):
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
    old_public = con.execute("SELECT count(*) FROM books b WHERE b.kind IS NULL").fetchone()[0]
    assert con.execute(f"SELECT count(*) FROM books b WHERE {A.PUBLIC_BOOKS}").fetchone()[0] == old_public
    con.close()


def test_rollback_sql_from_the_pr_works(tmp_path):
    """The rollback in the PR body: drop what this release added and the old schema is back."""
    path = tmp_path / "copy.db"
    con = A.connect()
    con.execute("VACUUM INTO ?", (str(path),))
    con.close()
    con = sqlite3.connect(path)  # like the sqlite3 command line: no fold() function registered
    con.executescript(H.ROLLBACK_SQL)
    assert "visibility" not in [r[1] for r in con.execute("PRAGMA table_info(books)")]
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name IN "
                           "('recipe_details', 'recipe_ingredient_lines', 'recipe_steps', 'books_one_house')").fetchall()
    assert con.execute("SELECT count(*) FROM books WHERE kind='house'").fetchone()[0] == 0
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


# ------------------------------------------------------------------ visibility, book level
def test_members_only_house_book_is_invisible_to_anonymous_readers(c):
    bid = house_id()
    buid = q("SELECT uid FROM books WHERE id=?", bid)[0][0]
    facets0, total0 = c.get("/api/facets").json["stats"], c.get("/api/books").json["total"]
    ing0 = {i["name"]: i["n"] for i in c.get("/api/ingredients").json["ingredients"]}
    add_house_recipe("Secret Saffron Chicken Stew", items=["saffron", "chicken thighs"])
    # pages and JSON API
    assert all(b["id"] != bid for b in c.get("/api/books").json["books"])
    assert c.get(f"/api/books/{bid}").status_code == 404
    assert c.get(f"/api/books/by-uid/{buid}").status_code == 404
    assert all(b["id"] != bid for b in c.get("/api/books?q=house recipes").json["books"])
    assert c.get("/api/recipes?q=saffron").json == {"recipes": [], "count": 0}
    assert c.get("/api/recipes?q=secret").json["count"] == 0
    assert c.get(f"/api/recipes?book={bid}").json["count"] == 0
    assert c.get("/api/recipes/books?q=saffron").json["books"] == []
    # counts: nothing moved
    assert c.get("/api/facets").json["stats"] == facets0 and c.get("/api/books").json["total"] == total0
    assert {i["name"]: i["n"] for i in c.get("/api/ingredients").json["ingredients"]} == ing0
    # editors see it
    assert any(b["id"] == bid for b in c.get("/admin/api/books").json["books"])
    assert c.get(f"/admin/api/books/{bid}").json["recipes"]
    assert c.get("/admin/api/recipes?q=secret").json["count"] == 1


def test_anonymous_requests_to_admin_routes_are_refused(c):
    bid = house_id()
    for path in ("/admin/api/books", f"/admin/api/books/{bid}", "/admin/api/recipes?q=secret", "/admin/api/facets"):
        assert c.get(path, environ_base=ANON).status_code == 401, path
    assert c.post("/admin/api/ask", json={"thread": [{"q": "saffron?"}]}, environ_base=ANON).status_code == 401


def mcp(c, name, args, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, headers=headers)
    return r.json["result"]


def test_public_mcp_does_not_show_members_only_books(c):
    bid = house_id()
    add_house_recipe("Secret Fennel Gratin", items=["fennel bulbs"])
    res = mcp(c, "search_recipes", {"query": "secret"})["structuredContent"]
    assert res["total"] == 0
    res = mcp(c, "get_book", {"book_id": bid})
    assert res["isError"] and "No such book" in res["content"][0]["text"]
    assert mcp(c, "get_book", {"title": "House Recipes"})["isError"]
    assert all(b["id"] != bid for b in mcp(c, "search_books", {"query": "house"})["structuredContent"]["books"])
    cats = mcp(c, "list_categories", {})["structuredContent"]
    assert cats["totals"]["books"] == q(f"SELECT count(*) FROM books b WHERE {A.COOKBOOKS}")[0][0]
    assert cats["totals"]["recipes"] == q(f"SELECT count(*) FROM recipes r JOIN books b ON b.id=r.book_id "
                                          f"WHERE {A.PUBLIC_BOOKS}")[0][0]


def test_online_books_stay_private_whatever_their_visibility(c):
    con = A.connect()
    bid = con.execute("INSERT INTO books(title, kind, site, status) VALUES ('A Site', 'online', 'site.example', 'owned')").lastrowid
    con.commit()
    con.close()
    assert q("SELECT visibility FROM books WHERE id=?", bid)[0][0] == "public"
    assert c.get(f"/api/books/{bid}").status_code == 404
    assert all(b["id"] != bid for b in c.get("/api/books").json["books"])


def test_a_public_house_book_is_readable_and_a_members_cookbook_is_not(c):
    bid = house_id()
    set_visibility(bid, "public")
    try:
        assert c.get(f"/api/books/{bid}").status_code == 200
        assert c.get(f"/api/books/{bid}").json["kind"] == "house"
        assert c.get("/api/recipes?q=secret").json["count"] >= 1
    finally:
        set_visibility(bid, "members")
    cookbook = q("SELECT id FROM books WHERE kind IS NULL ORDER BY id LIMIT 1")[0][0]
    set_visibility(cookbook, "members")
    try:
        assert c.get(f"/api/books/{cookbook}").status_code == 404
        assert all(b["id"] != cookbook for b in c.get("/api/books").json["books"])
    finally:
        set_visibility(cookbook, "public")


# ------------------------------------------------------------------ export / import of the house book
def test_restore_maps_the_house_book_onto_this_catalogs_own(c, monkeypatch, tmp_path):
    """A merge import into a catalog that already has a house book (with another uid) updates that one instead of
    failing on the one-house-book index, and takes the file's uid; --replace never deletes it."""
    import importer
    data = json.loads(c.get("/admin/api/export.json").data)
    house = next(b for b in data["books"] if b.get("kind") == "house")
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    A.init_db()  # seeded, with a house book of its own
    con = A.connect()
    assert con.execute("SELECT uid FROM books WHERE kind='house'").fetchone()[0] != house["uid"]
    rep = importer.import_data(con, {"version": A.EXPORT_VERSION, "books": [house]})
    assert not rep.conflicts and rep.counts["books"]["insert"] == 0
    assert [r[0] for r in con.execute("SELECT uid FROM books WHERE kind='house'")] == [house["uid"]]
    rep = importer.import_data(con, {"version": A.EXPORT_VERSION, "books": [data["books"][0]]}, replace=True)
    assert con.execute("SELECT count(*) FROM books WHERE kind='house'").fetchone()[0] == 1
    assert all(t != house["title"] for _, t in rep.deleted_books)
    con.close()


# ------------------------------------------------------------------ quantity parsing
@pytest.mark.parametrize("text,expected", [
    ("2", (2, None)), ("12", (12, None)), ("0.25", (0.25, None)), ("1.5", (1.5, None)), ("1,5", (1.5, None)),
    ("1,500", (1500, None)), ("1/2", (0.5, None)), ("3/4", (0.75, None)), ("1 1/2", (1.5, None)),
    ("1  1/2", (1.5, None)), ("1-1/2", (1.5, None)), ("2 3/4", (2.75, None)),
    ("½", (0.5, None)), ("1½", (1.5, None)), ("1 ½", (1.5, None)), ("⅓", (0.3333, None)), ("2⅔", (2.6667, None)),
    ("1⁄2", (0.5, None)),  # FRACTION SLASH, what some keyboards and pastes give
    ("2-3", (2, 3)), ("2–3", (2, 3)), ("2 — 3", (2, 3)), ("2 to 3", (2, 3)), ("2 or 3", (2, 3)),
    ("1/2-1", (0.5, 1)), ("1 1/2-2", (1.5, 2)), ("½-¾", (0.5, 0.75)), ("3-3", (3, None)),
    ("about 2", (2, None)), ("approx. 250", (250, None)), ("~3", (3, None)), ("ca. 1,5", (1.5, None)),
    ("a", (1, None)), ("one", (1, None)), ("Two", (2, None)), ("dozen", (12, None)), ("half", (0.5, None)),
    ("2.", (2, None)), ("  3  ", (3, None)),
])
def test_parse_quantity(text, expected):
    assert H.parse_quantity(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "a pinch", "to taste", "1 (14-oz)", "1 can", "pinch", "some", "3-2", "0", "0/0", "1/0", "-2",
    "1..5", "2 3", "x" * 41, "9999999", "1e3", "½ cup",
])
def test_parse_quantity_leaves_text_alone(text):
    assert H.parse_quantity(text) == (None, None)


# ------------------------------------------------------------------ write API
DOC = {"name": "Weeknight Ragù", "description": "Our Tuesday sauce.", "yield_text": "about 1 litre", "servings": "4",
       "prep_minutes": 15, "cook_minutes": "90", "methods": ["braise", "Braise", "simmer"], "equipment": "Dutch oven, ",
       "inspiration": "a trattoria near the market", "notes": "Better the next day.",
       "ingredients": [
           {"section": "Soffritto", "quantity": "1 ½", "unit": "tbsp", "item": "olive oil"},
           {"section": "Soffritto", "quantity": "2-3", "item": "carrots", "prep_note": "finely diced"},
           {"quantity": "a pinch", "item": "nutmeg", "optional": True},
           {"quantity": "", "unit": "", "item": "", "prep_note": ""},  # the editor's empty last row
           {"quantity": "500", "unit": "g", "item": "ground beef"}],
       "steps": [{"section": "Soffritto", "text": "Sweat the vegetables.", "timer_minutes": "10"},
                 "Add the beef and brown it.", {"text": "  "},
                 {"text": "Simmer, covered.", "timer_minutes": 90}]}


def test_create_and_read_a_house_recipe(c):
    r = c.post("/admin/api/house/recipes", json=DOC)
    assert r.status_code == 201, r.json
    j = r.json
    assert j["title"] == j["name"] == "Weeknight Ragù" and j["book"]["kind"] == "house" and len(j["uid"]) == 32
    d = j["detail"]
    assert d["servings"] == 4 and d["cook_minutes"] == 90 and d["methods"] == ["braise", "simmer"]
    assert d["equipment"] == ["Dutch oven"] and d["updated_at"]
    assert [(x["quantity_text"], x["quantity_num"], x["quantity_max"], x["item"]) for x in j["ingredients"]] == [
        ("1 ½", 1.5, None, "olive oil"), ("2-3", 2, 3, "carrots"), ("a pinch", None, None, "nutmeg"),
        ("500", 500, None, "ground beef")]
    assert j["ingredients"][2]["optional"] is True and j["ingredients"][1]["prep_note"] == "finely diced"
    assert [(s["text"], s["timer_minutes"]) for s in j["steps"]] == [
        ("Sweat the vegetables.", 10), ("Add the beef and brown it.", None), ("Simmer, covered.", 90)]
    assert c.get(f"/admin/api/recipes/by-uid/{j['uid']}").json["ingredients"] == j["ingredients"]
    book = c.get(f"/admin/api/books/{house_id()}").json
    assert next(x for x in book["recipes"] if x["id"] == j["id"])["has_detail"] is True


def test_update_replaces_reorders_and_removes(c):
    j = c.post("/admin/api/house/recipes", json=DOC).json
    new = {**DOC, "name": "Sunday Ragù", "servings": None, "methods": [],
           "ingredients": [DOC["ingredients"][4], DOC["ingredients"][0]], "steps": ["Only step."]}
    r = c.put(f"/admin/api/house/recipes/{j['id']}", json=new)
    assert r.status_code == 200, r.json
    assert r.json["name"] == "Sunday Ragù" and r.json["detail"]["servings"] is None and r.json["detail"]["methods"] == []
    assert [x["item"] for x in r.json["ingredients"]] == ["ground beef", "olive oil"]
    assert [s["text"] for s in r.json["steps"]] == ["Only step."]
    rows = q("SELECT position, item FROM recipe_ingredient_lines WHERE recipe_id=? ORDER BY position", j["id"])
    assert [tuple(x) for x in rows] == [(0, "ground beef"), (1, "olive oil")]
    assert c.delete(f"/admin/api/recipes/{j['id']}").status_code == 200  # the existing delete; details cascade
    for t in ("recipe_details", "recipe_ingredient_lines", "recipe_steps"):
        assert q(f"SELECT count(*) FROM {t} WHERE recipe_id=?", j["id"])[0][0] == 0


@pytest.mark.parametrize("bad,msg", [
    ({**DOC, "name": "  "}, "needs a name"),
    ({**DOC, "servings": 0}, "Servings"),
    ({**DOC, "prep_minutes": "soon"}, "Prep minutes"),
    ({**DOC, "ingredients": [{"quantity": "2", "unit": "cups"}]}, "needs an ingredient name"),
    ({**DOC, "ingredients": "flour"}, "must be lists"),
    ({**DOC, "steps": [{"text": "", "timer_minutes": 5}]}, "timer but no text"),
    ({**DOC, "steps": [{"text": "x", "timer_minutes": 99999}]}, "timer"),
    ({**DOC, "description": "x" * 4001}, "too long"),
    ({**DOC, "ingredients": [{"item": "salt"}] * 201}, "At most"),
    ([1, 2], "JSON object"),
])
def test_bad_documents_are_refused_with_a_reason(c, bad, msg):
    before = q("SELECT count(*) FROM recipes")[0][0]
    r = c.post("/admin/api/house/recipes", json=bad)
    assert r.status_code == 400 and msg in r.json["error"]
    assert q("SELECT count(*) FROM recipes")[0][0] == before


def test_details_only_on_house_recipes(c):
    rid = q("SELECT r.id FROM recipes r JOIN books b ON b.id=r.book_id WHERE b.kind IS NULL LIMIT 1")[0][0]
    assert c.put(f"/admin/api/house/recipes/{rid}", json=DOC).status_code == 400
    assert c.put("/admin/api/house/recipes/99999999", json=DOC).status_code == 404


def test_writes_need_an_editor(c, monkeypatch):
    j = c.post("/admin/api/house/recipes", json=DOC).json
    assert c.post("/admin/api/house/recipes", json=DOC, environ_base=ANON).status_code == 401
    assert c.put(f"/admin/api/house/recipes/{j['id']}", json=DOC, environ_base=ANON).status_code == 401
    assert c.post("/api/house/recipes", json=DOC).status_code == 405  # no public write route at all
    evil = {"Origin": "https://evil.example"}
    assert c.post("/admin/api/house/recipes", json=DOC, headers=evil).status_code == 403  # the CSRF check
    monkeypatch.setattr(A, "READ_TOKEN", "s3cret")
    bearer = {"Authorization": "Bearer s3cret"}
    assert c.post("/admin/api/house/recipes", json=DOC, headers=bearer, environ_base=ANON).status_code == 401
    assert c.get("/admin/api/house/recipes", headers=bearer, environ_base=ANON).status_code == 200


# ------------------------------------------------------------------ return URLs
ORIGINS = H.parse_origins("https://dinner.example.com, http://192.0.2.5:8000/, https://Other.Example:443, "
                          "ftp://x.example, https://bad.example/path, https://u:p@cred.example")


def test_parse_origins_keeps_only_bare_origins():
    assert ORIGINS == {"https://dinner.example.com", "http://192.0.2.5:8000", "https://other.example"}


@pytest.mark.parametrize("url,expected", [
    ("https://dinner.example.com/new?name=Rag%C3%B9", "https://dinner.example.com/new?name=Rag%C3%B9"),
    ("https://dinner.example.com", "https://dinner.example.com/"),
    ("https://DINNER.example.com:443/meals/3#top", "https://dinner.example.com/meals/3#top"),
    ("http://192.0.2.5:8000/new", "http://192.0.2.5:8000/new"),
    ("https://other.example/x", "https://other.example/x"),
    ("/admin#/book/3", "/admin#/book/3"),
    ("/", "/"),
])
def test_return_urls_allowed(url, expected):
    assert H.safe_return_url(url, ORIGINS) == expected


@pytest.mark.parametrize("url", [
    "https://evil.example/", "http://dinner.example.com/new",  # wrong scheme for that origin
    "https://dinner.example.com.evil.example/", "https://evil.example/?https://dinner.example.com",
    "https://dinner.example.com@evil.example/", "https://user:pw@dinner.example.com/",
    "https://dinner.example.com:8443/", "http://192.0.2.5/new",
    "//evil.example/x", "///evil.example", "/\\evil.example", "\\\\evil.example", "https:\\\\dinner.example.com",
    "javascript:alert(1)", "data:text/html,hi", "JaVaScRiPt:alert(1)", "dinner.example.com/new", "new",
    "https://dinner.example.com/a\nb", "https://dinner.example.com/a b", "https://dinner.example.com/\x00",
    " https://dinner.example.com/", "https://dinner.example.com:99999/", "https://[::1/", "",
    None, 42, "https://dinner.example.com/" + "a" * 2048,
])
def test_return_urls_refused(url):
    assert H.safe_return_url(url, ORIGINS) is None


def test_save_follows_only_an_allowed_return_url(c, monkeypatch):
    monkeypatch.setattr(H, "RETURN_URL_ORIGINS", {"https://dinner.example.com"})
    j = c.post("/admin/api/house/recipes", json={**DOC, "return": "https://dinner.example.com/new?day=2026-11-09"}).json
    assert j["return_refused"] is False
    assert j["return_url"] == (f"https://dinner.example.com/new?day=2026-11-09&cookbook_book_uid={j['book']['uid']}"
                               f"&cookbook_recipe_uid={j['uid']}")
    j = c.post("/admin/api/house/recipes", json={**DOC, "return": "https://evil.example/"}).json
    assert j["return_url"] is None and j["return_refused"] is True  # saved anyway; just not redirected
    assert "return_url" not in c.post("/admin/api/house/recipes", json=DOC).json
    assert c.get("/admin/api/house/return-check?url=https://dinner.example.com/new").json == \
        {"ok": True, "host": "dinner.example.com"}
    assert c.get("/admin/api/house/return-check?url=//evil.example").json == {"ok": False, "host": None}
    assert c.get("/admin/api/house/return-check?url=/x", environ_base=ANON).status_code == 401


# ------------------------------------------------------------------ read API for Dinner Log
def test_house_read_api_is_members_only(c, monkeypatch):
    j = c.post("/admin/api/house/recipes", json={**DOC, "name": "Lemon Pasta Our Way"}).json
    assert c.get("/api/house").status_code == 404
    assert c.get("/api/house/recipes").status_code == 404
    assert c.get(f"/api/house/recipes/{j['uid']}").status_code == 404
    assert c.get(f"/api/recipes/by-uid/{j['uid']}").status_code == 404
    assert c.get("/admin/api/house/recipes", environ_base=ANON).status_code == 401
    monkeypatch.setattr(A, "READ_TOKEN", "s3cret")
    hdr = {"Authorization": "Bearer s3cret"}
    lst = c.get("/admin/api/house/recipes?limit=500", headers=hdr, environ_base=ANON).json
    mine = next(x for x in lst["recipes"] if x["uid"] == j["uid"])
    assert set(mine) == {"id", "uid", "title", "updated_at"} and mine["title"] == "Lemon Pasta Our Way"
    assert lst["book"]["uid"] == q("SELECT uid FROM books WHERE kind='house'")[0][0]
    one = c.get(f"/admin/api/house/recipes/{j['uid']}", headers=hdr, environ_base=ANON).json
    assert one["uid"] == j["uid"] and one["ingredients"] and one["steps"]
    assert c.get("/admin/api/house/recipes/" + "0" * 32, headers=hdr, environ_base=ANON).status_code == 404
    assert c.get("/admin/api/house/recipes/not-a-uid", headers=hdr, environ_base=ANON).status_code == 404
    cookbook_recipe = q("SELECT r.uid FROM recipes r JOIN books b ON b.id=r.book_id WHERE b.kind IS NULL LIMIT 1")[0][0]
    assert c.get(f"/admin/api/house/recipes/{cookbook_recipe}", headers=hdr, environ_base=ANON).status_code == 404
    assert c.get(f"/api/recipes/by-uid/{cookbook_recipe}").status_code == 200  # public cookbook recipe: readable
    assert c.get(f"/api/recipes/by-uid/{cookbook_recipe}").json["ingredients"] == []
    assert c.get("/admin/api/house/recipes?q=lemon", headers=hdr, environ_base=ANON).json["recipes"][0]["uid"] == j["uid"]
    set_visibility(house_id(), "public")
    try:
        assert c.get(f"/api/house/recipes/{j['uid']}").status_code == 200
        assert "rating" not in c.get(f"/api/house/recipes/{j['uid']}").json  # private recipe fields stay private
        assert any(x["uid"] == j["uid"] for x in c.get("/api/house/recipes?limit=500").json["recipes"])
        assert "visibility" not in c.get("/api/house").json
    finally:
        set_visibility(house_id(), "members")


def test_house_list_pages(c):
    for n in range(3):
        c.post("/admin/api/house/recipes", json={"name": f"Paging test {n}"})
    first = c.get("/admin/api/house/recipes?limit=2").json
    assert len(first["recipes"]) == 2 and first["next_offset"] == 2 and first["count"] >= 3
    assert c.get("/admin/api/house/recipes?limit=x").status_code == 400


# ------------------------------------------------------------------ export / import of details
def test_export_and_import_carry_ingredients_and_steps(c, monkeypatch, tmp_path):
    import importer
    j = c.post("/admin/api/house/recipes", json={**DOC, "name": "Round Trip Soup"}).json
    data = json.loads(c.get("/admin/api/export.json").data)
    house = next(b for b in data["books"] if b.get("kind") == "house")
    rec = next(r for r in house["recipes"] if r["uid"] == j["uid"])
    assert [x["item"] for x in rec["detail"]["ingredients"]] == ["olive oil", "carrots", "nutmeg", "ground beef"]
    plain = next(r for b in data["books"] if not b.get("kind") for r in b["recipes"])
    assert "detail" not in plain
    monkeypatch.setenv("SKIP_SEED", "1")
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    A.init_db()
    con = A.connect()
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == 0  # a restore target starts empty
    bad = {**rec, "uid": "a" * 32, "name": "Broken", "detail": {"ingredients": [{"quantity": "1"}]}}
    misplaced = {**plain, "uid": "b" * 32, "detail": rec["detail"]}
    rep = importer.import_data(con, {"version": A.EXPORT_VERSION,
                                     "books": [{**house, "recipes": [rec, bad]}, {**data["books"][0], "recipes": [misplaced]}]})
    rid = con.execute("SELECT id FROM recipes WHERE uid=?", (j["uid"],)).fetchone()[0]
    assert con.execute("SELECT uid FROM books WHERE kind='house'").fetchone()[0] == house["uid"]
    assert [r[0] for r in con.execute("SELECT item FROM recipe_ingredient_lines WHERE recipe_id=? ORDER BY position",
                                      (rid,))] == ["olive oil", "carrots", "nutmeg", "ground beef"]
    assert con.execute("SELECT quantity_num, quantity_max FROM recipe_ingredient_lines WHERE recipe_id=? AND position=1",
                       (rid,)).fetchone()[:] == (2, 3)
    assert con.execute("SELECT updated_at FROM recipe_details WHERE recipe_id=?", (rid,)).fetchone()[0] == \
        rec["detail"]["updated_at"]
    assert any("Broken" in w and "ingredient name" in w for w in rep.warnings)
    assert any("isn't in the house book" in w for w in rep.warnings)
    con.close()


# ------------------------------------------------------------------ search, MCP and Ask find house recipes
SEARCH_DOC = {"name": "Grandpa's Sunday Gravy", "ingredients": [
    {"quantity": "2", "unit": "lb", "item": "pork neck bones"}, {"quantity": "1", "item": "can San Marzano tomatoes"},
    {"quantity": "1", "unit": "cup", "item": "chicken stock"}], "steps": ["Brown the pork.", "Simmer all day."]}


@pytest.fixture()
def gravy(c):
    j = c.post("/admin/api/house/recipes", json=SEARCH_DOC).json
    yield j
    c.delete(f"/admin/api/recipes/{j['id']}")


def test_site_search_finds_house_recipes_by_ingredient(c, gravy):
    for query in ("q=marzano", "q=pork neck", "q=pork gravy", "ingredient=pork", "ingredient=tomatoes"):
        hits = c.get(f"/admin/api/recipes?{query}&limit=500").json["recipes"]
        assert any(r["id"] == gravy["id"] for r in hits), query
    assert all(r["id"] != gravy["id"] for r in c.get("/api/recipes?q=marzano&limit=500").json["recipes"])
    assert all(r["id"] != gravy["id"] for r in c.get("/api/recipes?ingredient=pork&limit=500").json["recipes"])
    assert all(b["id"] != house_id() for b in c.get("/api/recipes/books?ingredient=pork").json["books"])


def test_ranking_explains_an_ingredient_list_match(gravy):
    import ingredients as I
    con = A.connect()
    try:
        base = " FROM recipes r JOIN books b ON b.id=r.book_id WHERE r.id=?"
        (pork,) = I.ranked(con, base, [gravy["id"]], ["pork"])
        assert pork["score"] == 1.5 and pork["match"] == "pork: ingredients"
        (chicken,) = I.ranked(con, base, [gravy["id"]], ["chicken"])  # only as stock: ranks like a minor tag
        assert chicken["score"] == 0.25 and "may be only chicken stock" in chicken["match"]
        (gravy_hit,) = I.ranked(con, base, [gravy["id"]], ["gravy", "pork"])
        assert gravy_hit["score"] == 3.5 and gravy_hit["match"] == "gravy: title, pork: ingredients"
    finally:
        con.close()


def test_mcp_finds_house_recipes_privately_and_only_privately(c, gravy, monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", "tok")
    res = mcp(c, "search_recipes", {"query": "marzano"}, token="tok")["structuredContent"]
    hit = next(r for r in res["recipes"] if r["id"] == gravy["id"])
    assert hit["full"] is True and "ingredients" in hit["match"]
    res = mcp(c, "search_recipes", {"ingredient": "pork"}, token="tok")["structuredContent"]
    assert any(r["id"] == gravy["id"] for r in res["recipes"])
    full = mcp(c, "get_recipe", {"recipe_id": gravy["id"]}, token="tok")["structuredContent"]
    assert full["ingredients"] == ["2 lb pork neck bones", "1 can San Marzano tomatoes", "1 cup chicken stock"]
    assert full["steps"] == ["1. Brown the pork.", "2. Simmer all day."]
    assert mcp(c, "get_recipe", {"uid": gravy["uid"]}, token="tok")["structuredContent"]["id"] == gravy["id"]
    # the public MCP: not found by search, by id or by uid
    assert all(r["id"] != gravy["id"] for r in mcp(c, "search_recipes", {"query": "marzano"})["structuredContent"]["recipes"])
    assert mcp(c, "get_recipe", {"recipe_id": gravy["id"]})["isError"]
    assert mcp(c, "get_recipe", {"uid": gravy["uid"]})["isError"]
    assert mcp(c, "get_recipe", {})["isError"]
    cookbook = q("SELECT r.id FROM recipes r JOIN books b ON b.id=r.book_id WHERE b.kind IS NULL AND r.page IS NOT NULL "
                 "LIMIT 1")[0][0]
    page = mcp(c, "get_recipe", {"recipe_id": cookbook})["structuredContent"]
    assert page["page"] and "in the book" in page["note"] and "ingredients" not in page


class Script:
    def __init__(self, items):
        self.items = list(items)

    def __call__(self, body, key, timeout):
        item = self.items.pop(0)
        return item(body) if callable(item) else item


def test_ask_finds_house_recipes_by_ingredient(c, gravy, monkeypatch):
    import ask
    monkeypatch.setattr(ask, "SEARCH_KEY", "k")

    def finish(body):
        found = json.loads(body["messages"][-1]["content"][0]["content"])
        assert any(r["id"] == gravy["id"] and r["full"] for r in found["recipes"])
        return {"content": [{"type": "tool_use", "id": "t2", "name": "show_results",
                             "input": {"answer": "Your gravy.", "recipe_ids": [gravy["id"]]}}], "usage": {}}
    monkeypatch.setattr(X, "messages_api", Script([
        {"content": [{"type": "tool_use", "id": "t1", "name": "search_recipes", "input": {"ingredient": "pork"}}],
         "usage": {}}, finish]))
    r = c.post("/admin/api/ask", json={"thread": [{"q": "what uses pork neck?"}]})
    assert r.status_code == 200, r.json
    item = r.json["items"][0]
    assert item["id"] == gravy["id"] and item["has_detail"] is True and item["uid"] == gravy["uid"]


def test_new_recipe_path_serves_the_app(c):
    """Dinner Log links /admin/recipe/new?title=…&return=… (a path, so the query survives the Access sign-in); the
    page itself has no data in it, and the app turns the address into #/recipe/new."""
    r = c.get("/admin/recipe/new?title=Soup&return=https://dinner.example.com/new")
    assert r.status_code == 200 and b"/static/app.js" in r.data


def test_readme_rollback_matches_the_code():
    readme = (A.APP_DIR / "README.md").read_text()
    assert H.ROLLBACK_SQL.strip() in readme
    for var in ("HOUSE_BOOK_TITLE", "RETURN_URL_ORIGINS"):  # documented, on the service's export line, in .env.example
        assert f"`{var}`" in readme
        assert var in (A.APP_DIR / "installer" / "setup-head.sh").read_text().split("export DATA_DIR", 1)[1].split("\n")[0]
        if (A.APP_DIR / ".env.example").exists():  # not shipped by the installer
            assert f"\n{var}=" in (A.APP_DIR / ".env.example").read_text()


def test_photos_of_house_recipes_stay_out_of_the_public_gallery(c):
    rid, _ = add_house_recipe("Photographed Secret Dish")
    con = A.connect()
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, ?)", (rid, "0" * 32 + ".jpg"))
    con.commit()
    con.close()
    assert all(p["recipe_id"] != rid for p in c.get("/api/photos").json["photos"])
    assert any(p["recipe_id"] == rid for p in c.get("/admin/api/photos").json["photos"])
    assert c.get(f"/api/books/{house_id()}").status_code == 404  # and its page (with the photos) isn't public
