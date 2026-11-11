"""Dinner Log link, registered onto the core app.

Pulls cooking history from Dinner Log's /api/history into dl_meals (and the photos it shares into dl_photos),
matches each dinner to a book and recipe (by the ids Dinner Log stamped, else title + page + name, with manual
overrides) and keeps the result in `cooked`. Requests never wait on Dinner Log: ensure_synced() starts a
background pull under a database lease.
"""
import difflib, json, os, re, threading, time, urllib.error, urllib.request

from flask import g, jsonify, request

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
DINNERLOG_URL = os.environ.get("DINNERLOG_URL", "").rstrip("/")  # public URL, for "Log this dinner" links


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
CREATE TABLE IF NOT EXISTS dl_photos (      -- photos Dinner Log shares, from the same pull (replaced with dl_meals)
  dl_file TEXT PRIMARY KEY,                 -- its file name: 32 hex + .jpg
  meal_id INTEGER NOT NULL);                -- dl_meals.id
INSERT OR IGNORE INTO meta(key, value) VALUES ('dl_lease', '0');
"""


def migrate(con):
    had_dl_photos = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dl_photos'").fetchone()
    con.executescript(MIGRATIONS)
    if not had_dl_photos:
        # The stored history predates the "photos" field. If Dinner Log already sends it, its ETag still
        # matches and every pull would be a 304, so the photo lists would never arrive: forget the ETag.
        con.execute("DELETE FROM meta WHERE key='dl_etag'")
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


DL_FILE_RE = re.compile(r"[0-9a-f]{32}\.jpg")  # Dinner Log's photo names; never a -t.jpg thumbnail


def store_snapshot(con, meals, etag, now=None):
    """Replace the stored history with `meals` (dicts as Dinner Log sends them) and record the pull.

    Each meal's "photos" (file names it shares with this catalog) go to dl_photos. Anything that isn't a list
    of Dinner Log photo names is ignored, not an error; a name listed twice keeps its first meal."""
    con.execute("DELETE FROM dl_meals")
    con.executemany(f"INSERT INTO dl_meals({', '.join(DL_MEAL_FIELDS)}) VALUES ({','.join('?' * len(DL_MEAL_FIELDS))})",
                    [tuple(m.get(f) for f in DL_MEAL_FIELDS) for m in meals])
    con.execute("DELETE FROM dl_photos")
    con.executemany("INSERT OR IGNORE INTO dl_photos(dl_file, meal_id) VALUES (?, ?)",
                    [(f, m["id"]) for m in meals if isinstance(m.get("photos"), list)
                     for f in m["photos"] if isinstance(f, str) and DL_FILE_RE.fullmatch(f)])
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


DL_PHOTO_TIMEOUT = 10
DL_PHOTO_MAX_BYTES = 10 * 1024 * 1024  # Dinner Log sends a 1600 px JPEG, well under this


class DLPhotoTooLarge(ValueError):
    pass


def fetch_dl_photo(name):
    """One shared photo from Dinner Log's GET /api/photos/<name>, as bytes; None on a 404 (not shared any more).
    Same rules as the history call: the bearer token, no redirects, no environment proxies. Other HTTP and
    network errors are raised; an oversized body is DLPhotoTooLarge. Decoding is the caller's (photos.store)."""
    if not DL_FILE_RE.fullmatch(name or ""):
        raise ValueError("not a Dinner Log photo name")
    req = urllib.request.Request(f"{DINNERLOG_API_URL}/api/photos/{name}",
                                 headers={"Authorization": f"Bearer {DINNERLOG_TOKEN}", "Accept": "image/jpeg"})
    try:
        with dl_open(req, DL_PHOTO_TIMEOUT) as resp:
            body = resp.read(DL_PHOTO_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if len(body) > DL_PHOTO_MAX_BYTES:
        raise DLPhotoTooLarge(f"larger than {DL_PHOTO_MAX_BYTES} bytes")
    return body


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
    """Pull and rebuild on a connection of its own; the request that started it doesn't wait. After a pull that
    worked (a 304 included), bring the photos Dinner Log shares in line with it (photos.sync_dl_photos), still
    under the lease. Never in rebuild_cooked, which also runs on the request path."""
    con = core.connect()
    try:
        pulled = pull_history(con)
        rebuild_cooked(con)
        if pulled.get("ok"):
            import photos
            try:
                photos.sync_dl_photos(con, fetch_dl_photo)
            except Exception:  # a photo problem isn't a failed pull: the history is fine, so no dl_error
                con.rollback()
                app.logger.exception("Dinner Log photo sync stopped")
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


# A recipe is "made" when a Dinner Log dinner is matched to it or an editor typed a date on it: the complement of
# cook.NEVER_MADE, which the Cook page's "Never made" mode uses (tests/test_insights.py checks they agree).
MADE_RECIPE = "(r.last_made IS NOT NULL OR EXISTS (SELECT 1 FROM cooked c WHERE c.recipe_id=r.id))"


def made_by_book(ids):
    """{book id: how many of its recipes have been made}, for the editor's "made 12 of 140". Editors only: the
    public site never sends cooking history beyond PUBLIC_RECIPE_COOKED."""
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in db().execute(
        f"SELECT r.book_id, count(*) FROM recipes r WHERE r.book_id IN ({qs}) AND {MADE_RECIPE} "
        "GROUP BY r.book_id", list(ids))}


def cooked_stats_for(ids):
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    return {r["recipe_id"]: {"cooked_count": r["n"], "cooked_avg": r["avg"], "cooked_last": r["last"]}
            for r in db().execute(f"SELECT recipe_id, count(*) n, round(avg(rating),1) avg, max(day) last FROM cooked "
                                  f"WHERE recipe_id IN ({qs}) GROUP BY recipe_id", ids)}


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
        import changes
        rec = changes.Recorder(con)
        rec.created("recipe", rid)
        rec.finish()  # before rebuild_cooked, which commits
    if rid is None and not d.get("unlink"):
        con.execute("DELETE FROM dl_link WHERE meal_id=?", (meal_id,))  # back to automatic
    else:
        row = con.execute("SELECT day, meal_name FROM cooked WHERE meal_id=?", (meal_id,)).fetchone()
        key = meal_key(row[0], row[1]) if row else None
        con.execute("INSERT INTO dl_link VALUES (?,?,?) ON CONFLICT(meal_id) DO UPDATE SET recipe_id=excluded.recipe_id, "
                    "meal_key=excluded.meal_key", (meal_id, int(rid) if rid else None, key))
    rebuild_cooked(con, force=True)
    return jsonify(ok=True, recipe_id=rid)
