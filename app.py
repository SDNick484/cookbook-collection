"""Cookbook Collection — the family cookbook catalog.  Flask + SQLite.

Public:  /            browse/search books and recipe names (read-only)
         /api/...     public JSON (no private fields)
Editors: /admin       same UI with editing; put this path behind Cloudflare Access
         /admin/api/  full JSON + writes; requires a verified Access JWT
                      (or a direct LAN request when LAN_EDIT=1)

Environment:
  DATA_DIR        database + covers (default ./data)
  CF_TEAM_DOMAIN  e.g. yourteam.cloudflareaccess.com
  CF_AUD          Access application AUD tag
  ADMIN_EMAILS    optional comma list; if set, only these emails may edit
  LAN_EDIT        1 (default) = requests that did not come through Cloudflare may edit
"""
import csv, io, json, os, re, sqlite3, time, unicodedata, urllib.parse, urllib.request, uuid
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory, Response

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
COVER_DIR = DATA_DIR / "covers"
DB_PATH = DATA_DIR / "cookbooks.db"
CF_TEAM_DOMAIN = re.sub(r"^https?://", "", os.environ.get("CF_TEAM_DOMAIN", "").strip().strip("'\"")).split("/")[0]
CF_AUD = os.environ.get("CF_AUD", "").strip().strip("'\"")
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
LAN_EDIT = os.environ.get("LAN_EDIT", "1") == "1"
UA = "cookbooks-catalog/1.0 (personal cookbook collection)"
COVER_HOSTS = {"covers.openlibrary.org", "archive.org", "books.google.com", "books.googleusercontent.com"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

# --------------------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL, subtitle TEXT, isbn TEXT, publisher TEXT, year INTEGER,
  cuisine TEXT, description TEXT,
  location TEXT, status TEXT NOT NULL DEFAULT 'owned', loaned_to TEXT,
  signed INTEGER NOT NULL DEFAULT 0, private_notes TEXT, needs_review TEXT,
  cover TEXT, legacy_category TEXT, source TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS books_isbn ON books(isbn) WHERE isbn IS NOT NULL AND isbn <> '';
CREATE TABLE IF NOT EXISTS authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS book_authors (
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  author_id INTEGER NOT NULL REFERENCES authors(id), pos INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (book_id, author_id));
CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS book_tags (
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id), PRIMARY KEY (book_id, tag_id));
CREATE TABLE IF NOT EXISTS recipes (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  name TEXT NOT NULL, heading TEXT, page TEXT, page_sort INTEGER,
  want_to_try INTEGER NOT NULL DEFAULT 0, rating REAL, notes TEXT, last_made TEXT,
  source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS recipes_book ON recipes(book_id, page_sort);
"""
# Both indexes hold fold()ed text, so "oeufs" finds "Œufs" and "strasse" finds "Straße". The triggers call the
# fold() SQL function that connect() registers, so recipes should only be written through this app.
FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(name, heading, tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS recipes_fts_ai AFTER INSERT ON recipes BEGIN
  INSERT INTO recipes_fts(rowid, name, heading) VALUES (new.id, fold(new.name), fold(new.heading));
END;
CREATE TRIGGER IF NOT EXISTS recipes_fts_ad AFTER DELETE ON recipes BEGIN
  DELETE FROM recipes_fts WHERE rowid=old.id;
END;
CREATE TRIGGER IF NOT EXISTS recipes_fts_au AFTER UPDATE OF name, heading ON recipes BEGIN
  DELETE FROM recipes_fts WHERE rowid=old.id;
  INSERT INTO recipes_fts(rowid, name, heading) VALUES (new.id, fold(new.name), fold(new.heading));
END;
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
  title, people, other, tokenize='unicode61 remove_diacritics 2');
"""
HAS_FTS = True


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.create_function("fold", 1, fold, deterministic=True)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db():
    global HAS_FTS
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    con.executescript(SCHEMA)
    import extras, eyb, ingredients
    extras.migrate(con)
    eyb.migrate(con)
    ingredients.migrate(con)
    merged = eyb.merge_garbled(con)  # recipes imported with "�" for lost letters, merged into their good copy
    if merged:  # shown on Needs review
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('garbled_merged', ?)",
                    (json.dumps({"n": merged, "at": time.strftime("%Y-%m-%d %H:%M")}),))
    old = con.execute("SELECT sql FROM sqlite_master WHERE name='recipes_fts'").fetchone()
    if old and "content=" in old[0]:  # the old index read unfolded text straight from recipes; replace it
        con.executescript("DROP TRIGGER IF EXISTS recipes_ai; DROP TRIGGER IF EXISTS recipes_ad; "
                          "DROP TRIGGER IF EXISTS recipes_au; DROP TABLE recipes_fts;")
    try:
        con.executescript(FTS)
    except sqlite3.OperationalError:
        HAS_FTS = False  # sqlite built without FTS5: search falls back to LIKE
    empty = con.execute("SELECT count(*) FROM books").fetchone()[0] == 0
    seed = APP_DIR / "seed.json"
    if empty and seed.exists():
        load_seed(con, json.loads(seed.read_text()))
    if HAS_FTS:  # rebuilt on every start, so a change to fold() reaches the whole catalog
        reindex_all_books(con)
        con.execute("DELETE FROM recipes_fts")
        con.execute("INSERT INTO recipes_fts(rowid, name, heading) SELECT id, fold(name), fold(heading) FROM recipes")
    con.commit()
    con.close()


