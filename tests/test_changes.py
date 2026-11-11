"""Change history (changes.py): what each write records, undo round trips that restore identical rows (uid
included), conflicts that refuse an undo, batches, retention, and that the log never reaches anyone but an editor."""
import json

import pytest

import app as A
import changes as C
import importer as IM
import mcp as MC
from access_jwt import AccessKeys

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}
ANON = {"REMOTE_ADDR": "203.0.113.9"}  # not on the LAN and no Access token: not an editor
EMAIL = "alice@example.com"
SECRET = "SECRET-private-note-71"


@pytest.fixture()
def c():
    return A.app.test_client()


def q(sql, *args):
    con = A.connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def one(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def last_change():
    return one("SELECT * FROM changes ORDER BY id DESC LIMIT 1")


def undo(c, cid, **kw):
    return c.post(f"/admin/api/changes/{cid}/undo", **kw)


def undo_batch(c, batch, **kw):
    return c.post(f"/admin/api/changes/batch/{batch}/undo", **kw)


def book_rows(bid):
    """Everything about a book a round trip must bring back exactly."""
    con = A.connect()
    try:
        b = dict(con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone())
        b["authors"] = [r[0] for r in con.execute("SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
                                                  "WHERE ba.book_id=? ORDER BY ba.pos", (bid,))]
        b["tags"] = sorted(r[0] for r in con.execute("SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
                                                      "WHERE bt.book_id=?", (bid,)))
        b["recipes"] = [recipe_rows_con(con, r[0]) for r in con.execute("SELECT id FROM recipes WHERE book_id=? ORDER BY id",
                                                                          (bid,))]
        b["fts"] = [tuple(r) for r in con.execute("SELECT title, people, other FROM books_fts WHERE rowid=?", (bid,))]
        return b
    finally:
        con.close()


def recipe_rows_con(con, rid):
    r = dict(con.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone())
    r["tags"] = sorted(x[0] for x in con.execute("SELECT i.name FROM ingredients i JOIN recipe_ingredients ri "
                                                  "ON ri.ingredient_id=i.id WHERE ri.recipe_id=?", (rid,)))
    det = con.execute("SELECT * FROM recipe_details WHERE recipe_id=?", (rid,)).fetchone()
    r["detail"] = dict(det) if det else None
    r["lines"] = [dict(x) | {"id": None} for x in con.execute(
        "SELECT * FROM recipe_ingredient_lines WHERE recipe_id=? ORDER BY position", (rid,))]
    r["steps"] = [dict(x) | {"id": None} for x in con.execute(
        "SELECT * FROM recipe_steps WHERE recipe_id=? ORDER BY position", (rid,))]
    r["fts"] = [tuple(x) for x in con.execute("SELECT name, heading FROM recipes_fts WHERE rowid=?", (rid,))]
    return r


def recipe_rows(rid):
    con = A.connect()
    try:
        return recipe_rows_con(con, rid)
    finally:
        con.close()


def new_book(c, title, **extra):
    r = c.post("/admin/api/books", json={"title": title, "authors": ["Ada Cook"], "tags": ["Weeknight"], **extra})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def new_recipe(c, bid, name, **extra):
    r = c.post(f"/admin/api/books/{bid}/recipes", json={"name": name, "page": "12", **extra})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def tag(rid, *names):
    con = A.connect()
    for n in names:
        con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, A.name_id(con, "ingredients", n)))
    con.commit()
    con.close()


def house_id():
    return one("SELECT id FROM books WHERE kind='house'")[0]


# ------------------------------------------------------------------ the table
def test_upgrade_adds_an_empty_table_and_seeding_records_nothing():
    cols = [r[1] for r in q("PRAGMA table_info(changes)")]
    assert cols == ["id", "at", "actor", "entity", "entity_uid", "action", "before", "after", "batch_id", "book_uid",
                    "label", "op", "undo_of", "undone_by"]
    con = A.connect()  # a database from the previous release: no table. Starting the app adds it, empty.
    con.execute("DROP TABLE changes")
    con.commit()
    con.close()
    A.init_db()
    assert one("SELECT count(*) FROM changes")[0] == 0


