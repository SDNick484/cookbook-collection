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
    con = sqlite3.connect(path)
    con.create_function("fold", 1, A.fold, deterministic=True)
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
