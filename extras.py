"""Cookbook Collection v2 features, registered onto the core app:

- Dinner Log link: reads Dinner Log's SQLite DB read-only and matches cookbook
  dinners to books and recipes (auto by title + page + name, with manual overrides)
- Photo index import via the Claude API
- "What should we cook?" suggestions, stats, off-the-shelf tracking
- MCP endpoint (Streamable HTTP, stateless JSON) at /mcp
"""
import base64, difflib, hmac, io, json, os, random, re, secrets, sqlite3, threading, time, urllib.error, urllib.request
from datetime import date, timedelta

from flask import abort, g, jsonify, request, Response

import app as core
from app import app, db, editor_required, fold, make_err, page_sort, clean

DINNERLOG_DB = os.environ.get("DINNERLOG_DB", "/var/lib/dinner-log/meals.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "low").strip()  # empty: the model's default
MCP_TOKEN = os.environ.get("MCP_TOKEN", "").strip()
MCP_PUBLIC = os.environ.get("MCP_PUBLIC", "1") == "1"

MIGRATIONS = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cooked (
  meal_id INTEGER PRIMARY KEY,           -- Dinner Log meals.id
  day TEXT, meal_name TEXT, dl_title TEXT, dl_detail TEXT, rating REAL,
  book_id INTEGER REFERENCES books(id) ON DELETE SET NULL,
  recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
  how TEXT                               -- auto | manual | book (book only) | none
);
CREATE INDEX IF NOT EXISTS cooked_recipe ON cooked(recipe_id);
CREATE INDEX IF NOT EXISTS cooked_book ON cooked(book_id);
CREATE TABLE IF NOT EXISTS dl_alias (title_key TEXT PRIMARY KEY, title TEXT, book_id INTEGER REFERENCES books(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS dl_link (meal_id INTEGER PRIMARY KEY, recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
  meal_key TEXT);  -- day|name, so links don't misfire if Dinner Log's DB is ever recreated
"""


def migrate(con):
    con.executescript(MIGRATIONS)
    con.execute(WISH_SCHEMA)
    con.execute(PHOTO_SCHEMA)
    con.execute("UPDATE photo_jobs SET status='error', "
                "error='Reading that photo stopped partway (the server restarted). Try it again.' "
                "WHERE status='running'")
    if "meal_key" not in [r[1] for r in con.execute("PRAGMA table_info(dl_link)")]:
        con.execute("ALTER TABLE dl_link ADD COLUMN meal_key TEXT")
    cols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    if "out_since" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN out_since TEXT")
    if "rating" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN rating REAL")  # 0–5 in half stars, editors only


# ======================================================== Dinner Log link
_sync_lock = threading.Lock()


def norm_title(s):
    s = fold(s)
    s = re.sub(r"^(the|a|an)\s+", "", s)
    s = re.sub(r"\b(cookbook|cooking|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", fold(s)) if t not in {"the", "a", "an", "of", "and", "cookbook"}}


def dl_signature(con=None):
    try:
        st = [os.stat(DINNERLOG_DB)]
    except OSError:
        return None
    try:
        st.append(os.stat(DINNERLOG_DB + "-wal"))
    except OSError:
        pass
    sig = DINNERLOG_DB + "|" + "|".join(f"{s.st_ino}:{s.st_mtime_ns}:{s.st_size}" for s in st)
    if con is not None:
        b = con.execute("SELECT count(*), coalesce(max(updated_at), '') FROM books").fetchone()
        r = con.execute("SELECT count(*), coalesce(max(id), 0) FROM recipes").fetchone()
        sig += f"|b:{b[0]}:{b[1]}|r:{r[0]}:{r[1]}"
    return sig


def meta_get(con, k):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


def match_book(t, books, aliases):
    k = norm_title(t)
    if not k:
        return None
    if k in aliases:
        return aliases[k]
    for bid, bk, _ in books:
        if bk == k:
            return bid
    # best word overlap first: "Simple (Ottolenghi)" -> "Ottolenghi Simple", not Diana Henry's "Simple"
    tt = tokens(t)
    best, score = None, 0.0
    for bid, _, btoks in books:
        if not tt or not btoks:
            continue
        j = len(tt & btoks) / len(tt | btoks)
        if j > score:
            best, score = bid, j
    if score >= 0.6:
        return best
    subs = [(bid, bk) for bid, bk, _ in books if len(k) >= 5 and len(bk) >= 5 and (k in bk or bk in k)]
    if subs:  # closest length wins among substring matches
        return min(subs, key=lambda x: abs(len(x[1]) - len(k)))[0]
    for bid, bk, _ in books:  # short names like "Zuni" -> "The Zuni Cafe Cookbook"
        if len(k) >= 4 and bk.startswith(k):
            return bid
    return None


def name_sim(a, b):
    return difflib.SequenceMatcher(None, fold(a), fold(b)).ratio()


def match_recipe(recipes, detail, meal_name):
    """recipes: list of (id, name, heading, page_sort, page). Returns recipe id or None."""
    m = re.search(r"\d+", detail or "")
    page = int(m.group()) if m else None

    def full(r):
        return f"{r[2]} {r[1]}" if r[2] else r[1]

    if page is not None:
        on_page = []
        for r in recipes:
            nums = [int(x) for x in re.findall(r"\d+", r[4] or "")]
            if r[3] == page or (len(nums) >= 2 and nums[0] <= page <= nums[1]) or page in nums:
                on_page.append(r)
        if len(on_page) == 1:
            return on_page[0][0]
        if on_page:
            best = max(on_page, key=lambda r: max(name_sim(meal_name, r[1]), name_sim(meal_name, full(r))))
            if max(name_sim(meal_name, best[1]), name_sim(meal_name, full(best))) >= 0.35:
                return best[0]
    if meal_name and recipes:
        best = max(recipes, key=lambda r: max(name_sim(meal_name, r[1]), name_sim(meal_name, full(r))))
        if max(name_sim(meal_name, best[1]), name_sim(meal_name, full(best))) >= 0.8:
            return best[0]
    return None


def meal_key(day, name):
    return f"{day}|{fold(name)}"


def sync_dinnerlog(con, force=False):
    """Rebuild the `cooked` table from Dinner Log when its DB file or local catalog changed."""
    sig = dl_signature(con)
    if sig is None:
        return {"available": False}
    with _sync_lock:
        if not force and meta_get(con, "dl_sig") == sig:
            return {"available": True, "changed": False}
        try:
            src = sqlite3.connect(f"file:{DINNERLOG_DB}?mode=ro", uri=True, timeout=5)
            src.row_factory = sqlite3.Row
            cols = {r[1] for r in src.execute("PRAGMA table_info(meals)")}
            rating = "rating" if "rating" in cols else "NULL AS rating"
            meals = src.execute(f"SELECT id, day, name, source_type, source_title, source_detail, {rating} FROM meals "
                                f"WHERE source_type='cookbook' OR source_detail LIKE '%://%' "
                                f"OR source_title LIKE '%://%'").fetchall()
            src.close()
        except sqlite3.Error as e:
            app.logger.warning("Dinner Log read failed: %s", e)
            return {"available": False, "error": str(e)}
        books = [(r[0], norm_title(r[1]), tokens(r[1]))
                 for r in con.execute("SELECT id, title FROM books WHERE kind IS NULL")]
        aliases = {r[0]: r[1] for r in con.execute("SELECT title_key, book_id FROM dl_alias")}
        links = {r[0]: (r[1], r[2]) for r in con.execute("SELECT meal_id, recipe_id, meal_key FROM dl_link")}
        rec_cache = {}
        to_insert = []
        import online
        for m in meals:
            web_bid, web_rid = online.match_meal(con, m["source_detail"], m["source_title"])
            if m["source_type"] != "cookbook" and not web_rid:
                continue  # a website we haven't saved, or no source at all
            bid = match_book(m["source_title"], books, aliases)
            rid, how = None, "none"
            link = links.get(m["id"])
            if link and link[1] not in (None, meal_key(m["day"], m["name"])):
                link = None  # a different meal now has this id
            if link:
                rid = link[0]
                how = "manual"
                if rid:
                    row = con.execute("SELECT book_id FROM recipes WHERE id=?", (rid,)).fetchone()
                    bid = row[0] if row else bid
            elif web_rid:  # a link to a recipe saved from a website
                bid, rid, how = web_bid, web_rid, "auto"
            elif bid:
                if bid not in rec_cache:
                    rec_cache[bid] = con.execute(
                        "SELECT id, name, heading, page_sort, page FROM recipes WHERE book_id=?", (bid,)).fetchall()
                rid = match_recipe(rec_cache[bid], m["source_detail"], m["name"])
                how = "auto" if rid else "book"
            to_insert.append((m["id"], m["day"], m["name"], m["source_title"], m["source_detail"], m["rating"],
                              bid, rid, how))
        con.execute("DELETE FROM cooked")
        con.executemany("INSERT INTO cooked VALUES (?,?,?,?,?,?,?,?,?)", to_insert)
        meta_set(con, "dl_sig", sig)
        meta_set(con, "dl_synced", time.strftime("%Y-%m-%d %H:%M:%S"))
        con.commit()
        return {"available": True, "changed": True, "meals": len(meals)}


def ensure_synced():
    if not getattr(g, "_dl_synced", False):
        sync_dinnerlog(db())
        g._dl_synced = True


def cooked_by_recipe(book_id):
    return {r["recipe_id"]: dict(r) for r in db().execute(
        "SELECT recipe_id, count(*) AS cooked_count, round(avg(rating),1) AS cooked_avg, max(day) AS cooked_last "
        "FROM cooked WHERE book_id=? AND recipe_id IS NOT NULL GROUP BY recipe_id", (book_id,))}


def cooked_by_book(ids):
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    return {r["book_id"]: dict(r) for r in db().execute(
        f"SELECT book_id, count(*) AS cooked_count, max(day) AS cooked_last FROM cooked "
        f"WHERE book_id IN ({qs}) GROUP BY book_id", list(ids))}


@app.get("/admin/api/dinnerlog")
@editor_required
def dinnerlog_status():
    con = db()
    st = sync_dinnerlog(con, force=request.args.get("force") == "1")
    if not st.get("available"):
        return jsonify(available=False, path=DINNERLOG_DB, error=st.get("error"))
    q = lambda sql: con.execute(sql).fetchone()[0]
    unmatched = [dict(r) for r in con.execute(
        "SELECT dl_title AS title, count(*) AS n, max(day) AS last FROM cooked WHERE book_id IS NULL "
        "AND trim(coalesce(dl_title,'')) <> '' GROUP BY fold(dl_title) ORDER BY n DESC")]
    return jsonify(available=True, path=DINNERLOG_DB, synced=meta_get(con, "dl_synced"),
                   meals=q("SELECT count(*) FROM cooked"),
                   recipe_matched=q("SELECT count(*) FROM cooked WHERE recipe_id IS NOT NULL"),
                   book_only=q("SELECT count(*) FROM cooked WHERE book_id IS NOT NULL AND recipe_id IS NULL"),
                   unmatched_titles=unmatched)


@app.post("/admin/api/dinnerlog/alias")
@editor_required
def dinnerlog_alias():
    d = request.get_json(force=True)
    title, bid = (d.get("title") or "").strip(), d.get("book_id")
    if not title:
        return make_err(400, "title required")
    con = db()
    if bid:
        con.execute("INSERT INTO dl_alias VALUES (?,?,?) ON CONFLICT(title_key) DO UPDATE SET book_id=excluded.book_id",
                    (norm_title(title), title, int(bid)))
    else:
        con.execute("DELETE FROM dl_alias WHERE title_key=?", (norm_title(title),))
    sync_dinnerlog(con, force=True)
    return jsonify(ok=True)


@app.get("/admin/api/books/<int:book_id>/dinners")
@editor_required
def book_dinners(book_id):
    ensure_synced()
    rows = db().execute(
        "SELECT c.*, r.name AS recipe_name, r.page AS recipe_page FROM cooked c LEFT JOIN recipes r ON r.id=c.recipe_id "
        "WHERE c.book_id=? ORDER BY c.day DESC", (book_id,)).fetchall()
    return jsonify(dinners=[dict(r) for r in rows])


@app.post("/admin/api/dinnerlog/link")
@editor_required
def dinnerlog_link():
    d = request.get_json(force=True)
    meal_id = int(d["meal_id"])
    con = db()
    rid = d.get("recipe_id")
    if d.get("create"):
        c = d["create"]
        bid = int(c["book_id"])
        name, page = clean(c.get("name")), clean(c.get("page"))
        if not name:
            return make_err(400, "Recipe name is required")
        rid = con.execute("INSERT INTO recipes(book_id, name, page, page_sort, source) VALUES (?,?,?,?, 'dinnerlog')",
                          (bid, name, page, page_sort(page))).lastrowid
    if rid is None and not d.get("unlink"):
        con.execute("DELETE FROM dl_link WHERE meal_id=?", (meal_id,))  # back to automatic
    else:
        row = con.execute("SELECT day, meal_name FROM cooked WHERE meal_id=?", (meal_id,)).fetchone()
        key = meal_key(row[0], row[1]) if row else None
        con.execute("INSERT INTO dl_link VALUES (?,?,?) ON CONFLICT(meal_id) DO UPDATE SET recipe_id=excluded.recipe_id, "
                    "meal_key=excluded.meal_key", (meal_id, int(rid) if rid else None, key))
    sync_dinnerlog(con, force=True)
    return jsonify(ok=True, recipe_id=rid)


# ======================================================== shelf tracking
@app.post("/admin/api/books/<int:book_id>/shelf")
@editor_required
def shelf(book_id):
    out = bool(request.get_json(force=True).get("out"))
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    if out:
        con.execute("UPDATE books SET status='out', out_since=date('now','localtime'), updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?", (book_id,))
    else:
        con.execute("UPDATE books SET status='owned', out_since=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (book_id,))
    con.commit()
    return core.get_book(book_id, private=True)


# ======================================================== suggestions
SUGGEST_MODES = {
    "want": "Starred but never cooked",
    "untried": "Never cooked",
    "neglected": "From a book we haven't cooked from in a year",
    "favorites": "Old favorites (rated 8+, not in 3 months)",
}


def suggest(mode="want", cuisine=None, tag=None, n=5, seed=None):
    ensure_synced()
    con = db()
    where, args = [], []
    if cuisine:
        cond, a = core.cuisine_condition(cuisine)
        where.append(cond)
        args += a
    if tag:
        where.append("b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)")
        args.append(tag)
    never = ("r.id NOT IN (SELECT recipe_id FROM cooked WHERE recipe_id IS NOT NULL) "
             "AND r.last_made IS NULL")
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    months3 = (date.today() - timedelta(days=90)).isoformat()
    if mode == "want":
        where += ["r.want_to_try=1", never]
    elif mode == "untried":
        where.append(never)
    elif mode == "neglected":
        where.append("b.id NOT IN (SELECT book_id FROM cooked WHERE book_id IS NOT NULL AND day >= ?)")
        args.append(year_ago)
        where.append("b.id NOT IN (SELECT book_id FROM recipes WHERE last_made >= ?)")
        args.append(year_ago)
    elif mode == "favorites":
        where.append("""r.id IN (
            SELECT r2.id FROM recipes r2 LEFT JOIN cooked c ON c.recipe_id=r2.id
            GROUP BY r2.id
            HAVING max(coalesce(max(c.rating), 0), coalesce(r2.rating, 0)) >= 8
               AND max(coalesce(max(c.day), ''), coalesce(r2.last_made, '')) < ?)""")
        args.append(months3)
    else:
        raise ValueError("unknown mode")
    sql = ("SELECT r.id, r.name, r.heading, r.page, r.url, r.want_to_try, r.rating, r.notes, r.last_made, "
           "b.id AS book_id, b.title AS book_title, b.cuisine, b.status FROM recipes r JOIN books b ON b.id=r.book_id "
           "WHERE b.status NOT IN ('gone','wanted') " + "".join(" AND " + w for w in where))
    rows = [dict(r) for r in con.execute(sql, args)]
    rng = random.Random(seed)
    # one per book first so a heavily indexed book doesn't dominate, then fill
    rng.shuffle(rows)
    picked, seen = [], set()
    for r in rows:
        if r["book_id"] not in seen:
            picked.append(r)
            seen.add(r["book_id"])
        if len(picked) >= n:
            break
    for r in rows:
        if len(picked) >= n:
            break
        if r not in picked:
            picked.append(r)
    stats = cooked_stats_for([r["id"] for r in picked])
    for r in picked:
        r.update(stats.get(r["id"], {}))
    return {"mode": mode, "label": SUGGEST_MODES[mode], "pool": len(rows), "recipes": picked}


def cooked_stats_for(ids):
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    return {r["recipe_id"]: {"cooked_count": r["n"], "cooked_avg": r["avg"], "cooked_last": r["last"]}
            for r in db().execute(f"SELECT recipe_id, count(*) n, round(avg(rating),1) avg, max(day) last FROM cooked "
                                  f"WHERE recipe_id IN ({qs}) GROUP BY recipe_id", ids)}


@app.get("/admin/api/suggest")
@editor_required
def suggest_route():
    try:
        return jsonify(suggest(request.args.get("mode", "want"), request.args.get("cuisine") or None,
                               request.args.get("tag") or None, min(int(request.args.get("n", 5)), 20)))
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
    ensure_synced()
    con = db()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    months = []
    for r in con.execute("SELECT substr(day,1,7) AS m, count(*) AS n FROM cooked WHERE day >= ? GROUP BY m ORDER BY m",
                         ((date.today().replace(day=1) - timedelta(days=335)).isoformat(),)):
        months.append(dict(r))
    return {
        "books": one(f"SELECT count(*) FROM books b WHERE {OWNED}"),
        "online": [dict(r) for r in con.execute(  # recipe websites: not counted as cookbooks
            "SELECT b.id, b.title, count(r.id) AS recipes FROM books b LEFT JOIN recipes r ON r.book_id=b.id "
            "WHERE b.kind='online' GROUP BY b.id ORDER BY recipes DESC")],
        "books_indexed": one(f"SELECT count(*) FROM books b WHERE {OWNED} "
                             "AND EXISTS (SELECT 1 FROM recipes r WHERE r.book_id=b.id)"),
        "recipes": one("SELECT count(*) FROM recipes"),
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
            "round(avg(coalesce(c.rating, r.rating)),1) AS avg FROM recipes r JOIN books b ON b.id=r.book_id "
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
        "dinnerlog": meta_get(con, "dl_synced"),
        **collection_stats(con),
    }


@app.get("/admin/api/stats")
@editor_required
def stats_route():
    return jsonify(stats())


# ======================================================== photo index import
# A dense index page (three columns, 250+ entries) takes Claude a minute or two to transcribe: longer than
# Cloudflare lets a request wait. So a photo becomes a job: the upload returns at once, a thread does the work,
# and the browser checks back. Jobs live in the database because gunicorn runs more than one worker process.
PHOTO_PROMPT = """This is a photo of a page from a cookbook's index. Transcribe every index entry that has a page number. A full page can have 250 or more; don't skip any.

Rules:
- Read in the book's order: the whole first column top to bottom, then the next column.
- A main entry with no page number (e.g. "Chicken") is a heading; the indented entries under it are sub-entries. For each sub-entry, give that main entry as its heading. Headings carry over from the previous page/column when the first lines are indented continuations.
- A main entry that has its own page number has no heading; it may also be the heading for sub-entries below it.
- Some indexes put the first sub-entry on the heading's line after a colon: "abalone: abalone sarsa 52" is heading "abalone", name "abalone sarsa", and the indented lines below it share that heading.
- Join entries that wrap onto a second line, including a page number alone on the next line.
- Keep page numbers exactly as printed, including ranges and lists ("42", "342–44", "51, 88"). Drop "see also" cross-references and letter dividers.
- Keep the wording as printed; don't add or invent anything. If a word is unreadable, give your best reading.

Reply with one entry per line and nothing else, as: heading | name | page
Leave the heading empty when there is none. For example:
Chicken | braised with figs | 350
 | Ceviche, classic | 42"""

PHOTO_SCHEMA = """CREATE TABLE IF NOT EXISTS photo_jobs (
  id TEXT PRIMARY KEY, book_id INTEGER, status TEXT,  -- running | done | error
  rows TEXT, warning TEXT, error TEXT, created REAL)"""
PHOTO_JOB_LIMIT = 600  # seconds; a job still "running" after this died with its worker


def call_claude(image_jpeg, media_type="image/jpeg", effort=None):
    """Returns (text, stop_reason)."""
    effort = ANTHROPIC_EFFORT if effort is None else effort
    body = {
        "model": ANTHROPIC_MODEL, "max_tokens": 16000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image_jpeg).decode()}},
            {"type": "text", "text": PHOTO_PROMPT}]}],
    }
    if effort:  # transcription needs little thinking, and thinking is slow
        body["output_config"] = {"effort": effort}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
        "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PHOTO_JOB_LIMIT - 30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 400 and effort and ("effort" in detail or "output_config" in detail):
            return call_claude(image_jpeg, media_type, effort="")  # a model without effort settings
        raise RuntimeError(f"Claude API error {e.code}: {detail}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, data.get("stop_reason")


def _photo_row(heading, name, page):
    name, page = clean(str(name or "")), clean(str(page or ""))
    if not name or not page or not re.search(r"\d", page):
        return None
    page = re.sub(r"\s*[-—]\s*", "–", re.sub(r"\s*,\s*", ", ", page))
    return {"heading": clean(str(heading or "")) or None, "name": name, "page": page}


def parse_claude_rows(text, truncated=False):
    """Reads "heading | name | page" lines (or the JSON older prompts asked for). When the reply was cut off,
    its last line may be partial ("Ceviche | 34" of "342"), so it's dropped."""
    s = text.strip()
    if s.startswith(("{", "[")) or s.startswith("```json"):
        m = re.search(r"\{.*\}|\[.*\]", s, re.S)
        if m:
            try:
                data = json.loads(m.group())
            except ValueError:
                data = None
            if data is not None:
                entries = data.get("entries", []) if isinstance(data, dict) else data
                return [r for e in entries if isinstance(e, dict)
                        for r in [_photo_row(e.get("heading"), e.get("name"), e.get("page"))] if r]
    raw_lines = s.splitlines()
    if truncated and raw_lines and not text.endswith("\n"):
        raw_lines = raw_lines[:-1]
    lines = [ln for ln in raw_lines if "|" in ln]
    rows = []
    for ln in lines:
        parts = [p.strip() for p in ln.strip().split("|")]
        if len(parts) >= 5 and not parts[0] and not parts[-1]:
            parts = parts[1:-1]  # a markdown table row: | heading | name | page |
        heading, name, page = (None, parts[0], parts[1]) if len(parts) == 2 else (parts[0], "|".join(parts[1:-1]), parts[-1])
        if fold(name) == "name" and fold(page) == "page":
            continue  # a header line
        r = _photo_row(re.sub(r"^[-*•]\s+", "", heading or ""), name, page)
        if r:
            rows.append(r)
    return rows


def prepare_photo(raw):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("L")  # grayscale: text only, smaller
    # Current models read images up to 2576px on the long side; a dense three-column index needs all of it
    # (older models scale it down themselves).
    img.thumbnail((2576, 2576))
    img = ImageOps.autocontrast(img, cutoff=1)
    for q in (88, 75, 60):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=q)
        if out.tell() < 3_500_000:  # the API takes up to 5 MB of base64 per image
            break
    return out.getvalue()


_photo_sem = threading.Semaphore(2)


def run_photo_job(job_id, jpeg):
    res_rows, warning, err_msg = None, None, None
    try:
        with _photo_sem:
            text, stop = call_claude(jpeg)
        cut = stop == "max_tokens"
        rows = parse_claude_rows(text, truncated=cut)
        if not rows:
            if stop == "refusal":
                raise RuntimeError("Claude declined to read that photo")
            raise RuntimeError("no entries with page numbers found" + (f": {clean(text)[:200]}" if text.strip() else ""))
        warning = (f"This page was cut off after {len(rows)} entries. Photograph the rest of the page "
                   "(a column or two at a time) to get the remaining entries.") if cut else None
        res_rows = json.dumps(rows)
    except Exception as e:  # noqa: BLE001
        err_msg = f"Couldn't read the index from that photo: {e}"
    con = core.connect()
    try:
        if err_msg is not None:
            con.execute("UPDATE photo_jobs SET status='error', error=? WHERE id=?", (err_msg, job_id))
        else:
            con.execute("UPDATE photo_jobs SET status='done', rows=?, warning=? WHERE id=?",
                        (res_rows, warning, job_id))
        con.commit()
    finally:
        con.close()


@app.post("/admin/api/books/<int:book_id>/index/photo")
@editor_required
def index_photo(book_id):
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    if not ANTHROPIC_API_KEY:
        return make_err(503, "Photo import needs ANTHROPIC_API_KEY in /etc/conf.d/cookbooks")
    f = request.files.get("file")
    if not f:
        return make_err(400, "No photo")
    try:
        jpeg = prepare_photo(f.read())
    except Exception as e:  # noqa: BLE001
        return make_err(400, f"Couldn't read that image: {e}")
    job_id = secrets.token_urlsafe(12)
    con.execute("DELETE FROM photo_jobs WHERE created < ?", (time.time() - 86400,))
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES (?,?,'running',?)",
                (job_id, book_id, time.time()))
    con.commit()
    threading.Thread(target=run_photo_job, args=(job_id, jpeg), daemon=True).start()
    return jsonify(job=job_id), 202


@app.get("/admin/api/index/photo/<job_id>")
@editor_required
def index_photo_job(job_id):
    j = db().execute("SELECT * FROM photo_jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        abort(404)
    elapsed = int(time.time() - j["created"])
    if j["status"] == "running":
        if elapsed > PHOTO_JOB_LIMIT:
            return jsonify(status="error", error="Reading that photo stopped partway (the server may have restarted). Try it again.")
        return jsonify(status="running", elapsed=elapsed)
    if j["status"] == "error":
        return jsonify(status="error", error=j["error"])
    return jsonify(status="done", warning=j["warning"], rows=core.mark_duplicates(j["book_id"], json.loads(j["rows"])))


@app.get("/admin/api/features")
@editor_required
def features():
    return jsonify(photo_import=bool(ANTHROPIC_API_KEY), dinnerlog=dl_signature() is not None,
                   dinnerlog_url=DINNERLOG_URL or None, wishlist=bool(WISHLIST_URL),
                   mcp_private=bool(MCP_TOKEN), mcp_public=MCP_PUBLIC)


# ======================================================== MCP endpoint
MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_INSTRUCTIONS = ("Search a home cookbook collection: books, and recipes indexed from those books "
                    "(name, index heading such as a main ingredient, and page). Recipes are physical: tell the user the "
                    "book and page. Index headings are often ingredients, so ingredient searches work well. "
                    "Some books were indexed without page numbers, so page is missing for their recipes. That's a "
                    "limitation of the data, not an error: the recipe name and book title are enough to find it in "
                    "the book's own index, so give those and don't apologize for or dwell on the missing page. "
                    "search_recipes returns the best matches first, with a total. match explains each result: 'title' "
                    "when every word is in the recipe's title, otherwise per word, e.g. 'chicken: title, leek: tag' "
                    "(tag = the recipe is on Eat Your Books' list for that ingredient, so it does use it even though the "
                    "title doesn't say so), 'chicken: may be only chicken stock' (the ingredient may only be used as stock, "
                    "tomato paste, rice wine…) or 'chicken: only as \u201cchicken stock\u201d in the title' (a recipe for "
                    "the stock itself); when next_offset is present there are more results to fetch.")


BOOK_FIELDS = ["title", "subtitle", "authors", "publisher", "year", "cuisine", "tags", "recipe_count",
               "status", "location", "rating", "cooked_count", "cooked_last", "want_count"]  # the last six: private only


def _book_out(b, private):
    keys = ["id"] + BOOK_FIELDS[:8] + (BOOK_FIELDS[8:] if private else [])
    return {k: b.get(k) for k in keys if b.get(k) not in (None, [], "")}


def _recipe_out(r, private):
    keys = ["id", "name", "heading", "page", "url", "book_id", "book_title", "match"]
    if private:
        keys += ["want_to_try", "rating", "notes", "cooked_count", "cooked_avg", "cooked_last", "last_made"]
    return {k: r.get(k) for k in keys if r.get(k) not in (None, "", 0) or k == "page"}


def t_search_recipes(a, private):
    q = (a.get("query") or "").strip()
    if not q and not a.get("ingredient"):
        raise ValueError("query (or ingredient) is required")
    import ingredients
    where, args = core.recipe_filter({"q": q, "cuisine": a.get("cuisine"), "book": a.get("book_id"),
                                      "ingredient": a.get("ingredient")}, private)
    base = " FROM recipes r JOIN books b ON b.id=r.book_id WHERE 1=1" + "".join(" AND " + w for w in where)
    order = ingredients.ranked(db(), base, args, ingredients.search_terms(q, a.get("ingredient")))  # best first
    limit = max(1, min(int(a.get("limit") or 25), 100))
    offset = max(0, int(a.get("offset") or 0))
    page = order[offset:offset + limit]
    match = {x["id"]: x["match"] for x in page}
    rows = [dict(r) for r in core.recipes_by_id(list(match))]
    for r in rows:
        r["match"] = match[r["id"]]
    if private:
        ensure_synced()
        st = cooked_stats_for([r["id"] for r in rows])
        for r in rows:
            r.update(st.get(r["id"], {}))
    out = {"total": len(order), "count": len(rows), "offset": offset,
           "recipes": [_recipe_out(r, private) for r in rows]}
    if offset + len(rows) < len(order):
        out["next_offset"] = offset + len(rows)  # more results: call again with this offset
    if a.get("ingredient"):  # say how the ingredient was read, so a miss isn't a silent 0
        import ingredients
        tags = [t for _, t in ingredients.resolve(db(), a["ingredient"])]
        out["ingredient_tags"] = tags
        out["ingredient_note"] = (
            f"Matched recipes tagged {' and '.join(tags)}, or with '{a['ingredient']}' in the title." if tags else
            f"No ingredient tag for '{a['ingredient']}', so it was matched against recipe titles only. "
            f"Tags: {ingredient_tag_summary()}.")
    return out


def ingredient_tag_names(hint=0):
    """Major ingredient tags (hint=0), or the search-hint ones (hint=1), most-used first."""
    return [r[0] for r in db().execute(
        "SELECT i.name FROM ingredients i JOIN recipe_ingredients ri ON ri.ingredient_id=i.id WHERE i.hint=? "
        "GROUP BY i.id ORDER BY count(*) DESC", (hint,))]


def ingredient_tag_summary():
    """'Eggs, Chicken, … plus search hints: Zucchini, Kale' for the MCP to know which tags exist."""
    major, hints = ", ".join(ingredient_tag_names(0)) or "none yet", ", ".join(ingredient_tag_names(1))
    return major + (f"; plus search hints: {hints}" if hints else "")


def t_search_books(a, private):
    sql, args = "SELECT b.* FROM books b WHERE 1=1", []
    if not private:
        sql += " AND b.status NOT IN ('gone') AND " + core.PUBLIC_BOOKS
    q = (a.get("query") or "").strip()
    if q:
        fts_words, like_words = core.search_words(q)
        fts_q = core.fts_query(fts_words)
        if core.HAS_FTS and fts_q:
            sql += " AND b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)"
            args.append(fts_q)
        elif fts_words and not core.HAS_FTS:
            sql += " AND fold(b.title) LIKE ?"
            args.append(f"%{fold(q)}%")
        for w in like_words:
            sql += " AND fold(b.title) LIKE ?"
            args.append(f"%{fold(w)}%")
        if not fts_q and not like_words:
            return {"count": 0, "books": []}
    if a.get("indexed") is not None:  # false: cookbooks with no recipes indexed yet; true: ones with some
        indexed = a["indexed"] not in (False, 0, "false", "False", "0", "no")
        sql += (" AND b.kind IS NULL AND " + ("" if indexed else "NOT ")
                + "EXISTS (SELECT 1 FROM recipes r WHERE r.book_id=b.id)")
    if a.get("cuisine"):
        cond, ca = core.cuisine_condition(a["cuisine"])
        sql += " AND " + cond
        args += ca
    for k, s in (("tag", " AND b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)"),
                 ("author", " AND b.id IN (SELECT ba.book_id FROM book_authors ba JOIN authors au ON au.id=ba.author_id WHERE au.name LIKE ?)")):
        if a.get(k):
            sql += s
            args.append(a[k] if k != "author" else f"%{a[k]}%")
    rows = db().execute(sql + " ORDER BY b.title LIMIT ?", args + [max(1, min(int(a.get("limit") or 50), 200))]).fetchall()
    books = core.book_dicts(rows, private)
    fields = set(a.get("fields") or [])
    if private and (not fields or fields & {"cooked_count", "cooked_last"}):
        ensure_synced()
        cb = cooked_by_book([b["id"] for b in books])
        for b in books:
            b.update(cb.get(b["id"], {}))
    out = [_book_out(b, private) for b in books]
    if fields:  # just these, for an overview of many books without filling the context
        out = [{k: v for k, v in b.items() if k in fields or k == "id"} for b in out]
    return {"count": len(books), "books": out}


def t_get_book(a, private):
    con = db()
    pub = "" if private else f" AND {core.PUBLIC_BOOKS}"
    if a.get("book_id"):
        row = con.execute("SELECT * FROM books b WHERE b.id=?" + pub, (int(a["book_id"]),)).fetchone()
    elif a.get("title"):
        row = con.execute("SELECT * FROM books b WHERE fold(b.title)=fold(?)" + pub, (a["title"],)).fetchone()
        if not row and core.HAS_FTS:
            fts_q = core.fts_query(a["title"])
            if fts_q:
                row = con.execute("SELECT b.* FROM books b WHERE b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)"
                                  + pub + " LIMIT 1", (fts_q,)).fetchone()
    else:
        raise ValueError("book_id or title is required")
    if not row or (not private and row["kind"]):
        raise ValueError("No such book")
    b = core.book_dicts([row], private)[0]
    recs = [dict(r) for r in con.execute("SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort", (b["id"],))]
    if private:
        ensure_synced()
        st = cooked_stats_for([r["id"] for r in recs])
        for r in recs:
            r.update(st.get(r["id"], {}))
        b.update(cooked_by_book([b["id"]]).get(b["id"], {}))
    out = _book_out(b, private)
    if b.get("description"):
        out["description"] = b["description"]
    out["recipes"] = [{k: v for k, v in _recipe_out(r, private).items() if k not in ("book_id", "book_title")} for r in recs]
    return out


def t_list_categories(a, private):
    con = db()
    pub = "" if private else f" WHERE {core.PUBLIC_BOOKS}"
    pub_and = "" if private else f" AND {core.PUBLIC_BOOKS}"
    return {
        "ingredients": [dict(r) for r in con.execute(
            "SELECT i.name, count(ri.recipe_id) AS recipes, i.hint = 1 AS search_hint FROM ingredients i "
            "JOIN recipe_ingredients ri ON ri.ingredient_id=i.id "
            "JOIN recipes r ON r.id=ri.recipe_id JOIN books b ON b.id=r.book_id"
            + pub + " GROUP BY i.id ORDER BY 2 DESC")],
        "cuisines": [dict(r) for r in con.execute(
            "SELECT cuisine AS name, count(*) AS books FROM books b WHERE cuisine IS NOT NULL"
            + pub_and + " GROUP BY 1 ORDER BY 2 DESC")],
        "tags": [dict(r) for r in con.execute(
            "SELECT t.name, count(*) AS books FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
            "JOIN books b ON b.id=bt.book_id" + pub + " GROUP BY t.id ORDER BY 2 DESC")],
        "totals": {"books": con.execute(f"SELECT count(*) FROM books b WHERE {core.PUBLIC_BOOKS}").fetchone()[0],
                   "recipes": con.execute("SELECT count(*) FROM recipes r JOIN books b ON b.id=r.book_id"
                                          + pub).fetchone()[0]},
    }


def t_suggest(a, private):
    res = suggest(a.get("mode") or "want", a.get("cuisine"), a.get("tag"), max(1, min(int(a.get("count") or 5), 20)))
    res["recipes"] = [_recipe_out(r, True) for r in res["recipes"]]
    return res


def t_stats(a, private):
    return stats()


def t_set_want(a, private):
    rid = int(a["recipe_id"])
    con = db()
    if not con.execute("SELECT 1 FROM recipes WHERE id=?", (rid,)).fetchone():
        raise ValueError("No such recipe")
    want = a.get("want", True) not in (False, 0, "false", "False", "0", "no")
    con.execute("UPDATE recipes SET want_to_try=? WHERE id=?", (1 if want else 0, rid))
    con.commit()
    return {"ok": True, "recipe_id": rid, "want_to_try": bool(want)}


def t_update_book(a, private):
    """Set a book's cuisine and add or remove its tags; nothing else about a book can change from here."""
    con = db()
    bid = int(a["book_id"])
    row = con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
    if not row:
        raise ValueError("No such book")
    tag_names = lambda: [r[0] for r in con.execute(
        "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=? ORDER BY t.name", (bid,))]
    before = {"cuisine": row["cuisine"], "tags": tag_names()}
    if "cuisine" in a:
        cuisine = clean(str(a["cuisine"] or ""))
        if cuisine:  # reuse an existing cuisine's spelling, so "mexican" joins "Mexican"
            same = con.execute("SELECT cuisine FROM books WHERE fold(cuisine)=fold(?) GROUP BY cuisine "
                               "ORDER BY count(*) DESC LIMIT 1", (cuisine,)).fetchone()
            cuisine = same[0] if same else cuisine
        con.execute("UPDATE books SET cuisine=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (cuisine, bid))
    rm_raw, add_raw = a.get("remove_tags") or [], a.get("add_tags") or []
    if isinstance(rm_raw, str):
        rm_raw = [rm_raw]
    if isinstance(add_raw, str):
        add_raw = [add_raw]
    drop = {fold(t) for t in rm_raw if t}
    tags = [t for t in before["tags"] if fold(t) not in drop] + [t for t in add_raw if clean(t)]
    if drop or add_raw:
        authors = [r[0] for r in con.execute("SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
                                             "WHERE ba.book_id=? ORDER BY pos", (bid,))]
        core.set_people(con, bid, authors, tags)
        con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
    core.reindex_book(con, bid)
    con.commit()
    after = {"cuisine": con.execute("SELECT cuisine FROM books WHERE id=?", (bid,)).fetchone()[0], "tags": tag_names()}
    return {"ok": True, "book_id": bid, "title": row["title"], "before": before, "after": after}


_S = lambda **p: {"type": "object", "properties": p}
TOOLS = [
    ("search_recipes", False, t_search_recipes,
     "Search recipes indexed from our cookbooks. Each word must match the recipe name, its index heading, or one "
     "of the major ingredient tags ({tags}), so 'chicken' finds recipes that use chicken even if the title doesn't "
     "say so. Only some recipes are tagged, so other ingredients are found through titles. Results are ranked: "
     "words in the title first, then specific ingredient tags, and recipes that may only use a minor form "
     "(chicken stock, tomato paste, rice wine…) last. Returns recipe name, heading, page (missing for some books) and book, the "
     "total number of matches, and next_offset when there are more.",
     {**_S(query={"type": "string", "description": "Words to match, e.g. 'braised short rib' or 'fennel'"},
           cuisine={"type": "string"}, book_id={"type": "integer"},
           ingredient={"type": "string", "description": "Only recipes using this ingredient: tagged with the major "
                       "tag it names ('ground beef' uses Beef) or with it in the title. Tags: {tags}."},
           limit={"type": "integer", "description": "Max results, default 25, at most 100"},
           offset={"type": "integer", "description": "Skip this many results: pass next_offset from the previous "
                   "call to get the next page"})}),
    ("search_books", False, t_search_books,
     "Find cookbooks in the collection by words (title, author, publisher, tags), cuisine, tag or author. A book has one "
     "main cuisine, and a tag with another cuisine's name gives it a second one; the cuisine filter matches both. "
     "indexed: false lists the cookbooks with no recipes indexed yet (so their recipes can't be searched), "
     "e.g. to find books whose index still needs scanning. recipe_count says how many recipes each book has.",
     _S(query={"type": "string"}, cuisine={"type": "string"}, tag={"type": "string"}, author={"type": "string"},
        indexed={"type": "boolean", "description": "false: only books with no recipes indexed; true: only books with some"},
        fields={"type": "array", "items": {"type": "string", "enum": BOOK_FIELDS},
                "description": "Return only these fields (id is always included), e.g. [\"title\", \"cuisine\", \"tags\"] "
                "when listing many books: the full records are several times larger"},
        limit={"type": "integer", "description": "Max books, default 50, at most 200"})),
    ("get_book", False, t_get_book, "A cookbook's details and every recipe indexed from it, in page order.",
     _S(book_id={"type": "integer"}, title={"type": "string"})),
    ("list_categories", False, t_list_categories, "Cuisines and tags used in the collection, with book counts.", _S()),
    ("suggest_recipes", True, t_suggest,
     "Random dinner ideas from the collection. Modes: want (starred, never cooked), untried (never cooked), "
     "neglected (from books not cooked from in a year), favorites (rated 8+, not made in 3 months).",
     _S(mode={"type": "string", "enum": list(SUGGEST_MODES)}, cuisine={"type": "string"}, tag={"type": "string"},
        count={"type": "integer"})),
    ("cooking_stats", True, t_stats,
     "What we've cooked from our cookbooks (from Dinner Log): most-used books, top-rated recipes, books never cooked from.",
     _S()),
    ("set_want_to_try", True, t_set_want, "Star (or unstar) a recipe as want-to-try.",
     {**_S(recipe_id={"type": "integer"}, want={"type": "boolean"}), "required": ["recipe_id"]}),
    ("update_book", True, t_update_book,
     "Fix a cookbook's cuisine or tags. Only these can change: give cuisine to set it (an empty string clears it), "
     "add_tags / remove_tags to change tags. Use the cuisine and tag names list_categories shows when one fits. "
     "For a book of two cuisines, set the main one as cuisine and add the other as a tag with the same name "
     "(cuisine American, tag Portuguese): the cuisine filter then finds it under both, and stats count the main one. "
     "Returns the book's cuisine and tags before and after.",
     {**_S(book_id={"type": "integer"}, cuisine={"type": "string"},
           add_tags={"type": "array", "items": {"type": "string"}},
           remove_tags={"type": "array", "items": {"type": "string"}}), "required": ["book_id"]}),
]
WRITE_TOOLS = {"set_want_to_try", "update_book"}


def mcp_auth(token):
    """Returns 'private', 'public' or None."""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
    supplied = token or bearer
    if supplied:
        if MCP_TOKEN and hmac.compare_digest(supplied, MCP_TOKEN):
            return "private"
        return None
    return "public" if MCP_PUBLIC else None


def rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def handle_rpc(msg, private):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    raw_params = msg.get("params")
    if raw_params is not None and not isinstance(raw_params, dict):
        return rpc_error(msg.get("id"), -32602, "Invalid params")
    mid, method, params = msg.get("id"), msg["method"], raw_params or {}
    if mid is None:  # notification
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return rpc_result(mid, {
            "protocolVersion": want if want in MCP_VERSIONS else MCP_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cookbooks", "title": "Cookbook Collection", "version": "2.0"},
            "instructions": MCP_INSTRUCTIONS + (
                " Some recipes come from recipe websites instead of a book: their book is the site (e.g. NYT Cooking) "
                "and they have a url instead of a page, so give the link." if private
                else " (Public view: no ratings or cooking history.)"),
        })
    if method == "ping":
        return rpc_result(mid, {})
    if method == "tools/list":
        tags = ingredient_tag_summary()
        fill = lambda x: json.loads(json.dumps(x).replace("{tags}", tags.replace('"', "'")))
        return rpc_result(mid, {"tools": [
            {"name": n, "description": fill(d), "inputSchema": fill(s),
             "annotations": {"readOnlyHint": n not in WRITE_TOOLS}}
            for n, priv_only, _, d, s in TOOLS if private or not priv_only]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        tool = next((t for t in TOOLS if t[0] == name and (private or not t[1])), None)
        if not tool:
            return rpc_error(mid, -32602, f"Unknown tool: {name}")
        try:
            res = tool[2](args, private)
            return rpc_result(mid, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                                    "structuredContent": res})
        except Exception as e:  # noqa: BLE001 — tool errors go back to the model
            return rpc_result(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    return rpc_error(mid, -32601, f"Method not found: {method}")


@app.route("/mcp", methods=["GET", "POST", "DELETE"])
@app.route("/mcp/<token>", methods=["GET", "POST", "DELETE"])
def mcp(token=None):
    level = mcp_auth(token)
    if level is None:
        return (jsonify(error="unauthorized"), 404 if token else 401)
    if request.method != "POST":
        return Response(status=405, headers={"Allow": "POST"})
    try:
        payload = request.get_json(force=True)
    except Exception:  # noqa: BLE001
        return jsonify(rpc_error(None, -32700, "Parse error")), 400
    private = level == "private"
    if isinstance(payload, list):
        out = [r for r in (handle_rpc(m, private) for m in payload) if r is not None]
        return (jsonify(out), 200) if out else Response(status=202)
    res = handle_rpc(payload, private)
    return (jsonify(res), 200) if res is not None else Response(status=202)


# ======================================================== family wish list
WISHLIST_URL = os.environ.get("WISHLIST_URL", "http://127.0.0.1:8766").rstrip("/")
WISHLIST_BOOK_URL = os.environ.get("WISHLIST_BOOK_URL", "https://www.amazon.com/s?k={q}")
DINNERLOG_URL = os.environ.get("DINNERLOG_URL", "").rstrip("/")  # public URL, for "Log this dinner" links

WISH_SCHEMA = """CREATE TABLE IF NOT EXISTS wish_items (
  id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  person_id INTEGER, person_name TEXT, item_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def wishlist_call(method, path, body=None):
    """Talk to the wish list app over localhost (it treats loopback requests as LAN editors)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(WISHLIST_URL + path, data=data, method=method, headers={
        "X-Wishlist": "1", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        raise RuntimeError(msg or f"wish list answered {e.code}")
    except OSError as e:
        raise RuntimeError(f"wish list isn't reachable at {WISHLIST_URL} ({e})")


@app.get("/admin/api/wishlist/people")
@editor_required
def wishlist_people():
    try:
        people = wishlist_call("GET", "/api/people")
    except RuntimeError as e:
        return make_err(502, str(e))
    return jsonify(people=[{"id": p["id"], "name": p["name"], "color": p.get("color")} for p in people])


@app.post("/admin/api/books/<int:book_id>/wishlist")
@editor_required
def add_to_wishlist(book_id):
    d = request.get_json(force=True)
    con = db()
    row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    b = core.book_dicts([row], True)[0]
    pid = int(d.get("person_id") or 0)
    by = f" by {', '.join(b['authors'][:2])}" if b["authors"] else ""
    q = b["isbn"] or f"{b['title']} {' '.join(b['authors'][:1])}"
    notes = ["Cookbook"]
    if b["isbn"]:
        notes.append(f"ISBN {b['isbn']}")
    if d.get("notes"):
        notes.append(str(d["notes"])[:300])
    item = {"name": f"{b['title']}{by}"[:200], "kind": "online",
            "url": WISHLIST_BOOK_URL.replace("{q}", urllib.parse.quote_plus(q)),
            "notes": " · ".join(notes), "priority": int(d.get("priority") or 2)}
    if d.get("price") not in (None, ""):
        item["price"] = d["price"]
    try:
        people = {p["id"]: p["name"] for p in wishlist_call("GET", "/api/people")}
        if pid not in people:
            return make_err(400, "Pick whose wish list")
        created = wishlist_call("POST", f"/api/people/{pid}/items", item)
    except RuntimeError as e:
        return make_err(502, str(e))
    con.execute("INSERT INTO wish_items(book_id, person_id, person_name, item_id) VALUES (?,?,?,?)",
                (book_id, pid, people[pid], created.get("id")))
    con.commit()
    return jsonify(ok=True, person=people[pid], item_id=created.get("id"))


def wishes_for_book(book_id):
    return [dict(r) for r in db().execute(
        "SELECT person_name, created_at FROM wish_items WHERE book_id=? ORDER BY id", (book_id,))]