# ------------------------------------------------------------------ updates
def test_book_update_round_trip_restores_identical_rows(c):
    bid = new_book(c, "Round Trip Kitchen", cuisine="Italian")
    original = book_rows(bid)
    r = c.put(f"/admin/api/books/{bid}", json={"title": "Renamed Kitchen", "cuisine": "Mexican", "private_notes": SECRET,
                                               "authors": ["Ada Cook", "Bo Baker"], "tags": ["Grill"], "rating": 4.5})
    assert r.status_code == 200
    ch = last_change()
    assert (ch["entity"], ch["action"], ch["entity_uid"], ch["batch_id"]) == ("book", "update", original["uid"], None)
    before, after = json.loads(ch["before"]), json.loads(ch["after"])
    assert before["title"] == "Round Trip Kitchen" and after["title"] == "Renamed Kitchen"
    assert "isbn" not in before  # only what changed
    assert undo(c, ch["id"]).status_code == 200
    assert book_rows(bid) == original
    assert one("SELECT undone_by FROM changes WHERE id=?", ch["id"])[0] == last_change()["id"]
    assert last_change()["undo_of"] == ch["id"] and last_change()["op"] == "undo"


def test_recipe_update_round_trip(c):
    bid = new_book(c, "Recipe Edits")
    rid = new_recipe(c, bid, "Braised fennel", heading="Fennel")
    tag(rid, "Fennel")
    original = recipe_rows(rid)
    c.put(f"/admin/api/recipes/{rid}", json={"name": "Braised fennel gratin", "rating": 9, "notes": "more cheese",
                                             "want_to_try": True})
    ch = last_change()
    assert ch["entity"] == "recipe" and ch["action"] == "update"
    assert undo(c, ch["id"]).status_code == 200
    assert recipe_rows(rid) == original
    assert c.get("/admin/api/recipes?q=gratin").get_json()["count"] == 0  # FTS follows the name back


def test_an_undo_is_recorded_and_can_itself_be_undone(c):
    bid = new_book(c, "Redo Book")
    rid = new_recipe(c, bid, "Polenta")
    c.put(f"/admin/api/recipes/{rid}", json={"rating": 7})
    edited = recipe_rows(rid)
    first = last_change()["id"]
    undo(c, first)
    second = last_change()
    assert second["action"] == "update" and second["undo_of"] == first
    assert undo(c, second["id"]).status_code == 200
    assert recipe_rows(rid) == edited


# ------------------------------------------------------------------ deletes
def test_recipe_delete_round_trip_keeps_uid_tags_star_notes_and_fts(c):
    bid = new_book(c, "Delete Me Not")
    rid = new_recipe(c, bid, "Zuppa di cozze", heading="Mussels", want_to_try=True, rating=8, notes="our favourite")
    tag(rid, "Mussels", "Tomato")
    con = A.connect()
    con.execute("INSERT INTO dl_link(meal_id, recipe_id, meal_key) VALUES (99001, ?, '2026-10-01|zuppa di cozze')", (rid,))
    con.commit()
    con.close()
    original = recipe_rows(rid)
    assert c.delete(f"/admin/api/recipes/{rid}").status_code == 200
    assert one("SELECT 1 FROM recipes WHERE id=?", rid) is None
    ch = last_change()
    assert (ch["action"], ch["entity_uid"]) == ("delete", original["uid"])
    assert undo(c, ch["id"]).status_code == 200
    assert recipe_rows(rid) == original  # same id (it was free), same uid, tags, star, rating, notes
    assert one("SELECT recipe_id FROM dl_link WHERE meal_id=99001")[0] == rid
    found = c.get("/admin/api/recipes?q=cozze").get_json()["recipes"]
    assert [x["uid"] for x in found] == [original["uid"]]


def test_book_delete_round_trip_brings_back_every_recipe(c):
    bid = new_book(c, "Whole Book Gone", isbn="9780306406157", private_notes=SECRET)
    rids = [new_recipe(c, bid, n) for n in ("Risotto alla milanese", "Ossobuco", "Gremolata")]
    tag(rids[0], "Rice")
    c.put(f"/admin/api/recipes/{rids[1]}", json={"rating": 9, "want_to_try": True})
    original = book_rows(bid)
    assert c.delete(f"/admin/api/books/{bid}").status_code == 200
    ch = last_change()
    assert (ch["entity"], ch["action"]) == ("book", "delete") and len(json.loads(ch["before"])["recipes"]) == 3
    r = undo(c, ch["id"])
    assert r.status_code == 200, r.get_json()
    assert book_rows(bid) == original
    assert [b["uid"] for b in c.get("/admin/api/books?q=whole+book+gone").get_json()["books"]] == [original["uid"]]
    assert c.get("/admin/api/recipes?q=ossobuco").get_json()["count"] == 1


