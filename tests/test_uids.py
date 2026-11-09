"""Stable public ids (`uid`) on books and recipes.

`id` is a SQLite rowid and is reused after the newest row is deleted, so a Dinner Log meal stamped with a book's
id can end up pointing at a different book. `uid` never is: every row gets one on insert (trigger), rows from
before the column existed get one on start (migrate_uids), and the public API resolves it.
"""
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

import app as A

HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture()
def c():
    return A.app.test_client()


def test_seeded_rows_have_uids():
    con = A.connect()
    for table in ("books", "recipes"):
        n, missing = con.execute(f"SELECT count(*), sum(uid IS NULL) FROM {table}").fetchone()
        assert n > 0 and missing == 0
        assert all(HEX32.match(r[0]) for r in con.execute(f"SELECT uid FROM {table}"))
    assert con.execute("SELECT count(DISTINCT uid) FROM books").fetchone()[0] == \
        con.execute("SELECT count(*) FROM books").fetchone()[0]
    con.close()


def test_rowid_is_reused_but_uid_is_not():
    """The bug this guards against: the new book takes the deleted book's id, but never its uid."""
    con = A.connect()
    a = con.execute("INSERT INTO books(title) VALUES ('Newest book')").lastrowid
    uid_a = con.execute("SELECT uid FROM books WHERE id=?", (a,)).fetchone()[0]
    con.execute("DELETE FROM books WHERE id=?", (a,))
    b = con.execute("INSERT INTO books(title) VALUES ('A different book')").lastrowid
    uid_b = con.execute("SELECT uid FROM books WHERE id=?", (b,)).fetchone()[0]
    con.commit()
    assert a == b  # SQLite behaviour, not ours: INTEGER PRIMARY KEY without AUTOINCREMENT
    assert HEX32.match(uid_a) and HEX32.match(uid_b) and uid_a != uid_b
    con.execute("DELETE FROM books WHERE id=?", (b,))
    con.commit()
    con.close()


def test_trigger_covers_every_insert_path(c):
    """Insert through the editor API, the index import and plain SQL: all get a uid; a caller-supplied uid is kept."""
    r = c.post("/admin/api/books", json={"title": "Uid test"}, base_url="http://127.0.0.1:8767",
               environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 201 and HEX32.match(r.json["uid"])
    bid = r.json["id"]
    r = c.post(f"/admin/api/books/{bid}/recipes", json={"name": "Soup", "page": "3"},
               base_url="http://127.0.0.1:8767", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 201 and HEX32.match(r.json["uid"])
    r = c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": [{"name": "Stew", "page": "4"}]},
               base_url="http://127.0.0.1:8767", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200 and r.json["added"] == 1
    con = A.connect()
    uids = [r[0] for r in con.execute("SELECT uid FROM recipes WHERE book_id=?", (bid,))]
    assert len(uids) == 2 and all(HEX32.match(u) for u in uids) and len(set(uids)) == 2
    given = "a" * 32
    rid = con.execute("INSERT INTO recipes(book_id, name, uid) VALUES (?, 'Given', ?)", (bid, given)).lastrowid
    assert con.execute("SELECT uid FROM recipes WHERE id=?", (rid,)).fetchone()[0] == given
    with pytest.raises(sqlite3.IntegrityError):  # uids are unique
        con.execute("INSERT INTO recipes(book_id, name, uid) VALUES (?, 'Twin', ?)", (bid, given))
    con.rollback()
    con.execute("DELETE FROM books WHERE id=?", (bid,))
    con.commit()
    con.close()


def test_migrate_backfills_a_database_without_the_column():
    """A database made by the previous version: no uid column anywhere. After migrate_uids every row has one."""
    old = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(old)
    con.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL, name TEXT NOT NULL);
        INSERT INTO books(title) VALUES ('One'), ('Two');
        INSERT INTO recipes(book_id, name) VALUES (1, 'a'), (1, 'b'), (2, 'c');
    """)
    A.migrate_uids(con)
    con.commit()
    assert [r[1] for r in con.execute("PRAGMA table_info(books)")][-1] == "uid"
    assert con.execute("SELECT count(*) FROM books WHERE uid IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM recipes WHERE uid IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT count(DISTINCT uid) FROM recipes").fetchone()[0] == 3
    A.migrate_uids(con)  # idempotent: a second start changes nothing
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('books_uid','recipes_uid')").fetchone()[0] == 2
    con.execute("INSERT INTO books(title) VALUES ('Three')")
    assert HEX32.match(con.execute("SELECT uid FROM books WHERE title='Three'").fetchone()[0])
    con.close()


def test_public_api_exposes_uid_and_resolves_it(c):
    books = c.get("/api/books").json["books"]
    assert books and all(HEX32.match(b["uid"]) for b in books)
    b = books[0]
    full = c.get(f"/api/books/{b['id']}").json
    assert full["uid"] == b["uid"]
    assert all("uid" in r for r in full["recipes"])
    by_uid = c.get(f"/api/books/by-uid/{b['uid']}")
    assert by_uid.status_code == 200 and by_uid.json["id"] == b["id"]
    assert set(by_uid.json) == set(full)  # the same public shape, no private fields
    assert c.get("/api/books/by-uid/" + "f" * 32).status_code == 404
    assert c.get("/api/books/by-uid/not-a-uid").status_code == 404
    hit = c.get("/api/recipes?q=a&limit=1").json["recipes"]
    assert hit and HEX32.match(hit[0]["uid"])


def test_by_uid_hides_editor_only_books(c):
    con = A.connect()
    bid = con.execute("INSERT INTO books(title, kind) VALUES ('A recipe site', 'online')").lastrowid
    uid = con.execute("SELECT uid FROM books WHERE id=?", (bid,)).fetchone()[0]
    con.commit()
    assert c.get(f"/api/books/by-uid/{uid}").status_code == 404
    r = c.get(f"/admin/api/books/by-uid/{uid}", base_url="http://127.0.0.1:8767",
              environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200 and r.json["id"] == bid
    con.execute("DELETE FROM books WHERE id=?", (bid,))
    con.commit()
    con.close()


def test_export_carries_uids(c):
    kw = dict(base_url="http://127.0.0.1:8767", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    data = c.get("/admin/api/export.json", **kw).json
    assert all(HEX32.match(b["uid"]) for b in data["books"])
    assert all(HEX32.match(r["uid"]) for b in data["books"] for r in b["recipes"])
    csv_head = c.get("/admin/api/export.csv", **kw).get_data(as_text=True).splitlines()[0]
    assert csv_head.lstrip("﻿").split(",")[:2] == ["id", "uid"]
