"""Load a Cookbook Collection JSON export (More -> Download JSON, or GET /admin/api/export.json) into a catalog.

    python3 -m importer EXPORT.json [--photos DIR] [--dry-run] [--replace]

It uses the catalog in $DATA_DIR (default ./data), like the app. Stop the service first (see the README).

Books and recipes are reconciled by their stable `uid`: a uid that exists is updated, an unknown one is inserted
with that uid. The columns written are the ones both the file and this database have, so a column added to the
schema later travels without a change here. Everything goes through the app's own helpers (`clean`, `fold`,
`set_people`, `name_id`, `reindex_book`) on `app.connect()`, because the search triggers call the `fold()` SQL
function that only connect() registers.

A book's shelf travels as the path in its `location` ("Kitchen › Pantry › Shelf 2"): each level is found under its
parent (case, accents and spacing don't count) or created, so the tree is rebuilt. An older export's flat text
("Kitchen shelf") becomes one root location. The text is only ever split on "›", never on commas or slashes.
`location_id` is skipped: it is a row id from the database that wrote the file. Empty locations and their order are
not in the export, so they are not restored; --replace leaves locations nobody is shelved in alone.

All writes happen in one transaction: a failure, or --dry-run, rolls the whole import back.
"""
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

SAFE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:jpe?g|png|webp)", re.I)  # a bare name, never a path
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")
DAY = re.compile(r"\d{4}-\d\d-\d\d")
STATUSES = ("owned", "out", "loaned", "gone", "wanted")
TABLES = ("books", "recipes", "photos")


class ImportFileError(ValueError):
    """The file can't be imported at all (not an export, or a newer format). Nothing was changed."""


def _app():
    import app  # imported late: the CLI must set SKIP_SEED before app's init_db() runs at import
    return app


class Report:
    """What an import did (or, for --dry-run, would do)."""

    def __init__(self):
        self.counts = {t: {"insert": 0, "update": 0, "skip": 0, "delete": 0} for t in TABLES}
        self.new_names = {"authors": 0, "tags": 0, "ingredients": 0, "locations": 0}
        self.hints_set = 0
        self.deleted_books = []  # (uid, title) of the books --replace removes (or, in a dry run, would)
        self.dry_run = False
        # Cover and photo files the export references. "copied" is "would copy" in a dry run.
        self.files = {"present": 0, "copied": 0, "missing": [], "unusable": []}
        self.conflicts = []  # (what, label, why): rows left out because they'd clash with something already there
        self.warnings = []   # values that were dropped or corrected

    def merge(self, other):
        for t in TABLES:
            for k, n in other.counts[t].items():
                self.counts[t][k] += n
        self.conflicts += other.conflicts
        self.warnings += other.warnings


# ------------------------------------------------------------------ values
def _columns(con, table):
    """{column: (declared type, NOT NULL)}, read from the live schema."""
    return {r[1]: (r[2].upper(), bool(r[3])) for r in con.execute(f"PRAGMA table_info({table})")}


def _coerce(v, typ):
    """`v` as the column's type, or None if it can't be."""
    A = _app()
    if v is None:
        return None
    if "INT" in typ:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v) if math.isfinite(v) and v.is_integer() else None
        return int(v) if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()) else None
    if "REAL" in typ:
        if isinstance(v, bool):
            return None
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                return None
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None
    return A.clean(v if isinstance(v, str) else str(v))


def _typed(cols, src, skip):
    """The values in `src` for the columns this database has, coerced to their types and cleaned. A NULL for a
    NOT NULL column is left out, so the column keeps its default (insert) or its current value (update)."""
    vals = {}
    for col, (typ, notnull) in cols.items():
        if col in skip or col not in src:
            continue
        v = _coerce(src[col], typ)
        if v is None and notnull:
            continue
        vals[col] = v
    return vals


def _uid(src, label, rep):
    uid = src.get("uid")
    if uid is None:
        return None
    if isinstance(uid, str) and _app().UID_RE.fullmatch(uid):
        return uid
    rep.warnings.append(f"{label}: uid {uid!r} is not a 32-character hex id, so it is ignored")
    return None