def test_house_recipe_delete_round_trip_restores_details_lines_and_steps(c):
    doc = {"name": "Nonna's ragù", "description": "Sunday sauce", "servings": 6, "methods": ["braise"],
           "ingredients": [{"quantity": "500", "unit": "g", "item": "beef"}, {"item": "tomato", "section": "Sauce"}],
           "steps": [{"text": "Brown the meat"}, {"text": "Simmer", "timer_minutes": 180}]}
    r = c.post("/admin/api/house/recipes", json=doc)
    rid = r.get_json()["id"]
    assert last_change()["entity"] == "house recipe"
    original = recipe_rows(rid)
    c.delete(f"/admin/api/recipes/{rid}")
    assert undo(c, last_change()["id"]).status_code == 200
    assert recipe_rows(rid) == original


def test_house_recipe_edit_round_trip(c):
    r = c.post("/admin/api/house/recipes", json={"name": "Focaccia", "ingredients": [{"item": "flour"}],
                                                 "steps": ["Mix", "Bake"]})
    rid, uid = r.get_json()["id"], r.get_json()["uid"]
    original = recipe_rows(rid)
    c.put(f"/admin/api/house/recipes/{rid}", json={"name": "Focaccia barese", "ingredients": [{"item": "flour"},
                                                   {"item": "potato"}], "steps": ["Mix", "Rest", "Bake"],
                                                   "notes": "olive oil, lots"})
    ch = last_change()
    assert (ch["entity"], ch["action"], ch["entity_uid"]) == ("house recipe", "update", uid)
    assert undo(c, ch["id"]).status_code == 200
    assert recipe_rows(rid) == original


def test_deleted_photos_are_named_as_not_restorable(c):
    bid = new_book(c, "Photo Book")
    rid = new_recipe(c, bid, "Grilled peaches")
    con = A.connect()
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, 'gone-forever.jpg')", (rid,))
    con.commit()
    con.close()
    c.delete(f"/admin/api/recipes/{rid}")
    entry = c.get("/admin/api/changes?limit=1").get_json()["changes"][0]
    assert "can't be restored" in entry["warning"]
    r = undo(c, entry["id"]).get_json()
    assert "can't be restored" in r["message"]
    assert one("SELECT count(*) FROM recipe_photos WHERE recipe_id=?", rid)[0] == 0


# ------------------------------------------------------------------ creates
def test_undoing_a_create_deletes_it(c):
    bid = new_book(c, "Brand New")
    ch = last_change()
    assert (ch["action"], ch["actor"]) == ("create", "lan")
    assert undo(c, ch["id"]).status_code == 200
    assert one("SELECT 1 FROM books WHERE id=?", bid) is None


def test_location_delete_round_trip_puts_books_back_on_their_shelves(c):
    room = c.post("/admin/api/locations", json={"name": "Den"}).get_json()["location"]["id"]
    shelf = c.post("/admin/api/locations", json={"name": "Top shelf", "parent_id": room}).get_json()["location"]["id"]
    bid = new_book(c, "Shelved Book")
    c.put(f"/admin/api/books/{bid}", json={"location_id": shelf})
    before = (q("SELECT * FROM locations WHERE id IN (?,?) ORDER BY id", room, shelf), book_rows(bid))
    c.delete(f"/admin/api/locations/{room}")
    assert one("SELECT location_id FROM books WHERE id=?", bid)[0] is None
    entry = c.get("/admin/api/changes?limit=1").get_json()["changes"][0]
    assert "1 book unshelved" in entry["summary"]
    r = undo(c, entry["id"])
    assert r.status_code == 200, r.get_json()
    after = (q("SELECT * FROM locations WHERE id IN (?,?) ORDER BY id", room, shelf), book_rows(bid))
    assert [tuple(x) for x in after[0]] == [tuple(x) for x in before[0]]
    assert after[1]["location_id"] == shelf


