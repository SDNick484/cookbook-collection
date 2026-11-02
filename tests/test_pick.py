"""Dinner Log's "Pick for tonight": /admin/api/pick (ingredients in/out, favorite vs. new, READ_TOKEN)."""
import os, sqlite3, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
import app as A  # noqa: E402
import extras as X  # noqa: E402
from dl_history import History  # noqa: E402

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}  # looks like it came through Cloudflare, no Access JWT
GARBURE = "La Garbure Landaise"  # no chicken in the title; tagged Chicken below


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

    def add(days_ago, name, title, page, rating=None):
        h.add((date.today() - timedelta(days=days_ago)).isoformat(), name, "cookbook", title, page, rating)
    return add


def names(c, query):
    r = c.get("/admin/api/pick?n=50&" + query)
    assert r.status_code == 200, r.json
    return [x["name"] for x in r.json["recipes"]], r.json


def test_include_uses_tags_and_titles(c, dl):
    got, res = names(c, "mode=any&with=chicken")
    assert GARBURE in got                                   # through the tag
    assert "Zuni Roast Chicken with Bread Salad" in got     # through the title
    assert all("chicken" in n.lower() or n == GARBURE for n in got)
    assert res["pool"] == len(got) and res["title_only"] == []
    both, _ = names(c, "mode=any&with=chicken,lemon")       # every ingredient must be used
    assert set(both) == {"Spring Roast Chicken with Preserved Lemon"}


def test_exclude_drops_tagged_and_titled(c, dl):
    got, res = names(c, "mode=any&without=chicken, potatoes")
    assert got and GARBURE not in got
    assert not any("chicken" in n.lower() or "potato" in n.lower() for n in got)
    # no Potatoes tag exists: excluding it can only go by titles, and the caller is told so
    assert res["title_only"] == ["potatoes"]


def test_favorite_mode_uses_ratings_and_rest(c, dl):
    dl(200, "Leeks", "Downtime", "80", rating=9)
    dl(150, "Lamb curry", "Downtime", "217", rating=6)      # not a favorite
    dl(5, "Zuni chicken", "The Zuni Cafe Cookbook", "342", rating=10)  # favorite, but made last week
    got, res = names(c, "mode=fav")
    assert got == ["Leeks with Vinagrette, Bacon, & Pine Nuts"]
    assert res["recipes"][0]["best_rating"] == 9 and res["recipes"][0]["times_cooked"] == 1
    assert res["linked_dinners"] == ["Lamb curry", "Leeks", "Zuni chicken"]  # matched to recipes by page
    got, _ = names(c, "mode=fav&rest=0")
    assert "Zuni Roast Chicken with Bread Salad" in got
    got, _ = names(c, "mode=fav&with=chicken&rest=0")
    assert got == ["Zuni Roast Chicken with Bread Salad"]


def test_new_mode_skips_anything_cooked(c, dl):
    dl(200, "Leeks", "Downtime", "80", rating=9)
    got, _ = names(c, "mode=new&with=leeks")
    assert got == []
    got, _ = names(c, "mode=new&with=chicken")
    assert GARBURE in got and all(x for x in got)


def test_any_mode_rests_recent_dinners(c, dl):
    dl(3, "Zuni chicken", "The Zuni Cafe Cookbook", "342", rating=10)
    got, _ = names(c, "mode=any&with=chicken")
    assert "Zuni Roast Chicken with Bread Salad" not in got


def test_spread_and_seed(c, dl):
    a = c.get("/admin/api/pick?mode=any&n=4&seed=7").json["recipes"]
    b = c.get("/admin/api/pick?mode=any&n=4&seed=7").json["recipes"]
    assert [x["id"] for x in a] == [x["id"] for x in b]       # reproducible with a seed
    assert len({x["book_id"] for x in a}) == 4                # one per book first


def test_auth_and_validation(c, dl, monkeypatch):
    assert c.get("/admin/api/pick?mode=bogus").status_code == 400
    assert c.get("/admin/api/pick", headers=CF).status_code == 401
    bearer = {**CF, "Authorization": "Bearer s3cret"}
    assert c.get("/admin/api/pick", headers=bearer).status_code == 401   # no READ_TOKEN set: off
    monkeypatch.setattr(A, "READ_TOKEN", "s3cret")
    assert c.get("/admin/api/pick", headers=bearer).status_code == 200
    assert c.get("/admin/api/pick", headers={**CF, "Authorization": "Bearer nope"}).status_code == 401
    # the token is read-only: it doesn't open write routes
    assert c.post("/admin/api/books", json={"title": "X"}, headers=bearer).status_code == 401


def test_pick_terms():
    assert X.pick_terms(" Chicken, leeks ,,chicken, LEEKS ") == ["Chicken", "leeks"]
    assert len(X.pick_terms(",".join(f"i{n}" for n in range(9)))) == X.PICK_MAX_TERMS
    assert X.pick_terms(None) == []
