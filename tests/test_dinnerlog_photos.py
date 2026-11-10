"""Photos shared from Dinner Log: the list stored with each history pull, the fetch, and the import/reconcile
into recipe_photos that runs in the background pull (imports, moves, removals, editor tombstones)."""
import io
import json
import sqlite3
import urllib.error

import pytest
from PIL import Image

import app as A
import extras as X
import photos as P

TOKEN = "dl-token-for-tests"
URL = "http://127.0.0.1:8000"
CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}   # through Cloudflare, no Access login

F1, F2, F3 = ("1" * 32 + ".jpg"), ("2" * 32 + ".jpg"), ("3" * 32 + ".jpg")


def jpeg(color=(200, 80, 30), size=(1600, 1200), gps=False):
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {2: (32.0, 42.0, 0.0)}
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", exif=exif)
    return out.getvalue()


class Fetch:
    """Stands in for extras.fetch_dl_photo: bytes per name, an exception to raise, or None (a 404)."""

    def __init__(self, answers=None, default=None):
        self.answers, self.calls = dict(answers or {}), []
        self.default = default

    def __call__(self, name):
        self.calls.append(name)
        a = self.answers.get(name, self.default if self.default is not None else jpeg())
        if isinstance(a, BaseException):
            raise a
        return a


@pytest.fixture()
def con(monkeypatch):
    monkeypatch.setattr(X, "DINNERLOG_API_URL", URL)
    monkeypatch.setattr(X, "DINNERLOG_TOKEN", TOKEN)
    monkeypatch.setattr(X, "start_background", lambda fn: None)   # pages never start a real pull here
    c = A.connect()
    for t in ("dl_meals", "dl_photos", "cooked", "dl_link"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM meta WHERE key LIKE 'dl\\_%' ESCAPE '\\' AND key <> 'dl_lease'")
    c.execute("UPDATE meta SET value='0' WHERE key='dl_lease'")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def client():
    return A.app.test_client()


@pytest.fixture()
def recipes(client):
    """Two recipes in a new cookbook and one in a recipe website: {name: (id, uid)} plus the book ids."""
    b = client.post("/admin/api/books", json={"title": "Weeknight Book", "authors": ["A. Cook"]}).json
    out = {}
    for name, page in (("Pan sauce chicken", "12"), ("Braised beans", "40")):
        r = client.post(f"/admin/api/books/{b['id']}/recipes", json={"name": name, "page": page}).json
        out[name] = r["id"]
    site = client.post("/admin/api/books", json={"title": "Example Recipes Site"}).json["id"]
    out["Web dal"] = client.post(f"/admin/api/books/{site}/recipes", json={"name": "Web dal"}).json["id"]
    c = A.connect()
    c.execute("UPDATE books SET kind='online', site='recipes.example.com' WHERE id=?", (site,))
    uids = {k: c.execute("SELECT uid FROM recipes WHERE id=?", (v,)).fetchone()[0] for k, v in out.items()}
    c.commit()
    c.close()
    yield {k: (out[k], uids[k]) for k in out}
    client.delete(f"/admin/api/books/{b['id']}")
    client.delete(f"/admin/api/books/{site}")


def meal(id_, recipe_uid=None, photos=None, day="2026-10-01", name="Dinner", **kw):
    m = {"id": id_, "day": day, "name": name, "source_type": "cookbook", "source_title": "", "source_detail": "",
         "rating": 8, "cookbook_book_id": None, "cookbook_recipe_id": None, "cookbook_book_uid": None,
         "cookbook_recipe_uid": recipe_uid, **kw}
    if photos is not None:
        m["photos"] = photos
    return m


def snapshot(con, meals, etag='"v"'):
    X.store_snapshot(con, meals, etag)
    X.rebuild_cooked(con, force=True)


def imported(con):
    return {r["dl_file"]: dict(r) for r in con.execute(
        "SELECT id, recipe_id, file, taken_on, caption, dl_file FROM recipe_photos WHERE dl_file IS NOT NULL")}


def skips(con):
    return dict(con.execute("SELECT dl_file, why FROM dl_photo_skip").fetchall())


# ------------------------------------------------------------------ schema

def test_upgrade_adds_tables_columns_and_forgets_the_etag(con):
    con.execute("DROP TABLE dl_photos")
    X.meta_set(con, "dl_etag", '"from-before"')
    con.commit()
    X.migrate(con)
    con.commit()
    assert X.meta_get(con, "dl_etag") is None          # else a 304 would keep the photo lists from ever arriving
    assert con.execute("SELECT count(*) FROM dl_photos").fetchone()[0] == 0
    X.meta_set(con, "dl_etag", '"kept"')
    con.commit()
    X.migrate(con)                                      # only the first time
    assert X.meta_get(con, "dl_etag") == '"kept"'


# ------------------------------------------------------------------ the stored list

def test_snapshot_stores_valid_photo_names_only(con):
    X.store_snapshot(con, [meal(1, photos=[F1, F2, "../x.jpg", F1[:-4] + "-t.jpg", 7, F1.upper()]),
                           meal(2, photos=[F2, F3]),        # a name listed twice keeps its first meal
                           meal(3, photos="nope"), meal(4)], '"a"')
    assert dict(con.execute("SELECT dl_file, meal_id FROM dl_photos").fetchall()) == {F1: 1, F2: 1, F3: 2}
    X.store_snapshot(con, [meal(1)], '"b"')                 # replaced with the history
    assert con.execute("SELECT count(*) FROM dl_photos").fetchone()[0] == 0


def test_history_without_photos_field_still_valid(con):
    body = json.dumps({"version": 1, "meals": [meal(1, photos=None)]}).encode()
    assert X._valid_history(body) is not None


# ------------------------------------------------------------------ fetch_dl_photo

class Resp:
    def __init__(self, body=b""):
        self._body = io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------ when it runs

def history_resp(meals, etag='"h1"'):
    class R(Resp):
        headers = {"ETag": etag}
    return R(json.dumps({"version": 1, "meals": meals}).encode())
