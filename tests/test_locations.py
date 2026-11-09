"""Hierarchical shelf locations: room > area > shelf, any depth.

A book sits in one location (`books.location_id`). Locations are public on purpose: the catalog shows where a
book lives and filters by it, but only locations that hold a public book (themselves or below them) are ever
listed, so no empty rooms and nothing from editor-only recipe websites leaks.

These tests share one database per module, so every test starts from `_clean`: no locations, no shelved books,
and none of the books earlier tests made. Test connections are committed and closed before the app is called,
or SQLite reports "database is locked".
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

import app as A

KW = dict(base_url="http://127.0.0.1:8767", environ_base={"REMOTE_ADDR": "127.0.0.1"})  # LAN editor
CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}  # arrives through Cloudflare with no sign-in
SEP = " › "


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        con = A.connect()
        con.execute("DELETE FROM books WHERE title LIKE 'LT %'")
        con.execute("UPDATE books SET location_id=NULL, location=NULL")
        con.execute("DELETE FROM locations")
        con.execute("DELETE FROM meta WHERE key='locations_migrated'")
        con.commit()
        con.close()
    wipe()
    yield
    wipe()


def mk(c, name, parent=None, **extra):
    r = c.post("/admin/api/locations", json={"name": name, "parent_id": parent, **extra}, **KW)
    assert r.status_code == 201, r.json
    return r.json["location"]["id"]


def book(title, kind=None, loc=None, legacy=None):
    con = A.connect()
    bid = con.execute("INSERT INTO books(title, kind, location_id, location) VALUES (?,?,?,?)",
                      (f"LT {title}", kind, loc, legacy)).lastrowid
    A.reindex_book(con, bid)  # as the app does after a write, so keyword search finds it
    con.commit()
    con.close()
    return bid


def admin_locs(c):
    return c.get("/admin/api/locations", **KW).json["locations"]


def paths(locs):
    return [l["path"] for l in locs]


def by_path(locs):
    return {l["path"]: l for l in locs}


# ------------------------------------------------------------------ schema
def test_schema_has_locations_and_the_book_column():
    con = A.connect()
    assert [r[1] for r in con.execute("PRAGMA table_info(locations)")] == ["id", "parent_id", "name", "sort_order"]
    assert "location_id" in [r[1] for r in con.execute("PRAGMA table_info(books)")]
    fks = {(r[2], r[3], r[6]) for r in con.execute("PRAGMA foreign_key_list(books)")}
    assert ("locations", "location_id", "SET NULL") in fks
    fks = {(r[2], r[3], r[6]) for r in con.execute("PRAGMA foreign_key_list(locations)")}
    assert ("locations", "parent_id", "CASCADE") in fks
    con.close()


def test_deleting_a_location_cascades_to_children_and_unshelves_books():
    con = A.connect()
    root = con.execute("INSERT INTO locations(name) VALUES ('Room')").lastrowid
    child = con.execute("INSERT INTO locations(parent_id, name) VALUES (?, 'Shelf')", (root,)).lastrowid
    bid = con.execute("INSERT INTO books(title, location_id) VALUES ('LT on shelf', ?)", (child,)).lastrowid
    con.commit()
    con.execute("DELETE FROM locations WHERE id=?", (root,))
    con.commit()
    assert con.execute("SELECT count(*) FROM locations").fetchone()[0] == 0
    assert con.execute("SELECT location_id FROM books WHERE id=?", (bid,)).fetchone()[0] is None
    con.close()


# --------------------------------------------------------------- migration
def legacy_db(rows):
    """A database from before locations: books with a free-text `location` and nothing else."""
    con = sqlite3.connect(Path(tempfile.mkdtemp()) / "old.db")
    con.executescript("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, location TEXT);")
    con.executemany("INSERT INTO books(title, location) VALUES (?,?)", rows)
    con.commit()
    return con


LEGACY = [("A", "Kitchen shelf"), ("B", "kitchen shelf "), ("C", "Kitchen  Shelf"), ("D", "Kitchen shelf"),
          ("E", "Office, top shelf"), ("F", None), ("G", ""), ("H", "   "), ("I", "Café nook"), ("J", "Cafe nook")]


def test_migrate_groups_spellings_into_root_locations():
    con = legacy_db(LEGACY)
    A.migrate_locations(con)
    con.commit()
    assert "location_id" in [r[1] for r in con.execute("PRAGMA table_info(books)")]
    locs = con.execute("SELECT id, parent_id, name, sort_order FROM locations ORDER BY sort_order").fetchall()
    # one root per folded, trimmed, whitespace-collapsed text; the commonest spelling names it (a tie: the first book's)
    assert [l[2] for l in locs] == ["Café nook", "Kitchen shelf", "Office, top shelf"]
    assert [l[3] for l in locs] == [0, 1, 2] and all(l[1] is None for l in locs)
    ids = {l[2]: l[0] for l in locs}
    got = dict(con.execute("SELECT title, location_id FROM books"))
    assert got == {"A": ids["Kitchen shelf"], "B": ids["Kitchen shelf"], "C": ids["Kitchen shelf"],
                   "D": ids["Kitchen shelf"], "E": ids["Office, top shelf"], "F": None, "G": None, "H": None,
                   "I": ids["Café nook"], "J": ids["Café nook"]}
    con.close()


def test_migrate_does_not_split_on_delimiters_and_keeps_the_legacy_text():
    con = legacy_db([("A", "Office, top shelf"), ("B", "Garage / bin 4"), ("C", "Den > left")])
    A.migrate_locations(con)
    con.commit()
    assert sorted(r[0] for r in con.execute("SELECT name FROM locations")) == ["Den > left", "Garage / bin 4",
                                                                                "Office, top shelf"]
    assert con.execute("SELECT count(*) FROM locations WHERE parent_id IS NOT NULL").fetchone()[0] == 0
    assert dict(con.execute("SELECT title, location FROM books")) == {"A": "Office, top shelf", "B": "Garage / bin 4",
                                                                      "C": "Den > left"}  # frozen, for rollback
    con.close()


def test_migrate_runs_once_so_a_cleared_shelf_stays_cleared():
    con = legacy_db(LEGACY)
    A.migrate_locations(con)
    con.commit()
    con.execute("UPDATE books SET location_id=NULL WHERE title='A'")
    con.commit()
    A.migrate_locations(con)  # the next start
    con.commit()
    assert con.execute("SELECT location_id FROM books WHERE title='A'").fetchone()[0] is None
    assert con.execute("SELECT count(*) FROM locations").fetchone()[0] == 3
    con.close()


def test_migrate_records_what_it_did_and_which_names_look_like_paths():
    con = legacy_db(LEGACY + [("K", "Top-shelf"), ("L", "Pantry; left")])
    A.migrate_locations(con)
    con.commit()
    info = json.loads(con.execute("SELECT value FROM meta WHERE key='locations_migrated'").fetchone()[0])
    assert info["locations"] == 5 and info["books"] == 9
    assert sorted(info["delimited"]) == ["Office, top shelf", "Pantry; left", "Top-shelf"]
    con.close()


def test_migrate_on_an_empty_database_still_marks_it_done():
    """Legacy text that shows up later (an old export imported) is the importer's job, not a second migration."""
    con = legacy_db([])
    A.migrate_locations(con)
    con.commit()
    con.execute("INSERT INTO books(title, location) VALUES ('Late', 'Kitchen')")
    con.commit()
    A.migrate_locations(con)
    assert con.execute("SELECT count(*) FROM locations").fetchone()[0] == 0
    assert con.execute("SELECT location_id FROM books WHERE title='Late'").fetchone()[0] is None
    con.close()


