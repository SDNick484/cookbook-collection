"""Change history: what changed in the catalog, who changed it, when, and undo. Editors only ("Recent changes",
under More).

What is recorded: books, recipes, house recipes and shelf locations, from every route and tool that writes them
(the editor, the imports, merges, the MCP write tools, importer.py). Not recorded: covers and photos (their files
aren't kept, so nothing could bring them back), ingredient hint switches, Dinner Log matching (aliases and manual
links) and the wish list.

How. A write makes a Recorder on its own connection, tells it which rows it is about to change (`book()`,
`recipe()`, `location()`; `deleting=True` before a delete, which keeps everything needed to recreate the row) and
which it inserted (`created()`), then calls `finish()` before committing. `finish()` compares each row with what it
was and writes the `changes` rows in the same transaction as the change itself: if the change rolls back, so does
its record.

  update   before/after hold only the fields that changed, and after["_fp"] a fingerprint of the row afterwards
  create   after holds the row (and a book's recipes as [uid, name, page, fingerprint]) with its fingerprint
  delete   before holds everything needed to recreate it with its uid: a book's authors, tags and recipes; a
           recipe's ingredient tags, star, rating, notes, house details, lines, steps and Dinner Log manual links;
           a shelf's sub-shelves and the books that were on them

Bulk operations (`bulk=True`) share one batch_id, and keep their size down: recipes created or deleted in one book
are one row (entity_uid NULL) listing them, and recipes whose only change is an ingredient tag are one row per book
(`tagged` / `untagged` by tag name). A 2,000-recipe index import adds one row of about 80 bytes a recipe.

Undo applies the inverse through a Recorder, so an undo is itself a change (and can be undone). A row is undone at
most once. It refuses, with the reason, when the row was changed again since (its fingerprint no longer matches),
is gone, or would clash (uid or ISBN in use, its book or parent shelf gone, a recipe to remove has photos).

Privacy. The rows hold editor emails and private fields (notes, ratings, private notes). They are read only by the
@editor_required routes below: never by a public route, the MCP (which has no undo tool), the exports or Ask.
Retention: CHANGES_KEEP_DAYS (default 180; 0 keeps everything), pruned at startup a whole batch at a time.
"""
import hashlib
import json
import re
import sqlite3
import uuid

from flask import g, has_request_context, jsonify, request

import app as core
from app import app, db, editor_required, make_err

KEEP_DAYS = core.env_int("CHANGES_KEEP_DAYS", 180)
FP_LEN = 12
PAGE_MAX = 200
BATCH_RE = re.compile(r"[0-9a-f]{32}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,       -- UTC
  actor TEXT NOT NULL,                              -- editor email ('lan' from the LAN), 'mcp', 'importer', 'system'
  entity TEXT NOT NULL CHECK (entity IN ('book', 'recipe', 'location', 'house recipe')),
  entity_uid TEXT,                                  -- books.uid / recipes.uid, a location's id; NULL: a grouped row
  action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete')),
  before TEXT,                                      -- JSON
  after TEXT,                                       -- JSON
  batch_id TEXT,                                    -- rows of one bulk operation (or one undo of several)
  book_uid TEXT,                                    -- the book it belongs to, for the filter; kept after a delete
  label TEXT,                                       -- its name at the time
  op TEXT,                                          -- what made it: edit, index import, undo, ...
  undo_of INTEGER,                                  -- on an undo's rows: the change (or first row of the batch) undone
  undone_by INTEGER);                               -- on an undone row: the first row its undo wrote
