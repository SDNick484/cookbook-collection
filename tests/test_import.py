"""The JSON export is a portable backup: export.json -> importer -> the same catalog.

Run: python3 -m pytest -q tests/test_import.py  (conftest.py gives this module its own seeded temp DATA_DIR)
"""
import re

import pytest

import app as A


@pytest.fixture()
def client():
    return A.app.test_client()


def _tag_a_recipe(name="Smoky tomato soup", ingredients=("Tomatoes", "Smoked paprika"), hints=("Paprika",)):
    """One extra recipe carrying ingredient tags, and some tags that are only search hints."""
    con = A.connect()
    bid = con.execute("SELECT id FROM books ORDER BY id LIMIT 1").fetchone()[0]
    rid = con.execute("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?,?,?,?)",
                      (bid, name, "12", 12)).lastrowid
    for n in ingredients:
        iid = A.name_id(con, "ingredients", n)
        con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, iid))
    for n in hints:
        iid = A.name_id(con, "ingredients", n)
        con.execute("UPDATE ingredients SET hint=1 WHERE id=?", (iid,))
    con.commit()
    con.close()
    return rid


# ------------------------------------------------------------------ export format
def test_export_has_version_and_timestamp(client):
    j = client.get("/admin/api/export.json").json
    assert j["version"] == A.EXPORT_VERSION == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", j["exported_at"])
    assert "exported" in j  # the older key stays, so nothing that reads it breaks


def test_export_carries_ingredient_tags(client):
    _tag_a_recipe()
    j = client.get("/admin/api/export.json").json
    recipes = [r for b in j["books"] for r in b["recipes"]]
    soup = next(r for r in recipes if r["name"] == "Smoky tomato soup")
    assert soup["ingredients"] == ["Smoked paprika", "Tomatoes"]
    assert all(isinstance(r["ingredients"], list) for r in recipes)  # [] = untagged, not "old export"
    assert "Paprika" in j["ingredient_hints"]


# ------------------------------------------------------------------ an empty catalog to restore into
@pytest.fixture()
def new_catalog(tmp_path, monkeypatch):
    """Call it to point the app at a new, unseeded catalog in its own folder; monkeypatch puts the seeded one back."""
    import photos

    def activate():
        monkeypatch.setenv("SKIP_SEED", "1")
        monkeypatch.setattr(A, "DATA_DIR", tmp_path)
        monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
        monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
        monkeypatch.setattr(photos, "PHOTO_DIR", tmp_path / "photos")
        A.init_db()
        return tmp_path
    return activate


def test_skip_seed_leaves_a_new_database_empty(new_catalog):
    new_catalog()
    con = A.connect()
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == 0
    con.close()


def test_a_new_database_is_seeded_by_default(tmp_path, monkeypatch):
    import photos
    monkeypatch.delenv("SKIP_SEED", raising=False)
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    monkeypatch.setattr(photos, "PHOTO_DIR", tmp_path / "photos")
    A.init_db()
    con = A.connect()
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] > 100
    con.close()


# ------------------------------------------------------------------ helpers
def catalog(con):
    """Everything the export carries, keyed by uid with the row ids left out, so two databases compare equal
    when they hold the same catalog."""
    out = {}
    for b in con.execute("SELECT * FROM books ORDER BY uid"):
        d = {k: b[k] for k in b.keys() if k != "id"}
        d["authors"] = [r[0] for r in con.execute(
            "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=? ORDER BY ba.pos",
            (b["id"],))]
        d["tags"] = sorted(r[0] for r in con.execute(
            "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=?", (b["id"],)))
        recipes = {}
        for r in con.execute("SELECT * FROM recipes WHERE book_id=?", (b["id"],)):
            rd = {k: r[k] for k in r.keys() if k not in ("id", "book_id")}
            rd["ingredients"] = sorted(x[0] for x in con.execute(
                "SELECT i.name FROM ingredients i JOIN recipe_ingredients ri ON ri.ingredient_id=i.id "
                "WHERE ri.recipe_id=?", (r["id"],)))
            rd["photos"] = sorted(tuple(x) for x in con.execute(
                "SELECT file, taken_on, caption FROM recipe_photos WHERE recipe_id=?", (r["id"],)))
            recipes[r["uid"]] = rd
        d["recipes"] = recipes
        out[b["uid"]] = d
    return out


def hints(con):
    return sorted(r[0] for r in con.execute("SELECT name FROM ingredients WHERE hint=1"))


def export(client):
    return client.get("/admin/api/export.json").json