# --------------------------------------------------------- tree and paths
def test_tree_paths_depth_order_and_counts(c):
    kitchen = mk(c, "Kitchen")
    pantry = mk(c, "Pantry", kitchen)
    shelf2 = mk(c, "Shelf 2", pantry)
    mk(c, "Shelf 1", pantry)
    office = mk(c, "Office")
    book("a", loc=shelf2)
    book("b", loc=shelf2)
    book("c", loc=kitchen)
    book("d", loc=office)
    locs = admin_locs(c)
    assert paths(locs) == ["Kitchen", f"Kitchen{SEP}Pantry", f"Kitchen{SEP}Pantry{SEP}Shelf 2",
                           f"Kitchen{SEP}Pantry{SEP}Shelf 1", "Office"]  # depth first; siblings in the order they were added
    p = by_path(locs)
    assert [p[k]["depth"] for k in paths(locs)] == [0, 1, 2, 2, 0]
    assert p["Kitchen"]["parent_id"] is None and p[f"Kitchen{SEP}Pantry"]["parent_id"] == kitchen
    # n counts the whole subtree (what the filter returns); direct only the location itself
    assert (p["Kitchen"]["n"], p["Kitchen"]["direct"]) == (3, 1)
    assert (p[f"Kitchen{SEP}Pantry"]["n"], p[f"Kitchen{SEP}Pantry"]["direct"]) == (2, 0)
    assert (p[f"Kitchen{SEP}Pantry{SEP}Shelf 2"]["n"], p[f"Kitchen{SEP}Pantry{SEP}Shelf 1"]["n"]) == (2, 0)
    assert (p["Office"]["n"], p["Office"]["direct"]) == (1, 1)