def load_seed(con, data):
    ids = {}
    for b in data["books"]:
        cur = con.execute(
            "INSERT INTO books(title, publisher, cuisine, needs_review, legacy_category, source) VALUES (?,?,?,?,?,?)",
            (b["title"], b.get("publisher"), b.get("cuisine"), b.get("needs_review"), b.get("legacy_category"), b.get("source")))
        ids[b["title"]] = cur.lastrowid
        set_people(con, cur.lastrowid, b.get("authors", []), b.get("tags", []))
    for r in data["recipes"]:
        con.execute(
            "INSERT INTO recipes(book_id, name, page, page_sort, want_to_try, rating, notes, last_made, source)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ids[r["book"]], r["name"], r.get("page"), page_sort(r.get("page")), r.get("want_to_try", 0),
             r.get("rating"), r.get("notes"), r.get("last_made"), r.get("source")))


# ------------------------------------------------------------------- helpers
def page_sort(page):
    m = re.search(r"\d+", page or "")
    return int(m.group()) if m else None


def clean(s):
    """Tidy text before it's stored: composed accents (NFC, so "é" typed on a Mac matches "é" from a CSV),
    and the ﬁ/ﬂ ligatures that PDFs and photographed indexes produce spelled out."""
    s = unicodedata.normalize("NFC", s or "").translate(LIGATURES).strip()
    return s or None


def name_id(con, table, name):
    """id of the author, tag or ingredient called `name`, adding it if new. Case and accents don't count,
    so "Jose Andres" from an EYB export joins "José Andrés"; an accented spelling replaces a plain one."""
    row = con.execute(f"SELECT id, name FROM {table} WHERE fold(name)=fold(?) ORDER BY id LIMIT 1", (name,)).fetchone()
    if not row:
        return con.execute(f"INSERT INTO {table}(name) VALUES (?)", (name,)).lastrowid
    if row[1] != name and row[1].isascii() and not name.isascii():
        try:
            con.execute(f"UPDATE {table} SET name=? WHERE id=?", (name, row[0]))
        except sqlite3.IntegrityError:
            pass
    return row[0]


def set_people(con, book_id, authors, tags):
    con.execute("DELETE FROM book_authors WHERE book_id=?", (book_id,))
    for i, name in enumerate(dict.fromkeys(filter(None, map(clean, authors)))):
        aid = name_id(con, "authors", name)
        con.execute("INSERT OR IGNORE INTO book_authors VALUES (?,?,?)", (book_id, aid, i))
    con.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
    for name in dict.fromkeys(filter(None, map(clean, tags))):
        tid = name_id(con, "tags", name)
        con.execute("INSERT OR IGNORE INTO book_tags VALUES (?,?)", (book_id, tid))


def reindex_book(con, book_id):
    if not HAS_FTS:
        return
    con.execute("DELETE FROM books_fts WHERE rowid=?", (book_id,))
    b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not b:
        return
    people = " ".join(r[0] for r in con.execute(
        "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=?", (book_id,)))
    tags = " ".join(r[0] for r in con.execute(
        "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=?", (book_id,)))
    other = " ".join(x for x in (b["subtitle"], b["publisher"], b["cuisine"], tags, b["isbn"]) if x)
    con.execute("INSERT INTO books_fts(rowid, title, people, other) VALUES (?,?,?,?)",
                (book_id, fold(b["title"]), fold(people), fold(other)))


def reindex_all_books(con):
    con.execute("DELETE FROM books_fts")
    for (bid,) in con.execute("SELECT id FROM books").fetchall():
        reindex_book(con, bid)


def fts_query(q):
    """Prefix match for every word, folded the same way as the index."""
    return " ".join(f'"{t}"*' for t in re.findall(r"\w+", fold(q)))


# Letters that aren't an accent on a plain letter, so stripping accents leaves them alone.
FOLD_EXTRA = str.maketrans({"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i",
                            "ħ": "h", "ŧ": "t", "ŋ": "n"})
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
# Scripts written without spaces between words: a word search can't find text in the middle of a run.
UNSPACED = re.compile(r"[\u0e00-\u0eff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def fold(s):
    """Search and matching key: "Crème Brûlée" -> "creme brulee", "Œufs" -> "oeufs", "Straße" -> "strasse"."""
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c)).translate(FOLD_EXTRA)


def search_words(q):
    """Split a search into (words for the full-text index, runs of Chinese/Japanese/Korean/Thai text). Those
    scripts don't put spaces between words, so they're matched as substrings rather than word prefixes."""
    words, runs = [], []
    for t in re.findall(r"\w+", unicodedata.normalize("NFKC", q or "")):
        (runs if UNSPACED.search(t) else words).append(t)
    return words, runs