CREATE INDEX IF NOT EXISTS changes_at ON changes(at);
CREATE INDEX IF NOT EXISTS changes_batch ON changes(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS changes_book ON changes(book_uid, id) WHERE book_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS changes_actor ON changes(actor, id);
"""
# Undo this release's schema: the previous release never reads the table, so this is only for tidiness.
ROLLBACK_SQL = "DROP TABLE IF EXISTS changes;"


def migrate(con):
    """Additive: one table. Pruned here, at startup."""
    con.executescript(SCHEMA)
    prune(con)


def prune(con, days=None):
    """Delete changes older than `days` (CHANGES_KEEP_DAYS); 0 or less keeps everything. A batch goes only when all
    of it is old, so an undo never meets half a batch. Returns how many rows went."""
    days = KEEP_DAYS if days is None else days
    if days <= 0:
        return 0
    age = f"-{int(days)} days"
    n = con.execute(
        "DELETE FROM changes WHERE at < datetime('now', ?) AND (batch_id IS NULL OR batch_id NOT IN "
        "(SELECT batch_id FROM changes WHERE batch_id IS NOT NULL AND at >= datetime('now', ?)))", (age, age)).rowcount
    if n:
        app.logger.info("change history: pruned %d rows older than %d days", n, days)
    return n


# ------------------------------------------------------------------ snapshots
def _cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _names_sorted(names):
    return sorted(names, key=lambda n: (core.fold(n), n))


def book_state(con, bid, deep=False):
    row = con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    s = {"id": d.pop("id"), "uid": d.pop("uid"), "row": d,
         "authors": [r[0] for r in con.execute(
             "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=? "
             "ORDER BY ba.pos", (bid,))],
         "tags": _names_sorted(r[0] for r in con.execute(
             "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=?", (bid,)))}
    if deep:
        s["recipes"] = [recipe_state(con, rid, deep=True) for (rid,) in con.execute(
            "SELECT id FROM recipes WHERE book_id=? ORDER BY id", (bid,)).fetchall()]
        s["dl_alias"] = [[k, t] for k, t in con.execute("SELECT title_key, title FROM dl_alias WHERE book_id=?", (bid,))]
    return s


def recipe_state(con, rid, deep=False):
    row = con.execute("SELECT r.*, b.uid AS _book_uid, b.kind AS _book_kind FROM recipes r JOIN books b "
                      "ON b.id=r.book_id WHERE r.id=?", (rid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    s = {"id": d.pop("id"), "uid": d.pop("uid"), "book_uid": d.pop("_book_uid"), "house": d.pop("_book_kind") == "house"}
    d.pop("book_id")
    s["row"] = d
    s["tags"] = _names_sorted(r[0] for r in con.execute(
        "SELECT i.name FROM ingredients i JOIN recipe_ingredients ri ON ri.ingredient_id=i.id WHERE ri.recipe_id=?",
        (rid,)))
    det = con.execute("SELECT * FROM recipe_details WHERE recipe_id=?", (rid,)).fetchone()
    s["detail"] = {k: det[k] for k in det.keys() if k != "recipe_id"} if det else None
    s["lines"] = [{k: r[k] for k in r.keys() if k not in ("id", "recipe_id")} for r in con.execute(
        "SELECT * FROM recipe_ingredient_lines WHERE recipe_id=? ORDER BY position", (rid,))]
    s["steps"] = [{k: r[k] for k in r.keys() if k not in ("id", "recipe_id")} for r in con.execute(
        "SELECT * FROM recipe_steps WHERE recipe_id=? ORDER BY position", (rid,))]
    if deep:
        s["photos"] = con.execute("SELECT count(*) FROM recipe_photos WHERE recipe_id=?", (rid,)).fetchone()[0]
        s["dl_link"] = [[m, k] for m, k in con.execute("SELECT meal_id, meal_key FROM dl_link WHERE recipe_id=?", (rid,))]
    return s


def location_state(con, lid, deep=False):
    row = con.execute("SELECT id, parent_id, name, sort_order FROM locations WHERE id=?", (lid,)).fetchone()
    if row is None:
        return None
    s = {"id": row["id"], "uid": str(row["id"]),
         "row": {"parent_id": row["parent_id"], "name": row["name"], "sort_order": row["sort_order"]}}
    if deep:  # the shelf and everything below it, parents first, with the books on each
        kids = {}
        for r in con.execute("SELECT id, parent_id, name, sort_order FROM locations ORDER BY sort_order, id").fetchall():
            kids.setdefault(r["parent_id"], []).append(r)
        sub, todo = [], [row]
        while todo:
            cur = todo.pop(0)
            sub.append({"id": cur["id"], "parent_id": cur["parent_id"], "name": cur["name"],
                        "sort_order": cur["sort_order"],
                        "books": [r[0] for r in con.execute("SELECT uid FROM books WHERE location_id=? ORDER BY id",
                                                            (cur["id"],))]})
            todo += kids.get(cur["id"], [])
        s["subtree"] = sub
    return s


STATE = {"book": book_state, "recipe": recipe_state, "location": location_state}
KIND = {"book": "book", "recipe": "recipe", "house recipe": "recipe", "location": "location"}
# Fields that change without being an edit: never a change on their own. A cover's file isn't kept, so the cover
# isn't tracked at all.
VOLATILE = {"book": {"updated_at", "enriched_at"}, "recipe": {"detail_updated_at"}, "location": set()}
UNTRACKED = {"book": {"cover"}, "recipe": set(), "location": set()}


def entity_of(kind, s):
    return "house recipe" if kind == "recipe" and s.get("house") else kind


def fields(kind, s):
    """A state flattened to the fields a diff compares."""
    if s is None:
        return {}
    f = {k: v for k, v in s["row"].items() if k not in UNTRACKED[kind]}
    if kind == "book":
        f["authors"], f["tags"] = s["authors"], s["tags"]
    elif kind == "recipe":
        f["tags"] = s["tags"]
        det = dict(s["detail"]) if s["detail"] else None
        if det is not None:
            f["detail_updated_at"] = det.pop("updated_at", None)
        f["detail"], f["lines"], f["steps"] = det, s["lines"], s["steps"]
    return f


def fingerprint(kind, s):
    """A short hash of what an editor can change on the row (not the volatile fields): equal fingerprints, same row."""
    f = {k: v for k, v in fields(kind, s).items() if k not in VOLATILE[kind]}
    if kind == "book":  # columns added after a change was recorded (wanted.py): unset, they aren't a change
        import wanted
        f = {k: v for k, v in f.items() if not (k in wanted.COLS and v is None)}
    if kind == "recipe":
        f["book_uid"] = s["book_uid"]
    raw = json.dumps(f, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:FP_LEN]


def diff(kind, a, b):
    """(before, after) holding the fields that differ, volatile ones included only alongside a real change; or
    None when nothing an editor would call a change happened."""
    fa, fb = fields(kind, a), fields(kind, b)
    keys = sorted(k for k in fa.keys() | fb.keys() if fa.get(k) != fb.get(k))
    if not any(k not in VOLATILE[kind] for k in keys):
        return None
    return {k: fa.get(k) for k in keys}, {k: fb.get(k) for k in keys}


def _dump(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def current_actor():
    """Who is making the change: the signed-in editor's email ('lan' for LAN editing), else 'system'."""
    if has_request_context():
        who = getattr(g, "editor", None)
        if who:
            return who
    return "system"


# ------------------------------------------------------------------ recording
class Recorder:
    """Collects one operation's changes and writes them (finish) on the same connection, before its commit."""

    def __init__(self, con, op="edit", actor=None, bulk=False, batch_id=None):
        self.con, self.op, self.bulk, self.batch_id = con, op, bulk, batch_id
        self.actor = actor or current_actor()
        self._touched = {}   # (kind, id) -> state before
        self._created = {}   # (kind, id) -> None, in order
        self._gone = []      # (kind, state): touched, deleted, and its id reused by an insert in this operation
        self.notes = []      # what an undo couldn't put back, for its message
        self.files = []      # cover files to remove once committed
        self._done = False

    def touch(self, kind, id_, deleting=False):
        """Call before changing (or deleting) a row. Rows inserted by this operation are ignored."""
        if id_ is None:
            return
        key = (kind, int(id_))
        if key in self._created:
            return
        if key in self._touched:
            if deleting:  # first touched for an update: add what a delete needs, keep the original fields
                deep = STATE[kind](self.con, key[1], deep=True) or {}
                for k, v in deep.items():
                    self._touched[key].setdefault(k, v)
            return
        s = STATE[kind](self.con, key[1], deep=deleting)
        if s is not None:
            self._touched[key] = s

    def book(self, id_, deleting=False):
        self.touch("book", id_, deleting)

    def recipe(self, id_, deleting=False):
        self.touch("recipe", id_, deleting)

    def location(self, id_, deleting=False):
        self.touch("location", id_, deleting)

    def created(self, kind, id_):
        """Call after inserting a row."""
        key = (kind, int(id_))
        if key in self._touched:  # an id this operation deleted, now reused
            self._gone.append((kind, self._touched.pop(key)))
        self._created[key] = None

    def _book_title(self, uid):
        row = self.con.execute("SELECT title FROM books WHERE uid=?", (uid,)).fetchone()
        if row:
            return row[0]
        for (kind, _), s in self._touched.items():
            if kind == "book" and s["uid"] == uid:
                return s["row"]["title"]
        return None

    def _row(self, kind, s, action, before=None, after=None, label=None):
        return {"entity": entity_of(kind, s), "entity_uid": s["uid"], "action": action, "before": before,
                "after": after, "book_uid": s["uid"] if kind == "book" else s.get("book_uid"),
                "label": label or (s["row"].get("title") if kind == "book" else s["row"].get("name"))}

    def finish(self, undo_of=None):
        """Write this operation's rows (in the caller's transaction); returns their ids."""
        if self._done:
            raise RuntimeError("Recorder.finish called twice")
        self._done = True
        con = self.con
        created_keys = set(self._created)
        lc, bc, rc, up, rd, bd, ld = [], [], [], [], [], [], []
        new_recipes, gone_recipes, tag_only = {}, {}, {}
        for kind, id_ in self._created:
            s = STATE[kind](con, id_)
            if s is None:
                continue  # inserted and deleted again in the same operation
            after = {k: v for k, v in s.items() if k != "id"}
            after["_fp"] = fingerprint(kind, s)
            if kind == "book":  # recipes that came with it (an undone delete), not recorded on their own
                after["recipes"] = [[r["uid"], r["name"], r["page"], fingerprint("recipe", recipe_state(con, r["id"]))]
                                    for r in con.execute("SELECT id, uid, name, page FROM recipes WHERE book_id=? "
                                                         "ORDER BY id", (id_,)).fetchall()
                                    if ("recipe", r["id"]) not in created_keys]
                bc.append(self._row(kind, s, "create", after=after))
            elif kind == "location":
                lc.append(self._row(kind, s, "create", after=after))
            elif self.bulk:
                new_recipes.setdefault((s["book_uid"], entity_of(kind, s)), []).append((s, after))
            else:
                rc.append(self._row(kind, s, "create", after=after))
        for (book_uid, entity), items in new_recipes.items():
            if len(items) == 1:
                rc.append(self._row("recipe", items[0][0], "create", after=items[0][1]))
            else:
                rc.append({"entity": entity, "entity_uid": None, "action": "create", "before": None,
                           "after": {"recipes": [[s["uid"], s["row"]["name"], s["row"]["page"], a["_fp"]]
                                                 for s, a in items]},
                           "book_uid": book_uid, "label": self._book_title(book_uid)})
        gone = list(self._gone)
        for (kind, id_), before in self._touched.items():
            now = STATE[kind](con, id_)
            if now is None:
                gone.append((kind, before))
                continue
            d = diff(kind, before, now)
            if d is None:
                continue
            b, a = d
            a["_fp"] = fingerprint(kind, now)
            if kind == "recipe" and self.bulk and set(b) - VOLATILE["recipe"] == {"tags"}:
                tag_only.setdefault((now["book_uid"], entity_of(kind, now)), []).append((before, now, a["_fp"]))
                continue
            up.append(self._row(kind, now, "update", b, a))
        for (book_uid, entity), items in tag_only.items():
            tagged, untagged, fps = {}, {}, {}
            for before, now, fp in items:
                was, is_ = set(before["tags"]), set(now["tags"])
                for t in sorted(is_ - was):
                    tagged.setdefault(t, []).append(now["uid"])
                for t in sorted(was - is_):
                    untagged.setdefault(t, []).append(now["uid"])
                fps[now["uid"]] = fp
            up.append({"entity": entity, "entity_uid": None, "action": "update", "before": None,
                       "after": {"tagged": tagged, "untagged": untagged, "fps": fps},
                       "book_uid": book_uid, "label": self._book_title(book_uid)})
        for kind, before in gone:
            before = {k: v for k, v in before.items()}
            if kind == "recipe" and self.bulk:
                gone_recipes.setdefault((before["book_uid"], entity_of(kind, before)), []).append(before)
            elif kind == "recipe":
                rd.append(self._row(kind, before, "delete", before=before))
            elif kind == "book":
                bd.append(self._row(kind, before, "delete", before=before))
            else:
                ld.append(self._row(kind, before, "delete", before=before))
        for (book_uid, entity), items in gone_recipes.items():
            if len(items) == 1:
                rd.append(self._row("recipe", items[0], "delete", before=items[0]))
            else:
                rd.append({"entity": entity, "entity_uid": None, "action": "delete", "before": {"recipes": items},
                           "after": None, "book_uid": book_uid, "label": self._book_title(book_uid)})
        # Undo runs newest first, so this order lets each step find what it needs: shelves come back before the
        # books that sit on them, books before their recipes; recipes go before their books, books before shelves.
        rows = lc + bc + rc + up + rd + bd + ld
        if not rows:
            return []
        batch = self.batch_id or (uuid.uuid4().hex if self.bulk or len(rows) > 1 else None)
        ids = []
        for r in rows:
            ids.append(con.execute(
                "INSERT INTO changes(actor, entity, entity_uid, action, before, after, batch_id, book_uid, label, op, "
                "undo_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.actor, r["entity"], r["entity_uid"], r["action"], _dump(r["before"]), _dump(r["after"]), batch,
                 r["book_uid"], r["label"], self.op, undo_of)).lastrowid)
        return ids