def test_sort_order_beats_name_and_new_locations_go_last(c):
    z = mk(c, "Zed")
    a = mk(c, "Alpha")
    assert paths(admin_locs(c)) == ["Zed", "Alpha"]  # created in this order; a new one is appended
    assert c.get("/admin/api/locations", **KW).json["locations"][0]["id"] == z
    con = A.connect()
    con.execute("UPDATE locations SET sort_order=0")  # equal orders: fall back to name
    con.commit()
    con.close()
    assert paths(admin_locs(c)) == ["Alpha", "Zed"]
    assert a and z


# ------------------------------------------------------------ CRUD + checks
def test_create_validates_name_and_parent(c):
    for body in ({"name": ""}, {"name": "   "}, {}):
        assert c.post("/admin/api/locations", json=body, **KW).status_code == 400
    assert c.post("/admin/api/locations", json={"name": "X", "parent_id": 9999}, **KW).status_code == 400
    assert c.post("/admin/api/locations", json={"name": "X", "parent_id": "abc"}, **KW).status_code == 400
    r = c.post("/admin/api/locations", json={"name": "  Big   room ", "parent_id": ""}, **KW)
    assert r.status_code == 201 and r.json["location"]["name"] == "Big room" and r.json["location"]["parent_id"] is None


def test_sibling_names_are_unique_by_fold_but_not_across_parents(c):
    a = mk(c, "Kitchen")
    b = mk(c, "Den")
    mk(c, "Café", a)
    for name in ("cafe", " CAFÉ ", "Café"):
        assert c.post("/admin/api/locations", json={"name": name, "parent_id": a}, **KW).status_code == 409
    assert c.post("/admin/api/locations", json={"name": "Café", "parent_id": b}, **KW).status_code == 201
    assert c.post("/admin/api/locations", json={"name": "kitchen"}, **KW).status_code == 409  # roots too
    pantry = mk(c, "Pantry", a)
    assert c.put(f"/admin/api/locations/{pantry}", json={"name": "cafe"}, **KW).status_code == 409
    assert c.put(f"/admin/api/locations/{pantry}", json={"name": "PANTRY"}, **KW).status_code == 200  # itself is fine


