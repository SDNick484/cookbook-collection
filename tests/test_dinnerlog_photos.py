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
    for t in ("dl_meals", "dl_photos", "cooked", "dl_link", "dl_photo_skip"):
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


def test_upgrade_adds_dl_file_to_existing_photos(con, recipes):
    rid = recipes["Pan sauce chicken"][0]
    con.execute("DROP INDEX recipe_photos_dl_file")
    con.execute("ALTER TABLE recipe_photos DROP COLUMN dl_file")
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg')", (rid,))
    con.commit()
    P.migrate(con)
    con.commit()
    assert con.execute("SELECT dl_file FROM recipe_photos WHERE recipe_id=?", (rid,)).fetchone()[0] is None
    con.execute("INSERT INTO recipe_photos(recipe_id, file, dl_file) VALUES (?, 'b.jpg', ?)", (rid, F1))
    with pytest.raises(sqlite3.IntegrityError):         # one imported copy per Dinner Log file
        con.execute("INSERT INTO recipe_photos(recipe_id, file, dl_file) VALUES (?, 'c.jpg', ?)", (rid, F1))
    con.rollback()


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


def test_fetch_sends_token_through_the_no_redirect_opener(con, monkeypatch):
    seen = []

    def fake(req, timeout):
        seen.append((req, timeout))
        return Resp(b"jpegbytes")
    monkeypatch.setattr(X, "dl_open", fake)
    assert X.fetch_dl_photo(F1) == b"jpegbytes"
    req, timeout = seen[0]
    assert req.full_url == f"{URL}/api/photos/{F1}" and timeout == X.DL_PHOTO_TIMEOUT
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"


