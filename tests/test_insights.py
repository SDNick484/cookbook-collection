"""Insights for editors: per-book progress ("made 12 of 140"), the Stats "Shelf check", and the neglected-book
mode of the one ranking (cook.cook_rank) as it reaches the Cook page, /admin/api/suggest, the MCP suggest_recipes
tool and /admin/api/pick. All of it is cooking history, so it is editor-only: test_house.py checks that the public
site and the public MCP see none of it."""
import os, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
import app as A  # noqa: E402
import cook as CK  # noqa: E402
import mcp as MC  # noqa: E402
from dl_history import History  # noqa: E402


@pytest.fixture()
def c():
    return A.app.test_client()


def ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def add_book(con, title, pages=(), **cols):
    """A cookbook (kind NULL unless cols says otherwise) with one recipe per page, as {page: recipe id}."""
    cols = {"title": title, "status": "owned", **cols}
    bid = con.execute(f"INSERT INTO books({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      list(cols.values())).lastrowid
    ids = {p: con.execute("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?,?,?,?)",
                          (bid, f"{title} dish {p}", str(p), p)).lastrowid for p in pages}
    return bid, ids


@pytest.fixture(scope="module")
def shelf(_own_database):
    """A small shelf on top of the seeded catalog, one book per case."""
    con = A.connect()
    out = {}
    out["progress"] = add_book(con, "Zz Progress", range(1, 7))                 # 6 recipes
    out["never"] = add_book(con, "Zz Never", range(1, 4))                       # indexed, nothing made
    out["lapsed"] = add_book(con, "Zz Lapsed", range(1, 4))                     # cooked from, but 400 days ago
    out["busy"] = add_book(con, "Zz Busy Unrated", range(1, 4))                 # 3 recent dinners, no rating
    out["rated"] = add_book(con, "Zz Busy Rated", range(1, 4), rating=4.5)      # 5 dinners, but rated
    out["bare"] = add_book(con, "Zz Not Indexed")                               # no recipes at all
    out["site"] = add_book(con, "Zz Site", range(1, 3), kind="online", site="site.example")
    out["members"] = add_book(con, "Zz Members Only", range(1, 3), visibility="members")
    con.commit()
    con.close()
    return out


@pytest.fixture()
def dl(monkeypatch):
    h = History(monkeypatch)

    def add(days_ago, book_title, page, name="Dinner", rating=None):
        h.add(ago(days_ago), name, "cookbook", book_title, str(page), rating)
    return add


def rid(shelf, key, page):
    return shelf[key][1][page]


def bid(shelf, key):
    return shelf[key][0]


def book(c, b):
    r = c.get(f"/admin/api/books/{b}")
    assert r.status_code == 200
    return r.json


def listed(c, b):
    return next(x for x in c.get("/admin/api/books?limit=1000").json["books"] if x["id"] == b)


# --------------------------------------------------------------------------- 1. progress
def test_progress_without_any_history_is_zero_of_n(c, shelf):
    assert book(c, bid(shelf, "progress"))["made_count"] == 0
    assert listed(c, bid(shelf, "progress"))["made_count"] == 0
    assert listed(c, bid(shelf, "progress"))["recipe_count"] == 6
    assert not any(r["made"] for r in book(c, bid(shelf, "progress"))["recipes"])


def test_progress_counts_recipes_not_dinners_and_typed_dates(c, shelf, dl):
    dl(10, "Zz Progress", 1)
    dl(5, "Zz Progress", 1)          # the same recipe twice is one recipe made
    dl(20, "Zz Progress", 2)
    con = A.connect()                # an editor typed a date on a third recipe: that counts, like in cook_rank
    con.execute("UPDATE recipes SET last_made=? WHERE id=?", (ago(300), rid(shelf, "progress", 3)))
    con.commit()
    con.close()
    b = book(c, bid(shelf, "progress"))
    assert b["made_count"] == 3 and b["recipe_count"] == 6
    assert [r["made"] for r in b["recipes"]] == [True, True, True, False, False, False]
    assert b["cooked_count"] == 3                                   # dinners, as before: not the same number
    assert listed(c, bid(shelf, "progress"))["made_count"] == 3


def test_progress_agrees_with_the_never_made_pool(c, shelf, dl):
    """"Made" is cook_rank's definition: every cookbook recipe is either made (counted in a book's progress) or in
    the 'new' pool, never both and never neither."""
    dl(10, "Zz Progress", 1)
    with A.app.test_request_context():
        never = sum(1 for r in CK.cook_rank("new", n=5000)["recipes"] if r["kind"] is None)
    made = sum(b["made_count"] for b in c.get("/admin/api/books?limit=1000").json["books"]
               if "made_count" in b and b["status"] not in ("gone", "wanted"))
    con = A.connect()
    total = con.execute(f"SELECT count(*) FROM recipes r JOIN books b ON b.id=r.book_id WHERE {A.COOKBOOKS} "
                        "AND b.status NOT IN ('gone','wanted')").fetchone()[0]
    con.close()
    assert made and made + never == total


def test_progress_is_for_cookbooks_only(c, shelf):
    assert "made_count" not in listed(c, bid(shelf, "site"))        # saved websites: not cookbooks
    assert "made_count" not in book(c, bid(shelf, "site"))
    assert not any("made" in r for r in book(c, bid(shelf, "site"))["recipes"])


def test_a_book_with_no_index_has_zero_of_zero(c, shelf):
    assert book(c, bid(shelf, "bare"))["made_count"] == 0 and book(c, bid(shelf, "bare"))["recipe_count"] == 0


# --------------------------------------------------------------------------- 2. shelf check
def check(c):
    r = c.get("/admin/api/stats")
    assert r.status_code == 200
    return r.json


def ids(rows):
    return [x["id"] for x in rows]


def test_never_cooked_list_still_has_the_untouched_books(c, shelf, dl):
    s = check(c)
    got = set(ids(s["never_cooked"]))
    assert bid(shelf, "never") in got and bid(shelf, "bare") in got
    assert bid(shelf, "site") not in got                            # websites are not cookbooks


def test_lapsed_books_were_cooked_from_but_not_in_a_year(c, shelf, dl):
    dl(400, "Zz Lapsed", 1)
    dl(5, "Zz Busy Unrated", 1)
    s = check(c)
    lapsed = {x["id"]: x for x in s["shelf_check"]["lapsed"]}
    assert bid(shelf, "lapsed") in lapsed
    assert lapsed[bid(shelf, "lapsed")]["last"] == ago(400)
    assert bid(shelf, "busy") not in lapsed                         # cooked this week
    assert bid(shelf, "never") not in lapsed                        # never cooked: that is the other list
    assert bid(shelf, "lapsed") not in set(ids(s["never_cooked"]))
    assert bid(shelf, "site") not in lapsed
    dated = [x["last"] for x in s["shelf_check"]["lapsed"] if x["last"]]
    assert dated == sorted(dated)                                   # longest ago first


def test_lapsed_and_never_cooked_are_exactly_the_neglected_pool_books(c, shelf, dl):
    """One predicate: the books Shelf check calls quiet are the books the neglected Cook mode draws from."""
    dl(400, "Zz Lapsed", 1)
    s = check(c)
    quiet = set(ids(s["never_cooked"])) | set(ids(s["shelf_check"]["lapsed"]))
    with A.app.test_request_context():
        pool = {r["book_id"] for r in CK.cook_rank("neglected", n=5000)["recipes"]}
    owned_with_recipes = {r[0] for r in A.connect().execute(
        f"SELECT DISTINCT b.id FROM books b JOIN recipes r ON r.book_id=b.id WHERE {CK.OWNED}")}
    assert pool == quiet & owned_with_recipes


def test_most_cooked_books_without_a_rating(c, shelf, dl):
    for n in range(3):
        dl(10 + n, "Zz Busy Unrated", 1 + n)
    for n in range(5):
        dl(10 + n, "Zz Busy Rated", 1 + n % 3)
    top = check(c)["shelf_check"]["unrated_most_cooked"]
    got = {x["id"]: x for x in top}
    assert bid(shelf, "busy") in got and got[bid(shelf, "busy")]["n"] == 3
    assert bid(shelf, "rated") not in got                           # it has a rating
    assert bid(shelf, "never") not in got                           # nothing cooked
    assert [x["n"] for x in top] == sorted((x["n"] for x in top), reverse=True)
    assert len(top) <= 10


def test_index_coverage_matches_the_tiles_and_the_review_list(c, shelf):
    s = check(c)
    cov = s["shelf_check"]["index"]
    assert cov["total"] == s["books"] and cov["indexed"] == s["books_indexed"]
    assert cov["not_indexed"] == cov["total"] - cov["indexed"]
    unindexed = c.get("/admin/api/books?unindexed=1").json["books"]
    assert cov["not_indexed"] == len(unindexed)                      # what "Not indexed yet" lists
    assert bid(shelf, "bare") in ids(unindexed)


def test_shelf_check_counts_cookbooks_like_the_rest_of_the_catalog(c, shelf):
    s = check(c)
    n = A.connect().execute(f"SELECT count(*) FROM books b WHERE {A.COOKBOOKS} AND b.status NOT IN ('gone','wanted')").fetchone()[0]
    assert s["shelf_check"]["index"]["total"] == n == s["books"]
    every = ids(s["never_cooked"]) + ids(s["shelf_check"]["lapsed"]) + ids(s["shelf_check"]["unrated_most_cooked"])
    sites = {r[0] for r in A.connect().execute("SELECT id FROM books WHERE kind IS NOT NULL")}
    assert not sites & set(every)                                   # no website or house book anywhere


def test_members_only_books_are_in_the_editors_numbers(c, shelf):
    assert bid(shelf, "members") in set(ids(check(c)["never_cooked"]))   # editors see every book


def test_mcp_cooking_stats_carries_shelf_check_and_stays_compact(c, shelf, monkeypatch):
    monkeypatch.setattr(MC, "MCP_TOKEN", "tok")
    con = A.connect()
    for n in range(40):             # more lapsed books than the MCP will list
        b, _ = add_book(con, f"Zz Old {n:02d}", [1])
        con.execute("UPDATE recipes SET last_made=? WHERE book_id=?", (ago(500 + n), b))
    con.commit()
    con.close()
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "cooking_stats", "arguments": {}}},
               headers={"Authorization": "Bearer tok"}).json["result"]
    sc = r["structuredContent"]["shelf_check"]
    assert len(sc["lapsed"]) == MC.SHELF_LIST_MAX and sc["lapsed_total"] > MC.SHELF_LIST_MAX
    assert {"unrated_most_cooked", "index"} <= set(sc)
    assert len(check(c)["shelf_check"]["lapsed"]) == sc["lapsed_total"]   # the editor page is not trimmed