def test_rename_updates_every_path_below_it(c):
    k = mk(c, "Kitchen")
    s = mk(c, "Shelf", mk(c, "Pantry", k))
    r = c.put(f"/admin/api/locations/{k}", json={"name": "Galley"}, **KW)
    assert r.status_code == 200 and r.json["location"]["path"] == "Galley"
    assert f"Galley{SEP}Pantry{SEP}Shelf" in paths(admin_locs(c))
    assert c.put(f"/admin/api/locations/{k}", json={"name": " "}, **KW).status_code == 400
    assert c.put("/admin/api/locations/9999", json={"name": "x"}, **KW).status_code == 404
    assert s


def test_move_reparents_with_its_subtree_and_books(c):
    k = mk(c, "Kitchen")
    d = mk(c, "Den")
    shelf = mk(c, "Shelf", mk(c, "Pantry", k))
    bid = book("moves", loc=shelf)
    r = c.put(f"/admin/api/locations/{k}", json={"parent_id": d}, **KW)
    assert r.status_code == 200 and r.json["location"]["path"] == f"Den{SEP}Kitchen"
    assert f"Den{SEP}Kitchen{SEP}Pantry{SEP}Shelf" in paths(admin_locs(c))
    assert c.get(f"/admin/api/books/{bid}", **KW).json["location_path"] == f"Den{SEP}Kitchen{SEP}Pantry{SEP}Shelf"
    r = c.put(f"/admin/api/locations/{k}", json={"parent_id": None}, **KW)  # back to a root, at the end
    assert r.status_code == 200 and r.json["location"]["parent_id"] is None
    assert paths(admin_locs(c))[-3:] == ["Kitchen", f"Kitchen{SEP}Pantry", f"Kitchen{SEP}Pantry{SEP}Shelf"]


def test_move_cannot_make_a_cycle(c):
    a = mk(c, "A")
    b = mk(c, "B", a)
    cc = mk(c, "C", b)
    for target in (a, b, cc):  # itself and anything below it
        r = c.put(f"/admin/api/locations/{a}", json={"parent_id": target}, **KW)
        assert r.status_code == 400, target
    assert c.put(f"/admin/api/locations/{cc}", json={"parent_id": a}, **KW).status_code == 200  # up the tree is fine
    assert c.put(f"/admin/api/locations/{b}", json={"parent_id": 9999}, **KW).status_code == 400
    con = A.connect()
    assert con.execute("SELECT parent_id FROM locations WHERE id=?", (a,)).fetchone()[0] is None
    con.close()


def test_move_onto_a_sibling_name_is_refused(c):
    a = mk(c, "A")
    mk(c, "Shelf", a)
    other = mk(c, "Shelf")
    assert c.put(f"/admin/api/locations/{other}", json={"parent_id": a}, **KW).status_code == 409


def test_reorder_rewrites_sibling_order(c):
    k = mk(c, "Kitchen")
    ids = [mk(c, n, k) for n in ("One", "Two", "Three")]
    r = c.post("/admin/api/locations/reorder", json={"parent_id": k, "ids": [ids[2], ids[0], ids[1]]}, **KW)
    assert r.status_code == 200
    assert paths(admin_locs(c))[1:] == [f"Kitchen{SEP}Three", f"Kitchen{SEP}One", f"Kitchen{SEP}Two"]
    roots = [l["id"] for l in admin_locs(c) if l["parent_id"] is None]
    other = mk(c, "Attic")
    r = c.post("/admin/api/locations/reorder", json={"parent_id": None, "ids": [other] + roots}, **KW)
    assert r.status_code == 200 and paths(admin_locs(c))[0] == "Attic"
    # the ids must be exactly the children of that parent: no more, no fewer, no strangers
    for bad in ([ids[0]], ids + [other], [ids[0], ids[0], ids[1]], ["x", 1, 2]):
        assert c.post("/admin/api/locations/reorder", json={"parent_id": k, "ids": bad}, **KW).status_code == 400
    assert c.post("/admin/api/locations/reorder", json={"parent_id": 9999, "ids": []}, **KW).status_code == 400


