"""What to cook, stats and shelf tracking, registered onto the core app.

cook_rank() is the one ranking behind the Cook page, /admin/api/suggest, the MCP suggest_recipes tool and Dinner
Log's "Pick for tonight" (/admin/api/pick); the rest are thin wrappers. Also the collection stats and marking a
book as out on loan.
"""
import random
from datetime import date, timedelta

from flask import abort, jsonify, request

import app as core
from app import app, db, editor_required, fold, make_err, clean
import dinnerlog


# ======================================================== shelf tracking
@app.post("/admin/api/books/<int:book_id>/shelf")
@editor_required
def shelf(book_id):
    out = bool(request.get_json(force=True).get("out"))
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    import changes
    rec = changes.Recorder(con)
    rec.book(book_id)
    if out:
        con.execute("UPDATE books SET status='out', out_since=date('now','localtime'), updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?", (book_id,))
    else:
        con.execute("UPDATE books SET status='owned', out_since=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (book_id,))
    rec.finish()
    con.commit()
    return core.get_book(book_id, private=True)


# ======================================================== what to cook: one ranking
# cook_rank() is the only query and ordering for "what should we cook?". Four callers shape its rows:
# the Cook page (/admin/api/cook), /admin/api/suggest and the MCP suggest_recipes (suggest()), and Dinner Log's
# "Pick for tonight" (/admin/api/pick, pick()).
SUGGEST_MODES = {
    "want": "Starred but never cooked",
    "untried": "Never cooked",
    "neglected": "From a book we haven't cooked from in a year",
    "favorites": "Old favorites (rated 8+, not in 3 months)",
}
COOK_MODES = {
    "any": "Anything",
    "new": "Never made",
    "stale": "Made before, but not lately",
    "rested": "Not made lately (never made counts)",
    "want": "Starred, not yet made",
    "neglected": SUGGEST_MODES["neglected"],
    "fav": "Old favorites (rated 8+, not made lately)",
}
COOK_REST_DAYS = 90   # "lately" on the Cook page and for old favorites; Dinner Log's picker rests a dish for 21
COOK_MAX_LIMIT = 50
PICK_MODES = ("any", "fav", "new")
PICK_FAV_MIN = 8      # the line Dinner Log draws for a favorite (its ratings are 1–10)
PICK_MAX_TERMS = 5    # ingredients per list
# Each recipe's cooking history: Dinner Log dinners matched to it, plus what an editor typed on the recipe.
PICK_HIST = ("LEFT JOIN (SELECT recipe_id, max(rating) AS best, max(day) AS last, count(*) AS times, "
             "round(avg(rating), 1) AS avg FROM cooked WHERE recipe_id IS NOT NULL GROUP BY recipe_id) h "
             "ON h.recipe_id=r.id")
PICK_BEST = "max(coalesce(h.best, 0), coalesce(r.rating, 0))"
PICK_LAST = "nullif(max(coalesce(h.last, ''), coalesce(r.last_made, '')), '')"
NEVER_MADE = "h.recipe_id IS NULL AND r.last_made IS NULL"


def pick_terms(raw):
    """'chicken, leeks' -> ['chicken', 'leeks']: trimmed, de-duplicated by fold(), at most PICK_MAX_TERMS."""
    out, seen = [], set()
    for s in str(raw or "").split(","):
        s = (clean(s) or "")[:40]
        if s and fold(s) not in seen:
            seen.add(fold(s))
            out.append(s)
    return out[:PICK_MAX_TERMS]


def cook_rank(mode="any", include=(), exclude=(), cuisine=None, tag=None, rest_days=COOK_REST_DAYS, seed=None,
              today=None, offset=0, n=20, star_boost=1):
    """Recipes worth cooking, in the order to show them: {mode, pool, offset, next_offset, title_only, recipes}.

    mode  any: every recipe  new: never made  want: starred and never made
          stale: made before, but not within rest_days  rested: not made within rest_days (never made counts)
          fav: rated >= PICK_FAV_MIN (Dinner Log or the recipe's own), not made within rest_days
          neglected: from a book we haven't cooked from in a year
    "Made" is a Dinner Log dinner matched to the recipe, or the date an editor typed on it.
    include  every one must be used: tagged with it or in the title, exactly like the ingredient chips.
    exclude  drop recipes that use any of them. Tags cover only some recipes and few ingredients, so this is best
             effort; `title_only` lists the ones with no tag at all.
    Order: a weighted random draw, best candidates first, reproducible for a seed (so paging through it is stable).
    Weight: the ingredient match score (title beats tag, see ingredients.ranked), x star_boost for starred recipes
    outside fav, and in fav 1 + how far the rating clears the bar. A book's first recipe comes before any book's
    second, so a heavily indexed book doesn't dominate."""
    if mode not in COOK_MODES:
        raise ValueError("mode must be one of " + ", ".join(COOK_MODES))
    import ingredients
    dinnerlog.ensure_synced()
    con = db()
    today = today or date.today()
    rest = (today - timedelta(days=max(0, rest_days))).isoformat()
    year_ago = (today - timedelta(days=365)).isoformat()
    where, args = ["b.status NOT IN ('gone','wanted')"], []
    if cuisine:
        cond, a = core.cuisine_condition(cuisine)
        where.append(cond)
        args += a
    if tag:
        where.append("b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)")
        args.append(tag)
    for name in include:
        cond, a = ingredients.ingredient_condition(con, name)
        where.append(cond)
        args += a
    for name in exclude:
        cond, a = ingredients.ingredient_condition(con, name)
        where.append(f"NOT {cond}")
        args += a
    if mode == "new":
        where.append(NEVER_MADE)
    elif mode == "want":
        where += ["r.want_to_try=1", NEVER_MADE]
    elif mode == "stale":
        where += [f"{PICK_LAST} IS NOT NULL", f"{PICK_LAST} < ?"]
        args.append(rest)
    elif mode == "rested":
        where.append(f"coalesce({PICK_LAST}, '') < ?")
        args.append(rest)
    elif mode == "fav":
        where += [f"{PICK_BEST} >= ?", f"coalesce({PICK_LAST}, '') < ?"]
        args += [PICK_FAV_MIN, rest]
    elif mode == "neglected":
        where += ["b.id NOT IN (SELECT book_id FROM cooked WHERE book_id IS NOT NULL AND day >= ?)",
                  "b.id NOT IN (SELECT book_id FROM recipes WHERE last_made >= ?)"]
        args += [year_ago, year_ago]
    base = f" FROM recipes r JOIN books b ON b.id=r.book_id {PICK_HIST} WHERE " + " AND ".join(where)

    terms = ingredients.search_terms("", " ".join(include))
    scored = {x["id"]: x for x in ingredients.ranked(con, base, args, terms)} if terms else {}
    rows = [dict(r) for r in con.execute(
        "SELECT r.id, r.uid, r.name, r.heading, r.page, r.url, r.want_to_try, r.rating, r.notes, r.last_made, "
        "b.id AS book_id, b.uid AS book_uid, b.title AS book_title, b.kind, b.cuisine, b.status, "
        f"{PICK_BEST} AS best, {PICK_LAST} AS last_cooked, coalesce(h.times, 0) AS times_cooked, "
        "h.avg AS cooked_avg, h.last AS dl_last" + base, args)]

    rng = random.Random(seed)
    keyed = []
    for r in rows:
        w = max(scored[r["id"]]["score"], 0.25) if r["id"] in scored else 1.0
        if mode == "fav":
            w *= 1 + (r["best"] - PICK_FAV_MIN)
        elif r["want_to_try"]:
            w *= star_boost
        r["match"] = scored[r["id"]]["match"] if r["id"] in scored else None
        keyed.append((rng.random() ** (1.0 / w), r))  # Efraimidis–Spirakis: weighted order in one sort
    keyed.sort(key=lambda kr: -kr[0])
    first, later, books = [], [], set()
    for _, r in keyed:  # each book's best-drawn recipe first, then everything else in draw order
        if r["book_id"] in books:
            later.append(r)
        else:
            books.add(r["book_id"])
            first.append(r)
    ordered = first + later
    offset, n = max(0, int(offset)), max(1, int(n))
    page = ordered[offset:offset + n]
    title_only = [s for s in [*include, *exclude] if not ingredients.resolve(con, s)]
    return {"mode": mode, "pool": len(rows), "offset": offset, "title_only": title_only, "recipes": page,
            "next_offset": offset + len(page) if offset + len(page) < len(ordered) else None}


SUGGEST_TO_COOK = {"want": "want", "untried": "new", "neglected": "neglected", "favorites": "fav"}


def suggest(mode="want", cuisine=None, tag=None, n=5, seed=None):
    """Random dinner ideas (MCP suggest_recipes, /admin/api/suggest)."""
    if mode not in SUGGEST_MODES:
        raise ValueError("unknown mode")
    res = cook_rank(SUGGEST_TO_COOK[mode], cuisine=cuisine, tag=tag, seed=seed, n=n)
    keys = ("id", "name", "heading", "page", "url", "want_to_try", "rating", "notes", "last_made",
            "book_id", "book_title", "cuisine", "status")
    recipes = []
    for r in res["recipes"]:
        d = {k: r[k] for k in keys}
        if r["times_cooked"]:
            d.update(cooked_count=r["times_cooked"], cooked_avg=r["cooked_avg"], cooked_last=r["dl_last"])
        recipes.append(d)
    return {"mode": mode, "label": SUGGEST_MODES[mode], "pool": res["pool"], "recipes": recipes}




@app.get("/admin/api/suggest")
@editor_required
def suggest_route():
    try:
        return jsonify(suggest(request.args.get("mode", "want"), request.args.get("cuisine") or None,
                               request.args.get("tag") or None, min(int(request.args.get("n", 5)), 20)))
    except ValueError as e:
        return make_err(400, str(e))


def thumbs_for(ids):
    """{recipe id: URL of the thumbnail of its newest photo} for the recipes that have one."""
    if not ids:
        return {}
    from photos import thumb_name
    out = {}
    for rid, file in db().execute(
            f"SELECT recipe_id, file FROM recipe_photos WHERE recipe_id IN ({','.join('?' * len(ids))}) "
            "ORDER BY coalesce(taken_on, substr(created_at, 1, 10)) DESC, id DESC", list(ids)):
        out.setdefault(rid, f"/photos/{thumb_name(file)}")
    return out


@app.get("/admin/api/cook")
@editor_required
def cook_route():
    """The Cook page. GET ?mode=any|new|stale|want|rested|neglected|fav &with=chicken&with=leeks &cuisine=French
    &limit=20 (at most 50) &offset=0 &seed=7 &rest=90. A seed keeps the order the same from one page to the next;
    next_offset is null on the last page."""
    p = request.args
    try:
        limit, offset, rest = int(p.get("limit", 20)), int(p.get("offset", 0)), int(p.get("rest", COOK_REST_DAYS))
    except ValueError:
        return make_err(400, "limit, offset and rest must be whole numbers")
    limit, offset, rest = max(1, min(limit, COOK_MAX_LIMIT)), max(0, offset), max(0, min(rest, 3650))
    include = pick_terms(",".join(p.getlist("with")))
    try:
        res = cook_rank(p.get("mode") or "any", include, cuisine=p.get("cuisine") or None, rest_days=rest,
                        seed=(p.get("seed") or "")[:40] or None, offset=offset, n=limit)
    except ValueError as e:
        return make_err(400, str(e))
    thumbs = thumbs_for([r["id"] for r in res["recipes"]])
    recipes = [{"id": r["id"], "uid": r["uid"], "name": r["name"], "heading": r["heading"], "page": r["page"],
                "url": r["url"], "book_id": r["book_id"], "book_uid": r["book_uid"], "book_title": r["book_title"],
                "book_status": r["status"],
                "online": r["kind"] == "online", "house": r["kind"] == "house", "starred": bool(r["want_to_try"]),
                "times_made": r["times_cooked"], "last_made": r["last_cooked"], "best_rating": r["best"] or None,
                "match": r["match"], "photo": thumbs.get(r["id"])} for r in res["recipes"]]
    return jsonify(mode=res["mode"], label=COOK_MODES[res["mode"]], include=include, title_only=res["title_only"],
                   pool=res["pool"], limit=limit, offset=res["offset"], next_offset=res["next_offset"],
                   recipes=recipes)


# ======================================================== Dinner Log's "Pick for tonight"
def pick(mode="any", include=(), exclude=(), n=10, rest_days=21, seed=None, today=None):
    """Recipes for Dinner Log's "Pick for tonight": a weighted random draw, best candidates first.

    mode  fav: best rating (from Dinner Log or the recipe itself) >= PICK_FAV_MIN, not cooked in rest_days
          new: never cooked
          any: not cooked in rest_days (never cooked counts)
    include / exclude and the weights are cook_rank's; starred recipes outside fav mode count x3 here."""
    if mode not in PICK_MODES:
        raise ValueError("mode must be one of " + ", ".join(PICK_MODES))
    res = cook_rank({"any": "rested", "fav": "fav", "new": "new"}[mode], include, exclude, rest_days=rest_days,
                    seed=seed, today=today, n=n, star_boost=3)
    out = [{"id": r["id"], "uid": r["uid"], "name": r["name"], "page": r["page"], "url": r["url"],
            "book_id": r["book_id"], "book_uid": r["book_uid"], "book_title": r["book_title"],
            "online": r["kind"] == "online",
            "starred": bool(r["want_to_try"]), "best_rating": r["best"] or None,
            "last_cooked": r["last_cooked"], "times_cooked": r["times_cooked"], "match": r["match"]}
           for r in res["recipes"]]
    # Dinner Log names of every dinner matched to a recipe here, so Dinner Log can leave those dishes to this
    # list (which knows their ingredient tags) instead of offering them twice.
    linked = [r[0] for r in db().execute("SELECT DISTINCT meal_name FROM cooked WHERE recipe_id IS NOT NULL "
                                         "AND meal_name IS NOT NULL ORDER BY meal_name")]
    return {"mode": mode, "include": list(include), "exclude": list(exclude), "title_only": res["title_only"],
            "pool": res["pool"], "recipes": out, "linked_dinners": linked}


@app.get("/admin/api/pick")
@core.reader_required
def pick_route():
    """GET ?mode=any|fav|new&with=chicken,leeks&without=pork&n=10&rest=21. Read-only; Dinner Log calls it
    over localhost with READ_TOKEN."""
    p = request.args
    try:
        n = max(1, min(int(p.get("n", 10)), 50))
        rest = max(0, min(int(p.get("rest", 21)), 3650))
        return jsonify(pick(p.get("mode") or "any", pick_terms(",".join(p.getlist("with"))),
                            pick_terms(",".join(p.getlist("without"))), n, rest, p.get("seed")))
    except ValueError as e:
        return make_err(400, str(e))


# ======================================================== stats
OWNED = "b.status NOT IN ('gone','wanted') AND b.kind IS NULL"  # cookbooks on the shelves (or out/loaned)


def by_name(con, column):
    """Owned books per value of a free-text books column, grouping spellings that differ only by case or
    accents ("Ten Speed Press" / "ten speed press") under the most common one."""
    groups = {}
    for name, n in con.execute(f"SELECT {column}, count(*) FROM books b WHERE {OWNED} "
                               f"AND trim(coalesce({column}, '')) <> '' GROUP BY {column}"):
        g = groups.setdefault(fold(name), {"name": name, "books": 0, "top": 0})
        g["books"] += n
        if n > g["top"]:
            g["name"], g["top"] = name, n
    return sorted(({"name": g["name"], "books": g["books"]} for g in groups.values()),
                  key=lambda g: (-g["books"], fold(g["name"])))


def collection_stats(con):
    """How the collection breaks down: by author, publisher, tag, decade, format and book rating."""
    one = lambda sql: con.execute(sql).fetchone()[0]
    ranked = lambda sql: sorted((dict(r) for r in con.execute(sql)), key=lambda r: (-r["books"], fold(r["name"])))
    return {
        "by_author": ranked(f"SELECT a.name, count(*) AS books FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
                            f"JOIN books b ON b.id=ba.book_id WHERE {OWNED} GROUP BY a.id"),
        "by_publisher": by_name(con, "b.publisher"),
        "by_tag": ranked(f"SELECT t.name, count(*) AS books FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
                         f"JOIN books b ON b.id=bt.book_id WHERE {OWNED} GROUP BY t.id"),
        "by_decade": [{"name": f"{r[0]}s", "books": r[1]} for r in con.execute(
            f"SELECT (b.year / 10) * 10 AS d, count(*) FROM books b WHERE {OWNED} AND b.year > 0 GROUP BY d ORDER BY d")],
        "by_format": by_name(con, "b.format"),
        "ratings": [dict(r) for r in con.execute(
            f"SELECT b.rating AS stars, count(*) AS books FROM books b WHERE {OWNED} AND b.rating IS NOT NULL "
            "GROUP BY b.rating ORDER BY b.rating DESC")],
        "rating_avg": one(f"SELECT round(avg(b.rating), 1) FROM books b WHERE {OWNED} AND b.rating IS NOT NULL"),
        "missing": {k: one(f"SELECT count(*) FROM books b WHERE {OWNED} AND {cond}") for k, cond in (
            ("author", "b.id NOT IN (SELECT book_id FROM book_authors)"),
            ("publisher", "trim(coalesce(b.publisher, '')) = ''"),
            ("year", "coalesce(b.year, 0) <= 0"),
            ("format", "trim(coalesce(b.format, '')) = ''"))},
    }


def stats():
    dinnerlog.ensure_synced()
    con = db()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    months = []
    # Cookbook dinners only, like the other dinner counts: saved websites (kind 'online') and no-book meals are left out.
    for r in con.execute("SELECT substr(c.day,1,7) AS m, count(*) AS n FROM cooked c JOIN books b ON b.id=c.book_id "
                         "WHERE b.kind IS NULL AND c.day >= ? GROUP BY m ORDER BY m",
                         ((date.today().replace(day=1) - timedelta(days=335)).isoformat(),)):
        months.append(dict(r))
    return {
        "books": one(f"SELECT count(*) FROM books b WHERE {OWNED}"),
        "online": [dict(r) for r in con.execute(  # recipe websites: not counted as cookbooks
            "SELECT b.id, b.title, count(r.id) AS recipes FROM books b LEFT JOIN recipes r ON r.book_id=b.id "
            "WHERE b.kind='online' GROUP BY b.id ORDER BY recipes DESC")],
        "books_indexed": one(f"SELECT count(*) FROM books b WHERE {OWNED} "
                             "AND EXISTS (SELECT 1 FROM recipes r WHERE r.book_id=b.id)"),
        "recipes": one(f"SELECT count(*) FROM recipes r JOIN books b ON b.id=r.book_id WHERE {OWNED}"),  # as in books_indexed
        "want_to_try": one("SELECT count(*) FROM recipes WHERE want_to_try=1"),
        "off_shelf": [dict(r) for r in con.execute(
            "SELECT id, title, out_since FROM books WHERE status='out' ORDER BY out_since")],
        "dinners_from_books": one("SELECT count(*) FROM cooked c JOIN books b ON b.id=c.book_id WHERE b.kind IS NULL"),
        "dinners_last_year": one(
            "SELECT count(*) FROM cooked c JOIN books b ON b.id=c.book_id WHERE b.kind IS NULL AND c.day >= ?",
            year_ago),
        "books_cooked_from": one(
            "SELECT count(DISTINCT c.book_id) FROM cooked c JOIN books b ON b.id=c.book_id WHERE b.kind IS NULL"),
        "top_books": [dict(r) for r in con.execute(
            "SELECT b.id, b.title, count(*) AS n, max(c.day) AS last FROM cooked c JOIN books b ON b.id=c.book_id "
            "WHERE b.kind IS NULL GROUP BY b.id ORDER BY n DESC, last DESC LIMIT 10")],
        "top_recipes": [dict(r) for r in con.execute(
            "SELECT r.id, r.name, r.heading, b.id AS book_id, b.title AS book_title, count(c.meal_id) AS n, "
            "round(coalesce(avg(c.rating), r.rating),1) AS avg FROM recipes r JOIN books b ON b.id=r.book_id "
            "LEFT JOIN cooked c ON c.recipe_id=r.id WHERE c.rating IS NOT NULL OR r.rating IS NOT NULL "
            "GROUP BY r.id HAVING avg >= 7 ORDER BY avg DESC, n DESC LIMIT 10")],
        "cuisines": [dict(r) for r in con.execute(
            "SELECT coalesce(b.cuisine,'(none)') AS name, count(DISTINCT b.id) AS books, count(c.meal_id) AS cooked "
            f"FROM books b LEFT JOIN cooked c ON c.book_id=b.id WHERE {OWNED} "
            "GROUP BY 1 ORDER BY books DESC")],
        "never_cooked": [dict(r) for r in con.execute(
            f"SELECT b.id, b.title FROM books b WHERE {OWNED} "
            "AND b.id NOT IN (SELECT book_id FROM cooked WHERE book_id IS NOT NULL) "
            "AND b.id NOT IN (SELECT book_id FROM recipes WHERE last_made IS NOT NULL OR rating IS NOT NULL) "
            "ORDER BY b.title")],
        "months": months,
        "dinnerlog": dinnerlog.meta_get(con, "dl_synced"),
        **collection_stats(con),
    }


@app.get("/admin/api/stats")
@editor_required
def stats_route():
    return jsonify(stats())


def migrate(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    if "out_since" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN out_since TEXT")
    if "rating" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN rating REAL")  # 0–5 in half stars, editors only