# ------------------------------------------------------------------ batches
def test_index_commit_is_one_small_batch_and_undoes_as_one(c):
    bid = new_book(c, "Indexed Book")
    n0 = one("SELECT count(*) FROM changes")[0]
    pages = lambda: one("PRAGMA page_count")[0] * one("PRAGMA page_size")[0]
    start = pages()
    rows = [{"name": f"Recipe number {i}", "page": str(i)} for i in range(1, 2001)]
    assert c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": rows}).get_json()["added"] == 2000
    assert one("SELECT count(*) FROM changes")[0] == n0 + 1  # one grouped row, not 2,000
    ch = last_change()
    assert ch["entity_uid"] is None and ch["batch_id"] and ch["op"] == "index import"
    grew = pages() - start  # the recipes, their indexes and search index, and the log row
    assert len(ch["after"]) < 100 * 2000 and len(ch["after"]) < grew / 2  # the log is the smaller part
    found = c.get("/admin/api/changes?limit=1").get_json()["changes"][0]
    assert found["summary"] == "Added 2000 recipes to “Indexed Book”"
    assert undo_batch(c, ch["batch_id"]).status_code == 200
    assert one("SELECT count(*) FROM recipes WHERE book_id=?", bid)[0] == 0


def test_batch_undo_is_all_or_nothing(c):
    bid = new_book(c, "Batch Conflict")
    c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": [{"name": "Alpha", "page": "1"},
                                                                  {"name": "Beta", "page": "2"}]})
    batch = last_change()["batch_id"]
    beta = one("SELECT id FROM recipes WHERE book_id=? AND name='Beta'", bid)[0]
    c.put(f"/admin/api/recipes/{beta}", json={"rating": 6})
    r = undo_batch(c, batch)
    assert r.status_code == 409 and "changed again" in r.get_json()["error"]
    assert one("SELECT count(*) FROM recipes WHERE book_id=?", bid)[0] == 2
    assert one("SELECT undone_by FROM changes WHERE batch_id=?", batch)[0] is None


def test_batch_round_trip_restores_identical_rows(c):
    bid = new_book(c, "Batch Round Trip")
    c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": [{"name": "Uno", "page": "1"},
                                                                  {"name": "Due", "page": "2"}]})
    batch = last_change()["batch_id"]
    created = book_rows(bid)
    undo_batch(c, batch)
    redo = last_change()
    assert redo["action"] == "delete" and redo["entity_uid"] is None
    assert undo(c, redo["id"]).status_code == 200  # undo the undo: back, with the same uids
    assert book_rows(bid) == created


def test_grouped_tagging_is_one_row_and_undoes(c):
    bid = new_book(c, "Tag Batch")
    rids = [new_recipe(c, bid, f"Chicken thing {i}") for i in range(5)]
    before = [recipe_rows(r) for r in rids]
    con = A.connect()
    rec = C.Recorder(con, op="ingredient import", actor="tester", bulk=True)
    iid = A.name_id(con, "ingredients", "Chicken test tag")
    for rid in rids:
        rec.recipe(rid)
        con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid, iid))
    ids = rec.finish()
    con.commit()
    con.close()
    assert len(ids) == 1
    row = one("SELECT * FROM changes WHERE id=?", ids[0])
    assert json.loads(row["after"])["tagged"] == {"Chicken test tag": [recipe_rows(r)["uid"] for r in rids]}
    assert undo(c, ids[0]).status_code == 200
    assert [recipe_rows(r) for r in rids] == before
    assert one("SELECT 1 FROM ingredients WHERE name='Chicken test tag'") is None


def test_merge_is_recorded_and_undo_brings_the_garbled_copy_back(c):
    bid = new_book(c, "Merge Book")
    good = new_recipe(c, bid, "Tudòusi", page="40")
    bad = new_recipe(c, bid, "Tud�usi", page="40", notes="from the garbled one")
    good_before, bad_before = recipe_rows(good), recipe_rows(bad)
    con = A.connect()
    import eyb
    assert eyb.merge_garbled(con) == 1
    con.commit()
    con.close()
    ch = last_change()
    assert ch["op"] == "merge" and ch["actor"] == "system"
    assert undo_batch(c, ch["batch_id"]).status_code == 200
    assert recipe_rows(good) == good_before and recipe_rows(bad) == bad_before


