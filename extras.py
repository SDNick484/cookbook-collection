"""Cookbook Collection v2 features, registered onto the core app:

- Dinner Log link: reads Dinner Log's SQLite DB read-only and matches cookbook
  dinners to books and recipes (auto by title + page + name, with manual overrides)
- Photo index import via the Claude API
- "What should we cook?" suggestions, stats, off-the-shelf tracking
- MCP endpoint (Streamable HTTP, stateless JSON) at /mcp
"""
import base64, difflib, hmac, io, json, os, random, re, sqlite3, threading, time, urllib.request
from datetime import date, timedelta

from flask import abort, g, jsonify, request, Response

import app as core
from app import app, db, editor_required, fold, make_err, page_sort, clean

DINNERLOG_DB = os.environ.get("DINNERLOG_DB", "/var/lib/dinner-log/meals.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
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


def dl_signature():
    try:
        st = [os.stat(DINNERLOG_DB)]
    except OSError:
        return None
    try:
        st.append(os.stat(DINNERLOG_DB + "-wal"))
    except OSError:
        pass
    return DINNERLOG_DB + "|" + "|".join(f"{s.st_ino}:{s.st_mtime_ns}:{s.st_size}" for s in st)


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
    """Rebuild the `cooked` table from Dinner Log when its DB file changed."""
    sig = dl_signature()
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
            meals = src.execute(f"SELECT id, day, name, source_title, source_detail, {rating} FROM meals "
                                f"WHERE source_type='cookbook'").fetchall()
            src.close()
        except sqlite3.Error as e:
            app.logger.warning("Dinner Log read failed: %s", e)
            return {"available": False, "error": str(e)}
        books = [(r[0], norm_title(r[1]), tokens(r[1])) for r in con.execute("SELECT id, title FROM books")]
        aliases = {r[0]: r[1] for r in con.execute("SELECT title_key, book_id FROM dl_alias")}
        links = {r[0]: (r[1], r[2]) for r in con.execute("SELECT meal_id, recipe_id, meal_key FROM dl_link")}
        rec_cache = {}
        con.execute("DELETE FROM cooked")
        for m in meals:
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
            elif bid:
                if bid not in rec_cache:
                    rec_cache[bid] = con.execute(
                        "SELECT id, name, heading, page_sort, page FROM recipes WHERE book_id=?", (bid,)).fetchall()
                rid = match_recipe(rec_cache[bid], m["source_detail"], m["name"])
                how = "auto" if rid else "book"
            con.execute("INSERT INTO cooked VALUES (?,?,?,?,?,?,?,?,?)",
                        (m["id"], m["day"], m["name"], m["source_title"], m["source_detail"], m["rating"],
                         bid, rid, how))
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
        "AND trim(coalesce(dl_title,'')) <> '' GROUP BY lower(dl_title) ORDER BY n DESC")]
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
        where.append("b.cuisine=?")
        args.append(cuisine)
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
            HAVING max(coalesce(c.rating, 0), coalesce(r2.rating, 0)) >= 8
               AND max(coalesce(c.day, ''), coalesce(r2.last_made, '')) < ?)""")
        args.append(months3)
    else:
        raise ValueError("unknown mode")
    sql = ("SELECT r.id, r.name, r.heading, r.page, r.want_to_try, r.rating, r.notes, r.last_made, "
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
        "books": one("SELECT count(*) FROM books WHERE status NOT IN ('gone','wanted')"),
        "books_indexed": one("SELECT count(DISTINCT book_id) FROM recipes WHERE source='index'"),
        "recipes": one("SELECT count(*) FROM recipes"),
        "want_to_try": one("SELECT count(*) FROM recipes WHERE want_to_try=1"),
        "off_shelf": [dict(r) for r in con.execute(
            "SELECT id, title, out_since FROM books WHERE status='out' ORDER BY out_since")],
        "dinners_from_books": one("SELECT count(*) FROM cooked WHERE book_id IS NOT NULL"),
        "dinners_last_year": one("SELECT count(*) FROM cooked WHERE book_id IS NOT NULL AND day >= ?", year_ago),
        "books_cooked_from": one("SELECT count(DISTINCT book_id) FROM cooked WHERE book_id IS NOT NULL"),
        "top_books": [dict(r) for r in con.execute(
            "SELECT b.id, b.title, count(*) AS n, max(c.day) AS last FROM cooked c JOIN books b ON b.id=c.book_id "
            "GROUP BY b.id ORDER BY n DESC, last DESC LIMIT 10")],
        "top_recipes": [dict(r) for r in con.execute(
            "SELECT r.id, r.name, r.heading, b.id AS book_id, b.title AS book_title, count(c.meal_id) AS n, "
            "round(avg(coalesce(c.rating, r.rating)),1) AS avg FROM recipes r JOIN books b ON b.id=r.book_id "
            "LEFT JOIN cooked c ON c.recipe_id=r.id WHERE c.rating IS NOT NULL OR r.rating IS NOT NULL "
            "GROUP BY r.id HAVING avg >= 7 ORDER BY avg DESC, n DESC LIMIT 10")],
        "cuisines": [dict(r) for r in con.execute(
            "SELECT coalesce(b.cuisine,'(none)') AS name, count(DISTINCT b.id) AS books, count(c.meal_id) AS cooked "
            "FROM books b LEFT JOIN cooked c ON c.book_id=b.id WHERE b.status NOT IN ('gone','wanted') "
            "GROUP BY 1 ORDER BY books DESC")],
        "never_cooked": [dict(r) for r in con.execute(
            "SELECT b.id, b.title FROM books b WHERE b.status NOT IN ('gone','wanted') "
            "AND b.id NOT IN (SELECT book_id FROM cooked WHERE book_id IS NOT NULL) "
            "AND b.id NOT IN (SELECT book_id FROM recipes WHERE last_made IS NOT NULL OR rating IS NOT NULL) "
            "ORDER BY b.title")],
        "months": months,
        "dinnerlog": meta_get(con, "dl_synced"),
    }


@app.get("/admin/api/stats")
@editor_required
def stats_route():
    return jsonify(stats())


# ======================================================== photo index import
PHOTO_PROMPT = """This is a photo of a page from a cookbook's index. Transcribe every index entry that has a page number.

