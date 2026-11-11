"""'What should we cook?': /admin/api/cook and the one ranking (extras.cook_rank) that also backs
/admin/api/suggest, the MCP suggest_recipes tool and /admin/api/pick."""
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
from dl_history import History  # noqa: E402

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}  # came through Cloudflare, no Access JWT
GARBURE = "La Garbure Landaise"                     # no chicken in the title; tagged Chicken below
ZUNI = ("The Zuni Cafe Cookbook", "342", "Zuni Roast Chicken with Bread Salad")
LEEKS = ("Downtime", "80", "Leeks with Vinagrette, Bacon, & Pine Nuts")


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture(scope="module", autouse=True)
def tags(_own_database):
    con = A.connect()
    chicken = A.name_id(con, "ingredients", "Chicken")
    rid = con.execute("SELECT id FROM recipes WHERE name=?", (GARBURE,)).fetchone()[0]
    con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, chicken))
    con.commit()
    con.close()


@pytest.fixture()
def dl(monkeypatch):
    h = History(monkeypatch)

    def add(days_ago, dish, rating=None):
        title, page, _ = dish
        h.add((date.today() - timedelta(days=days_ago)).isoformat(), dish[2], "cookbook", title, page, rating)
    return add


def cook(c, query="", **kw):
    r = c.get("/admin/api/cook?" + query, **kw)
    assert r.status_code == 200, r.json
    return r.json


def names(res):
    return [x["name"] for x in res["recipes"]]


def test_editor_only(c):
    assert c.get("/admin/api/cook", headers=CF).status_code == 401
    assert c.get("/api/cook").status_code == 404                # there is no public twin
    assert c.get("/admin/api/cook").status_code == 200           # LAN_EDIT=1 in the tests: localhost may edit


def test_modes_split_the_history(c, dl):
    dl(200, LEEKS, rating=9)        # made long ago
    dl(5, ZUNI, rating=10)          # made this week
    never = cook(c, "mode=new&limit=50")
    assert LEEKS[2] not in names(never) and ZUNI[2] not in names(never)
    assert all(x["times_made"] == 0 and x["last_made"] is None for x in never["recipes"])
    stale = cook(c, "mode=stale&limit=50")                       # made before, not within 90 days
    assert names(stale) == [LEEKS[2]]
    assert names(cook(c, "mode=stale&rest=300")) == []           # 200 days isn't "a while" with a 300-day bar
    assert set(names(cook(c, "mode=stale&rest=2"))) == {LEEKS[2], ZUNI[2]}   # a 2-day bar: both count
    want = cook(c, "mode=want&limit=50")
    assert want["recipes"] and all(x["starred"] and x["times_made"] == 0 for x in want["recipes"])
    anything = cook(c, "mode=any&limit=50")
    assert anything["pool"] == never["pool"] + 2                  # any = never made + the two we logged
    assert names(cook(c, "mode=fav&limit=50")) == [LEEKS[2]]     # rated 9 and rested; the Zuni dinner is too recent


def test_history_fields(c, dl):
    dl(10, ZUNI, rating=8)
    dl(3, ZUNI, rating=10)
    r = next(x for x in cook(c, "mode=any&with=chicken&limit=50")["recipes"] if x["name"] == ZUNI[2])
    assert r["times_made"] == 2
    assert r["last_made"] == (date.today() - timedelta(days=3)).isoformat()
    assert r["best_rating"] == 10
    assert r["book_title"] == ZUNI[0] and r["page"] == "342"


def test_ingredients_by_tag_and_title(c, dl):
    res = cook(c, "mode=any&with=chicken&limit=50")
    got = names(res)
    assert GARBURE in got and ZUNI[2] in got                    # one through the tag, one through the title
    assert all("chicken" in n.lower() or n == GARBURE for n in got)
    assert res["pool"] == len(got) and res["title_only"] == [] and res["include"] == ["chicken"]
    assert all(x["match"] for x in res["recipes"])              # says why each one matched
    both = cook(c, "mode=any&with=chicken&with=lemon&limit=50")  # every ingredient has to be used
    assert names(both) == ["Spring Roast Chicken with Preserved Lemon"]
    assert names(cook(c, "mode=any&with=chicken,lemon")) == names(both)   # comma form too
    nope = cook(c, "mode=any&with=zzzfoo")
    assert nope["recipes"] == [] and nope["title_only"] == ["zzzfoo"]    # no tag exists: told it's titles only