def _file_name(v, label, what, rep):
    """A cover or photo file name from the file, or None. Names end up in `COVER_DIR / name` and are unlinked when
    a book is deleted, so only a plain image file name is accepted: no directories, no `..`."""
    name = _app().clean(v if isinstance(v, str) else None)
    if name and not SAFE_FILE.fullmatch(name):
        rep.warnings.append(f"{label}: {what} file name {name!r} is not a plain image file name, left out")
        return None
    return name


def _names(ctx, v):
    """A list of authors/tags from the file, cleaned, one spelling per folded name; None if there isn't one."""
    if not isinstance(v, list):
        return None
    A = ctx
    seen = {}
    for x in v:
        n = A.clean(x if isinstance(x, str) else str(x))
        if n:
            seen.setdefault(A.fold(n), n)
    return list(seen.values())


def _book_vals(src, cols, label, rep):
    A = _app()
    vals = _typed(cols, src, skip={"id", "uid", "created_at", "updated_at", "cover", "location", "location_id"})
    if "isbn" in vals:
        raw = vals["isbn"]
        vals["isbn"] = A.normalize_isbn(raw) if raw else None
        if raw and not vals["isbn"]:
            rep.warnings.append(f"{label}: ISBN {raw!r} is not valid, left empty")
    if vals.get("status", "owned") not in STATUSES:
        vals["status"] = "owned"
    if "signed" in vals:
        vals["signed"] = 1 if vals["signed"] else 0
    r = vals.get("rating")
    if r is not None and not (0 <= r <= 5 and (r * 2).is_integer()):
        rep.warnings.append(f"{label}: book rating {r} is not 0-5 in half stars, left empty")
        vals["rating"] = None
    if "cover" in src:
        vals["cover"] = _file_name(src["cover"], label, "cover", rep) if src["cover"] else None
        if vals["cover"] is None and src["cover"] and "cover" in vals:
            del vals["cover"]  # an unsafe name: keep the cover the book has
    return vals


def _recipe_vals(src, cols, label, rep):
    A = _app()
    vals = _typed(cols, src, skip={"id", "book_id", "uid", "created_at", "page_sort"})
    if "want_to_try" in vals:
        vals["want_to_try"] = 1 if vals["want_to_try"] else 0
    r = vals.get("rating")
    if r is not None and not 0 <= r <= 10:
        rep.warnings.append(f"{label}: rating {r} is not 0-10, left empty")
        vals["rating"] = None
    if "page" in vals:
        vals["page_sort"] = A.page_sort(vals["page"])
    return vals


# ------------------------------------------------------------------ cover and photo files
# Where a file lives: covers in COVER_DIR, photos and their thumbnails in PHOTO_DIR. --photos DIR may be a copy of
# the data folder or of BACKUP_TARGET/files/<app> (covers/ and photos/ inside), or one flat folder of files.
SUBDIR = {"cover": "covers", "photo": "photos", "thumb": "photos"}


def _places(ctx, kind, name):
    """(where the file belongs here, where --photos might have it)."""
    import photos
    dest = (ctx.A.COVER_DIR if kind == "cover" else photos.PHOTO_DIR) / name
    if ctx.photos_dir is None:
        return dest, []
    return dest, [ctx.photos_dir / SUBDIR[kind] / name, ctx.photos_dir / name]


def _is_regular(path):
    """A plain file; a symlink doesn't count, so a backup folder can't point the import at files elsewhere."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _image_size(path):
    """(width, height) if `path` is an image of a kind the app accepts for uploads (JPEG, PNG, WebP, HEIC) and not
    absurdly big, else None. Decoding goes through the app's own open_image, which limits the formats."""
    A = _app()
    try:
        with open(path, "rb") as f:
            raw = f.read(A.app.config["MAX_CONTENT_LENGTH"] + 1)
        if len(raw) > A.app.config["MAX_CONTENT_LENGTH"]:
            return None
        return A.open_image(raw).size
    except Exception:  # noqa: BLE001 — not an image, truncated, or a decompression bomb: all "unusable"
        return None


def _known_size(ctx, kind, name):
    """The size of an image we already have or are about to copy, or None (the row is kept without one)."""
    dest, sources = _places(ctx, kind, name)
    for path in [dest, *sources]:
        if _is_regular(path):
            return _image_size(path)
    return None