def decorate_catalog():
    """Put the things a restore must not lose into the seeded catalog: a cooked-and-rated recipe, a want-to-try
    one, ingredient tags and search hints, an accented author, a recipe photo and an online book."""
    _tag_a_recipe()
    con = A.connect()
    b1, b2 = [r[0] for r in con.execute("SELECT id FROM books ORDER BY id LIMIT 2")]
    con.execute("UPDATE books SET rating=4.5, signed=1, status='loaned', loaned_to='A neighbour', location='Kitchen shelf', "
                "year=2011, private_notes='Spine is cracked' WHERE id=?", (b1,))
    A.set_people(con, b1, ["José Andrés", "Second Author"], ["Tapas", "Spanish"])
    rid = con.execute("INSERT INTO recipes(book_id, name, page, page_sort, want_to_try, rating, notes, last_made, source) "
                      "VALUES (?,?,?,?,?,?,?,?, 'manual')",
                      (b1, "Crème brûlée", "88", 88, 1, 9.5, "Less sugar next time", "2026-03-14")).lastrowid
    con.execute("INSERT INTO recipe_photos(recipe_id, file, width, height, taken_on, caption) VALUES (?,?,?,?,?,?)",
                (rid, "0123456789abcdef0123456789abcdef.jpg", 800, 600, "2026-03-14", "First try"))
    ob = con.execute("INSERT INTO books(title, kind, site, source) VALUES ('Example Kitchen', 'online', 'example.com', 'app')").lastrowid
    con.execute("INSERT INTO recipes(book_id, name, url, ext_id, source) VALUES (?, 'Web chili', 'https://example.com/chili', 'chili-1', 'online')", (ob,))
    A.reindex_all_books(con)
    con.commit()
    con.close()


# ------------------------------------------------------------------ importing by uid
def test_export_then_import_into_an_empty_database_reproduces_the_catalog(client, new_catalog):
    import importer
    decorate_catalog()
    con = A.connect()
    before, before_hints = catalog(con), hints(con)
    creme = con.execute("SELECT count(*) FROM recipes_fts WHERE recipes_fts MATCH 'creme'").fetchone()[0]
    con.close()
    data = export(client)
    assert len(before) >= 154 and creme >= 1

    new_catalog()
    con = A.connect()
    rep = importer.import_data(con, data)
    assert rep.counts["books"]["insert"] == len(before) and rep.counts["books"]["update"] == 0
    assert catalog(con) == before          # same uids, fields, authors, tags, ingredients, photos, notes, ratings
    assert hints(con) == before_hints
    # authors and tags go through the app's folding helpers, and the search indexes follow
    assert con.execute("SELECT count(*) FROM books_fts").fetchone()[0] == len(before)
    assert con.execute("SELECT count(*) FROM recipes_fts").fetchone()[0] == \
        con.execute("SELECT count(*) FROM recipes").fetchone()[0]
    assert con.execute("SELECT count(*) FROM recipes_fts WHERE recipes_fts MATCH 'creme'").fetchone()[0] == creme
    con.close()


def test_importing_onto_an_existing_catalog_updates_by_uid_and_never_duplicates(client):
    import importer
    data = export(client)
    con = A.connect()
    n_books = con.execute("SELECT count(*) FROM books").fetchone()[0]
    n_recipes = con.execute("SELECT count(*) FROM recipes").fetchone()[0]
    con.execute("INSERT INTO recipes(book_id, name) SELECT id, 'Added here only' FROM books ORDER BY id LIMIT 1")
    con.commit()

    first, second = data["books"][0], next(b for b in data["books"][1:] if b["recipes"])
    first["title"] = "Renamed in the export"
    first["rating"] = 3.5
    second["recipes"][0]["notes"] = "Notes from the export"
    second["recipes"][0]["want_to_try"] = 1
    second["recipes"].append({"uid": "f" * 32, "name": "Brand new recipe", "page": "7", "rating": 8})

    rep = importer.import_data(con, data)
    assert rep.counts["books"] == {"insert": 0, "update": 1, "skip": n_books - 1, "delete": 0}
    assert rep.counts["recipes"]["insert"] == 1 and rep.counts["recipes"]["update"] == 1
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == n_books
    assert con.execute("SELECT count(*) FROM recipes").fetchone()[0] == n_recipes + 2  # + ours, + the new one
    row = con.execute("SELECT title, rating FROM books WHERE uid=?", (first["uid"],)).fetchone()
    assert (row["title"], row["rating"]) == ("Renamed in the export", 3.5)
    row = con.execute("SELECT notes, want_to_try FROM recipes WHERE uid=?", (second["recipes"][0]["uid"],)).fetchone()
    assert (row["notes"], row["want_to_try"]) == ("Notes from the export", 1)
    assert con.execute("SELECT name, rating FROM recipes WHERE uid=?", ("f" * 32,)).fetchone()[:] == ("Brand new recipe", 8)
    assert con.execute("SELECT count(*) FROM recipes WHERE name='Added here only'").fetchone()[0] == 1  # merge keeps it
    # the book's search index follows its new title
    assert con.execute("SELECT count(*) FROM books_fts WHERE books_fts MATCH 'renamed'").fetchone()[0] == 1

    again = importer.import_data(con, data)  # idempotent: nothing left to do
    for table in ("books", "recipes"):
        assert again.counts[table]["insert"] == again.counts[table]["update"] == 0
    con.close()


def test_a_newer_export_version_is_refused_and_changes_nothing(client):
    import importer
    data = export(client)
    data["version"] = A.EXPORT_VERSION + 1
    data["books"][0]["title"] = "Should not land"
    con = A.connect()
    with pytest.raises(importer.ImportFileError, match="version"):
        importer.import_data(con, data)
    assert con.execute("SELECT count(*) FROM books WHERE title='Should not land'").fetchone()[0] == 0
    con.close()


def test_an_export_without_a_version_is_accepted(client):
    import importer
    data = export(client)
    del data["version"]
    con = A.connect()
    assert importer.import_data(con, data).counts["books"]["insert"] == 0
    con.close()
