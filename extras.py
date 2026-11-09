"""Cookbook Collection v2 features, registered onto the core app:

- Dinner Log link: pulls cooking history from Dinner Log's /api/history and matches
  dinners to books and recipes (by the ids Dinner Log stamped, else title + page +
  name, with manual overrides)
- Photo index import via the Claude API
- "What should we cook?" suggestions, stats, off-the-shelf tracking
- MCP endpoint (Streamable HTTP, stateless JSON) at /mcp
"""
import base64, difflib, hmac, io, json, logging, os, random, re, secrets, sqlite3, threading, time, urllib.error, urllib.request
from datetime import date, timedelta

from flask import abort, g, jsonify, request, Response

import app as core
from app import app, db, editor_required, fold, make_err, page_sort, clean



def clean_url(raw):
    """A configured base address without a trailing slash."""
    return (raw or "").strip().rstrip("/")


# Dinner Log's private address and its HISTORY_TOKEN, for cooking history over HTTP.
DINNERLOG_API_URL = clean_url(os.environ.get("DINNERLOG_API_URL"))
DINNERLOG_TOKEN = os.environ.get("DINNERLOG_TOKEN", "").strip()
PULL_EVERY = 60          # seconds a pulled history stays fresh
PULL_BACKOFF = 300       # after a failed pull, wait this long before trying again
LEASE_TTL = 120          # a pull lease older than this is abandoned
PULL_TIMEOUT = 5
MAX_HISTORY_BYTES = 10 * 1024 * 1024
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
CREATE TABLE IF NOT EXISTS dl_meals (       -- the last history pulled from Dinner Log, as sent
  id INTEGER PRIMARY KEY,                   -- Dinner Log meals.id
  day TEXT, name TEXT, source_type TEXT, source_title TEXT, source_detail TEXT,
  rating REAL, cookbook_book_id INTEGER, cookbook_recipe_id INTEGER,
  cookbook_book_uid TEXT, cookbook_recipe_uid TEXT);  -- this catalog's books.uid / recipes.uid, when stamped
INSERT OR IGNORE INTO meta(key, value) VALUES ('dl_lease', '0');
"""


def migrate(con):
    con.executescript(MIGRATIONS)
    con.execute(WISH_SCHEMA)
    con.execute(PHOTO_SCHEMA)
    # Whole-index scans (one parent row, one child row per page). Single-page jobs leave all three NULL.
    photo_cols = [r[1] for r in con.execute("PRAGMA table_info(photo_jobs)")]
    for col, typ in (("parent_id", "TEXT"), ("page_no", "INTEGER"), ("started", "REAL")):
        if col not in photo_cols:
            con.execute(f"ALTER TABLE photo_jobs ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS photo_jobs_parent ON photo_jobs(parent_id)")
    con.execute("UPDATE photo_jobs SET status='error', "
                "error='Reading that photo stopped partway (the server restarted). Try it again.' "
                "WHERE status IN ('running', 'queued')")  # their threads died with the process
    con.execute("DELETE FROM meta WHERE key='dl_sig'")  # from reading Dinner Log's file directly
    dl_cols = [r[1] for r in con.execute("PRAGMA table_info(dl_meals)")]
    if "cookbook_book_uid" not in dl_cols:
        for col in ("cookbook_book_uid", "cookbook_recipe_uid"):
            con.execute(f"ALTER TABLE dl_meals ADD COLUMN {col} TEXT")
        # The stored history was saved without the uid fields. If Dinner Log already sends them, its ETag
        # still matches and every pull would get a 304, so the uids would never arrive: forget the ETag.
        con.execute("DELETE FROM meta WHERE key='dl_etag'")
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


def catalog_sig(con):
    """Changes whenever books or recipes do, so cooking history is matched again."""
    b = con.execute("SELECT count(*), coalesce(max(updated_at), '') FROM books").fetchone()
    r = con.execute("SELECT count(*), coalesce(max(id), 0) FROM recipes").fetchone()
    return f"b:{b[0]}:{b[1]}|r:{r[0]}:{r[1]}"


def meta_get(con, k):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the bearer token must never follow it to another address."""

    def redirect_request(self, *args, **kwargs):
        return None


# No environment proxies either: the token goes to DINNERLOG_API_URL and nowhere else.
def build_dl_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


_dl_opener = build_dl_opener()


def dl_open(req, timeout):
    return _dl_opener.open(req, timeout=timeout)


DL_MEAL_FIELDS = ("id", "day", "name", "source_type", "source_title", "source_detail", "rating",
                  "cookbook_book_id", "cookbook_recipe_id", "cookbook_book_uid", "cookbook_recipe_uid")


def store_snapshot(con, meals, etag, now=None):
    """Replace the stored history with `meals` (dicts as Dinner Log sends them) and record the pull."""
    con.execute("DELETE FROM dl_meals")
    con.executemany(f"INSERT INTO dl_meals({', '.join(DL_MEAL_FIELDS)}) VALUES ({','.join('?' * len(DL_MEAL_FIELDS))})",
                    [tuple(m.get(f) for f in DL_MEAL_FIELDS) for m in meals])
    meta_set(con, "dl_etag", etag or "")
    meta_set(con, "dl_gen", str(int(meta_get(con, "dl_gen") or 0) + 1))  # what rebuild_cooked compares
    _pulled(con, time.time() if now is None else now)


def _pulled(con, now):
    meta_set(con, "dl_pulled_at", f"{now:.3f}")
    con.execute("DELETE FROM meta WHERE key IN ('dl_error', 'dl_failed_at')")
    con.commit()