def _resolve(ctx, kind, name, label, rep, plan):
    """Decide what happens to one file: already here, to be copied (added to `plan`), missing, or not an image.
    True if it is here or will be."""
    dest, sources = _places(ctx, kind, name)
    if os.path.lexists(dest):
        rep.files["present"] += 1
        return True
    found = next((s for s in sources if _is_regular(s)), None)
    if found is None:
        rep.files["missing"].append(label)
        return False
    if _image_size(found) is None:
        rep.files["unusable"].append(f"{label} (not a JPEG, PNG, WebP or HEIC image)")
        return False
    plan.append((found, dest, label))
    rep.files["copied"] += 1
    return True


def _transfer(ctx, rep, dry_run):
    """Copy the referenced files that are missing here and found in --photos, after the database commit, so a
    rolled-back import leaves no files behind. Nothing already here is overwritten. A missing file is reported."""
    import photos
    plan = []
    for kind, name in sorted(ctx.refs):
        here = _resolve(ctx, kind, name, f"{kind} {name}", rep, plan)
        if here and kind == "photo":
            thumb = photos.thumb_name(name)
            _resolve(ctx, "thumb", thumb, f"thumbnail {thumb}", rep, plan)
    if dry_run:
        return
    for src, dest, label in plan:
        tmp = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            rep.files["copied"] -= 1
            rep.files["unusable"].append(f"{label} (copy failed: {e.strerror or e})")


# ------------------------------------------------------------------ books
def _current_people(con, bid):
    authors = [r[0] for r in con.execute(
        "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=? ORDER BY ba.pos", (bid,))]
    tags = [r[0] for r in con.execute(
        "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=? ORDER BY t.name", (bid,))]
    return authors, tags


def _set_people(con, bid, src):
    """Make the book's authors and tags the file's (a list the file lacks is left alone). True if they changed."""
    A = _app()
    cur_a, cur_t = _current_people(con, bid)
    want_a, want_t = _names(A, src.get("authors")), _names(A, src.get("tags"))
    new_a = cur_a if want_a is None else want_a
    new_t = cur_t if want_t is None else want_t
    same = ([A.fold(x) for x in new_a] == [A.fold(x) for x in cur_a]
            and sorted(map(A.fold, new_t)) == sorted(map(A.fold, cur_t)))
    if not same:
        A.set_people(con, bid, new_a, new_t)
    return not same


FIRST_AUTHOR = ("coalesce((SELECT fold(a.name) FROM book_authors ba JOIN authors a ON a.id=ba.author_id "
                "WHERE ba.book_id=b.id ORDER BY ba.pos LIMIT 1), '')")


def _find_book(ctx, src, uid, vals):
    """The local book this entry is: by uid when it has one (a uid that isn't here means a new book). Without a
    uid (older exports) by ISBN, then by folded title plus first author. A local book answers to one entry only,
    so two look-alike entries become two books."""
    con, A = ctx.con, ctx.A
    if uid:
        row = con.execute("SELECT id FROM books WHERE uid=?", (uid,)).fetchone()
        if row:
            ctx.claimed_books.add(row[0])
            return row[0]
        return None
    if vals.get("isbn"):
        row = con.execute("SELECT id FROM books WHERE isbn=?", (vals["isbn"],)).fetchone()
        if row and row[0] not in ctx.claimed_books:
            ctx.claimed_books.add(row[0])
            return row[0]
    if vals.get("title"):
        authors = _names(A, src.get("authors")) or []
        first = A.fold(authors[0]) if authors else ""
        for (bid,) in con.execute(f"SELECT b.id FROM books b WHERE fold(b.title)=fold(?) AND {FIRST_AUTHOR}=? "
                                  "ORDER BY b.id", (vals["title"], first)).fetchall():
            if bid not in ctx.claimed_books:
                ctx.claimed_books.add(bid)
                return bid
    return None


def _shelf(ctx, raw):
    """The location id for a shelf path, creating the levels that are missing; None for no shelf."""
    A, con = ctx.A, ctx.con
    parent = None
    for name in filter(None, (A.loc_name(part) for part in raw.split("›"))):
        found = A.location_clash(con, parent, name)  # a sibling of that name, by folded text
        if found is None:
            found = con.execute("INSERT INTO locations(parent_id, name, sort_order) VALUES (?,?,?)",
                                (parent, name, A.next_sort_order(con, parent))).lastrowid
        parent = found
    return parent


