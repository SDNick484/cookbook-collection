"""Eat Your Books import.

Two CSV exports are understood (both are Windows-1252 with %uXXXX escapes for
characters outside that range):
  Bookshelf:  Book Title, Author, ISBN, Book Format, Indexing Status
  Recipes:    Recipe Title, Book Title, Author, Page No.   (EYB exports 200 rows at a time)

Every import is preview -> commit. The preview proposes a match for each EYB book
(ISBN first, then title + author); the commit applies the user's decisions. The
same file can be imported again: books are matched by ISBN/EYB title, and recipes
already present are skipped.

Also here: bulk "fetch covers & details" for books that have an ISBN.
"""
import csv, difflib, io, json, re, time, unicodedata

from flask import abort, jsonify, request

import app as core
from app import app, db, editor_required, fold, make_err, page_sort, clean


def migrate(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    for col in ("format", "eyb_title", "eyb_status", "enriched_at"):
        if col not in cols:
            con.execute(f"ALTER TABLE books ADD COLUMN {col} TEXT")


# ------------------------------------------------------------------ parsing
def decode(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    # UTF-8 that was read as Windows-1252 and saved again (e.g. combining exports in Excel): "CrÃ¨me" -> "Crème".
    if re.search("[ÂÃÅâ][\u0080-\u00bf\u2010-\u203a\u20ac\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc]", text):
        try:
            text = text.encode("cp1252").decode("utf-8")
        except UnicodeError:
            pass  # not cleanly reversible, so leave it as it is
    text = re.sub(r"%u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)
    return unicodedata.normalize("NFC", text).translate(core.LIGATURES)


def split_authors(s):
    s = clean(s) or ""
    return [a.strip() for a in re.split(r"\s+and\s+|\s*&\s*|;\s*", s) if a.strip()]


def parse(raw):
    """Returns ('books' | 'recipes', rows)."""
    reader = csv.reader(io.StringIO(decode(raw)))
    header = [h.strip().lower() for h in next(reader, [])]
    if "recipe title" in header:
        ix = {k: header.index(k) for k in ("recipe title", "book title", "author", "page no.") if k in header}
        rows = []
        for r in reader:
            get = lambda k: (r[ix[k]].strip() if k in ix and ix[k] < len(r) else "")
            name, book = get("recipe title"), get("book title")
            if not name or not book or (name.lower() == "recipe title" and book.lower() == "book title"):
                continue  # blank rows, and header rows repeated by concatenating several exports
            m = re.search(r"\d+(?:\s*[-–]\s*\d+)?", get("page no."))
            rows.append({"name": re.sub(r"\s+", " ", name), "book_title": book, "author": get("author"),
                         "page": re.sub(r"\s*-\s*", "–", m.group()) if m else None})
        return "recipes", rows
    if "book title" in header and "isbn" in header:
        ix = {k: header.index(k) for k in ("book title", "author", "isbn", "book format", "indexing status")
              if k in header}
        rows = []
        for r in reader:
            get = lambda k: (r[ix[k]].strip() if k in ix and ix[k] < len(r) else "")
            title = get("book title")
            if not title:
                continue
            rows.append({"title": title, "authors": split_authors(get("author")),
                         "isbn": core.normalize_isbn(get("isbn")), "format": get("book format") or None,
                         "eyb_status": get("indexing status") or None})
        return "books", rows
    abort(make_err(400, "That doesn't look like an Eat Your Books Bookshelf or Recipes export"))


# ------------------------------------------------------------------ matching
STOP = {"the", "a", "an", "of", "and", "cookbook", "cookbooks", "book", "recipe", "with", "from", "for", "to",
        "in", "vol", "volume", "edition", "revised"}
NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "i": "1", "ii": "2", "iii": "3"}
SYNONYMS = {"ny": "new york", "nyt": "new york times", "bbq": "barbecue", "flavour": "flavor", "favourite": "favorite"}


def toks(s):
    s = fold(s).replace("&", " and ")
    s = re.sub(r"['’]s\b", "", s)  # possessives: Cook's -> Cook
    out = set()
    for t in re.findall(r"[a-z0-9]+", s):
        for t in SYNONYMS.get(t, t).split():
            t = NUMBERS.get(t, t)
            if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
                t = t[:-1]  # plurals: techniques -> technique
            if t not in STOP:
                out.add(t)
    return out


def surnames(authors):
    out = set()
    for a in authors or []:
        parts = re.findall(r"[a-z]+", fold(re.sub(r"\(.*?\)", "", a)))
        if parts:
            out.add(parts[-1])
    return out


def title_score(a, b):
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    squash = lambda t: re.sub(r"[^a-z0-9]", "", re.sub(r"^(the|a|an)\s+", "", fold(t)))
    if ta == tb or squash(a) == squash(b):  # "Home Cheese Making" == "Home Cheesemaking"
        return 1.0
    jac = len(ta & tb) / len(ta | tb)
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 2:
        contain = len(small & large) / len(small)
    else:  # a one-word title only "contains" into a two-word title (Salt ⊂ Miracle of Salt)
        contain = 1.0 if small <= large and len(large) <= 2 else 0.0
    seq = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jac, 0.9 * contain, seq)


def catalog():
    con = db()
    rows = con.execute("SELECT * FROM books").fetchall()
    return core.book_dicts(rows, True)


def match_books(eyb_rows, books):
    """One-to-one matching, strongest evidence first:
    1. same ISBN, or the EYB title recorded by an earlier import
    2. strong title match (>= 0.75 with a shared author surname, >= 0.85 without), best pairs first
    3. an author's single remaining EYB book <-> that author's single remaining catalog book, when one
       title's words contain the other's (catches abbreviated titles like "Burma")
    Returns [(book | None, score, method)] aligned with eyb_rows."""
    by_isbn = {b["isbn"]: b for b in books if b.get("isbn")}
    by_eyb = {fold(b["eyb_title"]): b for b in books if b.get("eyb_title")}
    result = [None] * len(eyb_rows)
    taken = set()
    for i, e in enumerate(eyb_rows):
        b = (e["isbn"] and by_isbn.get(e["isbn"])) or by_eyb.get(fold(e["title"]))
        if b and b["id"] not in taken:
            result[i] = (b, 1.0, "ISBN" if e["isbn"] and b.get("isbn") == e["isbn"] else "earlier import")
            taken.add(b["id"])
    pairs = []
    for i, e in enumerate(eyb_rows):
        if result[i]:
            continue
        es = surnames(e["authors"])
        for b in books:
            if b["id"] in taken or (b.get("isbn") and e["isbn"] and b["isbn"] != e["isbn"]):
                continue
            s = title_score(e["title"], b["title"])
            same_author = bool(es & surnames(b["authors"]))
            if (same_author and s >= 0.75) or s >= 0.85:
                pairs.append((s + (0.25 if same_author else 0), i, b, same_author))
    for score, i, b, same_author in sorted(pairs, key=lambda p: (-p[0], p[1])):
        if result[i] or b["id"] in taken:
            continue
        result[i] = (b, round(min(score, 1.25) / 1.25, 2), "title + author" if same_author else "title")
        taken.add(b["id"])
    # 3. only remaining book by that author on both sides
    for sn in {x for e in eyb_rows for x in surnames(e["authors"])}:
        rows_left = [i for i, e in enumerate(eyb_rows) if not result[i] and sn in surnames(e["authors"])]
        books_left = [b for b in books if b["id"] not in taken and sn in surnames(b["authors"])
                      and b.get("status") not in ("gone", "wanted")]
        if len(rows_left) == 1 and len(books_left) == 1:
            i, b = rows_left[0], books_left[0]
            ta, tb = toks(eyb_rows[i]["title"]), toks(b["title"])
            nested = bool(ta and tb) and (ta <= tb or tb <= ta)  # "Burma" ⊂ "Burma - Rivers of Flavor"
            s = title_score(eyb_rows[i]["title"], b["title"])
            if nested and not (b.get("isbn") and eyb_rows[i]["isbn"] and b["isbn"] != eyb_rows[i]["isbn"]):
                result[i] = (b, round(s, 2), "only book by author")
                taken.add(b["id"])
    # 4. no author on the EYB side: a distinctive word shared with exactly one remaining book
    #    ("Creuset: A Collection of Recipes..." -> "Le Creuset Cookbook")
    left = [b for b in books if b["id"] not in taken and b.get("status") not in ("gone", "wanted")]
    for i, e in enumerate(eyb_rows):
        if result[i] or e["authors"]:
            continue
        for w in sorted(toks(e["title"]), key=len, reverse=True):
            if len(w) < 5 or w.isdigit():
                continue
            hits = [b for b in left if w in toks(b["title"])]
            in_other_rows = any(j != i and w in toks(x["title"]) for j, x in enumerate(eyb_rows))
            if len(hits) == 1 and not in_other_rows and hits[0]["id"] not in taken:
                result[i] = (hits[0], 0.5, f"shared word “{w}”")
                taken.add(hits[0]["id"])
                break
    return [r or (None, 0, None) for r in result]


def match_recipe_books(groups, books):
    """groups: {eyb_title: {"author":..}} -> {eyb_title: (book, method)}.
    Order of evidence: EYB title remembered from an earlier import; strong title match; same author with one
    title nested in the other ("Asada" / "Asada: The Art of Mexican-Style Grilling"); same author's only book
    here with a related title ("Let's Cook French" / "Kids Cook French")."""
    by_eyb = {fold(b["eyb_title"]): b for b in books if b.get("eyb_title")}
    owned = [b for b in books if b.get("status") not in ("gone", "wanted")]
    out = {}
    for t, g in groups.items():
        b = by_eyb.get(fold(t))
        if b:
            out[t] = (b, "eyb title")
            continue
        es = surnames(split_authors(g["author"]))
        best, bs, same = None, 0.0, False
        for c in books:
            s = title_score(t, c["title"])
            sa = bool(es & surnames(c["authors"]))
            eff = s + (0.25 if sa else 0)
            if ((sa and s >= 0.75) or s >= 0.85) and eff > bs:
                best, bs, same = c, eff, sa
        if best:
            out[t] = (best, "title + author" if same else "title")
            continue
        full = {re.sub(r"[^a-z]", "", fold(a)) for a in split_authors(g["author"])}
        by_author = [c for c in owned if full & {re.sub(r"[^a-z]", "", fold(a)) for a in c["authors"]}] if full else []
        tt = toks(t)
        nested = [c for c in by_author if tt and toks(c["title"]) and (tt <= toks(c["title"]) or toks(c["title"]) <= tt)]
        if len(nested) == 1:
            out[t] = (nested[0], "author + nested title")
        elif len(by_author) == 1 and title_score(t, by_author[0]["title"]) >= 0.4:
            out[t] = (by_author[0], "only book by author")
        else:
            out[t] = (None, None)
    return out


def norm_t(s):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"^(the|a|an)\s+", "", fold(s)))