PUBLIC_BOOK = ("id", "title", "subtitle", "isbn", "publisher", "year", "cuisine", "description", "cover", "signed", "format")
PUBLIC_RECIPE = ("id", "book_id", "name", "heading", "page")


def book_dicts(rows, private):
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    qs = ",".join("?" * len(ids))
    authors, tags, counts = {}, {}, {}
    for r in db().execute(f"SELECT ba.book_id, a.name FROM book_authors ba JOIN authors a ON a.id=ba.author_id "
                          f"WHERE ba.book_id IN ({qs}) ORDER BY ba.pos", ids):
        authors.setdefault(r[0], []).append(r[1])
    for r in db().execute(f"SELECT bt.book_id, t.name FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
                          f"WHERE bt.book_id IN ({qs}) ORDER BY t.name", ids):
        tags.setdefault(r[0], []).append(r[1])
    for r in db().execute(f"SELECT book_id, count(*), sum(want_to_try) FROM recipes WHERE book_id IN ({qs}) "
                          f"GROUP BY book_id", ids):
        counts[r[0]] = (r[1], r[2] or 0)
    out = []
    for r in rows:
        d = dict(r) if private else {k: r[k] for k in PUBLIC_BOOK}
        d["authors"] = authors.get(r["id"], [])
        d["tags"] = tags.get(r["id"], [])
        d["recipe_count"] = counts.get(r["id"], (0, 0))[0]
        if private:
            d["want_count"] = counts.get(r["id"], (0, 0))[1]
        out.append(d)
    return out


def recipe_dict(r, private):
    return dict(r) if private else {k: r[k] for k in PUBLIC_RECIPE + (("book_title",) if "book_title" in r.keys() else ())}


# ---------------------------------------------------------------------- auth
_jwks = None


def access_identity():
    """Return the editor's email, or None (with the reason in g.auth_reason)."""
    g.auth_reason = None
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    via_cloudflare = request.headers.get("Cf-Ray") or request.headers.get("Cf-Connecting-Ip")
    if token:
        if not (CF_TEAM_DOMAIN and CF_AUD):
            g.auth_reason = ("Cloudflare Access signed you in, but the server isn't configured to check it yet: "
                             "set CF_TEAM_DOMAIN and CF_AUD in /etc/conf.d/cookbooks, then rc-service cookbooks restart.")
            return None
        global _jwks
        import jwt
        try:
            if _jwks is None:
                _jwks = jwt.PyJWKClient(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs", cache_keys=True)
            key = _jwks.get_signing_key_from_jwt(token).key
        except Exception as e:  # noqa: BLE001
            _jwks = None
            g.auth_reason = (f"Couldn't fetch Cloudflare's signing keys from https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs "
                             f"({e}). Check CF_TEAM_DOMAIN (e.g. yourteam.cloudflareaccess.com).")
            app.logger.warning("Access: %s", g.auth_reason)
            return None
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD,
                                issuer=f"https://{CF_TEAM_DOMAIN}")
        except Exception as e:  # noqa: BLE001 — any failure means "not an editor"
            hint = ""
            if "audience" in str(e).lower():
                try:  # show which Access app issued the token (unverified, display only)
                    got = jwt.decode(token, options={"verify_signature": False}).get("aud")
                    got = ", ".join(got) if isinstance(got, list) else got
                except Exception:  # noqa: BLE001
                    got = None
                hint = (f" The sign-in came from the Access application with AUD {got}; CF_AUD is set to {CF_AUD}."
                        if got else "") + (" CF_AUD must be the AUD tag of the Access application that protects "
                                           "this site's /admin path.")
            elif "issuer" in str(e).lower():
                hint = " CF_TEAM_DOMAIN must match your Zero Trust team domain."
            g.auth_reason = f"Cloudflare Access token rejected: {e}.{hint}"
            app.logger.warning("Access: %s", g.auth_reason)
            return None
        email = (claims.get("email") or "").lower()
        if ADMIN_EMAILS and email not in ADMIN_EMAILS:
            g.auth_reason = f"Signed in as {email or 'an unknown user'}, which isn't in ADMIN_EMAILS."
            return None
        return email or "access-user"
    if LAN_EDIT and not via_cloudflare:
        return "lan"
    g.auth_reason = ("No Cloudflare Access sign-in reached the app. Check that the Access application for "
                     "this site covers the path 'admin'." if via_cloudflare else
                     "Editing from the LAN is turned off (LAN_EDIT=0).")
    return None


def editor_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        who = access_identity()
        if not who:
            return jsonify(error="Sign in to edit", reason=g.auth_reason), 401
        g.editor = who
        return fn(*a, **kw)
    return inner


# ------------------------------------------------------------------- pages
@app.get("/")
@app.get("/admin")
@app.get("/admin/")
def index():
    return send_from_directory(APP_DIR / "static", "index.html", max_age=0)


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory(APP_DIR / "static", p, max_age=300)


