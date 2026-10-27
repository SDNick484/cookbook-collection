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

from flask import abort, jsonify, request, Response

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
    if "hint" not in [r[1] for r in con.execute("PRAGMA table_info(ingredients)")]:
        # 1 = a search hint (zucchini, kale…): found by search and the MCP, but no chip on the catalog page
        con.execute("ALTER TABLE ingredients ADD COLUMN hint INTEGER NOT NULL DEFAULT 0")
    tidy_names(con)


def clean_name(s):
    s = re.sub(r"\s+", " ", clean(s) or "")
    return s[:1].upper() + s[1:] if s else ""


# Words that describe the file rather than the ingredient: "Chicken - all pages combined.csv", "lamb pages 1-4.csv".
# (Bounded by anything that isn't a letter or digit, so "pork_combined" counts too.)
FILE_NOISE = re.compile(r"(?i)(?<![^\W_])(?:all\s+pages?|pages?(?:\s*\d+(?:\s*(?:-|–|to)\s*\d+)?)?|combined|merged"
                        r"|part\s*\d+)(?![^\W_])")


def guess_name(filename):
    """'IngredientList-chicken.csv' -> 'Chicken', 'Jalapeño - all pages combined.csv' -> 'Jalapeño';
    the plain EYB name gives nothing."""
    stem = re.sub(r"\.csv$", "", filename or "", flags=re.I)
    stem = re.sub(r"(?i)ingredient\s*list|recipe\s*list|eyb|export|\(\d+\)", " ", stem)
    words = re.findall(r"[^\W\d_]+", FILE_NOISE.sub(" ", stem))
    return clean_name(" ".join(words)) if words else ""


def tidy_names(con):
    """Rename tags named after their file ("Chicken all pages combined" -> "Chicken"), merging into the
    tag of that name when there already is one. Runs at every start; does nothing once they're tidy."""
    for iid, name in con.execute("SELECT id, name FROM ingredients").fetchall():
        if not FILE_NOISE.search(name):
            continue
        tidy = re.sub(r"\(\s*\)|\[\s*\]", " ", FILE_NOISE.sub(" ", name))  # "Lamb (all pages combined)"
        tidy = clean_name(re.sub(r"\s+", " ", tidy).strip(" -–_,."))
        if not tidy or tidy == name:
            continue
        keep = con.execute("SELECT id FROM ingredients WHERE fold(name)=fold(?) AND id<>?", (tidy, iid)).fetchone()
        if keep:
            con.execute("INSERT OR IGNORE INTO recipe_ingredients(recipe_id, ingredient_id) "
                        "SELECT recipe_id, ? FROM recipe_ingredients WHERE ingredient_id=?", (keep[0], iid))
            con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        else:
            con.execute("UPDATE ingredients SET name=? WHERE id=?", (tidy, iid))


# ------------------------------------------------------------------ search support (used by app.py)
def token_condition(token):
    """SQL + args matching a recipe whose name/heading has a word starting with `token`, or that is
    tagged with an ingredient containing a word starting with it. Plural words match by their singular stem,
    so "leeks" also finds "Leek and potato soup"."""
    t = stem(fold(token))
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


# An ingredient filter means: tagged with the major ingredient tag it names, or the ingredient is in the recipe's
# title. Only some recipes carry tags, so the title catches the rest ("Best ground beef chili").
STOPWORDS = {"a", "an", "and", "the", "of", "with", "in", "or"}


def stem(w):
    """Singular form, close enough to compare: tomatoes -> tomato, cherries -> cherry, eggs -> egg."""
    if len(w) > 3 and w.endswith("oes"):
        return w[:-2]
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def ingredient_words(s):
    return [stem(w) for w in re.findall(r"\w+", fold(s)) if w not in STOPWORDS]


def resolve(con, name):
    """The ingredient tags `name` refers to: the tag itself, or every tag whose words it contains
    ("ground beef" -> Beef, "egg" -> Eggs, "cherry tomatoes" -> Tomatoes). When one of those tags is part of
    another ("sweet potatoes" contains both Sweet potatoes and Potatoes), only the more specific one counts.
    [(id, name)]"""
    want = set(ingredient_words(name))
    found = []
    for iid, tag in con.execute("SELECT id, name FROM ingredients"):
        words = set(ingredient_words(tag))
        if fold(tag) == fold(name) or (words and words <= want):
            found.append((iid, tag, words))
    return [(iid, tag) for iid, tag, words in found if not any(words < other for _, _, other in found)]