def brief(b):
    return {"id": b["id"], "title": b["title"], "authors": b["authors"], "isbn": b.get("isbn")} if b else None


def read_upload():
    f = request.files.get("file")
    if not f:
        abort(make_err(400, "Choose the CSV file exported from Eat Your Books"))
    return parse(f.read())


# ------------------------------------------------------------------ recipe de-duplication
class RecipeIndex:
    """What a book already has. A new row is a duplicate if it has the same name and page as anything,
    or if it re-words a recipe that came from somewhere other than EYB (the sheet, a photo, typed in)
    on the same page: "Roast chicken with bread salad" vs "Zuni Roast Chicken with Bread Salad", p. 342.
    The fuzzy rule is not applied between EYB rows, since one page often holds several close variants
    ("Poached eggs with duxelles" / "Poached eggs with creamed spinach")."""

    def __init__(self, con, book_id):
        self.exact, self.by_page = set(), {}
        for name, page, source in con.execute("SELECT name, page, source FROM recipes WHERE book_id=?", (book_id,)):
            self.exact.add((fold(name), page or ""))
            if page and source != "eyb":
                self.by_page.setdefault(page, []).append(name)

    def has(self, name, page):
        if (fold(name), page or "") in self.exact:
            return True
        ta = toks(name)
        for other in self.by_page.get(page or "", []) if page else []:
            tb = toks(other)
            small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if (small and small <= large) or difflib.SequenceMatcher(None, fold(name), fold(other)).ratio() >= 0.8:
                return True
        return False

    def add(self, name, page):
        self.exact.add((fold(name), page or ""))


