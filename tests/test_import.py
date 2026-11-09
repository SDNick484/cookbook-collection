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


# ------------------------------------------------------------------ files without uids (older exports)
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def strip_uids(data):
    for b in data["books"]:
        b.pop("uid", None)
        for r in b["recipes"]:
            r.pop("uid", None)
    return data


def _isbn(n):
    """A valid ISBN-13 that differs for every n, so tests in this module can't collide on the unique ISBN index."""
    core = f"978{n:09d}"
    return core + str((10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10)


def _local_book(con, title, authors, isbn=None, recipes=()):
    bid = con.execute("INSERT INTO books(title, isbn) VALUES (?,?)", (title, isbn)).lastrowid
    A.set_people(con, bid, authors, [])
    for name, page, *ext in recipes:
        con.execute("INSERT INTO recipes(book_id, name, page, page_sort, ext_id) VALUES (?,?,?,?,?)",
                    (bid, name, page, A.page_sort(page), ext[0] if ext else None))
    A.reindex_book(con, bid)
    con.commit()
    return bid


def test_without_uids_every_book_and_recipe_finds_itself(client):
    import importer
    decorate_catalog()
    data = strip_uids(export(client))
    con = A.connect()
    before = catalog(con)
    rep = importer.import_data(con, data)
    for table in ("books", "recipes"):
        assert rep.counts[table]["insert"] == 0 and rep.counts[table]["update"] == 0, (table, rep.conflicts)
    assert catalog(con) == before
    con.close()


def test_without_a_uid_a_book_matches_by_isbn_then_by_folded_title_and_first_author():
    import importer
    con = A.connect()
    x = _local_book(con, "Marmalade Notes", ["Pat Quince"], isbn=_isbn(1))
    y = _local_book(con, "Jam Book", ["Sam Berry"])
    uids = dict(con.execute("SELECT id, uid FROM books WHERE id IN (?,?)", (x, y)).fetchall())
    n = con.execute("SELECT count(*) FROM books").fetchone()[0]
    data = {"version": 1, "books": [
        {"title": "Marmalade Notes, 2nd ed.", "isbn": _isbn(1)[:3] + "-" + _isbn(1)[3:], "authors": ["Someone Else"]},  # ISBN wins
        {"title": "jam BOOK", "authors": ["Sám Berry"], "publisher": "Preserve Press"},                    # folded match
        {"title": "Jam Book", "authors": ["Someone Else"]},     # same title, other first author: a different book
        {"title": "Brand New", "authors": []},
    ]}
    rep = importer.import_data(con, data)
    assert (rep.counts["books"]["insert"], rep.counts["books"]["update"]) == (2, 2)
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == n + 2
    assert con.execute("SELECT title, uid FROM books WHERE id=?", (x,)).fetchone()[:] == ("Marmalade Notes, 2nd ed.", uids[x])
    assert con.execute("SELECT publisher, uid FROM books WHERE id=?", (y,)).fetchone()[:] == ("Preserve Press", uids[y])
    new = con.execute("SELECT uid FROM books WHERE title IN ('Brand New') OR (title='Jam Book' AND id<>?)", (y,)).fetchall()
    assert len(new) == 2 and all(HEX32.match(r[0]) for r in new)
    con.close()


def test_two_file_entries_never_claim_the_same_local_book():
    import importer
    con = A.connect()
    _local_book(con, "Twin Title", ["Same Author"])
    n = con.execute("SELECT count(*) FROM books WHERE title='Twin Title'").fetchone()[0]
    data = {"books": [{"title": "Twin Title", "authors": ["Same Author"], "cuisine": "First"},
                      {"title": "Twin Title", "authors": ["Same Author"], "cuisine": "Second"}]}
    rep = importer.import_data(con, data)
    assert (rep.counts["books"]["update"], rep.counts["books"]["insert"]) == (1, 1)
    assert con.execute("SELECT count(*) FROM books WHERE title='Twin Title'").fetchone()[0] == n + 1
    assert sorted(r[0] for r in con.execute("SELECT cuisine FROM books WHERE title='Twin Title' AND cuisine IN ('First','Second')")) \
        == ["First", "Second"]
    con.close()


def test_without_a_uid_a_recipe_matches_by_web_id_then_by_name_and_page():
    import importer
    con = A.connect()
    bid = _local_book(con, "Recipe Matching", ["Rita Match"], recipes=[
        ("Seville marmalade", "10"), ("Seville marmalade", "50"), ("Plain toast", "3"), ("Old name", None, "web-1")])
    n = con.execute("SELECT count(*) FROM recipes WHERE book_id=?", (bid,)).fetchone()[0]
    data = {"books": [{"title": "Recipe Matching", "authors": ["Rita Match"], "recipes": [
        {"name": "seville MARMALADE", "page": "50", "rating": 9},     # name + page: the page-50 one, not page 10
        {"name": "Lemon curd", "page": "12"},                          # new
        {"name": "Plain toast", "page": "3", "notes": "first"},        # matches the local one
        {"name": "Plain toast", "page": "3", "notes": "second"},       # the local one is taken: a new recipe
        {"name": "New name", "ext_id": "web-1"},                       # by web id, whatever its name says
    ]}]}
    rep = importer.import_data(con, data)
    assert rep.counts["books"]["insert"] == 0
    assert (rep.counts["recipes"]["insert"], rep.counts["recipes"]["update"]) == (2, 3)
    assert con.execute("SELECT count(*) FROM recipes WHERE book_id=?", (bid,)).fetchone()[0] == n + 2
    got = {(r["name"].lower(), r["page"]): r for r in con.execute("SELECT * FROM recipes WHERE book_id=?", (bid,))}
    assert got[("seville marmalade", "50")]["rating"] == 9 and got[("seville marmalade", "10")]["rating"] is None
    assert con.execute("SELECT name FROM recipes WHERE book_id=? AND ext_id='web-1'", (bid,)).fetchone()[0] == "New name"
    assert sorted(r[0] for r in con.execute("SELECT notes FROM recipes WHERE book_id=? AND name='Plain toast'", (bid,))) \
        == ["first", "second"]
    con.close()


def test_an_unknown_uid_with_an_isbn_already_here_is_reported_and_left_out():
    import importer
    con = A.connect()
    isbn = _isbn(2)
    _local_book(con, "Held Book", ["Hal Held"], isbn=isbn)
    n = con.execute("SELECT count(*) FROM books").fetchone()[0]
    data = {"books": [{"uid": "a" * 32, "title": "Same ISBN, other copy", "isbn": isbn, "recipes": [{"name": "Lost soup"}]}]}
    rep = importer.import_data(con, data)
    assert rep.counts["books"]["insert"] == 0 and len(rep.conflicts) == 1 and "ISBN" in rep.conflicts[0][2]
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == n
    assert con.execute("SELECT count(*) FROM recipes WHERE name='Lost soup'").fetchone()[0] == 0
    con.close()


def test_two_entries_with_the_same_isbn_do_not_overwrite_one_book():
    import importer
    con = A.connect()
    isbn = _isbn(3)
    bid = _local_book(con, "Isbn Twin", ["Ina Twin"], isbn=isbn)
    data = {"books": [{"title": "Isbn Twin", "isbn": isbn, "cuisine": "First"},
                      {"title": "A different book", "isbn": isbn, "cuisine": "Second"}]}
    rep = importer.import_data(con, data)
    assert rep.counts["books"]["update"] == 1 and rep.counts["books"]["insert"] == 0 and len(rep.conflicts) == 1
    assert con.execute("SELECT cuisine FROM books WHERE id=?", (bid,)).fetchone()[0] == "First"
    con.close()


# ------------------------------------------------------------------ --replace and --dry-run
def dump_all(con):
    """Every row of every table (the search indexes' tables included), to prove a run changed nothing."""
    out = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        out[name] = sorted(repr(tuple(r)) for r in con.execute(f'SELECT * FROM "{name}"'))
    return out


def _add_local_only_things(con, data):
    """Things this catalog has that `data` (exported first) does not: a book with a recipe, a photo and its own
    author and tag; plus a recipe and a photo row added to a book and recipe the file does list."""
    only = _local_book(con, "Only here", ["Nobody Else"], recipes=[("Orphan stew", "1")])
    A.set_people(con, only, ["Nobody Else"], ["Orphan tag"])
    rid = con.execute("SELECT id FROM recipes WHERE book_id=?", (only,)).fetchone()[0]
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg')", (rid,))
    kept = next(b for b in data["books"] if b["recipes"] and not b["recipes"][0].get("photos"))
    kb = con.execute("SELECT id FROM books WHERE uid=?", (kept["uid"],)).fetchone()[0]
    kr = con.execute("SELECT id FROM recipes WHERE uid=?", (kept["recipes"][0]["uid"],)).fetchone()[0]
    con.execute("INSERT INTO recipes(book_id, name) VALUES (?, 'Local extra recipe')", (kb,))
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, 'cccccccccccccccccccccccccccccccc.jpg')", (kr,))
    A.reindex_all_books(con)
    con.commit()
    return only