def _pull_failed(con, now, why):
    app.logger.warning("Dinner Log history pull failed: %s", why)
    meta_set(con, "dl_error", why)
    meta_set(con, "dl_failed_at", f"{now:.3f}")
    con.commit()
    return {"ok": False, "changed": False, "error": why}


def _valid_history(body):
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("meals"), list):
        return None
    meals = data["meals"]
    if not all(isinstance(m, dict) and type(m.get("id")) is int for m in meals):
        return None
    return meals


def pull_history(con, now=None):
    """Fetch Dinner Log's /api/history into dl_meals. Network only: rebuilding `cooked` is separate.
    Returns {"ok", "changed", "error"}; on any failure the stored history is left as it was."""
    now = time.time() if now is None else now
    if not DINNERLOG_API_URL:
        return {"ok": False, "changed": False, "error": "not configured"}
    if not DINNERLOG_API_URL.lower().startswith(("http://", "https://")):
        return _pull_failed(con, now, "DINNERLOG_API_URL must start with http:// or https://")
    headers = {"Authorization": f"Bearer {DINNERLOG_TOKEN}", "Accept": "application/json"}
    etag = meta_get(con, "dl_etag")
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(DINNERLOG_API_URL + "/api/history", headers=headers)
    try:
        with dl_open(req, PULL_TIMEOUT) as resp:
            body = resp.read(MAX_HISTORY_BYTES + 1)
            new_etag = resp.headers.get("ETag") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            _pulled(con, now)
            return {"ok": True, "changed": False, "error": None}
        return _pull_failed(con, now, f"Dinner Log answered HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", None) or e.__class__.__name__
        return _pull_failed(con, now, f"couldn't reach Dinner Log ({reason})")
    if len(body) > MAX_HISTORY_BYTES:
        return _pull_failed(con, now, "Dinner Log's history is larger than allowed")
    meals = _valid_history(body)
    if meals is None:
        return _pull_failed(con, now, "Dinner Log sent a history this version doesn't understand")
    store_snapshot(con, meals, new_etag, now)
    return {"ok": True, "changed": True, "error": None}


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


RESOLVE_LEVELS = ("manual", "recipe_uid", "book_uid", "int", "web", "text", "skipped")


def _uid(v):
    """A uid as Dinner Log stored it, or None if absent or malformed (so it can't shadow the ints)."""
    return v if isinstance(v, str) and core.UID_RE.fullmatch(v) else None


def rebuild_cooked(con, force=False):
    """Rebuild `cooked` from the stored Dinner Log history (dl_meals) and the catalog. Local only.

    Runs when a new history was stored or the catalog changed (or `force`), never before the first pull,
    so an upgrade keeps the old `cooked` until Dinner Log has answered. Per meal, the first that applies:

      manual      a link an editor made here (dl_link)
      recipe_uid  the recipe uid Dinner Log stamped
      book_uid    the book uid it stamped, then the page and name within that book
      int         the int recipe id, then the int book id (+ page/name), only for meals without any uid:
                  ints are rowids and can be reused after a delete, so a uid that no longer resolves must
                  not hand the meal to whatever now has its old int
      web         a link to a recipe saved from a website
      text        the cookbook title, page and name (fuzzy)
      skipped     not a cookbook, and no saved website matched

    How many meals each level resolved is logged and kept in meta `dl_resolved` (counts only, no rows)."""
    gen = meta_get(con, "dl_gen")
    if gen is None:
        return {"rebuilt": False, "meals": 0}
    sig = catalog_sig(con)
    with _sync_lock:
        if not force and meta_get(con, "dl_built_gen") == gen and meta_get(con, "dl_cat_sig") == sig:
            return {"rebuilt": False, "meals": 0}
        meals = con.execute("SELECT * FROM dl_meals ORDER BY id").fetchall()
        books = [(r[0], norm_title(r[1]), tokens(r[1]))
                 for r in con.execute("SELECT id, title FROM books WHERE kind IS NULL")]
        aliases = {r[0]: r[1] for r in con.execute("SELECT title_key, book_id FROM dl_alias")}
        links = {r[0]: (r[1], r[2]) for r in con.execute("SELECT meal_id, recipe_id, meal_key FROM dl_link")}
        recipe_book = dict(con.execute("SELECT id, book_id FROM recipes").fetchall())
        book_ids = {r[0] for r in con.execute("SELECT id FROM books")}
        recipe_by_uid = {r[0]: r[1] for r in con.execute("SELECT uid, id FROM recipes WHERE uid IS NOT NULL")}
        book_by_uid = {r[0]: r[1] for r in con.execute("SELECT uid, id FROM books WHERE uid IS NOT NULL")}
        rec_cache = {}

        def recipes_of(bid):
            if bid not in rec_cache:
                rec_cache[bid] = con.execute(
                    "SELECT id, name, heading, page_sort, page FROM recipes WHERE book_id=?", (bid,)).fetchall()
            return rec_cache[bid]

        import online
        to_insert = []
        counts = dict.fromkeys(RESOLVE_LEVELS, 0)
        uid_missed = 0
        for m in meals:
            title, detail, name = m["source_title"] or "", m["source_detail"] or "", m["name"] or ""
            r_uid, b_uid = _uid(m["cookbook_recipe_uid"]), _uid(m["cookbook_book_uid"])
            uid_rid = recipe_by_uid.get(r_uid) if r_uid else None
            uid_bid = book_by_uid.get(b_uid) if b_uid else None
            has_uid = bool(r_uid or b_uid)
            if has_uid and not (uid_rid or uid_bid):
                uid_missed += 1
            stamped_rid = None if has_uid else (m["cookbook_recipe_id"] if m["cookbook_recipe_id"] in recipe_book else None)
            stamped_bid = None if has_uid else (m["cookbook_book_id"] if m["cookbook_book_id"] in book_ids else None)
            link = links.get(m["id"])
            if link and link[1] not in (None, meal_key(m["day"], name)):
                link = None  # a different meal now has this id
            bid, rid, how = None, None, "none"
            if link:
                level = "manual"
                rid, how = link[0], "manual"
                bid = recipe_book.get(rid) if rid else (uid_bid or stamped_bid or match_book(title, books, aliases))
            elif uid_rid:
                level = "recipe_uid"
                bid, rid, how = recipe_book[uid_rid], uid_rid, "linked"
            elif uid_bid:
                level = "book_uid"
                bid = uid_bid
                rid = match_recipe(recipes_of(bid), detail, name)
                how = "auto" if rid else "book"
            elif stamped_rid:
                level = "int"
                bid, rid, how = recipe_book[stamped_rid], stamped_rid, "linked"
            elif stamped_bid:
                level = "int"
                bid = stamped_bid
                rid = match_recipe(recipes_of(bid), detail, name)
                how = "auto" if rid else "book"
            else:
                is_web = "://" in detail or "://" in title
                web_bid, web_rid = online.match_meal(con, detail, title) if is_web else (None, None)
                if web_rid:  # a link to a recipe saved from a website
                    level = "web"
                    bid, rid, how = web_bid, web_rid, "auto"
                elif m["source_type"] != "cookbook":
                    counts["skipped"] += 1
                    continue  # a website we haven't saved, or no source at all
                else:
                    level = "text"
                    bid = match_book(title, books, aliases)
                    if bid:
                        rid = match_recipe(recipes_of(bid), detail, name)
                        how = "auto" if rid else "book"
            counts[level] += 1
            to_insert.append((m["id"], m["day"], name, title, detail, m["rating"], bid, rid, how))
        con.execute("DELETE FROM cooked")
        con.executemany("INSERT INTO cooked VALUES (?,?,?,?,?,?,?,?,?)", to_insert)
        counts["uid_missed"] = uid_missed
        app.logger.info("Dinner Log history matched: %d meals; %s", len(meals),
                        " ".join(f"{k}={v}" for k, v in counts.items()))
        meta_set(con, "dl_resolved", json.dumps(counts, separators=(",", ":")))
        meta_set(con, "dl_built_gen", gen)
        meta_set(con, "dl_cat_sig", sig)
        meta_set(con, "dl_synced", time.strftime("%Y-%m-%d %H:%M:%S"))
        con.commit()
        return {"rebuilt": True, "meals": len(to_insert), "resolved": counts}