def title_condition(name):
    """Recipe name or index heading contains every word of `name` as a whole word, singular or plural."""
    conds, args = [], []
    for w in ingredient_words(name):
        forms = sorted({w, w + "s", w + "es"} | ({w[:-1] + "ies"} if w.endswith("y") else set()))
        if core.HAS_FTS:
            conds.append("r.id IN (SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH ?)")
            args.append(" OR ".join(f'"{f}"' for f in forms))
        else:
            conds.append("(" + " OR ".join(["(' ' || fold(r.name) || ' ' || fold(r.heading) || ' ') LIKE ?"] * len(forms)) + ")")
            args += [f"% {f} %" for f in forms]
    return " AND ".join(conds), args


def ingredient_condition(con, name):
    """SQL + args for "uses this ingredient": tagged with each tag it names, or has it in the title."""
    tags = resolve(con, name)
    parts, args = [], []
    if tags:
        parts.append(" AND ".join(["r.id IN (SELECT recipe_id FROM recipe_ingredients WHERE ingredient_id=?)"] * len(tags)))
        args += [iid for iid, _ in tags]
    tsql, targs = title_condition(name)
    if tsql:
        parts.append(tsql)
        args += targs
    if not parts:
        return "0", []
    return "(" + " OR ".join(f"({p})" for p in parts) + ")", args


def list_ingredients(private=False):
    """The ingredient tags with how many recipes each finds. Search hints are listed for editors only
    (to manage them); the public catalog only ever shows the major tags as chips."""
    out = []
    for r in db().execute("SELECT id, name, hint FROM ingredients" + ("" if private else " WHERE hint=0")).fetchall():
        cond, args = ingredient_condition(db(), r["name"])  # n = what picking the chip lists: tagged or in the title
        if not private:
            cond += f" AND r.book_id IN (SELECT b.id FROM books b WHERE {core.PUBLIC_BOOKS})"
        out.append({"id": r["id"], "name": r["name"], "hint": bool(r["hint"]),
                    "n": db().execute(f"SELECT count(*) FROM recipes r WHERE {cond}", args).fetchone()[0]})
    return jsonify(ingredients=sorted(out, key=lambda r: fold(r["name"])))


app.add_url_rule("/api/ingredients", "ingredients_public", list_ingredients)
app.add_url_rule("/admin/api/ingredients", "ingredients_private", editor_required(lambda: list_ingredients(True)))


@app.put("/admin/api/ingredients/<int:iid>")
@editor_required
def update_ingredient(iid):
    """Switch a tag between a catalog chip and a search hint: {"hint": true|false}."""
    hint = 1 if (request.get_json(force=True) or {}).get("hint") else 0
    if not db().execute("UPDATE ingredients SET hint=? WHERE id=?", (hint, iid)).rowcount:
        return make_err(404, "No such ingredient")
    db().commit()
    return jsonify(id=iid, hint=bool(hint))


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
        self.exact, self.by_name, self.by_page, self.on_page = {}, {}, {}, {}
        for rid, name, page, source in con.execute(
                "SELECT id, name, page, source FROM recipes WHERE book_id=?", (book_id,)):
            self.add(rid, name, page, source)

    def add(self, rid, name, page, source="eyb"):
        self.exact[(fold(name), page or "")] = rid
        self.by_name.setdefault(fold(name), []).append(rid)
        self.on_page.setdefault(page or "", []).append((rid, name))
        if page and source != "eyb" and eyb.GARBLED not in name:
            self.by_page.setdefault(page, []).append((rid, name))

    def find(self, name, page):
        rid = self.exact.get((fold(name), page or ""))
        if rid:
            return rid
        if eyb.GARBLED in name:  # "tud�usi" is the "tudòusi" already on that page
            twins = [rid for rid, other in self.on_page.get(page or "", []) if eyb.garbled_twin(name, other)]
            if len(twins) == 1:
                return twins[0]
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


def book_groups(rows):
    """EYB rows by book title: ({title: {author, count}}, {title: [rows]})."""
    groups, by_title = {}, {}
    for r in rows:
        g = groups.setdefault(r["book_title"], {"author": r["author"], "count": 0})
        g["count"] += 1
        by_title.setdefault(r["book_title"], []).append(r)
    return groups, by_title