def upload(c, url, raw, **form):
    import io
    return c.post(url, data={"file": (io.BytesIO(raw), "x.csv"), **form}, content_type="multipart/form-data")


def test_eyb_import_creating_a_book_undoes_as_one_batch(c):
    raw = ("Recipe Title,Book Title,Author,Page No.\r\n"
           "Kuku sabzi,Batch Persiana,Sabrina Ghayour,(page 12)\r\n"
           "Tahdig,Batch Persiana,Sabrina Ghayour,(page 40)\r\n").encode()
    n0 = one("SELECT count(*) FROM books")[0]
    r = upload(c, "/admin/api/eyb/commit", raw, decisions=json.dumps({"Batch Persiana": "create"}))
    assert r.get_json()["added"] == 2
    batch = last_change()["batch_id"]
    uid = one("SELECT uid FROM books WHERE title='Batch Persiana'")[0]
    rows = q("SELECT entity, action, entity_uid IS NULL AS grouped, op FROM changes WHERE batch_id=? AND book_uid=? "
             "ORDER BY id", batch, uid)  # (a garbled copy left by another test may be merged in the same batch)
    assert [tuple(x) for x in rows] == [("book", "create", 0, "Eat Your Books import"),
                                        ("recipe", "create", 1, "Eat Your Books import")]
    assert undo_batch(c, batch).status_code == 200
    assert one("SELECT count(*) FROM books")[0] == n0
    assert c.get("/admin/api/recipes?q=tahdig").get_json()["count"] == 0


def test_ingredient_import_round_trip(c):
    bid = new_book(c, "Tagged Pozole Book", authors=["Steve Sando"])
    pozole = new_recipe(c, bid, "Pozole verde", page="40")
    before = book_rows(bid)
    raw = ("Recipe Title,Book Title,Author,Page No.\r\n"
           "Pozole verde,Tagged Pozole Book,Steve Sando,(page 40)\r\n"
           "Pozole rojo,Tagged Pozole Book,Steve Sando,(page 42)\r\n").encode()
    r = upload(c, "/admin/api/ingredients/commit", raw, ingredient="Hominy test",
               decisions=json.dumps({"Tagged Pozole Book": bid}))
    assert r.get_json()["tagged"] == 2 and r.get_json()["added_recipes"] == 1
    batch = last_change()["batch_id"]
    rows = q("SELECT action, entity_uid IS NULL FROM changes WHERE batch_id=? AND book_uid=? ORDER BY id",
             batch, before["uid"])
    assert [tuple(x) for x in rows] == [("create", 0), ("update", 0), ("update", 1)]  # recipe, eyb_title, tag
    assert recipe_rows(pozole)["tags"] == ["Hominy test"]
    assert undo_batch(c, batch).status_code == 200
    assert book_rows(bid) == before
    assert one("SELECT 1 FROM ingredients WHERE name='Hominy test'") is None


# ------------------------------------------------------------------ refusals
def test_refuses_when_the_row_changed_again(c):
    bid = new_book(c, "Changed Twice")
    rid = new_recipe(c, bid, "Gnocchi")
    c.put(f"/admin/api/recipes/{rid}", json={"rating": 5})
    first = last_change()["id"]
    c.put(f"/admin/api/recipes/{rid}", json={"notes": "softer"})
    r = undo(c, first)
    assert r.status_code == 409 and "changed again since" in r.get_json()["error"]
    assert one("SELECT rating, notes FROM recipes WHERE id=?", rid)[:] == (5, "softer")


def test_refuses_when_the_parent_book_is_gone(c):
    bid = new_book(c, "Parent Gone")
    rid = new_recipe(c, bid, "Orphan stew")
    c.delete(f"/admin/api/recipes/{rid}")
    recipe_delete = last_change()["id"]
    c.delete(f"/admin/api/books/{bid}")
    r = undo(c, recipe_delete)
    assert r.status_code == 409 and "book it was in is gone" in r.get_json()["error"]