@app.get("/covers/<path:p>")
def covers(p):
    return send_from_directory(COVER_DIR, p, max_age=86400)


@app.get("/healthz")
def healthz():
    return {"ok": True, "fts": HAS_FTS}


# ------------------------------------------------------------- read API
def list_books(private):
    q = request.args.get("q", "").strip()
    sql, args = "SELECT b.* FROM books b WHERE 1=1", []
    words, runs = search_words(q)
    if words:
        if HAS_FTS:
            sql += " AND b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)"
            args.append(fts_query(" ".join(words)))
        else:
            sql += " AND fold(b.title) LIKE ?"
            args.append(f"%{fold(' '.join(words))}%")
    for t in runs:
        sql += " AND instr(fold(b.title || ' ' || coalesce(b.subtitle, '')), ?) > 0"
        args.append(fold(t))
    if request.args.get("cuisine"):
        sql += " AND b.cuisine = ?"
        args.append(request.args["cuisine"])
    if request.args.get("tag"):
        sql += " AND b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)"
        args.append(request.args["tag"])
    if request.args.get("author"):
        sql += " AND b.id IN (SELECT ba.book_id FROM book_authors ba JOIN authors a ON a.id=ba.author_id WHERE a.name=?)"
        args.append(request.args["author"])
    if request.args.get("publisher"):
        sql += " AND fold(b.publisher) = fold(?)"
        args.append(request.args["publisher"])
    if request.args.get("ingredient"):
        import ingredients
        cond, a = ingredients.ingredient_condition(db(), request.args["ingredient"])
        sql += f" AND b.id IN (SELECT r.book_id FROM recipes r WHERE {cond})"
        args += a
    if private and request.args.get("status"):
        sql += " AND b.status = ?"
        args.append(request.args["status"])
    if private and request.args.get("review"):
        sql += " AND (b.needs_review IS NOT NULL OR b.isbn IS NULL OR b.cover IS NULL)"
    sort = request.args.get("sort", "author")
    rows = db().execute(sql, args).fetchall()
    books = book_dicts(rows, private)
    if private:
        import extras
        extras.ensure_synced()
        cb = extras.cooked_by_book([b["id"] for b in books])
        for b in books:
            b.update(cb.get(b["id"], {}))
    if sort == "title":
        books.sort(key=lambda b: fold(re.sub(r"^(the|a|an)\s+", "", b["title"], flags=re.I)))
    elif sort == "cooked" and private:
        books.sort(key=lambda b: (-(b.get("cooked_count") or 0), fold(b["title"])))
    elif sort == "rating" and private:  # unrated books last
        books.sort(key=lambda b: (b["rating"] is None, -(b["rating"] or 0), fold(b["title"])))
    elif sort == "added":
        books.sort(key=lambda b: -b["id"])
    else:
        def sname(b):
            a = b["authors"][0] if b["authors"] else "~"
            return fold(a.split()[-1] if " " in a else a), fold(b["title"])
        books.sort(key=sname)
    return jsonify(books=books, total=db().execute("SELECT count(*) FROM books").fetchone()[0])


def get_book(book_id, private):
    row = db().execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    book = book_dicts([row], private)[0]
    recs = db().execute("SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort IS NULL, page_sort, name",
                        (book_id,)).fetchall()
    book["recipes"] = [recipe_dict(r, private) for r in recs]
    if private:
        import extras
        extras.ensure_synced()
        cr = extras.cooked_by_recipe(book_id)
        for r in book["recipes"]:
            r.update(cr.get(r["id"], {}))
        book.update(extras.cooked_by_book([book_id]).get(book_id, {}))
        book["wishes"] = extras.wishes_for_book(book_id)
    return jsonify(book)


def recipe_filter(params, private):
    """WHERE clause + args for recipe search. Each word of q must match the recipe's name/index heading or
    one of its ingredient tags; ingredient= limits to recipes that use it (see ingredients.ingredient_condition)."""
    import ingredients
    where, args = [], []
    q = (params.get("q") or "").strip()
    words, runs = search_words(q)
    for tok in (words + runs)[:8]:
        sql, a = ingredients.token_condition(tok)
        where.append(sql)
        args += a
    if params.get("ingredient"):  # tagged with it, or it's in the title
        cond, a = ingredients.ingredient_condition(db(), params["ingredient"])
        where.append(cond)
        args += a
    if params.get("book"):
        where.append("r.book_id=?")
        args.append(int(params["book"]))
    if params.get("cuisine"):
        where.append("b.cuisine=?")
        args.append(params["cuisine"])
    if params.get("tag"):
        where.append("b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)")
        args.append(params["tag"])
    if params.get("publisher"):
        where.append("fold(b.publisher) = fold(?)")
        args.append(params["publisher"])
    if private and params.get("want"):
        where.append("r.want_to_try=1")
    if private and params.get("made"):
        where.append("(r.last_made IS NOT NULL OR r.rating IS NOT NULL)")
    return where, args