def preview_groups(groups, by_title, matched, tagged_already, indexes):
    """Per EYB book: which catalog book it matched and how many of its recipes would be tagged (existing),
    added (new) or are tagged already. `indexes` caches BookRecipes per book between calls."""
    out = []
    for t, g in groups.items():
        b, method = matched[t]
        existing = new = already = 0
        if b:
            idx = indexes.get(b["id"]) or indexes.setdefault(b["id"], BookRecipes(db(), b["id"]))
            for r in by_title[t]:
                rid = idx.find(r["name"], r["page"])
                if rid is None:  # would be added; a stand-in id (negative: never a real one) so repeats count once
                    new += 1
                    standin = -1 - sum(len(x.exact) for x in indexes.values())
                    idx.add(standin, r["name"], r["page"])
                    tagged_already.add(standin)
                elif rid in tagged_already:
                    already += 1
                else:
                    existing += 1
                    tagged_already.add(rid)
        out.append({"eyb_title": t, "author": g["author"], "count": g["count"], "match": eyb.brief(b),
                    "method": method, "existing": existing, "new": new, "already": already})
    out.sort(key=lambda g: (g["match"] is not None, fold(g["eyb_title"])))
    return out


def tagged_with(name):
    iid = ingredient_id(name) if name else None
    return iid, ({r[0] for r in db().execute("SELECT recipe_id FROM recipe_ingredients WHERE ingredient_id=?", (iid,))}
                 if iid else set())


@app.post("/admin/api/ingredients/preview")
@editor_required
def ingredient_preview():
    filename, rows = read_upload()
    name = clean_name(request.form.get("ingredient")) or guess_name(filename)
    books = eyb.catalog()
    groups, by_title = book_groups(rows)
    iid, tagged_already = tagged_with(name)
    out = preview_groups(groups, by_title, eyb.match_recipe_books(groups, books), tagged_already, {})
    hint = db().execute("SELECT hint FROM ingredients WHERE id=?", (iid,)).fetchone()[0] if iid else None
    return jsonify(kind="ingredient", ingredient=name, exists=bool(iid), hint=None if hint is None else bool(hint),
                   groups=out, total=len(rows),
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
    if request.form.get("hint") in ("0", "1"):  # search hint only, or a chip on the catalog page
        con.execute("UPDATE ingredients SET hint=? WHERE id=?", (int(request.form["hint"]), iid))
    name, hint = con.execute("SELECT name, hint FROM ingredients WHERE id=?", (iid,)).fetchone()
    created = {}
    res = tag_rows(con, iid, rows, decisions, {}, created)
    if isinstance(res, Response):
        return res
    tagged, added, skipped = res
    eyb.merge_garbled(con)  # a good copy just added replaces any garbled one already here
    con.commit()
    total = con.execute("SELECT count(*) FROM recipe_ingredients WHERE ingredient_id=?", (iid,)).fetchone()[0]
    return jsonify(kind="ingredient", ingredient=name, hint=bool(hint), tagged=tagged, added_recipes=added,
                   skipped=skipped, created_books=len(created), total=total)


def tag_rows(con, iid, rows, decisions, books, created):
    """Tag EYB `rows` with ingredient `iid`, adding recipes that aren't in their book yet. decisions maps an EYB
    book title to a book id, "create" or "skip"; `books` caches BookRecipes per book, `created` new books.
    Returns (tagged, added, skipped), or an error response."""
    tagged = added = skipped = 0
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
    return tagged, added, skipped


# ------------------------------------------------------------------ zip of ingredient lists
# Many EYB ingredient exports at once. Each CSV goes into the tag named after it ("Kale - all pages combined.csv"
# -> Kale) unless the preview gives it another name; files given the same name are combined into one tag
# ("chicken broth" and "white stock" -> Chicken stock). A tags.csv in the zip (columns file, tag, hint) can set
# those names up front, and may list a file twice to feed two tags. Only books already in the catalog are used:
# recipes from other books are skipped, so a bulk import never adds books.
ZIP_MAX_FILES, ZIP_MAX_BYTES = 500, 100 * 1024 * 1024


def read_zip():
    """The uploaded zip -> ({file name: rows}, [manifest entries], [problems])."""
    import io, zipfile
    f = request.files.get("file")
    if not f:
        abort(make_err(400, "Choose a zip of CSV files exported from Eat Your Books"))
    try:
        z = zipfile.ZipFile(io.BytesIO(f.read()))
    except zipfile.BadZipFile:
        abort(make_err(400, "That isn't a zip file"))
    entries = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".csv")
               and "__MACOSX" not in i.filename and not i.filename.rsplit("/", 1)[-1].startswith(".")]
    if len(entries) > ZIP_MAX_FILES or sum(i.file_size for i in entries) > ZIP_MAX_BYTES:
        abort(make_err(400, f"That zip is too big (at most {ZIP_MAX_FILES} CSVs, 100 MB unzipped)"))
    files, manifest, problems = {}, [], []
    for i in sorted(entries, key=lambda i: fold(i.filename)):
        name = i.filename.rsplit("/", 1)[-1]
        raw = z.read(i)
        head = eyb.decode(raw[:2000]).splitlines()[0].lower() if raw else ""
        if "file" in head and "tag" in head and "recipe title" not in head:  # tags.csv: how to group the files
            import csv
            for row in csv.DictReader(io.StringIO(eyb.decode(raw))):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                if row.get("file") and row.get("tag"):
                    manifest.append({"file": row["file"], "tag": clean_name(row["tag"]),
                                     "hint": row.get("hint", "").lower() not in ("no", "n", "0", "false", "chip")})
            continue
        try:
            kind, rows = eyb.parse(raw)
        except Exception:  # noqa: BLE001 — report it, keep going
            kind, rows = None, []
        if kind != "recipes":
            problems.append(f"{name}: not an EYB recipe export")
            continue
        files[name] = rows
    return files, manifest, problems