def _book(ctx, src, label, rep):
    """Insert or update one book. Returns its id, or None if it was left out."""
    A, con = ctx.A, ctx.con
    uid = _uid(src, label, rep)
    vals = _book_vals(src, ctx.cols["books"], label, rep)
    if "location" in src and not src.get("kind"):  # a missing key leaves the shelf alone; null takes the book off it
        if src["location"] is None or isinstance(src["location"], str):
            vals["location_id"] = _shelf(ctx, src["location"]) if src["location"] else None
        else:
            rep.warnings.append(f"{label}: location {src['location']!r} is not text, left alone")
    if vals.get("cover"):
        ctx.book_files.add(("cover", vals["cover"]))
    bid = _find_book(ctx, src, uid, vals)
    if bid is None:
        if not vals.get("title"):
            rep.warnings.append(f"{label}: no title, left out")
            rep.counts["books"]["skip"] += 1
            return None
        clash = con.execute("SELECT id, title FROM books WHERE isbn=?", (vals["isbn"],)).fetchone() \
            if vals.get("isbn") else None
        if clash:
            ctx.keep.add(clash["id"])  # the local book it clashes with must survive a --replace
            rep.conflicts.append(("book", label, f"ISBN {vals['isbn']} is already on {clash['title']!r} here"))
            rep.counts["books"]["skip"] += 1
            return None
        ins = dict(vals)
        if uid:
            ins["uid"] = uid
        for k in ("created_at", "updated_at"):
            if isinstance(src.get(k), str) and TIMESTAMP.match(src[k]):
                ins[k] = src[k]
        bid = con.execute(f"INSERT INTO books({','.join(ins)}) VALUES ({','.join('?' * len(ins))})",
                          list(ins.values())).lastrowid
        _set_people(con, bid, src)
        A.reindex_book(con, bid)
        rep.counts["books"]["insert"] += 1
        ctx.keep.add(bid)
        ctx.claimed_books.add(bid)
        return bid
    ctx.keep.add(bid)
    cur = con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
    changes = {k: v for k, v in vals.items() if cur[k] != v}
    if changes.get("isbn"):
        clash = con.execute("SELECT title FROM books WHERE isbn=? AND id<>?", (changes["isbn"], bid)).fetchone()
        if clash:
            rep.warnings.append(f"{label}: ISBN {changes['isbn']} is already on {clash['title']!r} here, kept this book's own")
            del changes["isbn"]
    people = _set_people(con, bid, src)
    if changes or people:
        sets = "".join(f"{k}=?," for k in changes)
        con.execute(f"UPDATE books SET {sets} updated_at=CURRENT_TIMESTAMP WHERE id=?", [*changes.values(), bid])
        A.reindex_book(con, bid)
        rep.counts["books"]["update"] += 1
    else:
        rep.counts["books"]["skip"] += 1
    return bid


# ------------------------------------------------------------------ recipes
def _find_recipe(ctx, bid, uid, vals):
    """The local recipe this entry is: by uid (anywhere, so a recipe moved to another book follows it). Without
    one, inside the book: by the web id of a recipe from a website, then by folded name and page. A local recipe
    answers to one entry only."""
    con = ctx.con
    if uid:
        row = con.execute("SELECT * FROM recipes WHERE uid=?", (uid,)).fetchone()
        if row:
            ctx.claimed_recipes.add(row["id"])
        return row
    rows = []
    if vals.get("ext_id"):
        rows += con.execute("SELECT * FROM recipes WHERE book_id=? AND ext_id=?", (bid, vals["ext_id"])).fetchall()
    if vals.get("name"):
        rows += con.execute("SELECT * FROM recipes WHERE book_id=? AND fold(name)=fold(?) AND coalesce(page,'')=? "
                            "ORDER BY id", (bid, vals["name"], vals.get("page") or "")).fetchall()
    for row in rows:
        if row["id"] not in ctx.claimed_recipes:
            ctx.claimed_recipes.add(row["id"])
            return row
    return None


def _ingredient_names(ctx, v):
    """Ingredient tag names from the file, in the form the app stores them; None if the file has no list."""
    if not isinstance(v, list):
        return None
    seen = {}
    for x in v:
        n = ctx.ing.clean_name(x if isinstance(x, str) else str(x))
        if n:
            seen.setdefault(ctx.A.fold(n), n)
    return list(seen.values())