def test_paging_is_stable_and_complete(c):
    first = cook(c, "mode=any&limit=5&seed=7")
    assert first["limit"] == 5 and first["offset"] == 0 and len(first["recipes"]) == 5
    assert len({x["book_id"] for x in first["recipes"]}) == 5   # one per book before a book repeats
    assert cook(c, "mode=any&limit=5&seed=7") == first           # a seed makes the order reproducible
    assert names(cook(c, "mode=any&limit=5&seed=8")) != names(first)
    seen, offset = [], 0
    while offset is not None:
        page = cook(c, f"mode=any&limit=9&offset={offset}&seed=7")
        assert page["pool"] == first["pool"]
        seen += [x["id"] for x in page["recipes"]]
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == first["pool"]          # every recipe once, none twice
    past = cook(c, f"mode=any&offset={first['pool'] + 10}&seed=7")
    assert past["recipes"] == [] and past["next_offset"] is None


def test_limit_is_capped_and_inputs_are_checked(c):
    big = cook(c, "mode=any&limit=999")
    assert big["limit"] == 50 and len(big["recipes"]) == 50 and big["next_offset"] == 50
    assert cook(c, "mode=any&limit=0")["limit"] == 1
    for q in ("mode=bogus", "limit=lots", "offset=x", "rest=soon"):
        assert c.get("/admin/api/cook?" + q).status_code == 400, q


def test_cuisine_filter(c):
    res = cook(c, "mode=any&cuisine=Middle Eastern&limit=50")        # one seeded book has this cuisine and recipes
    assert res["pool"] == 17 and {x["book_title"] for x in res["recipes"]} == {"Ottolenghi Simple"}
    american = cook(c, "mode=any&cuisine=American&limit=50")
    assert {x["book_title"] for x in american["recipes"]} == {"The Zuni Cafe Cookbook", "The Lodge Cast Iron Nation"}
    assert cook(c, "mode=want&cuisine=Middle Eastern&limit=50")["pool"] <= 17   # combines with a mode
    assert cook(c, "mode=any&cuisine=Atlantean")["recipes"] == []


def test_photo_thumbnail_and_star(c):
    want = cook(c, "mode=want&limit=50")["recipes"]
    pic, bare = want[0], want[1]
    con = A.connect()
    con.execute("INSERT INTO recipe_photos(recipe_id, file, taken_on) VALUES (?,?,?)", (pic["id"], "aaa111.jpg", "2026-01-01"))
    con.execute("INSERT INTO recipe_photos(recipe_id, file, taken_on) VALUES (?,?,?)", (pic["id"], "bbb222.jpg", "2026-03-01"))
    con.commit()
    con.close()
    got = {x["id"]: x for x in cook(c, "mode=want&limit=50")["recipes"]}
    assert got[pic["id"]]["photo"] == "/photos/bbb222-t.jpg"      # the newest photo's thumbnail
    assert got[bare["id"]]["photo"] is None and got[bare["id"]]["starred"] is True


def test_one_ranking_behind_cook_suggest_and_pick(c, dl):
    dl(200, LEEKS, rating=9)
    with A.app.test_request_context():
        a = CK.cook_rank("new", n=500)
        b = CK.suggest("untried", n=500)
        p = CK.pick("new", n=500)
    ids = lambda rows: {r["id"] for r in rows}
    assert a["pool"] == b["pool"] == p["pool"] > 0
    assert ids(a["recipes"]) == ids(b["recipes"]) == ids(p["recipes"])
    assert cook(c, "mode=new&limit=50")["pool"] == a["pool"]
    # the old endpoint keeps its shape for the MCP tool and anything else calling it
    old = c.get("/admin/api/suggest?mode=favorites&n=5").json
    assert old["mode"] == "favorites" and old["label"] == CK.SUGGEST_MODES["favorites"]
    assert {"id", "name", "book_id", "book_title", "want_to_try", "cuisine", "status"} <= set(old["recipes"][0])
