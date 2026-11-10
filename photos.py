"""Photos of dishes cooked from the books, attached to a recipe and shown on its book's page and in the
public Photos gallery.

Viewing is public: image files are served from /photos/<name> and /api/photos lists photos of cookbook
recipes (recipe websites, kind 'online', stay editor-only via PUBLIC_BOOKS). Uploading, editing and
deleting are editor-only under /admin. Each upload is re-encoded to JPEG (which also drops EXIF, phone GPS
included) as a large copy and a thumbnail in DATA_DIR/photos, which the nightly backup already copies to
BACKUP_TARGET. File names are random (uuid4), so a photo of an editor-only recipe can't be guessed.

Photos shared from Dinner Log arrive the same way (sync_dl_photos, from the background history pull):
re-encoded under a name of our own, with `dl_file` naming the Dinner Log file they came from.
"""
import sqlite3
import time
import uuid
from datetime import date, datetime

from flask import abort, jsonify, request, send_from_directory

from app import DATA_DIR, PUBLIC_BOOKS, app, clean, db, editor_required, make_err, open_image

PHOTO_DIR = DATA_DIR / "photos"
FULL, THUMB = 2048, 480

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe_photos (
  id INTEGER PRIMARY KEY,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  file TEXT NOT NULL, width INTEGER, height INTEGER,
  taken_on TEXT, caption TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  dl_file TEXT                                  -- the Dinner Log file it was imported from; NULL = uploaded here
);
CREATE INDEX IF NOT EXISTS recipe_photos_recipe ON recipe_photos(recipe_id);
CREATE TABLE IF NOT EXISTS dl_photo_skip (      -- Dinner Log files never to import (again)
  dl_file TEXT PRIMARY KEY,
  why TEXT NOT NULL,                            -- deleted (an editor removed the copy) | unreadable
  at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
DL_PHOTO_BATCH = 8       # imports per pull, so a first sync with many photos doesn't hold the lease too long
DL_PHOTO_BUDGET = 45     # seconds of importing per pull (the lease is extras.LEASE_TTL, 120)
_clock = time.monotonic

try:  # iPhone photos, when the browser hands them over as HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def migrate(con):
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    con.executescript(SCHEMA)
    if "dl_file" not in [r[1] for r in con.execute("PRAGMA table_info(recipe_photos)")]:
        con.execute("ALTER TABLE recipe_photos ADD COLUMN dl_file TEXT")  # existing photos: NULL, never synced
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS recipe_photos_dl_file ON recipe_photos(dl_file)")


def thumb_name(file):
    return file[:-4] + "-t.jpg"


def exif_day(img):
    """The day the photo was taken, from its EXIF, or None."""
    try:
        exif = img.getexif()
        raw = (
            exif.get_ifd(0x8769).get(36867)
            or exif.get(306)
            or (getattr(img, "_getexif", None) and (img._getexif() or {}).get(36867))
            or (getattr(img, "_getexif", None) and (img._getexif() or {}).get(306))
        )
        if not raw:
            return None
        return datetime.strptime(str(raw).strip()[:10], "%Y:%m:%d").date().isoformat()
    except Exception:  # noqa: BLE001 — no date or a malformed one
        return None


def store(raw):
    """Save an uploaded image as <id>.jpg and <id>-t.jpg. Returns (file, width, height, day taken or None)."""
    from PIL import ImageOps
    img = open_image(raw)
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
    if "dl_file" in d:  # editors see where a photo came from, never Dinner Log's file name
        d["from_dinner_log"] = d.pop("dl_file") is not None
    d["url"] = f"/photos/{d['file']}"
    d["thumb"] = f"/photos/{thumb_name(d['file'])}"
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


GALLERY_SQL = (
    "SELECT p.id, p.file, p.width, p.height, coalesce(p.taken_on, substr(p.created_at, 1, 10)) AS taken_on, "
    "p.caption, r.id AS recipe_id, r.name AS recipe_name, r.page AS recipe_page, b.id AS book_id, "
    "b.title AS book_title FROM recipe_photos p JOIN recipes r ON r.id=p.recipe_id JOIN books b ON b.id=r.book_id"
)


def gallery(private):
    """Every photo, newest first by the day cooked. The public list leaves out recipe websites."""
    sql = GALLERY_SQL + ("" if private else f" WHERE {PUBLIC_BOOKS}") + \
        " ORDER BY coalesce(p.taken_on, substr(p.created_at, 1, 10)) DESC, p.id DESC"
    return [photo_dict(r) for r in db().execute(sql)]


@app.get("/photos/<name>")
@app.get("/admin/photos/<name>")  # older links; the files are no longer private
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name, max_age=86400)


@app.get("/api/photos")
def gallery_public():
    return jsonify(photos=gallery(private=False))