def _set_ingredients(ctx, rid, names):
    con, A = ctx.con, ctx.A
    cur = {A.fold(r[0]) for r in con.execute(
        "SELECT i.name FROM ingredients i JOIN recipe_ingredients ri ON ri.ingredient_id=i.id WHERE ri.recipe_id=?", (rid,))}
    if cur == {A.fold(n) for n in names}:
        return False
    con.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (rid,))
    for n in names:
        con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, A.name_id(con, "ingredients", n)))
    return True


def _set_photos(ctx, rid, photos, label, rep):
    """Photo rows are matched by file name, which is random per photo, so it is their identity."""
    A, con = ctx.A, ctx.con
    listed = ctx.book_photos.setdefault(rid, set())
    if not isinstance(photos, list):
        return
    for p in photos:
        if not isinstance(p, dict):
            continue
        name = _file_name(p.get("file"), label, "photo", rep)
        if not name:
            continue
        listed.add(name)
        ctx.book_files.add(("photo", name))
        day = A.clean(p.get("taken_on") if isinstance(p.get("taken_on"), str) else None)
        day = day if day and DAY.fullmatch(day) else None
        caption = A.clean(p.get("caption") if isinstance(p.get("caption"), str) else None)
        row = con.execute("SELECT id, taken_on, caption FROM recipe_photos WHERE recipe_id=? AND file=?",
                          (rid, name)).fetchone()
        if row is None:
            width, height = _known_size(ctx, "photo", name) or (None, None)
            con.execute("INSERT INTO recipe_photos(recipe_id, file, width, height, taken_on, caption) VALUES (?,?,?,?,?,?)",
                        (rid, name, width, height, day, caption))
            rep.counts["photos"]["insert"] += 1
        elif (row["taken_on"], row["caption"]) != (day, caption):
            con.execute("UPDATE recipe_photos SET taken_on=?, caption=? WHERE id=?", (day, caption, row["id"]))
            rep.counts["photos"]["update"] += 1
        else:
            rep.counts["photos"]["skip"] += 1


def _recipe(ctx, bid, src, label, rep):
    A, con = ctx.A, ctx.con
    uid = _uid(src, label, rep)
    vals = _recipe_vals(src, ctx.cols["recipes"], label, rep)
    cur = _find_recipe(ctx, bid, uid, vals)
    if cur is None and not vals.get("name"):
        rep.warnings.append(f"{label}: no name, left out")
        rep.counts["recipes"]["skip"] += 1
        return
    if vals.get("ext_id"):
        clash = con.execute("SELECT id FROM recipes WHERE book_id=? AND ext_id=? AND id IS NOT ?",
                            (bid, vals["ext_id"], cur["id"] if cur else None)).fetchone()
        if clash:
            ctx.book_recipes.add(clash["id"])  # held by the clash, so --replace keeps it
            rep.conflicts.append(("recipe", label, f"another recipe in this book already has web id {vals['ext_id']!r}"))
            rep.counts["recipes"]["skip"] += 1
            return
    names = _ingredient_names(ctx, src.get("ingredients"))
    if cur is None:
        ins = {"book_id": bid, **vals}
        if uid:
            ins["uid"] = uid
        if isinstance(src.get("created_at"), str) and TIMESTAMP.match(src["created_at"]):
            ins["created_at"] = src["created_at"]
        rid = con.execute(f"INSERT INTO recipes({','.join(ins)}) VALUES ({','.join('?' * len(ins))})",
                          list(ins.values())).lastrowid
        if names:
            _set_ingredients(ctx, rid, names)
        rep.counts["recipes"]["insert"] += 1
        ctx.claimed_recipes.add(rid)
    else:
        rid = cur["id"]
        changes = {k: v for k, v in vals.items() if cur[k] != v}
        if cur["book_id"] != bid:
            changes["book_id"] = bid  # the file moved it to another book
        tagged = _set_ingredients(ctx, rid, names) if names is not None else False
        if changes:
            con.execute(f"UPDATE recipes SET {','.join(f'{k}=?' for k in changes)} WHERE id=?", [*changes.values(), rid])
        rep.counts["recipes"]["update" if changes or tagged else "skip"] += 1
    ctx.book_recipes.add(rid)
    _set_photos(ctx, rid, src.get("photos"), label, rep)


