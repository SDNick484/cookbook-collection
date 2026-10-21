"""Ingredient tags from Eat Your Books ingredient searches.

EYB's ingredient export ("IngredientList.csv") has the same columns as its recipe export, but every row
is a recipe that *uses* one ingredient (searching "chicken" on EYB returns ~2x the recipes that have
chicken in the title). Importing one tags each of those recipes with the ingredient, so:

  * recipe search matches a word against recipe names/index headings OR tagged ingredients
    ("chicken curry" = curry recipes that are tagged chicken or say chicken), and
  * the catalog shows ingredient chips that list every recipe using that ingredient.

Recipes in the file that aren't in the catalog yet (but whose book is) are added, same as the recipe import.
"""
import json, re

from flask import abort, jsonify, request

import app as core
from app import app, db, editor_required, fold, make_err, page_sort, clean
import eyb

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS recipe_ingredients (
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
  PRIMARY KEY (recipe_id, ingredient_id));
CREATE INDEX IF NOT EXISTS recipe_ingredients_ing ON recipe_ingredients(ingredient_id);
"""


def migrate(con):
    con.executescript(SCHEMA)


def clean_name(s):
    s = re.sub(r"\s+", " ", clean(s) or "")
    return s[:1].upper() + s[1:] if s else ""


def guess_name(filename):
    """'IngredientList-chicken.csv' -> 'Chicken'; the plain EYB name gives nothing."""
    stem = re.sub(r"\.csv$", "", filename or "", flags=re.I)
    stem = re.sub(r"(?i)ingredient\s*list|recipe\s*list|eyb|export|\(\d+\)", " ", stem)
    words = re.findall(r"[A-Za-z]+", stem)
    return clean_name(" ".join(words)) if words else ""


# ------------------------------------------------------------------ search support (used by app.py)
def token_condition(token):
    """SQL + args matching a recipe whose name/heading has a word starting with `token`, or that is
    tagged with an ingredient containing a word starting with it."""
    t = fold(token)
    ing = ("r.id IN (SELECT ri.recipe_id FROM recipe_ingredients ri JOIN ingredients i ON i.id=ri.ingredient_id "
           "WHERE fold(i.name) LIKE ? OR fold(i.name) LIKE ?)")
    ing_args = [f"{t}%", f"% {t}%"]
    if core.UNSPACED.search(t):  # Chinese/Japanese/Korean/Thai: find it anywhere in the name, not just word starts
        return f"(instr(fold(r.name) || ' ' || fold(r.heading), ?) > 0 OR {ing})", [t] + ing_args
    if core.HAS_FTS:
        return f"(r.id IN (SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH ?) OR {ing})", \
               [core.fts_query(t)] + ing_args
    return f"(fold(r.name) LIKE ? OR fold(r.heading) LIKE ? OR {ing})", \
           [f"%{t}%", f"%{t}%"] + ing_args


def ingredient_id(name):
    row = db().execute("SELECT id FROM ingredients WHERE fold(name)=fold(?)", (name,)).fetchone()
    return row[0] if row else None


@app.get("/api/ingredients")
def list_ingredients():
    rows = db().execute(
        "SELECT i.id, i.name, count(ri.recipe_id) AS n FROM ingredients i "
        "LEFT JOIN recipe_ingredients ri ON ri.ingredient_id=i.id GROUP BY i.id").fetchall()
    return jsonify(ingredients=sorted((dict(r) for r in rows), key=lambda r: fold(r["name"])))


@app.delete("/admin/api/ingredients/<int:iid>")
@editor_required
def delete_ingredient(iid):
    db().execute("DELETE FROM ingredients WHERE id=?", (iid,))
    db().commit()
    return jsonify(ok=True)


# ------------------------------------------------------------------ import
class BookRecipes:
    """Find an existing recipe for an EYB row: same name + page, else same name if unique in the book,
    else a re-worded copy on the same page of a recipe that came from elsewhere (sheet, photo, typed)."""

    def __init__(self, con, book_id):
        self.exact, self.by_name, self.by_page = {}, {}, {}
        for rid, name, page, source in con.execute(
                "SELECT id, name, page, source FROM recipes WHERE book_id=?", (book_id,)):
            self.add(rid, name, page, source)

    def add(self, rid, name, page, source="eyb"):
        self.exact[(fold(name), page or "")] = rid
        self.by_name.setdefault(fold(name), []).append(rid)
        if page and source != "eyb":
            self.by_page.setdefault(page, []).append((rid, name))

    def find(self, name, page):
        rid = self.exact.get((fold(name), page or ""))
        if rid:
            return rid
        same = self.by_name.get(fold(name), [])
        if len(same) == 1 and not page:
            return same[0]
        for rid, other in self.by_page.get(page or "", []) if page else []:
            ta, tb = eyb.toks(name), eyb.toks(other)
            small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if small and small <= large:
                return rid
        return None


def read_upload():
    f = request.files.get("file")
    if not f:
        abort(make_err(400, "Choose the CSV file exported from Eat Your Books"))
    kind, rows = eyb.parse(f.read())
    if kind != "recipes":
        abort(make_err(400, "That's a Bookshelf export. Ingredient lists come from an EYB ingredient search."))
    return f.filename, rows


@app.post("/admin/api/ingredients/preview")
@editor_required
def ingredient_preview():
    filename, rows = read_upload()
    name = clean_name(request.form.get("ingredient")) or guess_name(filename)
    books = eyb.catalog()
    groups = {}
    for r in rows:
        g = groups.setdefault(r["book_title"], {"author": r["author"], "count": 0})
        g["count"] += 1
    matched = eyb.match_recipe_books(groups, books)
    by_title = {}
    for r in rows:
        by_title.setdefault(r["book_title"], []).append(r)
    iid = ingredient_id(name) if name else None
    tagged_already = {r[0] for r in db().execute(
        "SELECT recipe_id FROM recipe_ingredients WHERE ingredient_id=?", (iid,))} if iid else set()
    out = []
    for t, g in groups.items():
        b, method = matched[t]
        existing = new = already = 0
        if b:
            idx = BookRecipes(db(), b["id"])
            for r in by_title[t]:
                rid = idx.find(r["name"], r["page"])
                if rid is None:
                    new += 1
                    idx.add(-new, r["name"], r["page"])
                elif rid in tagged_already:
                    already += 1
                else:
                    existing += 1
        out.append({"eyb_title": t, "author": g["author"], "count": g["count"], "match": eyb.brief(b),
                    "method": method, "existing": existing, "new": new, "already": already})
    out.sort(key=lambda g: (g["match"] is not None, fold(g["eyb_title"])))
    return jsonify(kind="ingredient", ingredient=name, exists=bool(iid), groups=out, total=len(rows),
                   books=[{"id": b["id"], "title": b["title"]} for b in sorted(books, key=lambda b: fold(b["title"]))])


@app.post("/admin/api/ingredients/commit")
@editor_required
def ingredient_commit():
    _, rows = read_upload()
    name = clean_name(request.form.get("ingredient"))
    if not name:
        return make_err(400, "Name the ingredient (e.g. Chicken)")
    try:
        decisions = json.loads(request.form.get("decisions") or "{}")
    except ValueError:
        return make_err(400, "Bad decisions")
    con = db()
    iid = core.name_id(con, "ingredients", name)
    name = con.execute("SELECT name FROM ingredients WHERE id=?", (iid,)).fetchone()[0]
    tagged = added = skipped = 0
    books, created = {}, {}
    for r in rows:
        d = decisions.get(r["book_title"], "skip")
        if d == "create":
            if r["book_title"] not in created:
                bid = con.execute(
                    "INSERT INTO books(title, eyb_title, source, needs_review) VALUES (?,?, 'eyb', ?)",
                    (r["book_title"], r["book_title"],
                     "From Eat Your Books, which may be out of date: confirm you still own it, then set cuisine "
                     "and tags (or set status to No longer owned)")).lastrowid
                core.set_people(con, bid, eyb.split_authors(r["author"]), [])
                core.reindex_book(con, bid)
                created[r["book_title"]] = bid
            d = created[r["book_title"]]
        if not str(d).isdigit():
            skipped += 1
            continue
        bid = int(d)
        if bid not in books:
            if not con.execute("SELECT 1 FROM books WHERE id=?", (bid,)).fetchone():
                return make_err(400, f"No such book {bid}")
            books[bid] = BookRecipes(con, bid)
            con.execute("UPDATE books SET eyb_title=coalesce(eyb_title, ?) WHERE id=?", (r["book_title"], bid))
        rid = books[bid].find(r["name"], r["page"])
        if rid is None:
            rid = con.execute("INSERT INTO recipes(book_id, name, page, page_sort, source) VALUES (?,?,?,?, 'eyb')",
                              (bid, r["name"], r["page"], page_sort(r["page"]))).lastrowid
            books[bid].add(rid, r["name"], r["page"])
            added += 1
        tagged += con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, iid)).rowcount
    con.commit()
    total = con.execute("SELECT count(*) FROM recipe_ingredients WHERE ingredient_id=?", (iid,)).fetchone()[0]
    return jsonify(kind="ingredient", ingredient=name, tagged=tagged, added_recipes=added, skipped=skipped,
                   created_books=len(created), total=total)
