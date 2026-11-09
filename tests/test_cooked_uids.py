"""rebuild_cooked's resolution order with Dinner Log's uids.

manual link > recipe uid > book uid (+ page/name) > int ids (only for meals without uids) > website > title.
Ints are SQLite rowids and come back after a delete; uids never do. No HTTP: snapshots are stored directly.
"""
import json
import logging

import pytest

import app as A
import extras as X
from test_dinnerlog_http import ZUNI, DINING_IN, book_id, configured, cooked, lone_recipes, meal  # noqa: F401


def uid_of(con, table, id_):
    return con.execute(f"SELECT uid FROM {table} WHERE id=?", (id_,)).fetchone()[0]


def rebuild(con, meals):
    X.store_snapshot(con, meals, '"t"')
    res = X.rebuild_cooked(con, force=True)
    assert res["rebuilt"] is True
    return res["resolved"]


def test_recipe_uid_beats_ints_and_title(configured):
    con = configured
    zuni, dining = book_id(con, ZUNI), book_id(con, DINING_IN)
    (r1, n1, p1), (r2, _, _) = lone_recipes(con, zuni)
    m = meal(1, n1, ZUNI, str(p1), cookbook_book_id=dining, cookbook_recipe_id=r1,      # ints and title say r1
             cookbook_book_uid=uid_of(con, "books", zuni), cookbook_recipe_uid=uid_of(con, "recipes", r2))
    counts = rebuild(con, [m])
    assert cooked(con) == {1: (zuni, r2, "linked")}
    assert counts["recipe_uid"] == 1 and counts["int"] == counts["text"] == 0


def test_book_uid_then_page_and_name(configured):
    con = configured
    zuni, dining = book_id(con, ZUNI), book_id(con, DINING_IN)
    (r1, n1, p1), = lone_recipes(con, zuni, 1)
    m = meal(1, n1, "garbled title", str(p1), cookbook_book_id=dining,                    # int book is wrong
             cookbook_book_uid=uid_of(con, "books", zuni))
    counts = rebuild(con, [m])
    assert cooked(con) == {1: (zuni, r1, "auto")}
    assert counts["book_uid"] == 1
    rebuild(con, [{**m, "name": "Nothing like it", "source_detail": ""}])              # no page/name match
    assert cooked(con) == {1: (zuni, None, "book")}


def test_int_fallback_without_uids(configured):
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, _, _), (r2, n2, p2) = lone_recipes(con, zuni)
    counts = rebuild(con, [meal(1, "Whatever", "garbled", "", cookbook_book_id=zuni, cookbook_recipe_id=r1),
                           meal(2, n2, "garbled", str(p2), cookbook_book_id=zuni)])
    assert cooked(con) == {1: (zuni, r1, "linked"), 2: (zuni, r2, "auto")}
    assert counts["int"] == 2 and counts["recipe_uid"] == counts["book_uid"] == 0


def test_text_fallback_when_uids_miss(configured):
    """Uids that no longer resolve (deleted rows) skip the ints and go to title + page + name."""
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, n1, p1), (r2, _, _) = lone_recipes(con, zuni)
    m = meal(1, n1, ZUNI, str(p1), cookbook_book_id=zuni, cookbook_recipe_id=r2,
             cookbook_book_uid="f" * 32, cookbook_recipe_uid="e" * 32)
    counts = rebuild(con, [m])
    assert cooked(con) == {1: (zuni, r1, "auto")}                    # not r2, which the int names
    assert counts["text"] == 1 and counts["int"] == 0 and counts["uid_missed"] == 1


def test_reused_int_only_the_uid_gets_it_right(configured):
    """A recipe is deleted and re-imported with its uid (the importer reconciles by uid), so it gets a new id;
    a new recipe takes its old rowid. The meal's int now names the newcomer; its uid still names the original."""
    con = configured
    zuni = book_id(con, ZUNI)
    orig = con.execute("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?, 'Original Braise', '999', 999)",
                       (zuni,)).lastrowid
    orig_uid = uid_of(con, "recipes", orig)
    con.execute("DELETE FROM recipes WHERE id=?", (orig,))
    newcomer = con.execute("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?, 'Newcomer Salad', '998', 998)",
                           (zuni,)).lastrowid
    assert newcomer == orig                                          # SQLite handed the rowid out again
    moved = con.execute("INSERT INTO recipes(book_id, name, page, page_sort, uid) VALUES (?, 'Original Braise', "
                        "'999', 999, ?)", (zuni, orig_uid)).lastrowid
    con.commit()
    m = meal(1, "Original Braise", ZUNI, "999", cookbook_book_id=zuni, cookbook_recipe_id=orig,
             cookbook_book_uid=uid_of(con, "books", zuni), cookbook_recipe_uid=orig_uid)
    rebuild(con, [m])
    assert cooked(con) == {1: (zuni, moved, "linked")}
    rebuild(con, [{**m, "cookbook_book_uid": None, "cookbook_recipe_uid": None}])   # the same meal, ints only
    assert cooked(con) == {1: (zuni, newcomer, "linked")}           # ...is the bug uids fix
    con.execute("DELETE FROM recipes WHERE id IN (?, ?)", (newcomer, moved))
    con.commit()


