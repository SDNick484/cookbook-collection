"""Load a Cookbook Collection JSON export (More -> Download JSON, or GET /admin/api/export.json) into a catalog.

Books and recipes are reconciled by their stable `uid`: a uid that exists is updated, an unknown one is inserted
with that uid. The columns written are the ones both the file and this database have, so a column added to the
schema later travels without a change here. Everything goes through the app's own helpers (`clean`, `fold`,
`set_people`, `name_id`, `reindex_book`) on `app.connect()`, because the search triggers call the `fold()` SQL
function that only connect() registers.

All writes happen in one transaction: a failure, or --dry-run, rolls the whole import back.
"""
import math
import re
import sqlite3

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
        self.new_names = {"authors": 0, "tags": 0, "ingredients": 0}
        self.hints_set = 0
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
    vals = _typed(cols, src, skip={"id", "uid", "created_at", "updated_at", "cover"})
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


def _find_book(ctx, uid):
    if uid:
        row = ctx.con.execute("SELECT id FROM books WHERE uid=?", (uid,)).fetchone()
        return row[0] if row else None
    return None


def _book(ctx, src, label, rep):
    """Insert or update one book. Returns its id, or None if it was left out."""
    A, con = ctx.A, ctx.con
    uid = _uid(src, label, rep)
    vals = _book_vals(src, ctx.cols["books"], label, rep)
    bid = _find_book(ctx, uid)
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
def _find_recipe(ctx, bid, uid):
    if uid:
        return ctx.con.execute("SELECT * FROM recipes WHERE uid=?", (uid,)).fetchone()
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
    if not isinstance(photos, list):
        return
    for p in photos:
        if not isinstance(p, dict):
            continue
        name = _file_name(p.get("file"), label, "photo", rep)
        if not name:
            continue
        day = A.clean(p.get("taken_on") if isinstance(p.get("taken_on"), str) else None)
        day = day if day and DAY.fullmatch(day) else None
        caption = A.clean(p.get("caption") if isinstance(p.get("caption"), str) else None)
        row = con.execute("SELECT id, taken_on, caption FROM recipe_photos WHERE recipe_id=? AND file=?",
                          (rid, name)).fetchone()
        if row is None:
            con.execute("INSERT INTO recipe_photos(recipe_id, file, taken_on, caption) VALUES (?,?,?,?)",
                        (rid, name, day, caption))
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
    cur = _find_recipe(ctx, bid, uid)
    if cur is None and not vals.get("name"):
        rep.warnings.append(f"{label}: no name, left out")
        rep.counts["recipes"]["skip"] += 1
        return
    if vals.get("ext_id"):
        clash = con.execute("SELECT id FROM recipes WHERE book_id=? AND ext_id=? AND id IS NOT ?",
                            (bid, vals["ext_id"], cur["id"] if cur else None)).fetchone()
        if clash:
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
    else:
        rid = cur["id"]
        changes = {k: v for k, v in vals.items() if cur[k] != v}
        if cur["book_id"] != bid:
            changes["book_id"] = bid  # the file moved it to another book
        tagged = _set_ingredients(ctx, rid, names) if names is not None else False
        if changes:
            con.execute(f"UPDATE recipes SET {','.join(f'{k}=?' for k in changes)} WHERE id=?", [*changes.values(), rid])
        rep.counts["recipes"]["update" if changes or tagged else "skip"] += 1
    _set_photos(ctx, rid, src.get("photos"), label, rep)


# ------------------------------------------------------------------ the import
class _Ctx:
    def __init__(self, con):
        import ingredients
        self.A, self.ing, self.con = _app(), ingredients, con
        self.cols = {t: _columns(con, t) for t in ("books", "recipes")}
        self.keep = set()  # ids of the books the file accounts for (found, inserted, or held by a clash)


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


def import_data(con, data):
    """Import the parsed export `data` through `con` (from app.connect()), commit, and return a Report."""
    A = _app()
    if not isinstance(data, dict) or not isinstance(data.get("books"), list):
        raise ImportFileError("That isn't a Cookbook Collection export (no list of books)")
    check_version(data)
    if con.in_transaction:
        raise RuntimeError("import_data needs a connection with no open transaction")
    rep, ctx = Report(), _Ctx(con)
    con.execute("BEGIN IMMEDIATE")  # take the write lock now, and keep one transaction for the whole import
    try:
        before = {t: _count(con, t) for t in rep.new_names}
        for n, src in enumerate(data["books"], 1):
            label = _book_label(src, n) if isinstance(src, dict) else f"book #{n}"
            if not isinstance(src, dict):
                rep.warnings.append(f"{label}: not an object, left out")
                continue
            mine = Report()
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
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return rep