# --------------------------------------------------------------------------- 3. neglected mode, four entry points
def test_neglected_mode_is_in_every_entry_point(c, shelf, dl, monkeypatch):
    dl(400, "Zz Lapsed", 1)
    dl(5, "Zz Busy Unrated", 1)
    never, lapsed, busy = (set(shelf[k][1].values()) for k in ("never", "lapsed", "busy"))
    with A.app.test_request_context():
        base = CK.cook_rank("neglected", n=5000)
    pool = {r["id"] for r in base["recipes"]}
    assert never <= pool and lapsed <= pool and not busy & pool
    assert not set(shelf["site"][1].values()) & pool

    # /admin/api/cook
    r = c.get("/admin/api/cook?mode=neglected&limit=50&seed=1")
    assert r.status_code == 200 and r.json["pool"] == base["pool"] and r.json["label"] == CK.COOK_MODES["neglected"]
    assert not {x["id"] for x in r.json["recipes"]} & busy

    # /admin/api/suggest
    r = c.get("/admin/api/suggest?mode=neglected&n=20")
    assert r.status_code == 200 and r.json["pool"] == base["pool"]

    # MCP suggest_recipes (private)
    monkeypatch.setattr(MC, "MCP_TOKEN", "tok")
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "suggest_recipes", "arguments": {"mode": "neglected", "count": 20}}},
               headers={"Authorization": "Bearer tok"}).json["result"]
    assert not r.get("isError") and r["structuredContent"]["pool"] == base["pool"]

    # Dinner Log's /admin/api/pick
    r = c.get("/admin/api/pick?mode=neglected&n=50")
    assert r.status_code == 200 and r.json["mode"] == "neglected" and r.json["pool"] == base["pool"]
    assert not {x["id"] for x in r.json["recipes"]} & busy
    assert c.get("/admin/api/pick?mode=bogus").status_code == 400


def test_neglected_in_pick_keeps_pick_shape_and_ingredient_filters(c, shelf, dl):
    dl(5, "Zz Busy Unrated", 1)
    r = c.get("/admin/api/pick?mode=neglected&n=5&seed=3").json
    assert {"id", "uid", "name", "book_uid", "book_title", "last_cooked", "times_cooked"} <= set(r["recipes"][0])
    assert r["recipes"] and all(x["times_cooked"] == 0 or x["last_cooked"] for x in r["recipes"])
    only = c.get("/admin/api/pick?mode=neglected&with=zzzfoo").json
    assert only["recipes"] == [] and only["title_only"] == ["zzzfoo"]


def test_the_cook_page_offers_the_neglected_mode():
    js = (ROOT / "static" / "app.js").read_text()
    assert '["neglected", "From a neglected book"]' in js
    assert "neglected:" in js                                       # and says what an empty list means