def search_recipes(private):
    p = request.args
    limit = max(1, min(int(p.get("limit", 100)), 500))
    offset = max(0, int(p.get("offset", 0)))
    if not (p.get("q", "").strip() or p.get("ingredient") or p.get("book")
            or (private and (p.get("want") or p.get("made")))):
        return jsonify(recipes=[], count=0)
    where, args = recipe_filter(p, private)
    sql = ("SELECT r.*, b.title AS book_title FROM recipes r JOIN books b ON b.id=r.book_id WHERE 1=1"
           + "".join(" AND " + w for w in where))
    count = db().execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]
    sql += " ORDER BY b.title, r.page_sort IS NULL, r.page_sort, r.name LIMIT ? OFFSET ?"
    rows = db().execute(sql, args + [limit, offset]).fetchall()
    return jsonify(recipes=[recipe_dict(r, private) for r in rows], count=count)


def recipe_books(private):
    """Each book with matching recipes and how many match: the list shown when an ingredient is picked.
    Takes the same filters as search_recipes."""
    p = request.args
    if not (p.get("q", "").strip() or p.get("ingredient")):
        return jsonify(books=[], count=0)
    where, args = recipe_filter(p, private)
    rows = db().execute("SELECT b.id, b.title, count(*) AS n FROM recipes r JOIN books b ON b.id=r.book_id WHERE 1=1"
                        + "".join(" AND " + w for w in where) + " GROUP BY b.id", args).fetchall()
    books = sorted((dict(r) for r in rows), key=lambda b: fold(re.sub(r"^(the|a|an)\s+", "", b["title"], flags=re.I)))
    return jsonify(books=books, count=sum(b["n"] for b in books))


def facets(private):
    d = db()
    by_name = lambda rows: sorted((dict(r) for r in rows), key=lambda x: fold(x["name"]))  # É with E, not after Z
    return jsonify(
        cuisines=by_name(d.execute(
            "SELECT cuisine AS name, count(*) AS n FROM books WHERE cuisine IS NOT NULL GROUP BY cuisine")),
        tags=by_name(d.execute(
            "SELECT t.name, count(*) AS n FROM tags t JOIN book_tags bt ON bt.tag_id=t.id GROUP BY t.id")),
        authors=by_name(d.execute(
            "SELECT a.name, count(*) AS n FROM authors a JOIN book_authors ba ON ba.author_id=a.id GROUP BY a.id")),
        publishers=sorted((r[0] for r in d.execute(
            "SELECT DISTINCT publisher FROM books WHERE publisher IS NOT NULL")), key=fold) if private else [],
        stats={"books": d.execute("SELECT count(*) FROM books").fetchone()[0],
               "recipes": d.execute("SELECT count(*) FROM recipes").fetchone()[0]},
    )


for prefix, private in (("/api", False), ("/admin/api", True)):
    def _wrap(fn, private=private):
        f = (lambda *a, **kw: fn(*a, private=private, **kw))
        return editor_required(f) if private else f
    app.add_url_rule(f"{prefix}/books", f"books_{private}", _wrap(list_books))
    app.add_url_rule(f"{prefix}/books/<int:book_id>", f"book_{private}", _wrap(get_book))
    app.add_url_rule(f"{prefix}/recipes", f"recipes_{private}", _wrap(search_recipes))
    app.add_url_rule(f"{prefix}/recipes/books", f"recipe_books_{private}", _wrap(recipe_books))
    app.add_url_rule(f"{prefix}/facets", f"facets_{private}", _wrap(facets))


@app.get("/api/me")
def me_public():
    return jsonify(editor=bool(access_identity()))


@app.get("/admin/api/me")
@editor_required
def me():
    return jsonify(editor=True, email=g.editor)


# ------------------------------------------------------------- write API
BOOK_FIELDS = ("title", "subtitle", "isbn", "publisher", "year", "cuisine", "format", "description", "location",
               "status", "loaned_to", "signed", "private_notes", "needs_review", "rating")


def book_values(data):
    vals = {}
    for k in BOOK_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "year":
            v = int(v) if str(v or "").strip().isdigit() else None
        elif k == "signed":
            v = 1 if v else 0
        elif k == "isbn":
            v = normalize_isbn(v) if v else None
            if data[k] and not v:
                abort(make_err(400, "That doesn't look like a valid ISBN"))
        elif k == "status":
            v = v if v in ("owned", "out", "loaned", "gone", "wanted") else "owned"
        elif k == "rating":  # overall book rating: 0–5 stars in half stars, None = unrated
            try:
                v = float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                abort(make_err(400, "Rating must be a number"))
            if v is not None and not (0 <= v <= 5 and (v * 2).is_integer()):
                abort(make_err(400, "Rating is 0–5 stars, in half stars"))
        else:
            v = clean(v if isinstance(v, str) else (str(v) if v is not None else None))
        vals[k] = v
    return vals


def make_err(code, msg, **extra):
    r = jsonify(error=msg, **extra)
    r.status_code = code
    return r


def isbn_conflict(isbn, exclude=None):
    if not isbn:
        return None
    row = db().execute("SELECT id, title FROM books WHERE isbn=? AND id IS NOT ?", (isbn, exclude)).fetchone()
    return row