def test_fetch_404_is_none_and_other_errors_raise(con, monkeypatch):
    def answer(code):
        def fake(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return fake
    monkeypatch.setattr(X, "dl_open", answer(404))
    assert X.fetch_dl_photo(F1) is None
    for code in (302, 401, 500):                            # _NoRedirect turns a redirect into an HTTPError
        monkeypatch.setattr(X, "dl_open", answer(code))
        with pytest.raises(urllib.error.HTTPError):
            X.fetch_dl_photo(F1)


def test_fetch_caps_the_size(con, monkeypatch):
    monkeypatch.setattr(X, "DL_PHOTO_MAX_BYTES", 100)
    monkeypatch.setattr(X, "dl_open", lambda req, timeout: Resp(b"x" * 101))
    with pytest.raises(X.DLPhotoTooLarge):
        X.fetch_dl_photo(F1)
    monkeypatch.setattr(X, "dl_open", lambda req, timeout: Resp(b"x" * 100))
    assert len(X.fetch_dl_photo(F1)) == 100


def test_fetch_refuses_odd_names(con, monkeypatch):
    monkeypatch.setattr(X, "dl_open", lambda req, timeout: pytest.fail("no request for a bad name"))
    for name in ("../meals.db", F1[:-4] + "-t.jpg", "x"):
        with pytest.raises(ValueError):
            X.fetch_dl_photo(name)


# ------------------------------------------------------------------ sync: import

def test_import_shared_photo(con, recipes, client):
    rid, ruid = recipes["Pan sauce chicken"]
    snapshot(con, [meal(1, ruid, [F1], day="2026-10-12")])
    fetch = Fetch({F1: jpeg(gps=True)})
    st = P.sync_dl_photos(con, fetch)
    assert st["imported"] == 1 and fetch.calls == [F1]
    row = imported(con)[F1]
    assert row["recipe_id"] == rid and row["taken_on"] == "2026-10-12"   # the dinner's day
    assert row["file"] != F1                                               # a name of our own
    assert (P.PHOTO_DIR / row["file"]).exists() and (P.PHOTO_DIR / P.thumb_name(row["file"])).exists()
    assert not Image.open(P.PHOTO_DIR / row["file"]).getexif()             # re-encoded, no EXIF
    public = client.get("/api/photos", headers=CF).json["photos"]
    assert any(p["file"] == row["file"] for p in public)
    assert P.sync_dl_photos(con, fetch)["imported"] == 0 and fetch.calls == [F1]   # nothing left to do


def test_import_needs_a_recipe(con, recipes):
    bid = con.execute("SELECT book_id FROM recipes WHERE id=?", (recipes["Pan sauce chicken"][0],)).fetchone()[0]
    buid = con.execute("SELECT uid FROM books WHERE id=?", (bid,)).fetchone()[0]
    snapshot(con, [meal(1, None, [F1], name="Not in the index", cookbook_book_uid=buid),   # resolves to the book only
                   meal(2, None, [F2], source_type="restaurant")])                            # skipped entirely
    fetch = Fetch()
    assert P.sync_dl_photos(con, fetch)["imported"] == 0 and fetch.calls == []


def test_recipe_website_import_stays_editor_only(con, recipes, client):
    snapshot(con, [meal(1, recipes["Web dal"][1], [F1], source_type="online")])
    P.sync_dl_photos(con, Fetch())
    f = imported(con)[F1]["file"]
    assert f not in {p["file"] for p in client.get("/api/photos", headers=CF).json["photos"]}
    assert f in {p["file"] for p in client.get("/admin/api/photos").json["photos"]}


def test_import_cap_newest_first(con, recipes, monkeypatch):
    ruid = recipes["Pan sauce chicken"][1]
    monkeypatch.setattr(P, "DL_PHOTO_BATCH", 2)
    snapshot(con, [meal(1, ruid, [F1], day="2026-10-01"), meal(2, ruid, [F2], day="2026-10-03"),
                   meal(3, ruid, [F3], day="2026-10-02")])
    fetch = Fetch()
    st = P.sync_dl_photos(con, fetch)
    assert fetch.calls == [F2, F3] and st["imported"] == 2 and st["pending"] == 1
    P.sync_dl_photos(con, fetch)
    assert fetch.calls == [F2, F3, F1] and set(imported(con)) == {F1, F2, F3}


def test_import_time_budget(con, recipes, monkeypatch):
    ruid = recipes["Pan sauce chicken"][1]
    snapshot(con, [meal(1, ruid, [F1]), meal(2, ruid, [F2])])
    clock = iter([0.0, 0.0, P.DL_PHOTO_BUDGET + 1, P.DL_PHOTO_BUDGET + 2])
    monkeypatch.setattr(P, "_clock", lambda: next(clock))
    fetch = Fetch()
    assert P.sync_dl_photos(con, fetch)["imported"] == 1 and len(fetch.calls) == 1


def test_404_skips_for_now(con, recipes):
    snapshot(con, [meal(1, recipes["Pan sauce chicken"][1], [F1, F2])])
    fetch = Fetch({F1: None})
    P.sync_dl_photos(con, fetch)
    assert set(imported(con)) == {F2} and skips(con) == {}
    P.sync_dl_photos(con, fetch)
    assert fetch.calls.count(F1) == 2                         # retried: it may just have been unshared


def test_unreadable_is_remembered(con, recipes):
    snapshot(con, [meal(1, recipes["Pan sauce chicken"][1], [F1, F2])])
    fetch = Fetch({F1: b"not an image", F2: X.DLPhotoTooLarge("too big")})
    P.sync_dl_photos(con, fetch)
    assert imported(con) == {} and skips(con) == {F1: "unreadable", F2: "unreadable"}
    P.sync_dl_photos(con, fetch)
    assert len(fetch.calls) == 2                              # not fetched again every minute


def test_network_trouble_ends_the_batch(con, recipes):
    snapshot(con, [meal(1, recipes["Pan sauce chicken"][1], [F1, F2])])
    fetch = Fetch(default=OSError("connection refused"))
    st = P.sync_dl_photos(con, fetch)
    assert len(fetch.calls) == 1 and imported(con) == {} and skips(con) == {} and st["pending"] == 2


def test_lost_race_leaves_no_files(con, recipes):
    rid, ruid = recipes["Pan sauce chicken"]
    snapshot(con, [meal(1, ruid, [F1])])
    before = set(P.PHOTO_DIR.iterdir())

    def racing(name):                                          # another puller imports the same file meanwhile
        c = A.connect()
        c.execute("INSERT INTO recipe_photos(recipe_id, file, dl_file) VALUES (?, 'other.jpg', ?)", (rid, name))
        c.commit()
        c.close()
        return jpeg()
    assert P.sync_dl_photos(con, racing)["imported"] == 0
    assert set(P.PHOTO_DIR.iterdir()) == before
    con.execute("DELETE FROM recipe_photos WHERE file='other.jpg'")
    con.commit()


# ------------------------------------------------------------------ sync: reconcile

def test_unshared_or_deleted_meal_removes_the_copy(con, recipes):
    ruid = recipes["Pan sauce chicken"][1]
    snapshot(con, [meal(1, ruid, [F1, F2]), meal(2, ruid, [F3])])
    P.sync_dl_photos(con, Fetch())
    files = {k: v["file"] for k, v in imported(con).items()}
    snapshot(con, [meal(1, ruid, [F2])])                      # F1 unshared, meal 2 deleted
    fetch = Fetch()
    st = P.sync_dl_photos(con, fetch)
    assert st["removed"] == 2 and set(imported(con)) == {F2} and fetch.calls == []
    for f in (files[F1], files[F3]):
        assert not (P.PHOTO_DIR / f).exists() and not (P.PHOTO_DIR / P.thumb_name(f)).exists()
    assert (P.PHOTO_DIR / files[F2]).exists()


def test_meal_now_resolving_elsewhere_moves_the_copy(con, recipes, client):
    (r1, u1), (r2, u2) = recipes["Pan sauce chicken"], recipes["Braised beans"]
    snapshot(con, [meal(1, u1, [F1])])
    P.sync_dl_photos(con, Fetch())
    pid = imported(con)[F1]["id"]
    client.put(f"/admin/api/photos/{pid}", json={"caption": "Extra lemon"})
    snapshot(con, [meal(1, u2, [F1])])
    fetch = Fetch()
    assert P.sync_dl_photos(con, fetch)["moved"] == 1 and fetch.calls == []
    row = imported(con)[F1]
    assert (row["id"], row["recipe_id"], row["caption"]) == (pid, r2, "Extra lemon")   # same copy, editor's caption kept


def test_meal_no_longer_resolving_removes_the_copy(con, recipes):
    snapshot(con, [meal(1, recipes["Pan sauce chicken"][1], [F1])])
    P.sync_dl_photos(con, Fetch())
    snapshot(con, [meal(1, None, [F1], source_type="restaurant")])
    assert P.sync_dl_photos(con, Fetch())["removed"] == 1 and imported(con) == {}


def test_editor_delete_leaves_a_permanent_tombstone(con, recipes, client):
    ruid = recipes["Pan sauce chicken"][1]
    snapshot(con, [meal(1, ruid, [F1])])
    P.sync_dl_photos(con, Fetch())
    assert client.delete(f"/admin/api/photos/{imported(con)[F1]['id']}").status_code == 200
    assert skips(con) == {F1: "deleted"}
    fetch = Fetch()
    P.sync_dl_photos(con, fetch)
    assert imported(con) == {} and fetch.calls == []
    snapshot(con, [meal(1, ruid, [])])                         # unshared, then shared again: still gone
    snapshot(con, [meal(1, ruid, [F1])])
    P.sync_dl_photos(con, fetch)
    assert imported(con) == {} and fetch.calls == []


def test_editor_delete_of_own_upload_leaves_no_tombstone(con, recipes, client):
    rid = recipes["Pan sauce chicken"][0]
    up = client.post(f"/admin/api/recipes/{rid}/photos", content_type="multipart/form-data",
                     data={"file": (io.BytesIO(jpeg()), "x.jpg")}).json["photos"][0]
    client.delete(f"/admin/api/photos/{up['id']}")
    assert skips(con) == {}


def test_uploaded_photos_are_never_touched(con, recipes, client):
    rid = recipes["Pan sauce chicken"][0]
    up = client.post(f"/admin/api/recipes/{rid}/photos", content_type="multipart/form-data",
                     data={"file": (io.BytesIO(jpeg()), "x.jpg")}).json["photos"][0]
    snapshot(con, [])
    P.sync_dl_photos(con, Fetch())
    assert con.execute("SELECT count(*) FROM recipe_photos WHERE id=?", (up["id"],)).fetchone()[0] == 1


# ------------------------------------------------------------------ when it runs

def history_resp(meals, etag='"h1"'):
    class R(Resp):
        headers = {"ETag": etag}
    return R(json.dumps({"version": 1, "meals": meals}).encode())


def test_background_pull_syncs_after_200_and_304(con, recipes, monkeypatch):
    ruid = recipes["Pan sauce chicken"][1]
    calls = []
    monkeypatch.setattr(P, "sync_dl_photos", lambda c, fetch: calls.append(fetch) or {})
    monkeypatch.setattr(X, "dl_open", lambda req, timeout: history_resp([meal(1, ruid, [F1])]))
    X.background_pull()
    assert calls == [X.fetch_dl_photo]

    def not_modified(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 304, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(X, "dl_open", not_modified)
    X.background_pull()
    assert len(calls) == 2                                     # a capped backlog keeps going on a 304


def test_background_pull_skips_sync_after_a_failed_pull(con, monkeypatch):
    calls = []
    monkeypatch.setattr(P, "sync_dl_photos", lambda c, fetch: calls.append(fetch) or {})

    def down(req, timeout):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(X, "dl_open", down)
    X.background_pull()
    assert calls == []


def test_sync_crash_doesnt_look_like_a_failed_pull(con, recipes, monkeypatch):
    def boom(c, fetch):
        raise RuntimeError("bug")
    monkeypatch.setattr(P, "sync_dl_photos", boom)
    monkeypatch.setattr(X, "dl_open", lambda req, timeout: history_resp([meal(1)]))
    X.background_pull()
    assert X.meta_get(con, "dl_error") is None and X.meta_get(con, "dl_lease") == "0"


# ------------------------------------------------------------------ what visitors and editors see

def test_imported_photos_cache_briefly(con, recipes, client):
    snapshot(con, [meal(1, recipes["Pan sauce chicken"][1], [F1])])
    P.sync_dl_photos(con, Fetch())
    f = imported(con)[F1]["file"]
    for name in (f, P.thumb_name(f)):
        r = client.get(f"/photos/{name}", headers=CF)
        assert r.status_code == 200 and r.cache_control.max_age == P.DL_PHOTO_MAX_AGE
    rid = recipes["Braised beans"][0]
    up = client.post(f"/admin/api/recipes/{rid}/photos", content_type="multipart/form-data",
                     data={"file": (io.BytesIO(jpeg()), "x.jpg")}).json["photos"][0]
    assert client.get(up["url"], headers=CF).cache_control.max_age == 86400


def test_dl_file_never_reaches_output(con, recipes, client):
    rid, ruid = recipes["Pan sauce chicken"]
    snapshot(con, [meal(1, ruid, [F1])])
    P.sync_dl_photos(con, Fetch())
    bid = con.execute("SELECT book_id FROM recipes WHERE id=?", (rid,)).fetchone()[0]
    for body in (client.get("/api/photos", headers=CF).get_data(as_text=True),
                 client.get("/admin/api/photos").get_data(as_text=True),
                 client.get(f"/admin/api/books/{bid}").get_data(as_text=True),
                 client.get(f"/admin/api/recipes/{rid}/photos").get_data(as_text=True)):
        assert F1 not in body and "dl_file" not in body
    [p] = client.get(f"/admin/api/recipes/{rid}/photos").json["photos"]
    assert p["from_dinner_log"] is True
    assert "from_dinner_log" not in client.get("/api/photos", headers=CF).json["photos"][0]


def test_export_and_import_keep_dl_file(con, recipes, client):
    import importer
    rid, ruid = recipes["Pan sauce chicken"]
    snapshot(con, [meal(1, ruid, [F1])])
    P.sync_dl_photos(con, Fetch())
    bid = con.execute("SELECT book_id FROM recipes WHERE id=?", (rid,)).fetchone()[0]
    data = client.get("/admin/api/export.json").json
    book = next(b for b in data["books"] if b["id"] == bid)
    [ph] = next(r for r in book["recipes"] if r["id"] == rid)["photos"]
    assert ph["dl_file"] == F1
    con.execute("UPDATE recipe_photos SET dl_file=NULL WHERE dl_file=?", (F1,))   # as a restore would find it
    con.commit()
    importer.import_data(con, {**data, "books": [book]})
    assert imported(con)[F1]["file"] == ph["file"]
    fetch = Fetch()
    assert P.sync_dl_photos(con, fetch)["imported"] == 0 and fetch.calls == []   # no duplicate after a restore


def test_import_doesnt_steal_a_tracked_dl_file(con, recipes, client):
    import importer
    (r1, u1), (r2, _) = recipes["Pan sauce chicken"], recipes["Braised beans"]
    snapshot(con, [meal(1, u1, [F1])])
    P.sync_dl_photos(con, Fetch())
    con.execute("INSERT INTO recipe_photos(recipe_id, file) VALUES (?, 'dddddddddddddddddddddddddddddddd.jpg')", (r2,))
    con.commit()
    bid = con.execute("SELECT book_id FROM recipes WHERE id=?", (r2,)).fetchone()[0]
    data = client.get("/admin/api/export.json").json
    book = next(b for b in data["books"] if b["id"] == bid)
    for r in book["recipes"]:
        for p in r.get("photos", []):
            if p["file"].startswith("dddd"):
                p["dl_file"] = F1                               # an export that claims the tracked file again
    importer.import_data(con, {**data, "books": [book]})
    assert imported(con)[F1]["recipe_id"] == r1
    con.execute("DELETE FROM recipe_photos WHERE file LIKE 'dddd%'")
    con.commit()