Rules:
- Read in the book's order: the whole first column top to bottom, then the next column.
- A main entry with no page number (e.g. "Chicken") is a heading; the indented entries under it are sub-entries. For each sub-entry, set "heading" to that main entry. Headings carry over from the previous page/column when the first lines are indented continuations.
- A main entry that has its own page number is an entry with heading null; it may also be the heading for sub-entries below it.
- Join entries that wrap onto a second line.
- Keep page numbers exactly as printed, including ranges and lists ("42", "342–44", "51, 88"). Drop "see also" cross-references and letter dividers.
- Keep the wording as printed; don't add or invent anything. If a word is unreadable, give your best reading.

Reply with only JSON, no prose: {"entries": [{"heading": string|null, "name": string, "page": string}]}"""


def call_claude(image_jpeg, media_type="image/jpeg"):
    body = {
        "model": ANTHROPIC_MODEL, "max_tokens": 8000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image_jpeg).decode()}},
            {"type": "text", "text": PHOTO_PROMPT}]}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
        "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=170) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Claude API error {e.code}: {detail}")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_claude_rows(text):
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if not m:
        raise ValueError("No JSON in the response")
    data = json.loads(m.group())
    entries = data.get("entries", []) if isinstance(data, dict) else data
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        name, page = clean(str(e.get("name") or "")), clean(str(e.get("page") or ""))
        if not name or not page or not re.search(r"\d", page):
            continue
        page = re.sub(r"\s*[-—]\s*", "–", re.sub(r"\s*,\s*", ", ", page))
        heading = clean(str(e.get("heading") or "")) if e.get("heading") else None
        rows.append({"heading": heading, "name": name, "page": page})
    return rows


def prepare_photo(raw):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("L")  # grayscale: text only, smaller
    img.thumbnail((1568, 1568))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


@app.post("/admin/api/books/<int:book_id>/index/photo")
@editor_required
def index_photo(book_id):
    if not db().execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
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
    try:
        rows = parse_claude_rows(call_claude(jpeg))
    except Exception as e:  # noqa: BLE001
        return make_err(502, f"Couldn't read the index from that photo: {e}")
    return jsonify(rows=core.mark_duplicates(book_id, rows))


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
                    "book and page. Index headings are often ingredients, so ingredient searches work well.")


def _book_out(b, private):
    keys = ["id", "title", "subtitle", "authors", "publisher", "year", "cuisine", "tags", "recipe_count"]
    if private:
        keys += ["status", "location", "rating", "cooked_count", "cooked_last", "want_count"]
    return {k: b.get(k) for k in keys if b.get(k) not in (None, [], "")}


def _recipe_out(r, private):
    keys = ["id", "name", "heading", "page", "book_id", "book_title"]
    if private:
        keys += ["want_to_try", "rating", "notes", "cooked_count", "cooked_avg", "cooked_last", "last_made"]
    return {k: r.get(k) for k in keys if r.get(k) not in (None, "", 0) or k == "page"}


def t_search_recipes(a, private):
    q = (a.get("query") or "").strip()
    if not q and not a.get("ingredient"):
        raise ValueError("query (or ingredient) is required")
    where, args = core.recipe_filter({"q": q, "cuisine": a.get("cuisine"), "book": a.get("book_id"),
                                      "ingredient": a.get("ingredient")}, private)
    sql = ("SELECT r.*, b.title AS book_title FROM recipes r JOIN books b ON b.id=r.book_id WHERE 1=1"
           + "".join(" AND " + w for w in where))
    limit = max(1, min(int(a.get("limit") or 25), 100))
    rows = [dict(r) for r in db().execute(sql + " ORDER BY b.title, r.page_sort LIMIT ?", args + [limit])]
    if private:
        ensure_synced()
        st = cooked_stats_for([r["id"] for r in rows])
        for r in rows:
            r.update(st.get(r["id"], {}))
    return {"count": len(rows), "recipes": [_recipe_out(r, private) for r in rows]}


def t_search_books(a, private):
    sql, args = "SELECT b.* FROM books b WHERE 1=1", []
    if not private:
        sql += " AND b.status NOT IN ('gone')"
    q = (a.get("query") or "").strip()
    if q:
        sql += " AND b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)" if core.HAS_FTS else " AND lower(b.title) LIKE ?"
        args.append(core.fts_query(q) if core.HAS_FTS else f"%{fold(q)}%")
    for k, s in (("cuisine", " AND b.cuisine=?"),
                 ("tag", " AND b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)"),
                 ("author", " AND b.id IN (SELECT ba.book_id FROM book_authors ba JOIN authors au ON au.id=ba.author_id WHERE au.name LIKE ?)")):
        if a.get(k):
            sql += s
            args.append(a[k] if k != "author" else f"%{a[k]}%")
    rows = db().execute(sql + " ORDER BY b.title LIMIT ?", args + [max(1, min(int(a.get("limit") or 50), 200))]).fetchall()
    books = core.book_dicts(rows, private)
    if private:
        ensure_synced()
        cb = cooked_by_book([b["id"] for b in books])
        for b in books:
            b.update(cb.get(b["id"], {}))
    return {"count": len(books), "books": [_book_out(b, private) for b in books]}


def t_get_book(a, private):
    con = db()
    if a.get("book_id"):
        row = con.execute("SELECT * FROM books WHERE id=?", (int(a["book_id"]),)).fetchone()
    elif a.get("title"):
        row = con.execute("SELECT * FROM books WHERE lower(title)=lower(?)", (a["title"],)).fetchone()
        if not row and core.HAS_FTS:
            row = con.execute("SELECT b.* FROM books b WHERE b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?) "
                              "LIMIT 1", (core.fts_query(a["title"]),)).fetchone()
    else:
        raise ValueError("book_id or title is required")
    if not row:
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
    return {
        "ingredients": [dict(r) for r in con.execute(
            "SELECT i.name, count(ri.recipe_id) AS recipes FROM ingredients i JOIN recipe_ingredients ri "
            "ON ri.ingredient_id=i.id GROUP BY i.id ORDER BY 2 DESC")],
        "cuisines": [dict(r) for r in con.execute(
            "SELECT cuisine AS name, count(*) AS books FROM books WHERE cuisine IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")],
        "tags": [dict(r) for r in con.execute(
            "SELECT t.name, count(*) AS books FROM tags t JOIN book_tags bt ON bt.tag_id=t.id GROUP BY t.id ORDER BY 2 DESC")],
        "totals": {"books": con.execute("SELECT count(*) FROM books").fetchone()[0],
                   "recipes": con.execute("SELECT count(*) FROM recipes").fetchone()[0]},
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
    con.execute("UPDATE recipes SET want_to_try=? WHERE id=?", (1 if a.get("want", True) else 0, rid))
    con.commit()
    return {"ok": True, "recipe_id": rid, "want_to_try": bool(a.get("want", True))}


_S = lambda **p: {"type": "object", "properties": p}
TOOLS = [
    ("search_recipes", False, t_search_recipes,
     "Search recipes indexed from our cookbooks. Each word must match the recipe name, its index heading, or an "
     "ingredient it's tagged with (from Eat Your Books ingredient lists), so 'chicken' finds recipes that use "
     "chicken even if the title doesn't say so. Returns recipe name, heading, page and book.",
     {**_S(query={"type": "string", "description": "Words to match, e.g. 'braised short rib' or 'fennel'"},
           cuisine={"type": "string"}, book_id={"type": "integer"},
           ingredient={"type": "string", "description": "Only recipes tagged with this ingredient, e.g. 'Lamb'"},
           limit={"type": "integer", "description": "Max results, default 25"})}),
    ("search_books", False, t_search_books,
     "Find cookbooks in the collection by words (title, author, publisher, tags), cuisine, tag or author.",
     _S(query={"type": "string"}, cuisine={"type": "string"}, tag={"type": "string"}, author={"type": "string"},
        limit={"type": "integer"})),
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
]


def mcp_auth(token):
    """Returns 'private', 'public' or None."""
    if MCP_TOKEN:
        supplied = token or ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = supplied or auth[7:].strip()
        if supplied and hmac.compare_digest(supplied, MCP_TOKEN):
            return "private"
        if token:  # wrong token in the URL
            return None
    return "public" if MCP_PUBLIC else None


def rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def handle_rpc(msg, private):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if mid is None:  # notification
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return rpc_result(mid, {
            "protocolVersion": want if want in MCP_VERSIONS else MCP_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cookbooks", "title": "Cookbook Collection", "version": "2.0"},
            "instructions": MCP_INSTRUCTIONS + ("" if private else " (Public view: no ratings or cooking history.)"),
        })
    if method == "ping":
        return rpc_result(mid, {})
    if method == "tools/list":
        return rpc_result(mid, {"tools": [
            {"name": n, "description": d, "inputSchema": s,
             "annotations": {"readOnlyHint": n != "set_want_to_try"}}
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