def new_batch():
    return uuid.uuid4().hex


# ------------------------------------------------------------------ undo
class Conflict(Exception):
    """An undo that can't be done as things stand. The message says why, in words for the editor."""


WHAT = {"book": "The book", "recipe": "The recipe", "location": "The shelf"}


def _find(con, kind, uid):
    if kind == "location":
        if not re.fullmatch(r"\d+", str(uid or "")):
            return None
        row = con.execute("SELECT id FROM locations WHERE id=?", (int(uid),)).fetchone()
    else:
        row = con.execute(f"SELECT id FROM {'books' if kind == 'book' else 'recipes'} WHERE uid=?", (uid,)).fetchone()
    return row[0] if row else None


def _check(con, kind, uid, fp, label):
    """The row's id, if it is there and as the change left it; else a Conflict."""
    id_ = _find(con, kind, uid)
    if id_ is None:
        raise Conflict(f"{WHAT[kind]} “{label}” has been deleted since.")
    if fp and fingerprint(kind, STATE[kind](con, id_)) != fp:
        raise Conflict(f"“{label}” was changed again since. Undo the later change first.")
    return id_


def _location_ok(con, lid):
    return lid is None or con.execute("SELECT 1 FROM locations WHERE id=?", (lid,)).fetchone() is not None