def test_delete_reports_what_it_took_with_it(c):
    k = mk(c, "Kitchen")
    p = mk(c, "Pantry", k)
    mk(c, "Spare", k)
    keep = mk(c, "Den")
    b1, b2, b3 = book("one", loc=p), book("two", loc=k), book("three", loc=keep)
    r = c.delete(f"/admin/api/locations/{k}", **KW)
    assert r.status_code == 200
    assert (r.json["locations_deleted"], r.json["books_unshelved"]) == (3, 2)  # Kitchen, Pantry, Spare; two books
    assert paths(admin_locs(c)) == ["Den"]
    for bid, want in ((b1, None), (b2, None), (b3, keep)):
        assert c.get(f"/admin/api/books/{bid}", **KW).json["location_id"] == want
    assert c.delete(f"/admin/api/locations/{k}", **KW).status_code == 404


def test_the_manager_needs_an_editor(c):
    k = mk(c, "Kitchen")
    for method, url, body in (("post", "/admin/api/locations", {"name": "X"}),
                              ("put", f"/admin/api/locations/{k}", {"name": "Y"}),
                              ("delete", f"/admin/api/locations/{k}", None),
                              ("post", "/admin/api/locations/reorder", {"parent_id": None, "ids": [k]}),
                              ("get", "/admin/api/locations", None)):
        kw = {"json": body} if body is not None else {}
        assert getattr(c, method)(url, headers=CF, **kw).status_code == 401, (method, url)
    assert paths(admin_locs(c)) == ["Kitchen"]


# ------------------------------------------------------- books and shelves
def test_a_book_is_shelved_and_unshelved_through_the_edit_api(c):
    shelf = mk(c, "Shelf", mk(c, "Pantry"))
    bid = book("edit")
    r = c.put(f"/admin/api/books/{bid}", json={"location_id": shelf}, **KW)
    assert r.status_code == 200
    assert (r.json["location_id"], r.json["location_path"]) == (shelf, f"Pantry{SEP}Shelf")
    assert c.put(f"/admin/api/books/{bid}", json={"title": "LT edit renamed"}, **KW).json["location_id"] == shelf  # untouched
    assert c.put(f"/admin/api/books/{bid}", json={"location_id": str(shelf)}, **KW).json["location_id"] == shelf
    r = c.put(f"/admin/api/books/{bid}", json={"location_id": None}, **KW)
    assert r.json["location_id"] is None and r.json["location_path"] is None
    assert c.put(f"/admin/api/books/{bid}", json={"location_id": shelf}, **KW).status_code == 200
    assert c.put(f"/admin/api/books/{bid}", json={"location_id": ""}, **KW).json["location_id"] is None


def test_a_bad_location_id_is_refused_and_changes_nothing(c):
    shelf = mk(c, "Shelf")
    bid = book("bad", loc=shelf)
    for bad in (9999, "abc", 1.5, [1], True):
        assert c.put(f"/admin/api/books/{bid}", json={"location_id": bad}, **KW).status_code == 400, bad
    assert c.get(f"/admin/api/books/{bid}", **KW).json["location_id"] == shelf
    r = c.post("/admin/api/books", json={"title": "LT new", "location_id": shelf}, **KW)  # create takes it too
    assert r.status_code == 201 and r.json["location_id"] == shelf
    assert c.post("/admin/api/books", json={"title": "LT new2", "location_id": 9999}, **KW).status_code == 400


def test_recipe_websites_cannot_be_shelved(c):
    shelf = mk(c, "Shelf")
    site = book("website", kind="online")
    r = c.put(f"/admin/api/books/{site}", json={"location_id": shelf}, **KW)
    assert r.status_code == 400
    assert c.put(f"/admin/api/books/{site}", json={"location_id": None}, **KW).status_code == 200  # clearing is fine


