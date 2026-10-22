"""Photos of dishes cooked from a book: upload, show on the book's page, delete, and clean up files."""
import io, os, sys, tempfile
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
import app as A  # noqa: E402
import eyb  # noqa: E402
import photos as P  # noqa: E402

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}  # through Cloudflare with no Access sign-in


@pytest.fixture()
def c():
    return A.app.test_client()


def jpeg(day=None, size=(3000, 2000)):
    img = Image.new("RGB", size, (180, 90, 40))
    exif = Image.Exif()
    if day:
        exif.get_ifd(0x8769)[36867] = day.replace("-", ":") + " 19:30:00"
    exif[0x8825] = {2: (32.0, 42.0, 0.0)}  # GPS, which must not survive the re-encode
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def upload(c, rid, raw, **form):
    return c.post(f"/admin/api/recipes/{rid}/photos", content_type="multipart/form-data",
                  data={"file": (io.BytesIO(raw), "dish.jpg"), **form})


@pytest.fixture()
def tacos(c):
    b = c.post("/admin/api/books", json={"title": "Tacos", "authors": ["Alex Stupak"]}).json
    r = c.post(f"/admin/api/books/{b['id']}/recipes", json={"name": "Cochinita pibil tacos", "page": "112"}).json
    yield b["id"], r["id"]
    c.delete(f"/admin/api/books/{b['id']}")


def test_upload_shows_on_book_page(c, tacos):
    bid, rid = tacos
    r = upload(c, rid, jpeg("2026-10-18"), caption="Sunday dinner")
    assert r.status_code == 201
    [p] = r.json["photos"]
    assert p["taken_on"] == "2026-10-18"  # from the photo's EXIF
    assert p["caption"] == "Sunday dinner"
    assert max(p["width"], p["height"]) == P.FULL

    book = c.get(f"/admin/api/books/{bid}").json
    [bp] = book["photos"]
    assert bp["recipe_name"] == "Cochinita pibil tacos" and bp["recipe_page"] == "112"

    full = c.get(bp["url"])
    assert full.status_code == 200
    img = Image.open(io.BytesIO(full.data))
    assert 0x8825 not in img.getexif()  # no GPS
    thumb = Image.open(io.BytesIO(c.get(bp["thumb"]).data))
    assert max(thumb.size) == P.THUMB


def test_date_override_and_default(c, tacos):
    _, rid = tacos
    assert upload(c, rid, jpeg("2026-10-18"), taken_on="2026-10-19").json["photos"][0]["taken_on"] == "2026-10-19"
    photos = upload(c, rid, jpeg()).json["photos"]  # no EXIF date: today
    assert date.today().isoformat() in [p["taken_on"] for p in photos]
    assert upload(c, rid, jpeg(), taken_on="last week").status_code == 400


def test_edit_and_delete(c, tacos):
    bid, rid = tacos
    p = upload(c, rid, jpeg("2026-10-18")).json["photos"][0]
    assert c.put(f"/admin/api/photos/{p['id']}", json={"caption": "Extra habanero"}).json["caption"] == "Extra habanero"
    assert c.delete(f"/admin/api/photos/{p['id']}").status_code == 200
    assert not (P.PHOTO_DIR / p["file"]).exists() and not (P.PHOTO_DIR / P.thumb_name(p["file"])).exists()
    assert c.get(f"/admin/api/books/{bid}").json["photos"] == []
    assert c.delete(f"/admin/api/photos/{p['id']}").status_code == 404


def test_deleting_recipe_or_book_removes_files(c):
    bid = c.post("/admin/api/books", json={"title": "Photo cleanup book"}).json["id"]
    r1 = c.post(f"/admin/api/books/{bid}/recipes", json={"name": "One"}).json["id"]
    r2 = c.post(f"/admin/api/books/{bid}/recipes", json={"name": "Two"}).json["id"]
    f1 = upload(c, r1, jpeg()).json["photos"][0]["file"]
    f2 = upload(c, r2, jpeg()).json["photos"][0]["file"]
    c.delete(f"/admin/api/recipes/{r1}")
    assert not (P.PHOTO_DIR / f1).exists() and (P.PHOTO_DIR / f2).exists()
    c.delete(f"/admin/api/books/{bid}")
    assert not (P.PHOTO_DIR / f2).exists() and not (P.PHOTO_DIR / P.thumb_name(f2)).exists()


def test_merge_keeps_photos(c, tacos):
    bid, good = tacos
    bad = c.post(f"/admin/api/books/{bid}/recipes", json={"name": "Cochinita pibil t�cos", "page": "112"}).json["id"]
    upload(c, bad, jpeg())
    with A.app.app_context():
        eyb.merge_recipe(A.db(), bad, good)
        A.db().commit()
    assert [p["recipe_id"] for p in c.get(f"/admin/api/books/{bid}").json["photos"]] == [good]


def test_photos_are_private(c, tacos):
    bid, rid = tacos
    p = upload(c, rid, jpeg()).json["photos"][0]
    assert "photos" not in c.get(f"/api/books/{bid}").json
    assert c.get(p["url"], headers=CF).status_code == 401
    assert upload(c, rid, jpeg()).status_code == 201
    assert c.post(f"/admin/api/recipes/{rid}/photos", headers=CF, content_type="multipart/form-data",
                  data={"file": (io.BytesIO(jpeg()), "x.jpg")}).status_code == 401


def test_bad_uploads(c, tacos):
    _, rid = tacos
    assert upload(c, rid, b"not an image").status_code == 400
    assert c.post(f"/admin/api/recipes/{rid}/photos", data={}).status_code == 400
    assert upload(c, 999999, jpeg()).status_code == 404


def test_export_lists_photos(c, tacos):
    bid, rid = tacos
    f = upload(c, rid, jpeg("2026-10-18")).json["photos"][0]["file"]
    book = next(b for b in c.get("/admin/api/export.json").json["books"] if b["id"] == bid)
    rec = next(r for r in book["recipes"] if r["id"] == rid)
    assert rec["photos"] == [{"file": f, "taken_on": "2026-10-18", "caption": None}]
