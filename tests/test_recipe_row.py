"""Cooking history on a book's recipe rows: editors get count, last date and rating; the public gets the count.

PUBLIC_RECIPE, PUBLIC_RECIPE_COOKED and PUBLIC_RECIPE_FLAGS are the whole public contract for a recipe row.
"""

import pytest

import app as A
import dinnerlog as DL
from dl_history import History

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}   # through Cloudflare, no Access login
ZUNI = "The Zuni Cafe Cookbook"


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture()
def cooked_book(monkeypatch):
    """Zuni with its roast chicken made twice (rated 9 and 7) and nothing else made."""
    con = A.connect()
    bid, uid = con.execute("SELECT id, uid FROM books WHERE title=?", (ZUNI,)).fetchone()
    rid, ruid = con.execute("SELECT id, uid FROM recipes WHERE book_id=? AND name LIKE 'Zuni Roast Chicken%'",
                            (bid,)).fetchone()
    con.close()
    h = History(monkeypatch)
    h.add("2026-10-01", "Roast chicken", title=ZUNI, detail="342", rating=9, cookbook_recipe_uid=ruid)
    h.add("2026-10-12", "Roast chicken", title=ZUNI, detail="342", rating=7, cookbook_recipe_uid=ruid)
    return bid, uid, rid


def row(book, rid):
    return next(r for r in book["recipes"] if r["id"] == rid)


def test_editor_row_has_count_last_and_rating(c, cooked_book):
    bid, _, rid = cooked_book
    r = row(c.get(f"/admin/api/books/{bid}").json, rid)
    assert (r["cooked_count"], r["cooked_last"], r["cooked_avg"]) == (2, "2026-10-12", 8.0)


@pytest.mark.parametrize("path", ["/api/books/{bid}", "/api/books/by-uid/{uid}"])
def test_public_row_has_only_the_count(c, cooked_book, path):
    bid, uid, rid = cooked_book
    book = c.get(path.format(bid=bid, uid=uid), headers=CF).json
    allowed = set(A.PUBLIC_RECIPE) | set(A.PUBLIC_RECIPE_COOKED) | set(A.PUBLIC_RECIPE_FLAGS)
    assert row(book, rid)["cooked_count"] == 2
    assert set(row(book, rid)) == allowed                           # exactly the public lists, nothing more
    for r in book["recipes"]:
        assert set(r) <= allowed
        if r["id"] != rid:
            assert "cooked_count" not in r                          # never made: no field at all
    assert "cooked_count" not in book and "cooked_last" not in book  # the book's own totals stay editor-only


def test_public_lists_are_the_source_of_truth(c, cooked_book, monkeypatch):
    bid, _, rid = cooked_book
    monkeypatch.setattr(A, "PUBLIC_RECIPE_COOKED", ())
    assert "cooked_count" not in row(c.get(f"/api/books/{bid}", headers=CF).json, rid)


def test_public_view_keeps_history_fresh(c, cooked_book, monkeypatch):
    """Decision: a public book page syncs like an editor's, so counts don't wait for an editor visit."""
    bid, _, _ = cooked_book
    started = []
    monkeypatch.setattr(DL, "start_background", lambda fn: started.append(fn))
    con = A.connect()
    DL.meta_set(con, "dl_pulled_at", "0")                             # stale
    DL.meta_set(con, "dl_lease", "0")
    con.execute("DELETE FROM meta WHERE key='dl_failed_at'")
    con.commit()
    con.close()
    assert c.get(f"/api/books/{bid}", headers=CF).status_code == 200
    assert started == [DL.background_pull]


def test_public_view_without_dinnerlog(c, monkeypatch):
    monkeypatch.setattr(DL, "DINNERLOG_API_URL", "")
    r = c.get("/api/books/1", headers=CF)
    assert r.status_code == 200 and all(set(x) <= set(A.PUBLIC_RECIPE) | set(A.PUBLIC_RECIPE_COOKED) | set(A.PUBLIC_RECIPE_FLAGS)
                                        for x in r.json["recipes"])


def test_row_markup_gates_dates_and_ratings_to_editors():
    js = (A.APP_DIR / "static" / "app.js").read_text()
    line = next(l for l in js.splitlines() if "made ${r.cooked_count}×" in l)
    assert "ADMIN && r.cooked_last" in line and "ADMIN && r.cooked_avg != null" in line
    assert "${extra.length ? `<div class=\"extra\">" in js          # the public row shows its count