# ------------------------------------------------------------------ routes
@app.post("/admin/api/eyb/preview")
@editor_required
def eyb_preview():
    kind, rows = read_upload()
    books = catalog()
    if kind == "books":
        matches = match_books(rows, books)
        items = []
        for i, (e, (b, score, method)) in enumerate(zip(rows, matches)):
            fills = []
            if b:
                if e["isbn"] and not b.get("isbn"):
                    fills.append("ISBN")
                if e["format"] and not b.get("format"):
                    fills.append("format")
                if e["authors"] and not b["authors"]:
                    fills.append("authors")
                if e["isbn"] and b.get("isbn") and b["isbn"] != e["isbn"]:
                    fills.append("ISBN differs")
            # offer EYB's title when it differs in substance (not just "The"/punctuation); pre-check it when the
            # words are identical but reordered, or the sheet import already flagged this book's title
            retitle = suggest = False
            if b and norm_t(e["title"]) != norm_t(b["title"]):
                retitle = True
                words = lambda t: sorted(re.findall(r"[a-z0-9]+", fold(t)))
                suggest = words(e["title"]) == words(b["title"]) or "title" in (b.get("needs_review") or "").lower()
            items.append({"i": i, "eyb": e, "match": brief(b), "score": score, "method": method, "fills": fills,
                          "retitle": retitle, "retitle_default": suggest})
        return jsonify(kind="books", rows=items, total=len(rows),
                       matched=sum(1 for x in items if x["match"]),
                       catalog_unmatched=[brief(b) for b in books
                                          if b["id"] not in {x["match"]["id"] for x in items if x["match"]}
                                          and b.get("status") not in ("gone", "wanted")])
    groups = {}
    for r in rows:
        g = groups.setdefault(r["book_title"], {"author": r["author"], "count": 0, "pages": 0})
        g["count"] += 1
        g["pages"] += 1 if r["page"] else 0
    matched = match_recipe_books(groups, books)
    by_title = {}
    for r in rows:
        by_title.setdefault(r["book_title"], []).append(r)
    out = []
    for t, g in groups.items():
        b, method = matched[t]
        new = g["count"]
        if b:
            idx, new = RecipeIndex(db(), b["id"]), 0
            for r in by_title[t]:
                if not idx.has(r["name"], r["page"]):
                    idx.add(r["name"], r["page"])
                    new += 1
        out.append({"eyb_title": t, "author": g["author"], "count": g["count"], "with_pages": g["pages"],
                    "new": new, "match": brief(b), "method": method})
    out.sort(key=lambda g: (g["match"] is not None, g["method"] == "eyb title", fold(g["eyb_title"])))
    return jsonify(kind="recipes", groups=out, total=len(rows),
                   books=[{"id": b["id"], "title": b["title"]} for b in sorted(books, key=lambda b: fold(b["title"]))])