def file_key(name):
    """How tags.csv refers to a file: "demi-glace", "Demi-glace - all pages combined.csv" and "demi glace"
    are all the same file."""
    name = FILE_NOISE.sub(" ", re.sub(r"\.csv$", "", name, flags=re.I))
    return " ".join(re.findall(r"[^\W_]+", fold(name)))


def zip_plan(files, manifest, problems):
    """[{file, tag, hint}] for the preview: from tags.csv where it names the file, else one per file, named
    after it, a search hint unless the tag already exists as a catalog chip."""
    keys = {file_key(f): f for f in files}
    plan, covered = [], set()
    for m in manifest:
        f = keys.get(file_key(m["file"]))
        if f:
            plan.append({"file": f, "tag": m["tag"], "hint": m["hint"]})
            covered.add(f)
        else:
            problems.append(f"tags.csv: no file for \u201c{m['file']}\u201d")
    for f in files:
        if f not in covered:
            name = guess_name(f)
            row = db().execute("SELECT hint FROM ingredients WHERE fold(name)=fold(?)", (name,)).fetchone()
            plan.append({"file": f, "tag": name, "hint": bool(row[0]) if row else True})
    return plan


@app.post("/admin/api/ingredients/zip/preview")
@editor_required
def ingredient_zip_preview():
    files, manifest, problems = read_zip()
    if not files:
        return make_err(400, "No EYB recipe exports in that zip" + (f" ({problems[0]})" if problems else ""))
    books = eyb.catalog()
    per_file = {f: book_groups(rows) for f, rows in files.items()}
    all_groups = {}
    for groups, _ in per_file.values():
        for t, g in groups.items():
            all_groups.setdefault(t, g)
    matched = eyb.match_recipe_books(all_groups, books)  # one pass for every book named in the zip
    tags = {}
    out = []
    for item in zip_plan(files, manifest, problems):
        groups, by_title = per_file[item["file"]]
        key = fold(item["tag"])
        if key not in tags:
            iid, already = tagged_with(item["tag"])
            tags[key] = (iid, already, {})
        iid, already, indexes = tags[key]
        g = preview_groups(groups, by_title, matched, already, indexes)
        unmatched = [x for x in g if not x["match"]]
        out.append({**item, "exists": bool(iid), "recipes": sum(x["count"] for x in g),
                    "existing": sum(x["existing"] for x in g), "new": sum(x["new"] for x in g),
                    "already": sum(x["already"] for x in g), "skipped": sum(x["count"] for x in unmatched),
                    "skipped_books": [x["eyb_title"] for x in unmatched]})
    return jsonify(files=out, problems=problems, manifest=bool(manifest))