@app.post("/admin/api/books")
@editor_required
def create_book():
    data = request.get_json(force=True)
    vals = book_values(data)
    if not vals.get("title"):
        return make_err(400, "Title is required")
    dup = isbn_conflict(vals.get("isbn"))
    if dup:
        return make_err(409, f"Already in the catalog: {dup['title']}", book_id=dup["id"])
    con = db()
    cols = ",".join(vals)
    cur = con.execute(f"INSERT INTO books({cols}, source) VALUES ({','.join('?' * len(vals))}, 'app')",
                      list(vals.values()))
    bid = cur.lastrowid
    set_people(con, bid, data.get("authors", []), data.get("tags", []))
    if data.get("cover_url"):
        try:
            save_cover_from_url(bid, data["cover_url"])
        except Exception as e:  # noqa: BLE001 — a missing cover shouldn't block the save
            app.logger.warning("cover fetch failed: %s", e)
    reindex_book(con, bid)
    con.commit()
    return get_book(bid, private=True), 201


@app.put("/admin/api/books/<int:book_id>")
@editor_required
def update_book(book_id):
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    data = request.get_json(force=True)
    vals = book_values(data)
    dup = isbn_conflict(vals.get("isbn"), exclude=book_id)
    if dup:
        return make_err(409, f"Another book already has that ISBN: {dup['title']}", book_id=dup["id"])
    if vals:
        sets = ",".join(f"{k}=?" for k in vals)
        con.execute(f"UPDATE books SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", [*vals.values(), book_id])
    if "authors" in data or "tags" in data:
        cur_auth = [r[0] for r in con.execute("SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
                                              "WHERE ba.book_id=? ORDER BY pos", (book_id,))]
        cur_tags = [r[0] for r in con.execute("SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
                                              "WHERE bt.book_id=?", (book_id,))]
        set_people(con, book_id, data.get("authors", cur_auth), data.get("tags", cur_tags))
    if data.get("cover_url"):
        try:
            save_cover_from_url(book_id, data["cover_url"])
        except Exception as e:  # noqa: BLE001
            return make_err(502, f"Saved, but couldn't fetch that cover: {e}")
    reindex_book(con, book_id)
    con.commit()
    return get_book(book_id, private=True)