@app.post("/admin/api/eyb/commit")
@editor_required
def eyb_commit():
    kind, rows = read_upload()
    try:
        decisions = json.loads(request.form.get("decisions") or "{}")
    except ValueError:
        return make_err(400, "Bad decisions")
    con = db()
    if kind == "books":
        updated = created = skipped = retitled = 0
        retitle = {str(x) for x in json.loads(request.form.get("retitle") or "[]")}
        for i, e in enumerate(rows):
            d = decisions.get(str(i), "auto")
            if d == "skip":
                skipped += 1
                continue
            if d == "create":
                bid = None
            else:
                bid = int(d) if str(d).isdigit() else None
                if bid is None:  # "auto" without a match on the client means create
                    skipped += 1
                    continue
            isbn_free = not e["isbn"] or not con.execute("SELECT 1 FROM books WHERE isbn=? AND id IS NOT ?",
                                                         (e["isbn"], bid)).fetchone()
            if bid:
                b = con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
                if not b:
                    skipped += 1
                    continue
                sets = {"eyb_title": e["title"], "eyb_status": e["eyb_status"]}
                if e["isbn"] and not b["isbn"] and isbn_free:
                    sets["isbn"] = e["isbn"]
                if e["format"] and not b["format"]:
                    sets["format"] = e["format"]
                if str(i) in retitle:
                    sets["title"] = e["title"]
                    if "title" in (b["needs_review"] or "").lower():
                        sets["needs_review"] = None
                    retitled += 1
                con.execute(f"UPDATE books SET {', '.join(k + '=?' for k in sets)}, updated_at=CURRENT_TIMESTAMP "
                            f"WHERE id=?", [*sets.values(), bid])
                has_authors = con.execute("SELECT 1 FROM book_authors WHERE book_id=?", (bid,)).fetchone()
                if not has_authors and e["authors"]:
                    tags = [r[0] for r in con.execute("SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
                                                      "WHERE bt.book_id=?", (bid,))]
                    core.set_people(con, bid, e["authors"], tags)
                updated += 1
            else:
                bid = con.execute(
                    "INSERT INTO books(title, isbn, format, eyb_title, eyb_status, source, needs_review) "
                    "VALUES (?,?,?,?,?, 'eyb', ?)",
                    (e["title"], e["isbn"] if isbn_free else None, e["format"], e["title"], e["eyb_status"],
                     "From Eat Your Books, which may be out of date: confirm you still own it, then set cuisine "
                     "and tags (or set status to No longer owned)")).lastrowid
                core.set_people(con, bid, e["authors"], [])
                created += 1
            core.reindex_book(con, bid)
        con.commit()
        return jsonify(kind="books", updated=updated, created=created, skipped=skipped, retitled=retitled)

    # recipes: decisions = {eyb_title: book_id | "create" | "skip"}
    added = dup = skipped = 0
    existing_cache, created = {}, {}
    for r in rows:
        d = decisions.get(r["book_title"], "skip")
        if d == "create":
            if r["book_title"] not in created:
                bid = con.execute(
                    "INSERT INTO books(title, eyb_title, source, needs_review) VALUES (?,?, 'eyb', ?)",
                    (r["book_title"], r["book_title"],
                     "From Eat Your Books, which may be out of date: confirm you still own it, then set cuisine "
                     "and tags (or set status to No longer owned)")).lastrowid
                core.set_people(con, bid, split_authors(r["author"]), [])
                core.reindex_book(con, bid)
                created[r["book_title"]] = bid
            d = created[r["book_title"]]
        if d == "skip" or not str(d).isdigit():
            skipped += 1
            continue
        bid = int(d)
        if bid not in existing_cache:
            if not con.execute("SELECT 1 FROM books WHERE id=?", (bid,)).fetchone():
                return make_err(400, f"No such book {bid}")
            existing_cache[bid] = RecipeIndex(con, bid)
            con.execute("UPDATE books SET eyb_title=coalesce(eyb_title, ?) WHERE id=?", (r["book_title"], bid))
        if existing_cache[bid].has(r["name"], r["page"]):
            dup += 1
            continue
        existing_cache[bid].add(r["name"], r["page"])
        con.execute("INSERT INTO recipes(book_id, name, page, page_sort, source) VALUES (?,?,?,?, 'eyb')",
                    (bid, r["name"], r["page"], page_sort(r["page"])))
        added += 1
    con.commit()
    return jsonify(kind="recipes", added=added, duplicates=dup, skipped=skipped, books=len(existing_cache),
                   created_books=len(created))