def test_merge_keeps_what_the_file_lacks_and_replace_deletes_only_that(client):
    import importer
    decorate_catalog()
    data = export(client)
    con = A.connect()
    as_exported = catalog(con)                          # what the file describes
    only = _add_local_only_things(con, data)
    only_uid = con.execute("SELECT uid FROM books WHERE id=?", (only,)).fetchone()[0]
    n_books = con.execute("SELECT count(*) FROM books").fetchone()[0]

    rep = importer.import_data(con, data)              # merge: the default
    assert rep.counts["books"]["delete"] == rep.counts["recipes"]["delete"] == rep.counts["photos"]["delete"] == 0
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == n_books
    assert con.execute("SELECT count(*) FROM recipes WHERE name='Local extra recipe'").fetchone()[0] == 1

    said = []
    rep = importer.import_data(con, data, replace=True, log=said.append)
    assert rep.deleted_books == [(only_uid, "Only here")]
    assert any("Only here" in line for line in said) and any("1 book" in line for line in said)  # named before deleting
    assert rep.counts["books"]["delete"] == 1
    assert rep.counts["recipes"]["delete"] == 2         # Orphan stew (with its book) + Local extra recipe
    assert rep.counts["photos"]["delete"] == 2          # one on each of them
    assert catalog(con) == as_exported                  # exactly the file: nothing missing, nothing extra
    assert con.execute("SELECT count(*) FROM recipes WHERE name IN ('Orphan stew', 'Local extra recipe')").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM recipe_photos WHERE file LIKE 'b%' OR file LIKE 'cccc%'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM authors WHERE name='Nobody Else'").fetchone()[0] == 0   # orphans tidied
    assert con.execute("SELECT count(*) FROM tags WHERE name='Orphan tag'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM books_fts").fetchone()[0] == n_books - 1
    assert con.execute("SELECT count(*) FROM recipes_fts").fetchone()[0] == con.execute("SELECT count(*) FROM recipes").fetchone()[0]
    con.close()