# ------------------------------------------------------------------ the import
class _Ctx:
    def __init__(self, con, photos_dir=None):
        import ingredients
        self.A, self.ing, self.con = _app(), ingredients, con
        self.photos_dir = Path(photos_dir) if photos_dir else None
        self.refs = set()        # (kind, file name) the imported books reference
        self.book_files = set()  # the same for the book being imported; kept only if it went through
        self.cols = {t: _columns(con, t) for t in ("books", "recipes")}
        self.keep = set()  # ids of the books the file accounts for (found, inserted, or held by a clash)
        self.claimed_books, self.claimed_recipes = set(), set()  # local rows an entry has already matched
        # For --replace: what the file accounts for. Filled per book, and only kept when the book went through.
        self.complete = set()          # ids of books imported without a database error
        self.seen_recipes = {}         # book id -> ids of its recipes the file lists (or that a clash holds)
        self.seen_photos = {}          # recipe id -> file names of its photos the file lists
        self.book_recipes, self.book_photos = set(), {}  # the same, for the book being imported


def _chunks(ids, n=500):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _count_in(con, sql, ids):
    """`sql` (with {q} where the id list goes) summed over chunks of `ids`, to stay under SQLite's variable limit."""
    return sum(con.execute(sql.format(q=",".join("?" * len(c))), c).fetchone()[0] for c in _chunks(ids))


def _delete_in(con, sql, ids):
    for c in _chunks(ids):
        con.execute(sql.format(q=",".join("?" * len(c))), c)


def _replace(ctx, rep, log):
    """Make the catalog equal the file: delete the books it doesn't list, and the recipes and photo rows it doesn't
    list for the books and recipes it does. Files on disk are left alone. The list goes to `log` first."""
    A, con = ctx.A, ctx.con
    gone = [r for r in con.execute("SELECT id, uid, title FROM books ORDER BY id") if r["id"] not in ctx.keep]
    rep.deleted_books = [(r["uid"], r["title"]) for r in gone]
    gone_ids = [r["id"] for r in gone]
    stale_recipes = [r[0] for bid in sorted(ctx.complete) for r in con.execute("SELECT id FROM recipes WHERE book_id=?", (bid,))
                     if r[0] not in ctx.seen_recipes.get(bid, set())]
    stale_photos = [pid for rid, names in ctx.seen_photos.items()
                    for pid, f in con.execute("SELECT id, file FROM recipe_photos WHERE recipe_id=?", (rid,))
                    if f not in names]
    in_gone = "SELECT id FROM recipes WHERE book_id IN ({q})"
    rep.counts["books"]["delete"] = len(gone_ids)
    rep.counts["recipes"]["delete"] = len(stale_recipes) + _count_in(
        con, "SELECT count(*) FROM recipes WHERE book_id IN ({q})", gone_ids)
    rep.counts["photos"]["delete"] = len(stale_photos) \
        + _count_in(con, f"SELECT count(*) FROM recipe_photos WHERE recipe_id IN ({in_gone})", gone_ids) \
        + _count_in(con, "SELECT count(*) FROM recipe_photos WHERE recipe_id IN ({q})", stale_recipes)
    if log and (gone or stale_recipes or stale_photos):
        verb = "would be deleted" if rep.dry_run else "will be deleted"
        log(f"--replace: {len(gone)} book(s), {rep.counts['recipes']['delete']} recipe(s) and "
            f"{rep.counts['photos']['delete']} photo row(s) in this catalog are not in the file and {verb}:")
        for uid, title in rep.deleted_books:
            log(f"  - {title} ({uid})")
        if stale_recipes or stale_photos:
            log(f"  - and {len(stale_recipes)} recipe(s) / {len(stale_photos)} photo row(s) missing from books and "
                "recipes that are in the file")
    _delete_in(con, "DELETE FROM recipes WHERE id IN ({q})", stale_recipes)  # their photo rows cascade
    _delete_in(con, "DELETE FROM recipe_photos WHERE id IN ({q})", stale_photos)
    _delete_in(con, "DELETE FROM books WHERE id IN ({q})", gone_ids)         # and their recipes, photo rows, links
    if A.HAS_FTS:
        _delete_in(con, "DELETE FROM books_fts WHERE rowid IN ({q})", gone_ids)
    if gone_ids:  # the same tidying delete_book does
        con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
        con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")