def test_refuses_when_the_uid_is_taken(c):
    bid = new_book(c, "Uid Taken")
    rid = new_recipe(c, bid, "Twin")
    uid = recipe_rows(rid)["uid"]
    c.delete(f"/admin/api/recipes/{rid}")
    cid = last_change()["id"]
    con = A.connect()
    con.execute("INSERT INTO recipes(book_id, name, uid) VALUES (?, 'Impostor', ?)", (bid, uid))
    con.commit()
    con.close()
    r = undo(c, cid)
    assert r.status_code == 409 and "uid is in use" in r.get_json()["error"]


def test_refuses_an_isbn_now_on_another_book(c):
    bid = new_book(c, "Isbn Holder", isbn="9780140449136")
    c.delete(f"/admin/api/books/{bid}")
    cid = last_change()["id"]
    new_book(c, "Isbn Thief", isbn="9780140449136")
    r = undo(c, cid)
    assert r.status_code == 409 and "ISBN" in r.get_json()["error"]


def test_refuses_twice_and_unknown_ids(c):
    bid = new_book(c, "Twice")
    cid = last_change()["id"]
    assert undo(c, cid).status_code == 200
    assert undo(c, cid).status_code == 409
    assert undo(c, 10**9).status_code == 404
    assert undo_batch(c, "not-a-batch").status_code == 404
    assert one("SELECT 1 FROM books WHERE id=?", bid) is None


def test_refuses_to_remove_a_recipe_that_has_photos_now(c):
    bid = new_book(c, "Photo Since")
    rid = new_recipe(c, bid, "Tart")
    cid = last_change()["id"]
    con = A.connect()
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, 'tart.jpg')", (rid,))
    con.commit()
    con.close()
    r = undo(c, cid)
    assert r.status_code == 409 and "photo" in r.get_json()["error"]


# ------------------------------------------------------------------ who
def test_actors_editor_email_mcp_and_importer(c, monkeypatch):
    keys = AccessKeys(monkeypatch)
    h = {**CF, "Cf-Access-Jwt-Assertion": keys.token(EMAIL)}
    r = c.post("/admin/api/books", json={"title": "Signed In Book"}, headers=h)
    assert r.status_code == 201
    assert last_change()["actor"] == EMAIL
    monkeypatch.setattr(MC, "MCP_TOKEN", "tok")
    monkeypatch.setattr(MC, "MCP_WRITE", True)
    rid = one("SELECT id FROM recipes WHERE want_to_try=0 LIMIT 1")[0]
    c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "set_want_to_try", "arguments": {"recipe_id": rid}}},
           headers={"Authorization": "Bearer tok"})
    ch = last_change()
    assert (ch["actor"], ch["op"], ch["entity"]) == ("mcp", "MCP", "recipe")
    con = A.connect()
    IM.import_data(con, {"books": [{"title": "Imported Book", "recipes": [{"name": "Imported one"},
                                                                         {"name": "Imported two"}]}]})
    con.close()
    rows = q("SELECT actor, op, entity, action FROM changes WHERE batch_id=(SELECT batch_id FROM changes ORDER BY id "
             "DESC LIMIT 1) ORDER BY id")
    assert [tuple(r) for r in rows] == [("importer", "JSON import", "book", "create"),
                                        ("importer", "JSON import", "recipe", "create")]


def test_importer_replace_is_undoable(c):
    con = A.connect()
    IM.import_data(con, {"books": [{"title": "Replace Keeper", "recipes": [{"name": "kept"}]}]})
    con.close()
    total = one("SELECT count(*) FROM books")[0]
    snapshot = {r["uid"]: book_rows(r["id"]) for r in q("SELECT id, uid FROM books")}
    keeper = one("SELECT uid FROM books WHERE title='Replace Keeper'")[0]
    con = A.connect()
    IM.import_data(con, {"books": [{"uid": keeper, "title": "Replace Keeper", "recipes": []}]}, replace=True)
    con.close()
    assert one("SELECT count(*) FROM books")[0] < total
    batch = last_change()["batch_id"]
    r = undo_batch(c, batch)
    assert r.status_code == 200, r.get_json()
    after = {r["uid"]: book_rows(r["id"]) for r in q("SELECT id, uid FROM books")}
    assert set(after) == set(snapshot)
    for uid, b in snapshot.items():  # covers aside (their files aren't the history's), identical
        assert {k: v for k, v in after[uid].items() if k not in ("id", "cover")} == \
            {k: v for k, v in b.items() if k not in ("id", "cover")}