def test_replace_refuses_a_file_with_no_books(client):
    import importer
    con = A.connect()
    n = con.execute("SELECT count(*) FROM books").fetchone()[0]
    with pytest.raises(importer.ImportFileError, match="no books"):
        importer.import_data(con, {"version": 1, "books": []}, replace=True)
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == n
    con.close()


def test_replace_keeps_the_local_book_an_isbn_clash_is_held_by(new_catalog):
    import importer
    new_catalog()  # its own catalog: --replace with a one-book file deletes everything else
    con = A.connect()
    isbn = _isbn(4)
    bid = _local_book(con, "Held under replace", ["Hal Held"], isbn=isbn)
    data = {"books": [{"uid": "d" * 32, "title": "Other copy", "isbn": isbn}]}
    rep = importer.import_data(con, data, replace=True)
    assert len(rep.conflicts) == 1
    assert con.execute("SELECT count(*) FROM books WHERE id=?", (bid,)).fetchone()[0] == 1   # not deleted
    assert all(title != "Held under replace" for _, title in rep.deleted_books)
    con.close()


def test_dry_run_changes_nothing_but_reports_everything(client):
    import importer
    data = export(client)
    con = A.connect()
    only = _add_local_only_things(con, data)
    data["books"][0]["title"] = "Dry run rename"
    data["books"].append({"uid": "e" * 32, "title": "Dry run book", "authors": ["Dry Author"],
                          "recipes": [{"name": "Dry run soup", "ingredients": ["Dry ingredient"]}]})
    before = dump_all(con)
    rep = importer.import_data(con, data, replace=True, dry_run=True)
    assert rep.counts["books"]["insert"] == 1 and rep.counts["books"]["update"] == 1 and rep.counts["books"]["delete"] == 1
    assert rep.counts["recipes"]["insert"] == 1 and rep.counts["recipes"]["delete"] == 2
    assert rep.new_names["authors"] == 1 and rep.new_names["ingredients"] == 1
    assert [t for _, t in rep.deleted_books] == ["Only here"]
    assert not con.in_transaction
    assert dump_all(con) == before
    con.close()
    con = A.connect()
    assert dump_all(con) == before                      # nor anything left behind for another connection
    assert con.execute("SELECT count(*) FROM books WHERE id=?", (only,)).fetchone()[0] == 1
    con.close()


def test_dry_run_into_an_empty_database_leaves_it_empty(client, new_catalog):
    import importer
    data = export(client)
    new_catalog()
    con = A.connect()
    before = dump_all(con)
    rep = importer.import_data(con, data, dry_run=True)
    assert rep.counts["books"]["insert"] == len(data["books"])
    assert dump_all(con) == before
    con.close()


def test_a_database_error_leaves_out_that_book_only_and_replace_does_not_prune_it(new_catalog):
    """A constraint the pre-checks can't see (forced here with a trigger) rolls back that book's changes and
    nothing else; --replace must not then delete the recipes it never got to read."""
    import importer
    new_catalog()
    con = A.connect()
    bid = _local_book(con, "Fragile", ["Fran Fragile"], recipes=[("Local only", "1")])
    uid = con.execute("SELECT uid FROM books WHERE id=?", (bid,)).fetchone()[0]
    con.execute("CREATE TRIGGER boom BEFORE INSERT ON recipes WHEN new.name='Boom' BEGIN SELECT RAISE(ABORT, 'boom'); END")
    con.commit()
    data = {"books": [{"uid": uid, "title": "Fragile, renamed", "authors": ["Fran Fragile"],
                       "recipes": [{"name": "Boom"}]},
                      {"uid": "9" * 32, "title": "Fine book"}]}
    rep = importer.import_data(con, data, replace=True)
    assert len(rep.conflicts) == 1 and "boom" in rep.conflicts[0][2]
    assert rep.counts["books"]["insert"] == 1 and rep.counts["books"]["update"] == 0
    assert con.execute("SELECT title FROM books WHERE id=?", (bid,)).fetchone()[0] == "Fragile"   # rolled back
    assert con.execute("SELECT count(*) FROM recipes WHERE book_id=? AND name='Local only'", (bid,)).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM books WHERE title='Fine book'").fetchone()[0] == 1
    assert rep.deleted_books == []
    con.close()