def check_version(data):
    A = _app()
    v = data.get("version", 0)  # exports from before the key existed are accepted
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ImportFileError(f"The export's version, {v!r}, isn't a number")
    if v > A.EXPORT_VERSION:
        raise ImportFileError(f"This export is format version {v}; this importer reads up to version {A.EXPORT_VERSION}. "
                              "Update Cookbook Collection, then import again.")


def _book_label(src, n):
    A = _app()
    title = A.clean(src.get("title") if isinstance(src.get("title"), str) else None)
    return f"book {title!r}" if title else f"book #{n}"


def _count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def import_data(con, data, *, replace=False, dry_run=False, photos_dir=None, log=None):
    """Import the parsed export `data` through `con` (from app.connect()), commit, and return a Report.

    replace: also delete what the file doesn't list (see _replace); the default is to merge.
    dry_run: do all of it, report it, then roll the transaction back, so the plan is the real code path's.
    photos_dir: a folder to copy the covers and photos the export references from (see "cover and photo files").
    log: called with a line at a time for what --replace is about to delete."""
    A = _app()
    if not isinstance(data, dict) or not isinstance(data.get("books"), list):
        raise ImportFileError("That isn't a Cookbook Collection export (no list of books)")
    check_version(data)
    if replace and not data["books"]:
        raise ImportFileError("Refusing --replace: the file has no books, so it would delete the whole catalog")
    if con.in_transaction:
        raise RuntimeError("import_data needs a connection with no open transaction")
    if photos_dir is not None and not Path(photos_dir).is_dir():
        raise ImportFileError(f"--photos: {photos_dir} is not a folder")
    rep, ctx = Report(), _Ctx(con, photos_dir)
    rep.dry_run = dry_run
    con.execute("BEGIN IMMEDIATE")  # take the write lock now, and keep one transaction for the whole import
    try:
        before = {t: _count(con, t) for t in rep.new_names}
        for n, src in enumerate(data["books"], 1):
            label = _book_label(src, n) if isinstance(src, dict) else f"book #{n}"
            if not isinstance(src, dict):
                rep.warnings.append(f"{label}: not an object, left out")
                continue
            mine = Report()
            ctx.book_recipes, ctx.book_photos, ctx.book_files = set(), {}, set()
            con.execute("SAVEPOINT book")
            try:
                bid = _book(ctx, src, label, mine)
                if bid is not None:
                    for m, rsrc in enumerate(src.get("recipes") or [], 1):
                        if isinstance(rsrc, dict):
                            name = A.clean(rsrc.get("name") if isinstance(rsrc.get("name"), str) else None)
                            _recipe(ctx, bid, rsrc, f"{label} / recipe {name!r}" if name else f"{label} / recipe #{m}", mine)
                        else:
                            mine.warnings.append(f"{label}: recipe #{m} is not an object, left out")
                con.execute("RELEASE book")
                if bid is not None:
                    ctx.complete.add(bid)
                    ctx.seen_recipes.setdefault(bid, set()).update(ctx.book_recipes)
                    ctx.seen_photos.update(ctx.book_photos)
                    ctx.refs |= ctx.book_files
            except sqlite3.Error as e:  # a constraint the checks above missed: leave this book out, not the whole import
                con.execute("ROLLBACK TO book")
                con.execute("RELEASE book")
                rep.conflicts.append(("book", label, f"the database refused it ({e})"))
                rep.counts["books"]["skip"] += 1
                continue
            rep.merge(mine)
        for name in data.get("ingredient_hints") or []:
            if isinstance(name, str) and ctx.ing.clean_name(name):
                iid = A.name_id(con, "ingredients", ctx.ing.clean_name(name))
                rep.hints_set += con.execute("UPDATE ingredients SET hint=1 WHERE id=? AND hint=0", (iid,)).rowcount
        rep.new_names = {t: _count(con, t) - before[t] for t in rep.new_names}
        if replace:
            _replace(ctx, rep, log)
        if dry_run:
            con.rollback()
        else:
            con.commit()
    except BaseException:
        con.rollback()
        raise
    _transfer(ctx, rep, dry_run)
    return rep