def test_importer_dry_run_records_nothing():
    n = one("SELECT count(*) FROM changes")[0]
    con = A.connect()
    IM.import_data(con, {"books": [{"title": "Dry Run Book"}]}, dry_run=True)
    con.close()
    assert one("SELECT count(*) FROM changes")[0] == n


# ------------------------------------------------------------------ the page's API
def test_list_newest_first_with_filters_and_plain_words(c, monkeypatch):
    bid = new_book(c, "Filter Me")
    rid = new_recipe(c, bid, "Panzanella")
    c.put(f"/admin/api/recipes/{rid}", json={"rating": 8, "want_to_try": True})
    new_book(c, "Other Book")
    uid = book_rows(bid)["uid"]
    d = c.get(f"/admin/api/changes?book={uid}").get_json()
    assert [x["summary"] for x in d["changes"]] == ["Changed the recipe “Panzanella” in “Filter Me”",
                                                    "Added the recipe “Panzanella” in “Filter Me”",
                                                    "Added the book “Filter Me”"]
    assert d["changes"][0]["details"] == ["rating none → 8", "starred"]
    assert {"uid": uid, "title": "Filter Me"} in d["books"] and "lan" in d["actors"]
    by_actor = c.get("/admin/api/changes?actor=nobody@example.com").get_json()
    assert by_actor["changes"] == []
    ids = [x["id"] for x in c.get("/admin/api/changes?limit=200").get_json()["changes"]]
    assert ids == sorted(ids, reverse=True)
    page = c.get("/admin/api/changes?limit=1").get_json()
    assert page["next_before"] and c.get(f"/admin/api/changes?limit=1&before={page['next_before']}").status_code == 200


def test_a_page_does_not_split_a_batch(c):
    for name in ("Split shelf A", "Split shelf B"):
        c.post("/admin/api/locations", json={"name": name})
    roots = [x["id"] for x in c.get("/admin/api/locations").get_json()["locations"] if x["parent_id"] is None]
    c.post("/admin/api/locations/reorder", json={"parent_id": None, "ids": list(reversed(roots))})
    batch = last_change()["batch_id"]
    assert batch and one("SELECT count(*) FROM changes WHERE batch_id=?", batch)[0] > 1
    page = c.get("/admin/api/changes?limit=1").get_json()["changes"]
    assert {x["batch_id"] for x in page} == {batch} and len(page) == page[0]["batch_size"]


# ------------------------------------------------------------------ retention
def test_pruning_keeps_whole_batches():
    con = A.connect()
    old, new = "datetime('now', '-200 days')", "datetime('now', '-1 days')"
    for at, batch in ((old, None), (old, "a" * 32), (old, "b" * 32), (new, "b" * 32), (new, None)):
        con.execute(f"INSERT INTO changes(at, actor, entity, action, batch_id, label) VALUES ({at}, 'x', 'book', "
                    "'update', ?, 'prune-test')", (batch,))
    assert C.prune(con, 0) == 0
    C.prune(con, 180)
    left = [(r["batch_id"]) for r in con.execute("SELECT batch_id FROM changes WHERE label='prune-test' ORDER BY id")]
    assert left == ["b" * 32, "b" * 32, None]  # the old single and the all-old batch went; batch b stays whole
    con.commit()
    con.close()


def test_pruned_at_startup(monkeypatch):
    con = A.connect()
    con.execute("INSERT INTO changes(at, actor, entity, action, label) VALUES "
                "(datetime('now', '-400 days'), 'x', 'book', 'update', 'startup-prune')")
    con.commit()
    con.close()
    monkeypatch.setattr(C, "KEEP_DAYS", 180)
    A.init_db()
    assert one("SELECT 1 FROM changes WHERE label='startup-prune'") is None