def _write_doc(con, rid, vals, cur=None):
    """House recipe details, lines and steps from a state's raw columns: whichever of them `vals` has."""
    if "detail" in vals or "detail_updated_at" in vals:
        cur_det = (cur or {}).get("detail") or {}
        det = vals["detail"] if "detail" in vals else (cur_det or None)
        if det is not None:
            det = dict(det)
            stamp = vals["detail_updated_at"] if "detail_updated_at" in vals else \
                (det.get("updated_at") or cur_det.get("updated_at"))
            det.pop("updated_at", None)
            if stamp is not None:
                det["updated_at"] = stamp  # else the column default: now
        con.execute("DELETE FROM recipe_details WHERE recipe_id=?", (rid,))
        if det is not None:
            cols = [k for k in det if k in _cols(con, "recipe_details") and k != "recipe_id"]
            con.execute(f"INSERT INTO recipe_details(recipe_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                        [rid, *(det[k] for k in cols)])
    for key, table in (("lines", "recipe_ingredient_lines"), ("steps", "recipe_steps")):
        if key not in vals:
            continue
        con.execute(f"DELETE FROM {table} WHERE recipe_id=?", (rid,))
        have = _cols(con, table)
        for x in vals[key] or []:
            cols = [k for k in x if k in have and k not in ("id", "recipe_id")]
            con.execute(f"INSERT INTO {table}(recipe_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                        [rid, *(x[k] for k in cols)])


def _set_tags(con, rid, names):
    con.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (rid,))
    for n in names or []:
        con.execute("INSERT OR IGNORE INTO recipe_ingredients(recipe_id, ingredient_id) VALUES (?,?)",
                    (rid, core.name_id(con, "ingredients", n)))


def _prune_ingredients(con, ids):
    """Ingredient tags an undo left on no recipe: the import that made them is being taken back."""
    for iid in set(ids):
        if not con.execute("SELECT 1 FROM recipe_ingredients WHERE ingredient_id=?", (iid,)).fetchone():
            con.execute("DELETE FROM ingredients WHERE id=?", (iid,))


def _isbn_clash(con, isbn, label, exclude=None):
    if not isbn:
        return
    other = con.execute("SELECT title FROM books WHERE isbn=? AND id IS NOT ?", (isbn, exclude)).fetchone()
    if other:
        raise Conflict(f"Can't put back “{label}”: “{other[0]}” has its ISBN ({isbn}) now.")


def _apply(con, rec, kind, id_, vals, label):
    """Set these fields (an update's `before`) on the row."""
    if kind == "book":
        cols = _cols(con, "books") - {"id", "uid"}
        row = {k: v for k, v in vals.items() if k in cols}
        _isbn_clash(con, row.get("isbn"), label, id_)
        if "location_id" in row and not _location_ok(con, row["location_id"]):
            row["location_id"] = None
            rec.notes.append(f"The shelf “{label}” was on has been deleted, so it is now unshelved.")
        if row:
            con.execute(f"UPDATE books SET {', '.join(f'{k}=?' for k in row)} WHERE id=?", [*row.values(), id_])
        if "authors" in vals or "tags" in vals:
            cur = book_state(con, id_)
            core.set_people(con, id_, vals.get("authors", cur["authors"]), vals.get("tags", cur["tags"]))
            con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
            con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
        core.reindex_book(con, id_)
    elif kind == "recipe":
        cols = _cols(con, "recipes") - {"id", "uid", "book_id"}
        row = {k: v for k, v in vals.items() if k in cols}
        if row.get("ext_id") and con.execute("SELECT 1 FROM recipes WHERE ext_id=? AND id<>? AND book_id=(SELECT book_id "
                                             "FROM recipes WHERE id=?)", (row["ext_id"], id_, id_)).fetchone():
            raise Conflict(f"Can't put back “{label}”: another recipe in its book has that web link now.")
        if row:
            con.execute(f"UPDATE recipes SET {', '.join(f'{k}=?' for k in row)} WHERE id=?", [*row.values(), id_])
        if "tags" in vals:
            _set_tags(con, id_, vals["tags"])
        _write_doc(con, id_, vals, recipe_state(con, id_))
    else:
        cur = location_state(con, id_)["row"]
        new = {**cur, **{k: v for k, v in vals.items() if k in cur}}
        if new["parent_id"] is not None:
            if not _location_ok(con, new["parent_id"]):
                raise Conflict(f"Can't move the shelf “{label}” back: the shelf it was under has been deleted.")
            if new["parent_id"] in core.descendant_location_ids(con, id_):
                raise Conflict(f"Can't move the shelf “{label}” back: that would put it inside itself.")
        if core.location_clash(con, new["parent_id"], new["name"], exclude=id_):
            raise Conflict(f"Can't put back the shelf “{new['name']}”: there is another shelf of that name there now.")
        con.execute("UPDATE locations SET parent_id=?, name=?, sort_order=? WHERE id=?",
                    (new["parent_id"], new["name"], new["sort_order"], id_))


def _insert(con, table, s_row, extra, want_id):
    cols = _cols(con, table)
    row = {k: v for k, v in s_row.items() if k in cols and k not in ("id", "uid")}
    row.update(extra)
    if want_id is not None and not con.execute(f"SELECT 1 FROM {table} WHERE id=?", (want_id,)).fetchone():
        row["id"] = want_id  # the old rowid when it is free, so anything that still holds it matches again
    return con.execute(f"INSERT INTO {table}({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                       list(row.values())).lastrowid


def _restore_recipe(con, rec, s, bid=None, record=True):
    name = s["row"].get("name") or "a recipe"
    if bid is None:
        bid = _find(con, "book", s["book_uid"])
        if bid is None:
            raise Conflict(f"Can't restore the recipe “{name}”: the book it was in is gone. Undo that book's "
                           "deletion first.")
    if _find(con, "recipe", s["uid"]):
        raise Conflict(f"Can't restore the recipe “{name}”: its uid is in use again.")
    if s["row"].get("ext_id") and con.execute("SELECT 1 FROM recipes WHERE book_id=? AND ext_id=?",
                                              (bid, s["row"]["ext_id"])).fetchone():
        raise Conflict(f"Can't restore the recipe “{name}”: that web link is saved in its book again.")
    rid = _insert(con, "recipes", s["row"], {"uid": s["uid"], "book_id": bid}, s.get("id"))
    _set_tags(con, rid, s.get("tags"))
    _write_doc(con, rid, {"detail": s.get("detail"), "lines": s.get("lines") or [], "steps": s.get("steps") or []})
    for meal_id, key in s.get("dl_link") or []:
        con.execute("INSERT OR IGNORE INTO dl_link(meal_id, recipe_id, meal_key) VALUES (?,?,?)", (meal_id, rid, key))
    if s.get("photos"):
        rec.notes.append(f"“{name}” had {s['photos']} photo{'s' if s['photos'] > 1 else ''}; deleted photos can't be "
                         "restored.")
    if record:
        rec.created("recipe", rid)
    return rid


def _restore_book(con, rec, s):
    title = s["row"].get("title") or "a book"
    if _find(con, "book", s["uid"]):
        raise Conflict(f"Can't restore the book “{title}”: its uid is in use again.")
    _isbn_clash(con, s["row"].get("isbn"), title)
    if s["row"].get("kind") == "house" and con.execute("SELECT 1 FROM books WHERE kind='house'").fetchone():
        raise Conflict(f"Can't restore “{title}”: there is a house book already.")
    extra = {"uid": s["uid"]}
    lid = s["row"].get("location_id")
    if lid is not None and not _location_ok(con, lid):
        extra["location_id"] = None
        rec.notes.append(f"The shelf “{title}” was on has been deleted, so it is back unshelved.")
    cover = s["row"].get("cover")
    if cover and not (core.COVER_DIR / cover).is_file():
        extra["cover"] = None
        rec.notes.append(f"The cover of “{title}” was deleted with it and isn't restored.")
    bid = _insert(con, "books", s["row"], extra, s.get("id"))
    core.set_people(con, bid, s.get("authors") or [], s.get("tags") or [])
    core.reindex_book(con, bid)
    rec.created("book", bid)
    for r in s.get("recipes") or []:
        _restore_recipe(con, rec, r, bid, record=False)
    for key, t in s.get("dl_alias") or []:
        con.execute("INSERT OR IGNORE INTO dl_alias(title_key, title, book_id) VALUES (?,?,?)", (key, t, bid))
    return bid


def _restore_location(con, rec, s):
    sub = s.get("subtree") or [{"id": s["id"], **s["row"], "books": []}]
    root = sub[0]
    if root["parent_id"] is not None and not _location_ok(con, root["parent_id"]):
        raise Conflict(f"Can't restore the shelf “{root['name']}”: the shelf it was under has been deleted.")
    if core.location_clash(con, root["parent_id"], root["name"]):
        raise Conflict(f"Can't restore the shelf “{root['name']}”: there is another shelf of that name there now.")
    ids, moved = {}, 0
    for x in sub:
        parent = ids.get(x["parent_id"], x["parent_id"])
        lid = _insert(con, "locations", {"parent_id": parent, "name": x["name"], "sort_order": x["sort_order"]}, {},
                      x["id"])
        ids[x["id"]] = lid
        rec.created("location", lid)
        for uid in x.get("books") or []:
            row = con.execute("SELECT id, location_id FROM books WHERE uid=?", (uid,)).fetchone()
            if row and row["location_id"] is None:
                rec.book(row["id"])
                con.execute("UPDATE books SET location_id=? WHERE id=?", (lid, row["id"]))
            elif row:
                moved += 1
    if moved:
        rec.notes.append(f"{moved} book{'s' if moved > 1 else ''} that were on it are on another shelf now and "
                         "were left there.")


def _remove(con, rec, kind, uid, fp, label, after=None):
    """Undo a create: delete the row, if it is as the create left it."""
    id_ = _check(con, kind, uid, fp, label)
    if kind == "recipe":
        n = con.execute("SELECT count(*) FROM recipe_photos WHERE recipe_id=?", (id_,)).fetchone()[0]
        if n:
            raise Conflict(f"“{label}” has {n} photo{'s' if n > 1 else ''} now, which would be deleted with it.")
        rec.recipe(id_, deleting=True)
        tags = [r[0] for r in con.execute("SELECT ingredient_id FROM recipe_ingredients WHERE recipe_id=?", (id_,))]
        con.execute("DELETE FROM recipes WHERE id=?", (id_,))
        _prune_ingredients(con, tags)
    elif kind == "book":
        listed = {x[0]: x for x in (after or {}).get("recipes") or []}
        for r in con.execute("SELECT id, uid, name FROM recipes WHERE book_id=?", (id_,)).fetchall():
            if r["uid"] not in listed:
                raise Conflict(f"“{label}” has recipes added since (“{r['name']}”…). Delete or undo those first.")
            if fingerprint("recipe", recipe_state(con, r["id"])) != listed[r["uid"]][3]:
                raise Conflict(f"Its recipe “{r['name']}” was changed since. Undo that change first.")
            if con.execute("SELECT 1 FROM recipe_photos WHERE recipe_id=?", (r["id"],)).fetchone():
                raise Conflict(f"Its recipe “{r['name']}” has photos now, which would be deleted with it.")
        if con.execute("SELECT 1 FROM books WHERE id=? AND kind='house'", (id_,)).fetchone():
            raise Conflict("The house book can't be deleted.")
        rec.book(id_, deleting=True)
        cover = con.execute("SELECT cover FROM books WHERE id=?", (id_,)).fetchone()[0]
        con.execute("DELETE FROM books WHERE id=?", (id_,))
        if core.HAS_FTS:
            con.execute("DELETE FROM books_fts WHERE rowid=?", (id_,))
        con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
        con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
        if cover:
            rec.files.append(core.COVER_DIR / cover)
    else:
        if con.execute("SELECT 1 FROM locations WHERE parent_id=?", (id_,)).fetchone():
            raise Conflict(f"The shelf “{label}” has shelves under it now.")
        n = con.execute("SELECT count(*) FROM books WHERE location_id=?", (id_,)).fetchone()[0]
        if n:
            raise Conflict(f"{n} book{'s are' if n > 1 else ' is'} on the shelf “{label}” now.")
        rec.location(id_, deleting=True)
        con.execute("DELETE FROM locations WHERE id=?", (id_,))


def _retag(con, rec, after):
    """Undo a grouped tag change: take off the tags it added, put back the ones it removed."""
    ids = {}
    for uid, fp in (after.get("fps") or {}).items():
        name = con.execute("SELECT name FROM recipes WHERE uid=?", (uid,)).fetchone()
        ids[uid] = _check(con, "recipe", uid, fp, name[0] if name else "a recipe")
    for rid in ids.values():
        rec.recipe(rid)
    for tag, uids in (after.get("tagged") or {}).items():
        row = con.execute("SELECT id FROM ingredients WHERE fold(name)=fold(?)", (tag,)).fetchone()
        if row:
            con.executemany("DELETE FROM recipe_ingredients WHERE recipe_id=? AND ingredient_id=?",
                            [(ids[u], row[0]) for u in uids if u in ids])
            _prune_ingredients(con, [row[0]])
    for tag, uids in (after.get("untagged") or {}).items():
        iid = core.name_id(con, "ingredients", tag)
        con.executemany("INSERT OR IGNORE INTO recipe_ingredients(recipe_id, ingredient_id) VALUES (?,?)",
                        [(ids[u], iid) for u in uids if u in ids])


def _undo_one(con, rec, r):
    kind = KIND[r["entity"]]
    before = json.loads(r["before"]) if r["before"] else None
    after = json.loads(r["after"]) if r["after"] else None
    label = r["label"] or "it"
    if r["entity_uid"] is None:  # a grouped row of a bulk operation
        if r["action"] == "create":
            for uid, name, _page, fp in after["recipes"]:
                _remove(con, rec, "recipe", uid, fp, name)
        elif r["action"] == "delete":
            for s in before["recipes"]:
                _restore_recipe(con, rec, s)
        else:
            _retag(con, rec, after)
    elif r["action"] == "create":
        _remove(con, rec, kind, r["entity_uid"], after.get("_fp"), label, after)
    elif r["action"] == "delete":
        {"book": _restore_book, "recipe": _restore_recipe, "location": _restore_location}[kind](con, rec, before)
    else:
        id_ = _check(con, kind, r["entity_uid"], after.get("_fp"), label)
        rec.touch(kind, id_)
        _apply(con, rec, kind, id_, before, label)


def undo(con, rows, actor=None):
    """Undo these change rows, newest first, as one recorded operation in the caller's transaction (the caller
    commits, or rolls back on Conflict). Returns (ids of the rows the undo wrote, notes, files to delete after the
    commit)."""
    rows = sorted(rows, key=lambda r: r["id"], reverse=True)
    if not rows:
        raise Conflict("Nothing to undo.")
    if any(r["undone_by"] is not None for r in rows):
        raise Conflict("That was undone already.")
    grouped = any(r["entity_uid"] is None for r in rows)
    rec = Recorder(con, op="undo", actor=actor, bulk=len(rows) > 1 or grouped)
    for r in rows:
        _undo_one(con, rec, r)
    ids = rec.finish(undo_of=rows[-1]["id"])
    con.executemany("UPDATE changes SET undone_by=? WHERE id=?", [(ids[0] if ids else 0, r["id"]) for r in rows])
    return ids, rec.notes, rec.files


# ------------------------------------------------------------------ plain words
FIELD = {"title": "title", "subtitle": "subtitle", "isbn": "ISBN", "publisher": "publisher", "year": "year",
         "cuisine": "cuisine", "format": "format", "description": "description", "location_id": "shelf",
         "status": "status", "loaned_to": "loaned to", "private_notes": "private notes", "needs_review": "review note",
         "rating": "rating", "visibility": "visibility", "authors": "authors", "tags": "tags", "eyb_title": "EYB title",
         "eyb_status": "EYB status", "out_since": "out since", "site": "site", "name": "name", "heading": "heading",
         "page": "page", "notes": "notes", "last_made": "last made", "url": "link", "detail": "details",
         "lines": "ingredients", "steps": "steps", "parent_id": "place", "sort_order": "position", "source": "source",
         "legacy_category": "old category", "kind": "kind", "ext_id": "web id"}
HIDDEN = {"page_sort", "updated_at", "enriched_at", "detail_updated_at", "created_at", "_fp"}
LONG = {"description", "private_notes", "notes", "needs_review"}
STATUS = {"owned": "on the shelf", "out": "out", "loaned": "loaned", "gone": "no longer owned", "wanted": "wanted"}


def _v(k, v, paths):
    if v in (None, "", []):
        return "none"
    if k in ("location_id", "parent_id"):
        return f"“{paths.get(v, f'shelf #{v}')}”" if k == "location_id" else (paths.get(v) or "a deleted shelf")
    if k == "status":
        return STATUS.get(v, v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"“{v}”" if isinstance(v, str) else str(v)


def describe(k, b, a, paths):
    """One field's change in words."""
    if k == "want_to_try":
        return "starred" if a else "unstarred"
    if k == "signed":
        return "marked signed" if a else "marked not signed"
    if k in ("tags", "authors"):
        was, is_ = list(b or []), list(a or [])
        parts = [f"+{x}" for x in is_ if x not in was] + [f"−{x}" for x in was if x not in is_]
        return f"{FIELD[k]} {' '.join(parts) if parts else 'reordered'}"
    if k in ("lines", "steps"):
        nb, na = len(b or []), len(a or [])
        return f"{FIELD[k]} edited" + (f" ({nb} → {na})" if nb != na else "")
    if k == "detail":
        return "details edited"
    name = FIELD.get(k, k.replace("_", " "))
    if k in LONG and (len(str(b or "")) > 40 or len(str(a or "")) > 40):
        return f"{name} edited" if b and a else (f"{name} added" if a else f"{name} cleared")
    if k == "parent_id":
        return f"moved under {_v(k, a, paths)}" if a else "moved to the top level"
    return f"{name} {_v(k, b, paths)} → {_v(k, a, paths)}"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def summarize(r, titles, paths):
    """(summary, details, warning) for one change row, in plain words."""
    entity, action, label = r["entity"], r["action"], r["label"] or ""
    before = (json.loads(r["before"]) if r["before"] else None) or {}
    after = (json.loads(r["after"]) if r["after"] else None) or {}
    book = titles.get(r["book_uid"]) if entity != "book" else None
    where = f" in “{book}”" if book else ""
    noun = {"house recipe": "house recipe", "recipe": "recipe", "book": "book", "location": "shelf"}[entity]
    warning = None
    if r["entity_uid"] is None:
        if action == "create":
            items = after.get("recipes") or []
            return f"Added {_plural(len(items), noun)} to “{label}”", [x[1] for x in items[:50]], None
        if action == "delete":
            items = before.get("recipes") or []
            photos = sum(x.get("photos") or 0 for x in items)
            if photos:
                warning = f"{_plural(photos, 'photo')} went with them and can't be restored."
            return (f"Deleted {_plural(len(items), noun)} from “{label}”", [x["row"].get("name") for x in items[:50]],
                    warning)
        parts = [f"tagged {_plural(len(u), noun)} {t}" for t, u in (after.get("tagged") or {}).items()]
        parts += [f"untagged {t} from {_plural(len(u), noun)}" for t, u in (after.get("untagged") or {}).items()]
        return f"Ingredient tags in “{label}”: " + "; ".join(parts), [], None
    if action == "create":
        extra = len(after.get("recipes") or [])
        return (f"Added the {noun} “{label}”" + (where if entity != "location" else "")
                + (f" with {_plural(extra, 'recipe')}" if extra else ""), [], None)
    if action == "delete":
        if entity == "book":
            recs = before.get("recipes") or []
            photos = sum(x.get("photos") or 0 for x in recs)
            if photos:
                warning = f"Its {_plural(photos, 'photo')} can't be restored."
            return (f"Deleted the book “{label}”" + (f" and its {_plural(len(recs), 'recipe')}" if recs else ""),
                    [], warning)
        if entity == "location":
            sub = before.get("subtree") or []
            books = sum(len(x.get("books") or []) for x in sub)
            return (f"Deleted the shelf “{label}”" + (f" and {_plural(len(sub) - 1, 'shelf')} under it"
                                                      .replace("shelfs", "shelves") if len(sub) > 1 else "")
                    + (f"; {_plural(books, 'book')} unshelved" if books else ""), [], None)
        if before.get("photos"):
            warning = f"Its {_plural(before['photos'], 'photo')} can't be restored."
        return f"Deleted the {noun} “{label}”{where}", [], warning
    details = [describe(k, before.get(k), after.get(k), paths) for k in after if k not in HIDDEN]
    return f"Changed the {noun} “{label}”{where}", details, None


# ------------------------------------------------------------------ routes (editors only)
def _book_titles(con, uids):
    uids = [u for u in uids if u]
    out = {}
    for i in range(0, len(uids), 500):
        chunk = uids[i:i + 500]
        out.update(con.execute(f"SELECT uid, title FROM books WHERE uid IN ({','.join('?' * len(chunk))})", chunk))
    for u in set(uids) - set(out):  # deleted since: the name it last had here
        row = con.execute("SELECT label FROM changes WHERE entity='book' AND entity_uid=? AND label IS NOT NULL "
                          "ORDER BY id DESC LIMIT 1", (u,)).fetchone()
        if row:
            out[u] = row[0]
    return out


@app.get("/admin/api/changes")
@editor_required
def list_changes():
    """Newest first. ?book=<book uid>, ?actor=, ?before=<id> (the next_before of the last page), ?limit=.
    A page never ends in the middle of a batch."""
    con = db()
    p = request.args
    limit = core.arg_int(p, "limit", 50, 1, PAGE_MAX)
    before = core.arg_int(p, "before", 0, 0)
    where, args = [], []
    if before:
        where.append("id < ?")
        args.append(before)
    if p.get("book"):
        where.append("book_uid = ?")
        args.append(p["book"])
    if p.get("actor"):
        where.append("actor = ?")
        args.append(p["actor"])
    sql = "SELECT * FROM changes" + (" WHERE " + " AND ".join(where) if where else "")
    rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    if more and rows[-1]["batch_id"]:  # finish the last batch, so its rows show together
        rest = con.execute(sql + (" AND" if where else " WHERE") + " batch_id = ? AND id < ? ORDER BY id DESC",
                           [*args, rows[-1]["batch_id"], rows[-1]["id"]]).fetchall()
        rows += rest
        more = bool(con.execute(sql + (" AND" if where else " WHERE") + " id < ? LIMIT 1",
                                [*args, rows[-1]["id"]]).fetchone())
    titles = _book_titles(con, {r["book_uid"] for r in rows})
    paths = core.location_paths(con)
    sizes = {}
    for b in {r["batch_id"] for r in rows if r["batch_id"]}:
        sizes[b] = con.execute("SELECT count(*), sum(undone_by IS NULL) FROM changes WHERE batch_id=?", (b,)).fetchone()
    ids = {r[0]: r[1] for r in con.execute("SELECT uid, id FROM books WHERE uid IN (%s)" % ",".join(
        "?" * len(titles)), list(titles))} if titles else {}
    out = []
    for r in rows:
        summary, details, warning = summarize(r, titles, paths)
        out.append({"id": r["id"], "at": r["at"], "actor": r["actor"], "op": r["op"], "entity": r["entity"],
                    "entity_uid": r["entity_uid"], "action": r["action"], "batch_id": r["batch_id"],
                    "batch_size": sizes[r["batch_id"]][0] if r["batch_id"] else 1,
                    "batch_open": (sizes[r["batch_id"]][1] or 0) if r["batch_id"] else int(r["undone_by"] is None),
                    "book_uid": r["book_uid"], "book_title": titles.get(r["book_uid"]),
                    "book_id": ids.get(r["book_uid"]), "summary": summary, "details": details, "warning": warning,
                    "undone_by": r["undone_by"], "undo_of": r["undo_of"]})
    actors = [x[0] for x in con.execute("SELECT DISTINCT actor FROM changes ORDER BY actor")]
    books = _book_titles(con, [x[0] for x in con.execute("SELECT DISTINCT book_uid FROM changes WHERE book_uid IS NOT NULL")])
    return jsonify(changes=out, next_before=rows[-1]["id"] if more and rows else None, actors=actors,
                   books=sorted(({"uid": u, "title": t} for u, t in books.items()), key=lambda b: core.fold(b["title"])),
                   keep_days=KEEP_DAYS)


def _run_undo(rows):
    con = db()
    if not rows:
        return make_err(404, "No such change")
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")  # check and write under the write lock: two undos can't both pass the checks
    rows = con.execute(f"SELECT * FROM changes WHERE id IN ({','.join('?' * len(rows))})",
                       [r["id"] for r in rows]).fetchall()
    try:
        ids, notes, files = undo(con, rows, actor=g.editor)
    except Conflict as e:
        con.rollback()
        return make_err(409, str(e))
    except sqlite3.IntegrityError as e:
        con.rollback()
        app.logger.warning("undo of change %s refused by the database: %s", rows[0]["id"], e)
        return make_err(409, "Undoing that would clash with something that is in the catalog now.")
    con.commit()
    for f in files:
        f.unlink(missing_ok=True)
    app.logger.info("undo by %s of change(s) %s -> %s", g.editor, [r["id"] for r in rows], ids)
    return jsonify(ok=True, changes=ids, notes=notes,
                   message="Undone." + (" " + " ".join(notes) if notes else ""))


@app.post("/admin/api/changes/<int:cid>/undo")
@editor_required
def undo_change(cid):
    return _run_undo(db().execute("SELECT id FROM changes WHERE id=?", (cid,)).fetchall())


@app.post("/admin/api/changes/batch/<batch_id>/undo")
@editor_required
def undo_batch(batch_id):
    """The whole batch, or nothing. Rows of it undone one by one before are left as they are."""
    if not BATCH_RE.fullmatch(batch_id):
        return make_err(404, "No such batch")
    rows = db().execute("SELECT id FROM changes WHERE batch_id=? AND undone_by IS NULL", (batch_id,)).fetchall()
    if not rows and db().execute("SELECT 1 FROM changes WHERE batch_id=?", (batch_id,)).fetchone():
        return make_err(409, "That was undone already.")
    return _run_undo(rows)