# ------------------------------------------------------------------ command line
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_LEFT_OUT = 0, 1, 2, 3  # argparse itself exits 2 on a usage error
EPILOG = """\
The catalog is the one in $DATA_DIR (default ./data next to the app), the same as the service uses.
Exit status: 0 done; 1 the file or options were refused, nothing changed; 2 usage error;
3 done, but some books or recipes were left out (listed) because they clash with something already there."""


def load_export(path):
    """The parsed JSON in `path`."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ImportFileError(f"can't read {path}: {e.strerror or e}") from None
    except UnicodeDecodeError:
        raise ImportFileError(f"{path} isn't a UTF-8 text file") from None
    try:
        return json.loads(text)
    except ValueError as e:
        raise ImportFileError(f"{path} is not valid JSON ({e})") from None


def _lines(title, items, limit=10):
    if not items:
        return []
    out = [title]
    out += [f"  - {x}" for x in items[:limit]]
    if len(items) > limit:
        out.append(f"  ... and {len(items) - limit} more")
    return out


def format_report(rep):
    """The plan (dry run) or the result, as text."""
    out = []
    if rep.dry_run:
        out.append("Dry run: nothing was changed. This is what an import would do.")
    for table, title in (("books", "Books"), ("recipes", "Recipes"), ("photos", "Photos")):
        c = rep.counts[table]
        line = f"{title:<8}{c['insert']} inserted, {c['update']} updated, {c['skip']} skipped"
        out.append(line + (f", {c['delete']} deleted" if c["delete"] else ""))
    n = rep.new_names
    out.append(f"New:    {n['authors']} authors, {n['tags']} tags, {n['ingredients']} ingredient tags, "
               f"{n['locations']} shelf locations"
               + (f" ({rep.hints_set} marked as search hints)" if rep.hints_set else ""))
    f = rep.files
    if f["present"] or f["copied"] or f["missing"] or f["unusable"]:
        out.append(f"Files:  {f['copied']} {'to copy' if rep.dry_run else 'copied'}, {f['present']} already here, "
                   f"{len(f['missing'])} not found, {len(f['unusable'])} not usable")
        out += _lines("Files not found (the rows are kept; add the files and they show up):", f["missing"])
        out += _lines("Files not usable:", f["unusable"])
    out += _lines(f"Left out ({len(rep.conflicts)}), because they clash with something already here:",
                  [f"{label}: {why}" for _, label, why in rep.conflicts], limit=20)
    out += _lines(f"Warnings ({len(rep.warnings)}):", rep.warnings, limit=20)
    return "\n".join(out)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m importer", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Import a Cookbook Collection JSON export. Books and recipes are matched by uid (older exports: by "
                    "ISBN, then title and first author); what matches is updated, the rest is added. Nothing is "
                    "deleted unless you pass --replace.")
    ap.add_argument("export", type=Path, help="the file from More -> Download JSON")
    ap.add_argument("--photos", metavar="DIR", type=Path,
                    help="copy the cover and photo files the export references from DIR (a copy of the data folder, "
                         "of BACKUP_TARGET/files/<app>, or one folder of files); missing ones are reported")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen and change nothing")
    ap.add_argument("--replace", action="store_true",
                    help="also delete the books the file doesn't list, and the recipes and photo rows it doesn't "
                         "list for the books it does, so the catalog ends up equal to the file")
    args = ap.parse_args(argv)
    if "app" not in sys.modules:  # before `import app`, whose init_db() would load the seed catalog into a new database
        os.environ["SKIP_SEED"] = "1"
    try:
        data = load_export(args.export)
        con = _app().connect()
        try:
            print(f"Importing into {_app().DB_PATH}" + (" (dry run)" if args.dry_run else ""))
            rep = import_data(con, data, replace=args.replace, dry_run=args.dry_run, photos_dir=args.photos, log=print)
        finally:
            con.close()
    except ImportFileError as e:
        print(f"importer: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except sqlite3.OperationalError as e:  # "database is locked": the service is writing
        print(f"importer: the database is busy ({e}). Stop the service and try again.", file=sys.stderr)
        return EXIT_REFUSED
    print(format_report(rep))
    return EXIT_LEFT_OUT if rep.conflicts else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