def test_the_old_text_field_is_no_longer_written_or_shown(c):
    shelf = mk(c, "Shelf")
    bid = book("legacy", loc=None, legacy="stale text")
    got = c.get(f"/admin/api/books/{bid}", **KW).json
    assert got["location"] is None and got["location_id"] is None  # frozen text never leaks into the API
    r = c.put(f"/admin/api/books/{bid}", json={"location": "Kitchen shelf 2", "location_id": shelf}, **KW)
    assert r.json["location_id"] == shelf and r.json["location"] == "Shelf"  # `location` now means the path
    con = A.connect()
    assert con.execute("SELECT location FROM books WHERE id=?", (bid,)).fetchone()[0] == "stale text"
    con.close()


def test_public_and_private_book_json_carry_the_path(c):
    shelf = mk(c, "Shelf", mk(c, "Pantry"))
    bid = book("shape", loc=shelf)
    pub = c.get(f"/api/books/{bid}").json
    assert (pub["location_id"], pub["location_path"]) == (shelf, f"Pantry{SEP}Shelf")
    assert "location" not in pub  # the private name for it stays private
    row = next(b for b in c.get("/api/books").json["books"] if b["id"] == bid)
    assert row["location_path"] == f"Pantry{SEP}Shelf"
    priv = c.get(f"/admin/api/books/{bid}", **KW).json
    assert priv["location"] == priv["location_path"] == f"Pantry{SEP}Shelf"
    other = next(b for b in c.get("/api/books").json["books"] if b["id"] != bid)
    assert other["location_id"] is None and other["location_path"] is None


# ---------------------------------------------------------------- filtering
def test_filter_by_a_location_includes_everything_below_it(c):
    k = mk(c, "Kitchen")
    pantry = mk(c, "Pantry", k)
    shelf = mk(c, "Shelf", pantry)
    den = mk(c, "Den")
    ids = {n: book(n, loc=l) for n, l in (("on-kitchen", k), ("on-pantry", pantry), ("on-shelf", shelf),
                                        ("on-den", den), ("nowhere", None))}

    def titles(url, **kw):
        return {b["title"][3:] for b in c.get(url, **kw).json["books"] if b["title"].startswith("LT ")}
    assert titles(f"/api/books?location_id={k}") == {"on-kitchen", "on-pantry", "on-shelf"}
    assert titles(f"/api/books?location_id={pantry}") == {"on-pantry", "on-shelf"}
    assert titles(f"/api/books?location_id={shelf}") == {"on-shelf"}
    assert titles(f"/admin/api/books?location_id={den}", **KW) == {"on-den"}
    assert titles(f"/api/books?location_id={k}&q=on-shelf") == {"on-shelf"}  # combines with the other filters
    assert c.get("/api/books?location_id=9999").json["books"] == []
    assert c.get("/api/books?location_id=abc").status_code == 400
    assert c.get("/api/books?location_id=").status_code == 200  # an empty parameter means no filter
    assert ids