def test_book_uid_without_recipe_uid_ignores_the_int_recipe(configured):
    """What backfill_uids leaves when the recipe int was reused inside the right book: book uid, no recipe uid.
    The int recipe id must not win; the book's page and name decide."""
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, n1, p1), (r2, _, _) = lone_recipes(con, zuni)
    rebuild(con, [meal(1, n1, ZUNI, str(p1), cookbook_book_id=zuni, cookbook_recipe_id=r2,
                       cookbook_book_uid=uid_of(con, "books", zuni))])
    assert cooked(con) == {1: (zuni, r1, "auto")}


def test_manual_link_still_first(configured):
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, n1, p1), (r2, _, _) = lone_recipes(con, zuni)
    m = meal(1, n1, ZUNI, str(p1), cookbook_recipe_uid=uid_of(con, "recipes", r1))
    con.execute("INSERT INTO dl_link VALUES (1, ?, ?)", (r2, X.meal_key("2026-10-01", n1)))
    con.commit()
    counts = rebuild(con, [m])
    assert cooked(con) == {1: (zuni, r2, "manual")} and counts["manual"] == 1


@pytest.mark.parametrize("bad", ["", "XYZ", "F" * 32, "f" * 31, 12345])
def test_malformed_uid_is_absent_not_a_miss(configured, bad):
    """Junk in a uid field mustn't switch off the int path."""
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, _, _), = lone_recipes(con, zuni, 1)
    counts = rebuild(con, [meal(1, "x", "garbled", "", cookbook_book_id=zuni, cookbook_recipe_id=r1,
                                cookbook_recipe_uid=bad, cookbook_book_uid=bad)])
    assert cooked(con) == {1: (zuni, r1, "linked")} and counts["int"] == 1 and counts["uid_missed"] == 0


def test_counts_logged_stored_and_reported(configured, caplog, monkeypatch):
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, n1, p1), (r2, n2, p2) = lone_recipes(con, zuni)
    meals = [meal(1, n1, cookbook_recipe_uid=uid_of(con, "recipes", r1)),
             meal(2, n2, "garbled", str(p2), cookbook_book_uid=uid_of(con, "books", zuni)),
             meal(3, "x", cookbook_recipe_id=r1),
             meal(4, n2, ZUNI, str(p2)),
             meal(5, "Tacos", "Taqueria", source_type="restaurant")]
    with caplog.at_level(logging.INFO, logger=A.app.logger.name):
        rebuild(con, meals)
    line = next(r.getMessage() for r in caplog.records if "history matched" in r.getMessage())
    assert line == ("Dinner Log history matched: 5 meals; manual=0 recipe_uid=1 book_uid=1 int=1 web=0 text=1 "
                    "skipped=1 uid_missed=0")
    assert n1 not in line and "Taqueria" not in line                # counts, never rows
    stored = json.loads(X.meta_get(con, "dl_resolved"))
    assert stored["recipe_uid"] == 1 and stored["skipped"] == 1
    monkeypatch.setattr(X, "pull_history", lambda con: {"ok": True})
    r = A.app.test_client().get("/admin/api/dinnerlog", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200 and r.json["resolved"] == stored


def test_migration_adds_uid_columns_and_forgets_etag(configured):
    """An older dl_meals gains the columns, and the stored ETag goes, so the next pull fetches the uids
    even when Dinner Log was upgraded first and its body (and ETag) haven't changed since."""
    con = configured
    con.executescript("""
        DROP TABLE dl_meals;
        CREATE TABLE dl_meals (id INTEGER PRIMARY KEY, day TEXT, name TEXT, source_type TEXT, source_title TEXT,
          source_detail TEXT, rating REAL, cookbook_book_id INTEGER, cookbook_recipe_id INTEGER);
        INSERT INTO dl_meals(id, day, name) VALUES (1, '2026-10-01', 'Kept');""")
    X.meta_set(con, "dl_etag", '"same-body"')
    con.commit()
    X.migrate(con)
    con.commit()
    cols = [r[1] for r in con.execute("PRAGMA table_info(dl_meals)")]
    assert cols[-2:] == ["cookbook_book_uid", "cookbook_recipe_uid"]
    assert [tuple(r) for r in con.execute("SELECT name, cookbook_recipe_uid FROM dl_meals")] == [("Kept", None)]
    assert X.meta_get(con, "dl_etag") is None
    X.meta_set(con, "dl_etag", '"later"')
    con.commit()
    X.migrate(con)                                                   # a second start leaves the ETag alone
    assert X.meta_get(con, "dl_etag") == '"later"'


def test_snapshot_keeps_uids_and_tolerates_their_absence(configured):
    con = configured
    X.store_snapshot(con, [meal(1, "a", cookbook_recipe_uid="a" * 32), meal(2, "b")], '"t"')
    rows = con.execute("SELECT id, cookbook_book_uid, cookbook_recipe_uid FROM dl_meals ORDER BY id")
    assert [tuple(r) for r in rows] == [(1, None, "a" * 32), (2, None, None)]