# ------------------------------------------------------------------ bulk covers & details
def enrich_candidates(con, retry_days=14):
    return con.execute(
        "SELECT * FROM books WHERE isbn IS NOT NULL AND (cover IS NULL OR year IS NULL OR publisher IS NULL) "
        "AND (enriched_at IS NULL OR enriched_at < datetime('now', ?)) ORDER BY id",
        (f"-{retry_days} days",)).fetchall()


@app.get("/admin/api/enrich")
@editor_required
def enrich_status():
    return jsonify(remaining=len(enrich_candidates(db())))


@app.post("/admin/api/enrich")
@editor_required
def enrich_batch():
    """Fill missing cover / year / publisher / subtitle for a few books per call (the page loops)."""
    con = db()
    n = max(1, min(int((request.get_json(silent=True) or {}).get("limit") or 5), 20))
    batch = enrich_candidates(con)[:n]
    results = []
    for b in batch:
        got, filled = None, []
        for fn in (core.ol_by_isbn, core.google_by_isbn):
            try:
                r = fn(b["isbn"])
            except Exception:  # noqa: BLE001 — try the next source
                continue
            if r:
                got = got or {}
                for k, v in r.items():
                    if v and not got.get(k):
                        got[k] = v
                if all(got.get(k) for k in ("year", "publisher")) and (b["cover"] or got.get("cover_url")):
                    break
        sets = {}
        if got:
            if not b["year"] and got.get("year"):
                sets["year"] = got["year"]
            if not b["publisher"] and got.get("publisher"):
                sets["publisher"] = got["publisher"]
            if not b["subtitle"] and got.get("subtitle"):
                sets["subtitle"] = got["subtitle"]
        filled += list(sets)
        sets["enriched_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        con.execute(f"UPDATE books SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), b["id"]])
        if not b["cover"] and got and got.get("cover_url"):
            try:
                core.save_cover_from_url(b["id"], got["cover_url"])
                filled.append("cover")
            except Exception:  # noqa: BLE001 — many books simply have no cover image
                pass
        core.reindex_book(con, b["id"])
        con.commit()
        results.append({"id": b["id"], "title": b["title"], "filled": filled})
    return jsonify(results=results, remaining=len(enrich_candidates(con)))