# ------------------------------------------------------------------ security and privacy
def test_routes_need_an_editor_and_a_same_site_origin(c):
    cid = new_book(c, "Gatekeeping") and last_change()["id"]
    for r in (c.get("/admin/api/changes", environ_base=ANON), c.get("/admin/api/changes", headers=CF),
              undo(c, cid, environ_base=ANON), undo(c, cid, headers=CF), undo_batch(c, "a" * 32, environ_base=ANON)):
        assert r.status_code == 401
    r = undo(c, cid, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert last_change()["undone_by"] is None


def test_no_mcp_undo_tool_and_nothing_in_ask():
    names = {t[0] for t in MC.TOOLS}
    assert not any(w in n for n in names for w in ("undo", "change", "history"))
    assert not any(w in n for n in MC.WRITE_TOOLS for w in ("undo", "change", "history"))


def test_nothing_from_the_log_reaches_anyone_but_an_editor(c, monkeypatch):
    keys = AccessKeys(monkeypatch)
    h = {**CF, "Cf-Access-Jwt-Assertion": keys.token(EMAIL)}
    bid = c.post("/admin/api/books", json={"title": "Leak Check"}, headers=h).get_json()["id"]
    c.put(f"/admin/api/books/{bid}", json={"private_notes": SECRET, "rating": 3}, headers=h)
    rid = c.post(f"/admin/api/books/{bid}/recipes", json={"name": "Leak soup", "notes": SECRET + "-r", "rating": 9},
                 headers=h).get_json()["id"]
    c.delete(f"/admin/api/recipes/{rid}", headers=h)  # its notes now live only in the log
    assert EMAIL in json.dumps(c.get("/admin/api/changes", headers=h).get_json())  # the editor's page shows who
    assert SECRET + "-r" in one("SELECT before FROM changes ORDER BY id DESC LIMIT 1")[0]  # the log holds the note
    monkeypatch.setattr(MC, "MCP_PUBLIC", True)
    monkeypatch.setattr(MC, "MCP_TOKEN", "tok")
    monkeypatch.setattr(MC, "MCP_WRITE", True)

    def mcp_calls(auth):
        out = [c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth)]
        for name, args in (("search_books", {"query": "leak"}), ("get_book", {"book_id": bid}),
                           ("search_recipes", {"query": "leak"}), ("list_categories", {}), ("suggest_recipes", {}),
                           ("cooking_stats", {})):
            out.append(c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": name, "arguments": args}}, headers=auth))
        return out

    public = [c.get(u, environ_base=ANON) for u in (
        "/", "/api/books", f"/api/books/{bid}", "/api/recipes?q=leak", "/api/facets", "/api/locations", "/api/me",
        "/api/photos", "/api/ingredients", "/api/house", "/robots.txt", "/healthz")] + mcp_calls({})
    for r in public:  # anonymous: no email, no private field, nothing of the log
        body = r.get_data(as_text=True)
        assert EMAIL not in body and SECRET not in body and "undone_by" not in body, r.request.path
    exports = [c.get("/admin/api/export.json"), c.get("/admin/api/export.csv")]
    for r in mcp_calls({"Authorization": "Bearer tok"}) + exports:  # private, but not the log
        body = r.get_data(as_text=True)
        assert EMAIL not in body and SECRET + "-r" not in body and "undone_by" not in body, r.request.path
    assert "changes" not in exports[0].get_json()


def test_ask_tools_never_read_the_log(c):
    """Ask runs MCP read tools through run_tool (never WRITE_TOOLS); none of them reads the change history."""
    import ask
    assert "changes" not in vars(ask)  # it doesn't import the module
    c.put(f"/admin/api/recipes/{one('SELECT id FROM recipes LIMIT 1')[0]}", json={"notes": "ask-probe"})
    with A.app.test_request_context():
        for name, private_only, _fn, _desc, _schema in MC.TOOLS:
            if name in MC.WRITE_TOOLS:
                continue
            args = {"book_id": one("SELECT id FROM books LIMIT 1")[0]} if name == "get_book" else \
                {"recipe_id": one("SELECT id FROM recipes LIMIT 1")[0]} if name == "get_recipe" else {}
            res, _err = MC.run_tool(name, args, private=True)
            assert "undone_by" not in json.dumps(res) and "lan" not in json.dumps(res).split('"'), name