@app.delete("/admin/api/books/<int:book_id>")
@editor_required
def delete_book(book_id):
    con = db()
    row = con.execute("SELECT cover FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    con.execute("DELETE FROM books WHERE id=?", (book_id,))
    if HAS_FTS:
        con.execute("DELETE FROM books_fts WHERE rowid=?", (book_id,))
    con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
    con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
    con.commit()
    if row["cover"]:
        (COVER_DIR / row["cover"]).unlink(missing_ok=True)
    return jsonify(ok=True)


# ---- covers
def store_cover(book_id, raw):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.width < 40 or img.height < 40:
        raise ValueError("image too small (probably a placeholder)")
    img.thumbnail((800, 1200))
    name = f"{book_id}-{uuid.uuid4().hex[:8]}.jpg"
    img.save(COVER_DIR / name, "JPEG", quality=85, optimize=True)
    con = db()
    old = con.execute("SELECT cover FROM books WHERE id=?", (book_id,)).fetchone()
    con.execute("UPDATE books SET cover=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (name, book_id))
    if old and old["cover"]:
        (COVER_DIR / old["cover"]).unlink(missing_ok=True)
    return name


def save_cover_from_url(book_id, url):
    host = urllib.parse.urlparse(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in COVER_HOSTS):
        raise ValueError("cover host not allowed")
    if "openlibrary.org" in host and "default=" not in url:
        url += ("&" if "?" in url else "?") + "default=false"
    return store_cover(book_id, http_get(url, binary=True))


@app.post("/admin/api/books/<int:book_id>/cover")
@editor_required
def upload_cover(book_id):
    if not db().execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    f = request.files.get("file")
    if not f:
        return make_err(400, "No image")
    try:
        store_cover(book_id, f.read())
    except Exception as e:  # noqa: BLE001
        return make_err(400, f"Couldn't read that image: {e}")
    db().commit()
    return get_book(book_id, private=True)


# ---- recipes
RECIPE_FIELDS = ("name", "heading", "page", "want_to_try", "rating", "notes", "last_made")


def recipe_values(data):
    vals = {}
    for k in RECIPE_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "want_to_try":
            v = 1 if v else 0
        elif k == "rating":
            try:
                v = float(v) if v not in (None, "") else None
            except ValueError:
                abort(make_err(400, "Rating must be a number"))
            if v is not None and not 0 <= v <= 10:
                abort(make_err(400, "Rating is 0–10"))
        else:
            v = clean(str(v) if v is not None else None)
        vals[k] = v
    if "page" in vals:
        vals["page_sort"] = page_sort(vals["page"])
    return vals


@app.post("/admin/api/books/<int:book_id>/recipes")
@editor_required
def add_recipe(book_id):
    if not db().execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    vals = recipe_values(request.get_json(force=True))
    if not vals.get("name"):
        return make_err(400, "Recipe name is required")
    vals.setdefault("page_sort", None)
    cols = ",".join(vals)
    cur = db().execute(f"INSERT INTO recipes(book_id, source, {cols}) VALUES (?, 'manual', {','.join('?' * len(vals))})",
                       [book_id, *vals.values()])
    db().commit()
    return jsonify(dict(db().execute("SELECT * FROM recipes WHERE id=?", (cur.lastrowid,)).fetchone())), 201


@app.put("/admin/api/recipes/<int:rid>")
@editor_required
def update_recipe(rid):
    vals = recipe_values(request.get_json(force=True))
    if "name" in vals and not vals["name"]:
        return make_err(400, "Recipe name is required")
    if vals:
        db().execute(f"UPDATE recipes SET {','.join(f'{k}=?' for k in vals)} WHERE id=?", [*vals.values(), rid])
        db().commit()
    row = db().execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.delete("/admin/api/recipes/<int:rid>")
@editor_required
def delete_recipe(rid):
    db().execute("DELETE FROM recipes WHERE id=?", (rid,))
    db().commit()
    return jsonify(ok=True)


# ---- index import
PAGES = r"\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*"
ENTRY_RE = re.compile(rf"^(?P<name>.*?\S)[\s,.;:]+(?P<pages>{PAGES})\.?$")
BULLETS = "•·▪–—-*"


def parse_index(text):
    """Turn pasted cookbook-index text into rows of {heading, name, page}.

    Handles the common shapes:
        Braised fennel, 255              -> top-level entry
        Chicken                          -> heading (no page)
          roast, with bread salad, 342   -> sub-entry (indented or lowercase)
        Chicken, 42                      -> entry that is also a heading for what follows
    Lines with "see"/"see also", lone letters and the word "Index" are skipped.
    """
    rows, heading = [], None
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        indented = raw[:1] in (" ", "\t")
        s = raw.strip()
        if s[:1] in BULLETS and len(s) > 1 and not s[1:2].isdigit():
            indented, s = True, s[1:].strip()
        s = re.sub(r"\(\s*see[^)]*\)", "", s, flags=re.I).strip()
        s = re.sub(r"[.;,]?\s*see also .*$", "", s, flags=re.I).strip()
        if not s or re.match(r"(?i)^see\b", s) or re.fullmatch(r"[A-Za-z]", s) or fold(s) in ("index",):
            continue
        m = ENTRY_RE.match(s)
        if not m:
            heading = s.rstrip(":").strip()
            continue
        name = m.group("name").strip(" ,;:")
        pages = re.sub(r"\s*[-—]\s*", "–", re.sub(r"\s*,\s*", ", ", m.group("pages")))
        if fold(name) == "index":
            continue
        is_sub = heading is not None and (indented or name[:1].islower())
        if is_sub:
            rows.append({"heading": heading, "name": name, "page": pages})
        else:
            rows.append({"heading": None, "name": name, "page": pages})
            heading = name
    return rows


def mark_duplicates(book_id, rows):
    existing = {(fold(r["name"]), fold(r["heading"] or ""), r["page"] or "") for r in
                db().execute("SELECT name, heading, page FROM recipes WHERE book_id=?", (book_id,))}
    seen = set()
    for r in rows:
        key = (fold(r["name"]), fold(r["heading"] or ""), r["page"] or "")
        r["duplicate"] = key in existing or key in seen
        seen.add(key)
    return rows


@app.post("/admin/api/books/<int:book_id>/index/parse")
@editor_required
def index_parse(book_id):
    rows = parse_index(request.get_json(force=True).get("text", ""))
    return jsonify(rows=mark_duplicates(book_id, rows))


@app.post("/admin/api/books/<int:book_id>/index/commit")
@editor_required
def index_commit(book_id):
    if not db().execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    con = db()
    existing = {(fold(r["name"]), fold(r["heading"] or ""), r["page"] or "") for r in
                con.execute("SELECT name, heading, page FROM recipes WHERE book_id=?", (book_id,))}
    added = skipped = 0
    for r in request.get_json(force=True).get("rows", []):
        name, heading, page = clean(r.get("name")), clean(r.get("heading")), clean(r.get("page"))
        if not name:
            continue
        key = (fold(name), fold(heading or ""), page or "")
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        con.execute("INSERT INTO recipes(book_id, name, heading, page, page_sort, source) VALUES (?,?,?,?,?, 'index')",
                    (book_id, name, heading, page, page_sort(page)))
        added += 1
    con.commit()
    return jsonify(added=added, skipped=skipped)


# ---- metadata lookup (Open Library, then Google Books)
def http_get(url, binary=False, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(15 * 1024 * 1024)
    return body if binary else json.loads(body)


def isbn10_to_13(i10):
    core = "978" + i10[:9]
    s = sum(int(c) * (1 if n % 2 == 0 else 3) for n, c in enumerate(core))
    return core + str((10 - s % 10) % 10)


def normalize_isbn(raw):
    s = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()
    if len(s) == 10:
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s) if c.isdigit() or c == "X")
        if "X" in s[:9] or total % 11:
            return None
        return isbn10_to_13(s)
    if len(s) == 13 and s.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s))
        return s if total % 10 == 0 else None
    return None