# ------------------------------------------------------- public visibility
def test_public_list_hides_empty_rooms_and_recipe_website_shelves(c):
    k = mk(c, "Kitchen")
    pantry = mk(c, "Pantry", k)
    shelf = mk(c, "Shelf 2", pantry)
    mk(c, "Fridge", k)  # empty
    mk(c, "Office")  # empty
    garage = mk(c, "Garage Secret")
    book("public", loc=shelf)
    con = A.connect()  # a website entry shelved directly in SQL (the API refuses it, but old data or a CLI might)
    con.execute("INSERT INTO books(title, kind, location_id) VALUES ('LT site', 'online', ?)", (garage,))
    con.commit()
    con.close()
    pub = c.get("/api/locations").json["locations"]
    assert paths(pub) == ["Kitchen", f"Kitchen{SEP}Pantry", f"Kitchen{SEP}Pantry{SEP}Shelf 2"]
    p = by_path(pub)
    assert (p["Kitchen"]["n"], p["Kitchen"]["direct"]) == (1, 0)  # an ancestor is listed because of what is below
    assert (p[f"Kitchen{SEP}Pantry{SEP}Shelf 2"]["n"], p[f"Kitchen{SEP}Pantry{SEP}Shelf 2"]["direct"]) == (1, 1)
    assert "Garage" not in json.dumps(c.get("/api/locations").json)
    assert "Garage" not in json.dumps(c.get("/api/facets").json)
    assert c.get(f"/api/books?location_id={garage}").json["books"] == []
    admin = by_path(admin_locs(c))  # the editor sees everything, with the website counted
    assert set(admin) == {"Kitchen", f"Kitchen{SEP}Pantry", f"Kitchen{SEP}Pantry{SEP}Shelf 2", f"Kitchen{SEP}Fridge",
                          "Office", "Garage Secret"}
    assert admin["Garage Secret"]["n"] == 1 and admin["Office"]["n"] == 0


def test_facets_list_locations_only_when_some_book_has_one(c):
    assert c.get("/api/facets").json["locations"] == []  # nothing shelved: the filter is simply absent
    assert c.get("/api/locations").json["locations"] == []
    mk(c, "Empty room")
    assert c.get("/api/facets").json["locations"] == []
    shelf = mk(c, "Shelf")
    book("one", loc=shelf)
    assert paths(c.get("/api/facets").json["locations"]) == ["Shelf"]
    assert c.get("/api/facets").json["locations"] == c.get("/api/locations").json["locations"]
    admin = c.get("/admin/api/facets", **KW).json["locations"]  # the editor's picker needs the empty room too
    assert set(paths(admin)) == {"Empty room", "Shelf"}


def test_public_location_names_are_not_matched_loosely_to_other_rooms(c):
    """Filtering is by id, never by name: two shelves with one name under different rooms stay apart."""
    a, b = mk(c, "Kitchen"), mk(c, "Den")
    sa, sb = mk(c, "Shelf 1", a), mk(c, "Shelf 1", b)
    book("in-kitchen", loc=sa)
    book("in-den", loc=sb)
    got = {x["title"][3:] for x in c.get(f"/api/books?location_id={sa}").json["books"] if x["title"].startswith("LT ")}
    assert got == {"in-kitchen"}


def test_recipe_search_follows_the_location_filter(c):
    """With a shelf picked in the catalog, the recipe list can't show recipes from books that are somewhere else."""
    con = A.connect()
    bid = con.execute("SELECT b.id FROM books b JOIN recipes r ON r.book_id=b.id WHERE b.kind IS NULL "
                      "GROUP BY b.id HAVING count(*) > 3 LIMIT 1").fetchone()[0]
    con.close()
    name = c.get(f"/api/books/{bid}").json["recipes"][0]["name"]
    room, den = mk(c, "Room"), mk(c, "Den")
    shelf = mk(c, "Shelf", room)
    con = A.connect()
    con.execute("UPDATE books SET location_id=? WHERE id=?", (shelf, bid))
    con.commit()
    con.close()
    everywhere = c.get("/api/recipes", query_string={"q": name}).json
    here = c.get("/api/recipes", query_string={"q": name, "location_id": room}).json
    assert here["count"] >= 1 and {r["book_id"] for r in here["recipes"]} == {bid}
    assert here["count"] <= everywhere["count"]
    assert c.get("/api/recipes", query_string={"q": name, "location_id": den}).json["count"] == 0
    books = c.get("/api/recipes/books", query_string={"q": name, "location_id": room}).json["books"]
    assert [b["id"] for b in books] == [bid]
    assert c.get("/api/recipes", query_string={"q": name, "location_id": "abc"}).status_code == 400