def dinnerlog_available(con):
    """Configured, pulled at least once, and the last pull worked."""
    return bool(DINNERLOG_API_URL) and meta_get(con, "dl_gen") is not None and meta_get(con, "dl_error") is None


def try_lease(con, now):
    """Claim the one Dinner Log pull allowed at a time, across workers. True if this caller got it."""
    cur = con.execute("UPDATE meta SET value=? WHERE key='dl_lease' AND CAST(value AS REAL) < ?",
                      (f"{now:.3f}", now - LEASE_TTL))
    con.commit()
    return cur.rowcount == 1


def release_lease(con):
    con.execute("UPDATE meta SET value='0' WHERE key='dl_lease'")
    con.commit()


def start_background(fn):
    threading.Thread(target=fn, daemon=True, name="dinnerlog-pull").start()


def background_pull():
    """Pull and rebuild on a connection of its own; the request that started it doesn't wait."""
    con = core.connect()
    try:
        pull_history(con)
        rebuild_cooked(con)
    except Exception as e:
        app.logger.exception("Dinner Log history pull stopped")
        try:
            con.rollback()
            _pull_failed(con, time.time(), f"pull stopped ({e.__class__.__name__})")
        except Exception:
            pass
    finally:
        try:
            release_lease(con)
        finally:
            con.close()


def pull_due(con, now):
    """Configured, the stored history is older than PULL_EVERY, and no failure within PULL_BACKOFF."""
    if not DINNERLOG_API_URL:
        return False
    pulled, failed = meta_get(con, "dl_pulled_at"), meta_get(con, "dl_failed_at")
    if failed is not None and now - float(failed) < PULL_BACKOFF:
        return False
    return pulled is None or now - float(pulled) > PULL_EVERY


