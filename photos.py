"""Photos of dishes cooked from the books, attached to a recipe and shown on its book's page.

Editors only, like ratings and cooking history: the files are served under /admin, so Cloudflare Access
covers them. Each upload is re-encoded to JPEG (which also drops EXIF, phone GPS included) as a large
copy and a thumbnail in DATA_DIR/photos, which the nightly backup already copies to BACKUP_TARGET.
"""
import io, uuid
from datetime import date, datetime

from flask import abort, jsonify, request, send_from_directory

from app import DATA_DIR, app, clean, db, editor_required, make_err

PHOTO_DIR = DATA_DIR / "photos"
FULL, THUMB = 2048, 480

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe_photos (
  id INTEGER PRIMARY KEY,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  file TEXT NOT NULL, width INTEGER, height INTEGER,
  taken_on TEXT, caption TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS recipe_photos_recipe ON recipe_photos(recipe_id);
"""

try:  # iPhone photos, when the browser hands them over as HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def migrate(con):
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    con.executescript(SCHEMA)


def thumb_name(file):
    return file[:-4] + "-t.jpg"


def exif_day(img):
    """The day the photo was taken, from its EXIF, or None."""
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, else DateTime
        return datetime.strptime(str(raw).strip()[:10], "%Y:%m:%d").date().isoformat()
    except Exception:  # noqa: BLE001 — no date or a malformed one
        return None


def store(raw):
    """Save an uploaded image as <id>.jpg and <id>-t.jpg. Returns (file, width, height, day taken or None)."""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(raw))
    day = exif_day(img)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((FULL, FULL))
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(PHOTO_DIR / name, "JPEG", quality=85, optimize=True)
    w, h = img.size
    img.thumbnail((THUMB, THUMB))
    img.save(PHOTO_DIR / thumb_name(name), "JPEG", quality=80, optimize=True)
    return name, w, h, day


def unlink(files):
    for f in files:
        (PHOTO_DIR / f).unlink(missing_ok=True)
        (PHOTO_DIR / thumb_name(f)).unlink(missing_ok=True)


def files_where(con, where, *args):
    """Files of the photos matching `where`; read them before a delete cascades the rows away."""
    return [r[0] for r in con.execute(f"SELECT file FROM recipe_photos WHERE {where}", args)]


def photo_dict(r):
    d = dict(r)
    d["url"] = f"/admin/photos/{d['file']}"
    d["thumb"] = f"/admin/photos/{thumb_name(d['file'])}"
    return d


def for_book(book_id):
    """A book's photos, newest first, each with its recipe's name and page."""
    return [photo_dict(r) for r in db().execute(
        "SELECT p.*, r.name AS recipe_name, r.heading AS recipe_heading, r.page AS recipe_page "
        "FROM recipe_photos p JOIN recipes r ON r.id=p.recipe_id WHERE r.book_id=? "
        "ORDER BY coalesce(p.taken_on, substr(p.created_at, 1, 10)) DESC, p.id DESC", (book_id,))]


def for_recipe(rid):
    return [photo_dict(r) for r in db().execute(
        "SELECT * FROM recipe_photos WHERE recipe_id=? "
        "ORDER BY coalesce(taken_on, substr(created_at, 1, 10)) DESC, id DESC", (rid,))]


def valid_day(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        abort(make_err(400, "Date must be YYYY-MM-DD"))


@app.get("/admin/photos/<name>")
@editor_required
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name, max_age=86400)


@app.get("/admin/api/recipes/<int:rid>/photos")
@editor_required
def list_photos(rid):
    return jsonify(photos=for_recipe(rid))


@app.post("/admin/api/recipes/<int:rid>/photos")
@editor_required
def add_photo(rid):
    """One image per request (phone photos are big). Form fields: file, and optional taken_on and caption.
    Without taken_on the date comes from the photo's EXIF, else today."""
    con = db()
    if not con.execute("SELECT 1 FROM recipes WHERE id=?", (rid,)).fetchone():
        abort(404)
    f = request.files.get("file")
    if not f:
        return make_err(400, "No photo")
    day = valid_day(request.form.get("taken_on"))
    try:
        name, w, h, exif = store(f.read())
    except Exception as e:  # noqa: BLE001
        return make_err(400, f"Couldn't read that image: {e}")
    con.execute("INSERT INTO recipe_photos(recipe_id, file, width, height, taken_on, caption) VALUES (?,?,?,?,?,?)",
                (rid, name, w, h, day or exif or date.today().isoformat(), clean(request.form.get("caption"))))
    con.commit()
    return jsonify(photos=for_recipe(rid)), 201


@app.put("/admin/api/photos/<int:pid>")
@editor_required
def update_photo(pid):
    d = request.get_json(force=True)
    vals = {}
    if "caption" in d:
        vals["caption"] = clean(d["caption"])
    if "taken_on" in d:
        vals["taken_on"] = valid_day(d["taken_on"])
    con = db()
    if vals:
        con.execute(f"UPDATE recipe_photos SET {','.join(f'{k}=?' for k in vals)} WHERE id=?", [*vals.values(), pid])
        con.commit()
    row = con.execute("SELECT * FROM recipe_photos WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    return jsonify(photo_dict(row))


@app.delete("/admin/api/photos/<int:pid>")
@editor_required
def delete_photo(pid):
    con = db()
    files = files_where(con, "id=?", pid)
    if not files:
        abort(404)
    con.execute("DELETE FROM recipe_photos WHERE id=?", (pid,))
    con.commit()
    unlink(files)
    return jsonify(ok=True)