@app.post("/admin/api/ingredients/zip/commit")
@editor_required
def ingredient_zip_commit():
    """Tag everything in one go. plan: [{file, tag, hint}] from the preview (as edited); a file can appear twice."""
    files, _, _ = read_zip()
    try:
        plan = json.loads(request.form.get("plan") or "[]")
    except ValueError:
        return make_err(400, "Bad plan")
    plan = [p for p in plan if p.get("file") in files and clean_name(p.get("tag"))]
    if not plan:
        return make_err(400, "Nothing to import: give at least one file a tag name")
    con = db()
    books = eyb.catalog()
    all_groups = {}
    for p in plan:
        for t, g in book_groups(files[p["file"]])[0].items():
            all_groups.setdefault(t, g)
    decisions = {t: (b["id"] if b else "skip") for t, (b, _) in eyb.match_recipe_books(all_groups, books).items()}
    results, indexes = {}, {}
    for p in plan:
        name = clean_name(p["tag"])
        key = fold(name)
        if key not in results:
            iid = core.name_id(con, "ingredients", name)
            con.execute("UPDATE ingredients SET hint=? WHERE id=?", (1 if p.get("hint") else 0, iid))
            results[key] = {"iid": iid, "files": [], "tagged": 0, "added": 0, "skipped": 0}
        res = results[key]
        out = tag_rows(con, res["iid"], files[p["file"]], decisions, indexes, {})
        if isinstance(out, Response):
            return out
        res["files"].append(p["file"])
        for k, v in zip(("tagged", "added", "skipped"), out):
            res[k] += v
    eyb.merge_garbled(con)
    con.commit()
    summary = []
    for res in results.values():
        name, hint = con.execute("SELECT name, hint FROM ingredients WHERE id=?", (res["iid"],)).fetchone()
        total = con.execute("SELECT count(*) FROM recipe_ingredients WHERE ingredient_id=?", (res["iid"],)).fetchone()[0]
        summary.append({"tag": name, "hint": bool(hint), "files": res["files"], "tagged": res["tagged"],
                        "added_recipes": res["added"], "skipped": res["skipped"], "total": total})
    return jsonify(tags=sorted(summary, key=lambda x: fold(x["tag"])))


# ------------------------------------------------------------------ ranking
# Search finds recipes by title or tag; ranking puts the ones that clearly use what was asked for first.
# EYB's list for an ingredient includes recipes that only use a minor form of it: "chicken" brings in osso buco
# made with chicken stock, "tomatoes" anything with a spoonful of tomato paste, "rice" Chinese dishes seasoned
# with rice wine. A tag named "<main tag> <form>" (Chicken stock, Tomato paste, Rice wine, Duck fat…) marks
# those, so a recipe tagged Chicken *and* Chicken stock, with no chicken in its title or a more specific tag,
# ranks low: it may use only the stock. It is never dropped, since most chicken soups use both.
MINOR_FORMS = {"stock", "broth", "fat", "paste", "sauce", "vinegar", "wine", "flour", "starch"}


def tag_kinds(con):
    """{tag id: (stemmed words, kind, name)}, kind 'minor' (Chicken stock, Tomato paste), 'broad' (a tag that
    has a minor form, like Chicken) or 'plain'; and {broad tag id: its minor-form tag ids}."""
    names = dict(con.execute("SELECT id, name FROM ingredients").fetchall())
    words = {iid: set(ingredient_words(name)) for iid, name in names.items()}
    minors = {iid: frozenset(w - MINOR_FORMS) for iid, w in words.items() if w & MINOR_FORMS and w - MINOR_FORMS}
    kinds, minor_of = {}, {}
    for iid, w in words.items():
        mine = {mid for mid, base in minors.items() if base == frozenset(w)}
        kinds[iid] = (w, "minor" if iid in minors else "broad" if mine else "plain", names[iid])
        if mine:
            minor_of[iid] = mine
    return kinds, minor_of


def search_terms(q, ingredient):
    """What a search asked for, as stems to look for in titles and tags: "chicken leeks" -> ["chicken", "leek"]."""
    out = []
    words, runs = core.search_words(q)
    for t in [stem(fold(w)) for w in words] + [fold(r) for r in runs] + ingredient_words(ingredient or ""):
        if t and t not in out:
            out.append(t)
    return out


# Words that make "<ingredient> ___" in a title a stock, fat or seasoning rather than the ingredient itself:
# "Chicken stock", "Basic chicken broth", "Chicken jus", "Duck-fat potatoes".
TITLE_FORMS = MINOR_FORMS | {"jus", "bouillon", "consomme", "glace", "fumet"}


def minor_in_title(term, text, asked):
    """The phrase if `term` is in the title only as a stock/fat/paste of it ("chicken stock", "chicken or turkey
    stock", "stock for chicken", "duck-fat"), and the search didn't ask for that form; else None."""
    word = re.escape(term) + "(?:s|es)?"
    forms = "|".join(sorted(f for f in TITLE_FORMS if f not in asked))
    if not forms:
        return None
    phrase = re.compile(rf"(?<!\w){word}(?:\s+(?:or|and|&)\s+\w+)?[\s-]+(?:{forms})(?!\w)"
                        rf"|(?<!\w)(?:{forms})\s+(?:of|for|from)\s+(?:\w+\s+)?{word}(?!\w)")
    m = phrase.search(text)
    if not m or in_title(term, phrase.sub(" ", text)):
        return None  # not in such a phrase, or also elsewhere in the title ("chicken soup with chicken stock")
    return m.group(0)