@app.get("/admin/api/photos")
@editor_required
def gallery_editor():
    return jsonify(photos=gallery(private=True))


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
    # A copy imported from Dinner Log leaves a tombstone, or the next pull would bring it back. Permanent:
    # sharing it again in Dinner Log doesn't override an editor's delete.
    con.execute("INSERT OR IGNORE INTO dl_photo_skip(dl_file, why) "
                "SELECT dl_file, 'deleted' FROM recipe_photos WHERE id=? AND dl_file IS NOT NULL", (pid,))
    con.execute("DELETE FROM recipe_photos WHERE id=?", (pid,))
    con.commit()
    unlink(files)
    return jsonify(ok=True)


# ======================================================== photos shared from Dinner Log
def sync_dl_photos(con, fetch, batch=None, budget=None):
    """Make the imported copies match what Dinner Log shares, after a history pull (extras.background_pull, under
    its lease). `fetch(name)` returns the photo's bytes, None if it isn't shared any more, or raises.

    Wanted: each file in dl_photos whose dinner `cooked` resolves to a recipe here, at that recipe, unless it is
    in dl_photo_skip. In order:
      remove  imported copies no longer wanted (unshared, deleted, the dinner no longer resolves): rows, then
              files. Never capped: taking a photo down comes first.
      move    copies whose dinner now resolves to another recipe: just the recipe_id; caption and date stay.
      import  missing copies, newest dinner first, at most DL_PHOTO_BATCH and DL_PHOTO_BUDGET seconds per pull;
              the rest wait for the next one. Each goes through store() (open_image, re-encoded JPEG, no
              EXIF) with taken_on = the dinner's day. Unreadable or oversized: skipped for good. A 404: skipped
              for now (it was unshared since the pull). Any other error ends this pull's imports.
    No transaction stays open across a fetch. Uploaded photos (dl_file NULL) are never touched."""
    from extras import DLPhotoTooLarge
    batch = DL_PHOTO_BATCH if batch is None else batch
    budget = DL_PHOTO_BUDGET if budget is None else budget
    st = dict.fromkeys(("imported", "moved", "removed", "skipped", "pending"), 0)
    want = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT p.dl_file, c.recipe_id, c.day FROM dl_photos p JOIN cooked c ON c.meal_id = p.meal_id "
        "JOIN recipes r ON r.id = c.recipe_id WHERE p.dl_file NOT IN (SELECT dl_file FROM dl_photo_skip)")}
    have = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT dl_file, id, recipe_id FROM recipe_photos WHERE dl_file IS NOT NULL")}

    gone = [pid for f, (pid, _) in have.items() if f not in want]
    if gone:
        marks = ",".join("?" * len(gone))
        files = files_where(con, f"id IN ({marks})", *gone)
        con.execute(f"DELETE FROM recipe_photos WHERE id IN ({marks})", gone)
        con.commit()
        unlink(files)
        st["removed"] = len(gone)

    moves = [(want[f][0], pid) for f, (pid, rid) in have.items() if f in want and want[f][0] != rid]
    if moves:
        con.executemany("UPDATE recipe_photos SET recipe_id=? WHERE id=?", moves)
        con.commit()
        st["moved"] = len(moves)

    todo = sorted(((day or "", f, rid) for f, (rid, day) in want.items() if f not in have), reverse=True)
    start, done = _clock(), 0
    for i, (day, f, rid) in enumerate(todo):
        if done >= batch or _clock() - start > budget:
            st["pending"] = len(todo) - i
            break
        try:
            raw = fetch(f)
        except DLPhotoTooLarge as e:
            raw, why = b"", str(e)
        except Exception as e:  # noqa: BLE001 — Dinner Log unreachable, a 401, a 5xx: try again next pull
            app.logger.warning("Dinner Log photo fetch stopped: %s", getattr(e, "reason", None) or e.__class__.__name__)
            st["pending"] = len(todo) - i
            break
        done += 1
        if raw is None:
            continue
        try:
            if not raw:
                raise ValueError(why)
            name, w, h, _ = store(raw)
        except Exception as e:  # noqa: BLE001 — not an image we decode, or too big: don't fetch it every minute
            app.logger.info("Skipped Dinner Log photo %s: %s", f, e)
            con.execute("INSERT OR IGNORE INTO dl_photo_skip(dl_file, why) VALUES (?, 'unreadable')", (f,))
            con.commit()
            st["skipped"] += 1
            continue
        try:
            con.execute("INSERT INTO recipe_photos(recipe_id, file, width, height, taken_on, dl_file) "
                        "VALUES (?,?,?,?,?,?)", (rid, name, w, h, day or None, f))
            con.commit()
            st["imported"] += 1
        except sqlite3.IntegrityError:  # imported meanwhile, or the recipe is gone: drop our copy
            con.rollback()
            unlink([name])
    if any(st.values()):
        app.logger.info("Dinner Log photos: %s", " ".join(f"{k}={v}" for k, v in st.items()))
    return st