def ol_by_isbn(isbn):
    data = http_get(f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data")
    d = data.get(f"ISBN:{isbn}")
    if not d:
        return None
    year = re.search(r"\d{4}", d.get("publish_date", "") or "")
    return {
        "source": "Open Library", "isbn": isbn, "title": d.get("title"), "subtitle": d.get("subtitle"),
        "authors": [a["name"] for a in d.get("authors", [])],
        "publisher": (d.get("publishers") or [{}])[0].get("name"),
        "year": int(year.group()) if year else None,
        "cover_url": f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
    }


def google_by_isbn(isbn):
    data = http_get(f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}")
    items = data.get("items") or []
    if not items:
        return None
    v = items[0]["volumeInfo"]
    year = re.search(r"\d{4}", v.get("publishedDate", "") or "")
    cover = (v.get("imageLinks") or {}).get("thumbnail")
    if cover:
        cover = cover.replace("http://", "https://").replace("&edge=curl", "").replace("zoom=1", "zoom=2")
    return {
        "source": "Google Books", "isbn": isbn, "title": v.get("title"), "subtitle": v.get("subtitle"),
        "authors": v.get("authors", []), "publisher": v.get("publisher"),
        "year": int(year.group()) if year else None, "cover_url": cover,
    }


@app.get("/admin/api/lookup/isbn/<raw>")
@editor_required
def lookup_isbn(raw):
    isbn = normalize_isbn(raw)
    if not isbn:
        return make_err(400, "That doesn't look like a valid ISBN")
    dup = isbn_conflict(isbn)
    result, errors = None, []
    for fn in (ol_by_isbn, google_by_isbn):
        try:
            r = fn(isbn)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
            continue
        if r and r.get("title"):
            if result is None:
                result = r
            else:  # fill gaps from the second source
                for k, v in r.items():
                    if not result.get(k) and v:
                        result[k] = v
            if all(result.get(k) for k in ("title", "authors", "publisher", "year")):
                break
    return jsonify(isbn=isbn, result=result, existing=dict(dup) if dup else None, errors=errors)


@app.get("/admin/api/lookup/search")
@editor_required
def lookup_search():
    title, author = request.args.get("title", ""), request.args.get("author", "")
    params = {"title": title, "limit": "8",
              "fields": "key,title,subtitle,author_name,first_publish_year,publisher,isbn,cover_i"}
    if author:
        params["author"] = author
    try:
        data = http_get("https://openlibrary.org/search.json?" + urllib.parse.urlencode(params))
    except Exception as e:  # noqa: BLE001
        return make_err(502, f"Open Library didn't answer: {e}")
    out = []
    for d in data.get("docs", []):
        isbns = [normalize_isbn(i) for i in d.get("isbn", [])]
        isbns = [i for i in dict.fromkeys(isbns) if i]
        out.append({
            "title": d.get("title"), "subtitle": d.get("subtitle"), "authors": d.get("author_name", []),
            "year": d.get("first_publish_year"), "publishers": (d.get("publisher") or [])[:5],
            "isbns": isbns[:12],
            "cover_url": f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg" if d.get("cover_i") else None,
            "url": f"https://openlibrary.org{d.get('key')}",
        })
    return jsonify(results=out)


# ---- export
@app.get("/admin/api/export.json")
@editor_required
def export_json():
    rows = db().execute("SELECT * FROM books ORDER BY id").fetchall()
    books = book_dicts(rows, True)
    for b in books:
        b["recipes"] = [dict(r) for r in db().execute("SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort",
                                                      (b["id"],))]
    body = json.dumps({"exported": time.strftime("%Y-%m-%dT%H:%M:%S"), "books": books}, ensure_ascii=False, indent=1)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=cookbooks.json"})


@app.get("/admin/api/export.csv")
@editor_required
def export_csv():
    rows = book_dicts(db().execute("SELECT * FROM books ORDER BY id").fetchall(), True)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "title", "subtitle", "authors", "publisher", "year", "isbn", "cuisine", "tags", "status",
            "location", "signed", "rating", "recipe_count"]
    w.writerow(cols)
    for b in rows:
        w.writerow(["; ".join(b[c]) if isinstance(b[c], list) else b[c] for c in cols])
    # The byte-order mark tells Excel the file is UTF-8; without it "Pépin" opens as "PÃ©pin".
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=cookbooks.csv"})


if __name__ == "__main__":
    import app as _real  # import as a module so extras.py registers onto the same app
    _real.app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8767)), debug=False)
else:
    import extras  # noqa: E402,F401 — registers v2 routes
    import eyb  # noqa: E402,F401 — Eat Your Books import
    import ingredients  # noqa: E402,F401 — ingredient tags
    init_db()