def in_title(term, text):
    """`term` (a stem) is a whole word of the folded title, singular or plural: "egg" in "Eggs Benedict",
    not in "Eggplant"; Chinese/Japanese/Korean/Thai anywhere."""
    if core.UNSPACED.search(term):
        return term in text
    forms = [re.escape(term) + "(?:s|es)?"] + ([re.escape(term[:-1]) + "ies"] if term.endswith("y") else [])
    return re.search(r"(?<!\w)(?:" + "|".join(forms) + r")(?!\w)", text) is not None


def ranked(con, base_sql, args, terms, by_book=False):
    """Every recipe matching `base_sql` (a "FROM recipes r JOIN books b ... WHERE ..." clause), best first, as
    [{id, score, match}]. Per term: 2 if it's in the title or index heading; 1½ if a tag gives it (Leeks, Chicken
    pieces, Chicken without Chicken stock, or Chicken stock when the search names the stock too); ½ if the title
    has it only as a stock or fat of it ("Chicken stock", "Duck-fat potatoes"); ¼ if only Chicken + one of its
    minor forms do. +1 if every term is in the title. So for "chicken leeks": both in the
    title (5), one in the title and one from a tag (3½), both from tags (3), leeks in the title but the chicken
    maybe only stock (2¼). Ties, and searches without terms, go by book and page. by_book keeps each book's
    recipes together, books with the best matches first (the website groups results under a heading per book)."""
    rows = con.execute("SELECT r.id, r.name, r.heading, r.page_sort, b.id AS book_id, b.title" + base_sql, args).fetchall()
    tags = {}
    if terms and rows:
        for rid, iid in con.execute("SELECT ri.recipe_id, ri.ingredient_id FROM recipe_ingredients ri "
                                    "WHERE ri.recipe_id IN (SELECT r.id" + base_sql + ")", args):
            tags.setdefault(rid, set()).add(iid)
    kinds, minor_of = tag_kinds(con) if terms else ({}, {})
    asked = set(terms)
    out = []
    for r in rows:
        text = fold(f"{r['name']} {r['heading'] or ''}")
        mine = tags.get(r["id"], set())
        points, titled, why = [], 0, []
        for t in terms:
            stock_title = in_title(t, text) and minor_in_title(t, text, asked)
            if in_title(t, text) and not stock_title:
                points.append(2)
                titled += 1
                why.append(f"{t}: title")
                continue
            best, minor_names = (0.5, []) if stock_title else (0, [])
            for iid in mine:
                w, kind, _ = kinds.get(iid, (set(), "plain", ""))
                if t not in w:
                    continue
                if kind == "minor":  # Tomato paste counts only for a search that names the paste
                    if w <= asked:
                        best = max(best, 1.5)
                elif kind == "broad" and minor_of[iid] & mine:  # Tomatoes + Tomato paste: maybe only the paste
                    best = max(best, 0.25)
                    minor_names += [kinds[m][2].lower() for m in sorted(minor_of[iid] & mine)]
                else:
                    best = max(best, 1.5)
            points.append(best)
            why.append(f"{t}: " + ("tag" if best >= 1.5 else f"only as \u201c{stock_title}\u201d in the title" if best == 0.5
                                   else "may be only " + " or ".join(dict.fromkeys(minor_names)) if best == 0.25 else "matched"))
        score = sum(points) + (1 if terms and titled == len(terms) else 0)
        # for the MCP, why it matched: "title" when every word is in the title, else per word
        match = ("title" if titled == len(terms) else ", ".join(why)) if terms else None
        out.append({"id": r["id"], "score": score, "match": match, "_book": r["book_id"],
                    "_key": (r["title"], r["page_sort"] is None, r["page_sort"] or 0, r["name"])})
    if by_book:
        best = {}
        for x in out:
            best[x["_book"]] = max(best.get(x["_book"], 0), x["score"])
        out.sort(key=lambda x: (-best[x["_book"]], x["_key"][0], x["_book"], -x["score"], x["_key"][1:]))
    else:
        out.sort(key=lambda x: (-x["score"], x["_key"]))
    for x in out:
        del x["_book"], x["_key"]
    return out