def ensure_synced():
    """Once per request: rebuild `cooked` if the catalog or stored history changed (local, fast), and if
    the stored history is stale, start a background pull. Requests never wait on Dinner Log."""
    if getattr(g, "_dl_synced", False):
        return
    g._dl_synced = True
    con = db()
    rebuild_cooked(con)
    now = time.time()
    if pull_due(con, now) and try_lease(con, now):
        start_background(background_pull)


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
    force = request.args.get("force") == "1"
    if force or (DINNERLOG_API_URL and meta_get(con, "dl_gen") is None):  # nothing to show yet: fetch now
        pull_history(con)
    rebuild_cooked(con, force=force)
    pulled = meta_get(con, "dl_pulled_at")
    info = dict(source=DINNERLOG_API_URL or None, error=meta_get(con, "dl_error"), synced=meta_get(con, "dl_synced"),
                pulled=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(pulled))) if pulled else None)
    if not dinnerlog_available(con):
        return jsonify(available=False, **info)
    q = lambda sql: con.execute(sql).fetchone()[0]
    unmatched = [dict(r) for r in con.execute(
        "SELECT dl_title AS title, count(*) AS n, max(day) AS last FROM cooked WHERE book_id IS NULL "
        "AND trim(coalesce(dl_title,'')) <> '' GROUP BY fold(dl_title) ORDER BY n DESC")]
    resolved = meta_get(con, "dl_resolved")
    return jsonify(available=True, **info,
                   resolved=json.loads(resolved) if resolved else None,  # meals per resolution level
                   meals=q("SELECT count(*) FROM cooked"),
                   linked=q("SELECT count(*) FROM cooked WHERE how='linked'"),
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
    rebuild_cooked(con, force=True)
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
    rebuild_cooked(con, force=True)
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
    ensure_synced()
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
                "online": r["kind"] == "online", "starred": bool(r["want_to_try"]),
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
    ensure_synced()
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
  id TEXT PRIMARY KEY, book_id INTEGER, status TEXT,  -- running | done | error; scans add queued and scan (parent)
  rows TEXT, warning TEXT, error TEXT, created REAL,
  parent_id TEXT, page_no INTEGER, started REAL)""" # pages of a scan; NULL on single-page jobs (migrate() adds them)
PHOTO_JOB_LIMIT = 600  # seconds; a job still "running" after this died with its worker
PHOTO_QUEUE_LIMIT = 7200  # seconds a scan page may wait for a slot before it's presumed dead
PHOTO_JOBS_MAX = 3     # reading photos at once across the server; more get 429 and the browser retries
PHOTO_JOB_KEEP = 86400  # seconds before finished jobs are deleted; must cover "since local midnight" (pages_today)
PHOTO_DAILY_LIMIT = core.env_int("PHOTO_DAILY_LIMIT", 100)  # pages per day, all editors together; each is one model call


class ClaudeAPIError(Exception):
    """The Messages API answered with an HTTP error status."""
    def __init__(self, status, detail):
        super().__init__(f"Claude API error {status}: {detail}")
        self.status, self.detail = status, detail


def messages_api(body, key, timeout):
    """POST body to the Messages API and return the parsed JSON. Only HTTP errors become
    ClaudeAPIError; timeouts and connection errors propagate unchanged."""
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ClaudeAPIError(e.code, e.read().decode(errors="replace")[:300])


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
    try:
        data = messages_api(body, ANTHROPIC_API_KEY, PHOTO_JOB_LIMIT - 30)
    except ClaudeAPIError as e:
        if e.status == 400 and effort and ("effort" in e.detail or "output_config" in e.detail):
            return call_claude(image_jpeg, media_type, effort="")  # a model without effort settings
        raise RuntimeError(f"Claude API error {e.status}: {e.detail}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, data.get("stop_reason")


_ODD_PAGE_RE = re.compile(rf"^{core.ODD_PAGE}$")


def _photo_row(heading, name, page):
    name, page = clean(str(name or "")), clean(str(page or ""))
    if not name or not page:
        return None
    page = re.sub(rf"^{core.PAGE_PREFIX}", "", page)  # "p. 42" -> "42"
    row = {"heading": clean(str(heading or "")) or None, "name": name, "page": page}
    if not re.search(r"\d", page):
        if not _ODD_PAGE_RE.match(page):
            return None
        row["warn"] = "page"  # a roman numeral: kept for the review, flagged
        return row
    row["page"] = re.sub(r"\s*[-—]\s*", "–", re.sub(r"\s*,\s*", ", ", page))
    if _ODD_PAGE_RE.match(page):
        row["warn"] = "page"  # "12a"
    return row


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


OVERLAP_WINDOW = 60  # rows at the end of a page that the next photo can show again


def merge_pages(pages):
    """Rows of a whole-index scan, page by page in order (pages[i] is page i+1; an empty list is a page that
    failed or hasn't been read). Returns one list with page_no on every row, after:
    - carrying the last heading of a page into the headless rows at the top of the next page (a section
      continues across a page or column break, and the model can't see the previous page), marked carried;
    - dropping the leading run of rows on a page that repeat (folded name + page) rows near the end of
      the previous page: the photos overlapped. Same-name recipes elsewhere are kept."""
    out, prev = [], None  # prev: the previous page's rows, or None after a gap
    for i, rows in enumerate(pages):
        if not rows:
            prev = None
            continue
        carry = prev[-1]["heading"] if prev else None
        tail = {(fold(r["name"]), r["page"] or "") for r in prev[-OVERLAP_WINDOW:]} if prev else set()
        kept, overlapping = [], True
        for r in rows:
            r = {**r, "page_no": i + 1}
            overlapping = overlapping and (fold(r["name"]), r["page"] or "") in tail
            if overlapping:
                continue  # the leading run of repeats; a repeat further down is for mark_duplicates to flag
            if carry is not None:
                if r.get("heading") is None:
                    r["heading"], r["carried"] = carry, True
                else:
                    carry = None  # the leading headless run is over
            kept.append(r)
        out.extend(kept)
        prev = rows
    return out


def prepare_photo(raw):
    from PIL import ImageOps
    img = ImageOps.exif_transpose(core.open_image(raw)).convert("L")  # grayscale: text only, smaller
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
            con = core.connect()  # a scan page waited for this slot: now it's running, and the clock starts
            try:
                con.execute("UPDATE photo_jobs SET status='running', started=? WHERE id=? AND status='queued'",
                            (time.time(), job_id))
                con.commit()
            finally:
                con.close()
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
    con.commit()
    con.execute("BEGIN IMMEDIATE")  # one writer at a time, so the count and the insert agree across workers
    capped = daily_limit_reached(con, 1)
    if capped:
        con.rollback()
        return capped
    active = con.execute("SELECT count(*) FROM photo_jobs WHERE status='running' AND created > ?",
                         (time.time() - PHOTO_JOB_LIMIT,)).fetchone()[0]
    if active >= PHOTO_JOBS_MAX:
        con.rollback()
        resp = make_err(429, "Too many photos are being read right now. Trying again shortly.")
        resp.headers["Retry-After"] = "5"
        return resp
    con.execute("DELETE FROM photo_jobs WHERE created < ?", (time.time() - PHOTO_JOB_KEEP,))
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES (?,?,'running',?)",
                (job_id, book_id, time.time()))
    con.commit()
    threading.Thread(target=run_photo_job, args=(job_id, jpeg), daemon=True).start()
    return jsonify(job=job_id), 202


@app.get("/admin/api/index/photo/<job_id>")
@editor_required
def index_photo_job(job_id):
    j = db().execute("SELECT * FROM photo_jobs WHERE id=?", (job_id,)).fetchone()
    if not j or j["status"] == "scan":  # a whole-index scan is polled at /index/scan/<id>
        abort(404)
    elapsed = int(time.time() - j["created"])
    if j["status"] == "running":
        if elapsed > PHOTO_JOB_LIMIT:
            msg = "Reading that photo stopped partway (the server may have restarted). Try it again."
            db().execute("UPDATE photo_jobs SET status='error', error=? WHERE id=? AND status='running'", (msg, job_id))
            db().commit()
            return jsonify(status="error", error=msg)
        return jsonify(status="running", elapsed=elapsed)
    if j["status"] == "error":
        return jsonify(status="error", error=j["error"])
    return jsonify(status="done", warning=j["warning"], rows=core.mark_duplicates(j["book_id"], json.loads(j["rows"])))


# ---- whole-index scans: one parent job, one child job per page, merged for a single review
# The parent row (status 'scan') only ties the pages together; its state is derived from its children on every
# poll, so it's the same from either gunicorn worker. Pages wait for a _photo_sem slot in their own thread
# ('queued'), then run through the single-photo path above.
def pages_today(con):
    """Pages (single photos and scan pages) accepted since local midnight. Counted from photo_jobs itself,
    which the cleanup keeps for PHOTO_JOB_KEEP >= a day."""
    return con.execute("SELECT count(*) FROM photo_jobs WHERE status != 'scan' AND created >= ?",
                       (core.local_midnight(),)).fetchone()[0]


def daily_limit_reached(con, n):
    """A 429 response if accepting n more pages would pass PHOTO_DAILY_LIMIT, else None. No Retry-After:
    unlike the concurrency 429, waiting a few seconds won't help."""
    used = pages_today(con)
    if used + n <= PHOTO_DAILY_LIMIT:
        return None
    return make_err(429, f"Today's limit of {PHOTO_DAILY_LIMIT} pages is used up ({used} read so far"
                    f"{f', {n} more asked for' if n > 1 else ''}). It resets at midnight; PHOTO_DAILY_LIMIT in "
                    "/etc/conf.d/cookbooks raises it.", daily_limit=True)


def _scan_row(con, scan_id):
    j = con.execute("SELECT * FROM photo_jobs WHERE id=? AND status='scan'", (scan_id,)).fetchone()
    if not j:
        abort(404)
    return j


def _queue_pages(con, parent, files, page_nos):
    """Insert a child row per file (queued, or error if the image can't be read) and return the threads to
    start once the rows are committed: a worker finishing before its row exists would update nothing."""
    threads = []
    for f, page_no in zip(files, page_nos):
        job_id = secrets.token_urlsafe(12)
        try:
            jpeg = prepare_photo(f.read())
        except Exception as e:  # noqa: BLE001
            con.execute("INSERT INTO photo_jobs (id, book_id, status, error, created, parent_id, page_no) "
                        "VALUES (?,?,'error',?,?,?,?)",
                        (job_id, parent["book_id"], f"Couldn't read that image: {e}", time.time(), parent["id"], page_no))
            continue
        con.execute("INSERT INTO photo_jobs (id, book_id, status, created, parent_id, page_no) VALUES (?,?,'queued',?,?,?)",
                    (job_id, parent["book_id"], time.time(), parent["id"], page_no))
        threads.append(threading.Thread(target=run_photo_job, args=(job_id, jpeg), daemon=True))
    return threads


@app.post("/admin/api/books/<int:book_id>/index/scan")
@editor_required
def index_scan_start(book_id):
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    if not ANTHROPIC_API_KEY:
        return make_err(503, "Photo import needs ANTHROPIC_API_KEY in /etc/conf.d/cookbooks")
    scan_id = secrets.token_urlsafe(12)
    con.execute("DELETE FROM photo_jobs WHERE created < ?", (time.time() - PHOTO_JOB_KEEP,))
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES (?,?,'scan',?)", (scan_id, book_id, time.time()))
    con.commit()
    return jsonify(scan=scan_id), 202


@app.post("/admin/api/index/scan/<scan_id>/pages")
@editor_required
def index_scan_pages(scan_id):
    con = db()
    parent = _scan_row(con, scan_id)
    files = request.files.getlist("file")
    if not files:
        return make_err(400, "No photos")
    con.commit()
    con.execute("BEGIN IMMEDIATE")  # the daily count and the inserts agree across workers
    capped = daily_limit_reached(con, len(files))
    if capped:
        con.rollback()
        return capped
    first = (con.execute("SELECT coalesce(max(page_no), 0) FROM photo_jobs WHERE parent_id=?", (scan_id,)).fetchone()[0] or 0) + 1
    page_nos = list(range(first, first + len(files)))
    threads = _queue_pages(con, parent, files, page_nos)
    con.commit()
    for t in threads:
        t.start()
    return jsonify(pages=page_nos), 202


@app.put("/admin/api/index/scan/<scan_id>/pages/<int:page_no>")
@editor_required
def index_scan_retry(scan_id, page_no):
    """Read one page again from a new upload (the browser still has the photo). Replaces that page's row."""
    con = db()
    parent = _scan_row(con, scan_id)
    old = con.execute("SELECT * FROM photo_jobs WHERE parent_id=? AND page_no=?", (scan_id, page_no)).fetchone()
    if not old:
        abort(404)
    if old["status"] in ("queued", "running"):
        return make_err(409, "That page is still being read.")
    f = request.files.get("file")
    if not f:
        return make_err(400, "No photo")
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    con.execute("DELETE FROM photo_jobs WHERE id=?", (old["id"],))  # a retry replaces the page, so it isn't counted twice
    capped = daily_limit_reached(con, 1)
    if capped:
        con.rollback()
        return capped
    threads = _queue_pages(con, parent, [f], [page_no])
    con.commit()
    for t in threads:
        t.start()
    return jsonify(pages=[page_no]), 202


def scan_state(con, parent):
    """The parent's status, derived from its pages, with the merged rows of the pages read so far."""
    now = time.time()
    kids = con.execute("SELECT * FROM photo_jobs WHERE parent_id=? ORDER BY page_no", (parent["id"],)).fetchall()
    stale = [k["id"] for k in kids if (k["status"] == "running" and (k["started"] or k["created"]) < now - PHOTO_JOB_LIMIT)
             or (k["status"] == "queued" and k["created"] < now - PHOTO_QUEUE_LIMIT)]
    if stale:
        msg = "Reading that page stopped partway (the server may have restarted). Retry the page."
        con.executemany("UPDATE photo_jobs SET status='error', error=? WHERE id=? AND status IN ('running', 'queued')",
                        [(msg, i) for i in stale])
        con.commit()
        kids = con.execute("SELECT * FROM photo_jobs WHERE parent_id=? ORDER BY page_no", (parent["id"],)).fetchall()
    pages, by_no = [], {}
    for k in kids:
        rows = json.loads(k["rows"]) if k["status"] == "done" and k["rows"] else None
        by_no[k["page_no"]] = rows or []
        pages.append({"page_no": k["page_no"], "status": k["status"], "rows": len(rows) if rows is not None else None,
                      "error": k["error"], "warning": k["warning"],
                      "elapsed": int(now - (k["started"] or k["created"])) if k["status"] in ("queued", "running") else None})
    if not kids:
        status = "empty"
    elif any(k["status"] in ("queued", "running") for k in kids):
        status = "running"
    else:
        status = "done"
    merged = merge_pages([by_no.get(n, []) for n in range(1, max(by_no, default=0) + 1)])
    merged = core.mark_duplicates(parent["book_id"], merged)
    present = sum(1 for r in merged if r["duplicate"])
    return {"status": status, "book_id": parent["book_id"], "pages": pages, "rows": merged,
            "added": len(merged) - present, "present": present}


@app.get("/admin/api/index/scan/<scan_id>")
@editor_required
def index_scan_status(scan_id):
    con = db()
    return jsonify(scan_state(con, _scan_row(con, scan_id)))


@app.get("/admin/api/features")
@editor_required
def features():
    import ask  # local: ask imports extras
    return jsonify(ask=ask.enabled(), photo_import=bool(ANTHROPIC_API_KEY), dinnerlog=bool(DINNERLOG_API_URL),
                   dinnerlog_url=DINNERLOG_URL or None, wishlist=wishlist_available(),
                   mcp_private=bool(MCP_TOKEN), mcp_public=MCP_PUBLIC)


# ======================================================== MCP endpoint
MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_BATCH_MAX = 20  # JSON-RPC requests per POST; a batch is a convenience, not a bulk API
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

MCP_WEB_RECIPES = (" Some recipes come from recipe websites instead of a book: their book is the site (e.g. NYT Cooking) "
                   "and they have a url instead of a page, so give the link.")  # private view only


class ToolError(ValueError):
    """A tool error written for the model: shown as is, so it says what to change and never shows internals.
    Any other exception from a tool is a bug: it's logged, and the model gets a generic message."""


def _check_value(label, p, v):
    t = p.get("type")
    if t == "integer":
        if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
            v = int(v)  # models often send numbers as strings
        elif isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ToolError(f"{label} must be a whole number")
    elif t == "boolean":
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
            v = v.strip().lower() in ("true", "yes", "1")
        elif isinstance(v, int) and v in (0, 1):
            v = bool(v)
        if not isinstance(v, bool):
            raise ToolError(f"{label} must be true or false")
    elif t == "string":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str):
            raise ToolError(f"{label} must be a string")
    elif t == "array":
        if isinstance(v, str):
            v = [v]  # one tag for add_tags, one field for fields
        if not isinstance(v, list):
            raise ToolError(f"{label} must be a list")
        if p.get("items"):
            v = [_check_value(f"each item of {label}", p["items"], x) for x in v]
    if "enum" in p and t != "array" and v not in p["enum"]:
        raise ToolError(f"{label} must be one of: {', '.join(map(str, p['enum']))}")
    return v


def check_args(schema, args):
    """Check a tools/call's arguments against the tool's inputSchema before the tool runs, so a bad call gets
    a message the model can act on. Converts in place where models are often loose (numbers as strings, a
    single string for a list). Unknown arguments are ignored; limits are clamped by the tools themselves."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    props = schema.get("properties", {})
    for k in schema.get("required", []):
        if args.get(k) in (None, ""):
            raise ToolError(f"'{k}' is required")
    for k, v in list(args.items()):
        if k in props and v is not None:
            args[k] = _check_value(f"'{k}'", props[k], v)


def fill_tags(x, tags):
    """Put the ingredient tag list wherever "{tags}" appears in a tool's description or schema. Works on the
    decoded structure, so tag names need no escaping (a backslash or tab in one used to break tools/list)."""
    if isinstance(x, str):
        return x.replace("{tags}", tags)
    if isinstance(x, dict):
        return {k: fill_tags(v, tags) for k, v in x.items()}
    if isinstance(x, list):
        return [fill_tags(v, tags) for v in x]
    return x


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
        raise ToolError("query (or ingredient) is required")
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
            f"Tags: {ingredient_tag_summary(private)}.")
    return out


def ingredient_tag_names(hint=0, private=True):
    """Major ingredient tags (hint=0), or the search-hint ones (hint=1), most-used first."""
    pub = "" if private else f" AND {core.PUBLIC_BOOKS}"
    return [r[0] for r in db().execute(
        "SELECT i.name FROM ingredients i JOIN recipe_ingredients ri ON ri.ingredient_id=i.id "
        "JOIN recipes r ON r.id=ri.recipe_id JOIN books b ON b.id=r.book_id WHERE i.hint=?" + pub
        + " GROUP BY i.id ORDER BY count(*) DESC", (hint,))]


def ingredient_tag_summary(private=True):
    """'Eggs, Chicken, … plus search hints: Zucchini, Kale' for the MCP to know which tags exist."""
    major, hints = ", ".join(ingredient_tag_names(0, private)) or "none yet", ", ".join(ingredient_tag_names(1, private))
    return major + (f"; plus search hints: {hints}" if hints else "")


def t_search_books(a, private):
    sql, args = "SELECT b.* FROM books b WHERE 1=1", []
    if not private:
        sql += " AND b.status NOT IN ('gone') AND " + core.PUBLIC_BOOKS
    q = (a.get("query") or "").strip()
    if q:
        fts_words, like_words = core.search_words(q)
        fts_q = core.fts_query(" ".join(fts_words))
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


GET_BOOK_LIMIT, GET_BOOK_MAX = 200, 500  # recipes per get_book call: default, most


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
                hits = con.execute(
                    "SELECT b.* FROM books b WHERE b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)"
                    + pub + " ORDER BY b.title LIMIT 10", (fts_q,)).fetchall()
                if len(hits) == 1:
                    row = hits[0]
                elif len(hits) > 1:
                    choices = ", ".join(f"{h['title']} (book_id={h['id']})" for h in hits)
                    raise ToolError(f"Multiple books match '{a['title']}': {choices}. Call get_book with book_id.")
    else:
        raise ToolError("book_id or title is required")
    if not row or (not private and row["kind"]):
        raise ToolError("No such book")
    b = core.book_dicts([row], private)[0]
    # Paged: a well-indexed book has hundreds of recipes, and the whole list would go into the model's context.
    limit = max(1, min(int(a.get("limit") or GET_BOOK_LIMIT), GET_BOOK_MAX))
    offset = max(0, int(a.get("offset") or 0))
    recs = [dict(r) for r in con.execute(
        "SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort IS NULL, page_sort, name LIMIT ? OFFSET ?",
        (b["id"], limit, offset))]
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
    if offset:
        out["offset"] = offset
    if offset + len(recs) < (b.get("recipe_count") or 0):
        out["next_offset"] = offset + len(recs)  # more recipes: call again with this offset
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
        raise ToolError("No such recipe")
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
        raise ToolError("No such book")
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
           limit={"type": "integer", "minimum": 1, "maximum": 100, "default": 25,
                  "description": "Max results, default 25, at most 100"},
           offset={"type": "integer", "minimum": 0, "default": 0,
                   "description": "Skip this many results: pass next_offset from the previous "
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
        limit={"type": "integer", "minimum": 1, "maximum": 200, "default": 50,
               "description": "Max books, default 50, at most 200"})),
    ("get_book", False, t_get_book,
     "A cookbook's details and the recipes indexed from it, in page order. recipe_count is the total; up to "
     f"{GET_BOOK_LIMIT} recipes come back per call, and next_offset is present when there are more.",
     _S(book_id={"type": "integer"}, title={"type": "string"},
        limit={"type": "integer", "minimum": 1, "maximum": GET_BOOK_MAX, "default": GET_BOOK_LIMIT,
               "description": f"Max recipes, default {GET_BOOK_LIMIT}, at most {GET_BOOK_MAX}"},
        offset={"type": "integer", "minimum": 0, "default": 0,
                "description": "Skip this many recipes: pass next_offset from the previous call"})),
    ("list_categories", False, t_list_categories, "Cuisines and tags used in the collection, with book counts.", _S()),
    ("suggest_recipes", True, t_suggest,
     "Random dinner ideas from the collection. Modes: want (starred, never cooked), untried (never cooked), "
     "neglected (from books not cooked from in a year), favorites (rated 8+, not made in 3 months).",
     _S(mode={"type": "string", "enum": list(SUGGEST_MODES)}, cuisine={"type": "string"}, tag={"type": "string"},
        count={"type": "integer", "minimum": 1, "maximum": 20, "default": 5,
               "description": "How many ideas, default 5, at most 20"})),
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
WRITE_TOOLS = {
    "set_want_to_try": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "update_book": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
}
if app.logger.level == logging.NOTSET:  # Flask's default is WARNING, which would drop the INFO MCP write log
    app.logger.setLevel(logging.INFO)


MCP_TOKEN_KEY = "cookbooks.mcp_token"


class RedactMcpToken:
    """WSGI wrapper: takes the secret out of /mcp/<token> before anything logs the URL.

    The token moves into environ[MCP_TOKEN_KEY], PATH_INFO becomes /mcp, and RAW_URI / REQUEST_URI read
    /mcp/<redacted>. gunicorn's access log formats the request line from this same environ dict after the
    app returns, so the log never sees the secret. (Cloudflare's own logs still see the path; clients that
    can send an Authorization header should use `Bearer <MCP_TOKEN>` instead.)"""
    PREFIX = "/mcp/"

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        token = path[len(self.PREFIX):].rstrip("/") if path.startswith(self.PREFIX) else ""
        if token:
            environ[MCP_TOKEN_KEY] = token
            environ["PATH_INFO"] = "/mcp"
            for k in ("RAW_URI", "REQUEST_URI"):
                if k in environ:
                    q = environ[k].partition("?")[2]
                    environ[k] = "/mcp/<redacted>" + ("?" + q if q else "")
        try:
            return self.wsgi_app(environ, start_response)
        finally:
            # Flask has finished dispatching by now; drop the secret so no %({key}e)s log format can show it.
            environ.pop(MCP_TOKEN_KEY, None)


app.wsgi_app = RedactMcpToken(app.wsgi_app)


def mcp_auth(token):
    """Returns 'private', 'public' or None."""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    supplied = token or bearer
    if supplied:
        # compare bytes: compare_digest raises TypeError on non-ASCII str
        if MCP_TOKEN and hmac.compare_digest(supplied.encode(), MCP_TOKEN.encode()):
            return "private"
        return None
    return "public" if MCP_PUBLIC else None


def rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


class UnknownTool(LookupError):
    """No tool by that name is available to this caller (including private-only tools when not private)."""


def run_tool(name, args, private):
    """Run one MCP tool: (result, None) on success, (None, error_text) for a ToolError or a bug (redacted).
    Raises UnknownTool when no tool matches. Shared by handle_rpc and the Ask search."""
    tool = next((t for t in TOOLS if t[0] == name and (private or not t[1])), None)
    if not tool:
        raise UnknownTool(name)
    try:
        check_args(tool[4], args)
        res = tool[2](args, private)
    except ToolError as e:  # written for the model: says what to change
        return None, str(e)
    except Exception:  # noqa: BLE001 — a bug: log it, and keep exception text (SQL, internals) from the model
        ref = secrets.token_hex(4)
        app.logger.exception("MCP tool %s failed (ref %s), arguments %s", name, ref,
                             json.dumps(args, ensure_ascii=False)[:500])
        return None, (f"{name} failed with an internal error (ref {ref}). The same call will fail again: "
                      "try other arguments or another tool.")
    if name in WRITE_TOOLS:  # what the assistant changed, for the owner to review
        app.logger.info("MCP write %s %s -> %s", name, json.dumps(args, ensure_ascii=False)[:500],
                        json.dumps(res, ensure_ascii=False)[:1000])
    return res, None


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
                MCP_WEB_RECIPES if private else " (Public view: no ratings or cooking history.)"),
        })
    if method == "ping":
        return rpc_result(mid, {})
    if method == "tools/list":
        tags = ingredient_tag_summary(private)
        return rpc_result(mid, {"tools": [
            {"name": n, "description": fill_tags(d, tags), "inputSchema": fill_tags(s, tags),
             "annotations": WRITE_TOOLS.get(n, {"readOnlyHint": True, "openWorldHint": False})}
            for n, priv_only, _, d, s in TOOLS if private or not priv_only]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            res, err_text = run_tool(name, args, private)
        except UnknownTool:
            return rpc_error(mid, -32602, f"Unknown tool: {name}")
        if err_text is not None:
            return rpc_result(mid, {"content": [{"type": "text", "text": err_text}], "isError": True})
        return rpc_result(mid, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                                "structuredContent": res})
    return rpc_error(mid, -32601, f"Method not found: {method}")


@app.route("/mcp", methods=["GET", "POST", "DELETE"])
def mcp():
    token = request.environ.get(MCP_TOKEN_KEY)  # set by RedactMcpToken from /mcp/<token>
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
        if len(payload) > MCP_BATCH_MAX:
            return jsonify(rpc_error(None, -32600, f"Batch too large: at most {MCP_BATCH_MAX} requests")), 400
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


def wishlist_call(method, path, body=None, timeout=5):
    """Talk to the wish list app over localhost (it treats loopback requests as LAN editors)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(WISHLIST_URL + path, data=data, method=method, headers={
        "X-Wishlist": "1", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        raise RuntimeError(msg or f"wish list answered {e.code}")
    except OSError as e:
        raise RuntimeError(f"wish list isn't reachable at {WISHLIST_URL} ({e})")


def wishlist_available():
    """True only when the wish list app answers on WISHLIST_URL. The URL is set by default even when the app
    isn't installed, so checking the URL alone would show a button that always fails. A refused localhost
    connection returns at once, so checking on each features request is cheap."""
    if not WISHLIST_URL:
        return False
    try:
        wishlist_call("GET", "/api/people", timeout=1)
        return True
    except RuntimeError:
        return False


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
