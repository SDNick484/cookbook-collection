"""Cookbook Collection — the family cookbook catalog.  Flask + SQLite.

Public:  /            browse/search books and recipe names (read-only)
         /api/...     public JSON (no private fields)
Editors: /admin       same UI with editing; put this path behind Cloudflare Access
         /admin/api/  full JSON + writes; requires a verified Access JWT
                      (or a direct LAN request when LAN_EDIT=1, see lan_identity())

Environment:
  DATA_DIR        database + covers (default ./data)
  CF_TEAM_DOMAIN  e.g. yourteam.cloudflareaccess.com
  CF_AUD          Access application AUD tag
  ADMIN_EMAILS    optional comma list; if set, only these emails may edit
  LAN_EDIT        1 = requests that did not come through Cloudflare may edit (default 0)
  LAN_CIDRS       client networks LAN editing is allowed from (default: loopback + private ranges)
  LAN_HOSTS       extra Host names accepted for LAN editing, e.g. cookbooks.lan (IP literals always are)
  ALLOWED_ORIGINS extra origins allowed to send edits, e.g. https://cookbooks.example.com
  READ_TOKEN      optional bearer token for read-only private routes (Dinner Log's picks)
"""
import csv, fcntl, io, ipaddress, json, os, re, sqlite3, time, unicodedata, urllib.parse, urllib.request, uuid
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
def _flag(name, default="0"):
    """An on/off setting from the environment: "1" (quotes and spaces allowed) is on."""
    return os.environ.get(name, default).strip().strip("'\"").strip() == "1"


LAN_EDIT = _flag("LAN_EDIT")  # off unless explicitly turned on
PRIVATE_NETS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,fe80::/10"


def _split(raw):
    return [x.strip() for x in raw.strip().strip("'\"").split(",") if x.strip()]


LAN_CIDRS = [ipaddress.ip_network(n, strict=False) for n in _split(os.environ.get("LAN_CIDRS", "") or PRIVATE_NETS)]
LAN_HOSTS = {h.lower() for h in _split(os.environ.get("LAN_HOSTS", ""))} | {"localhost"}
ALLOWED_ORIGINS = {o.lower().rstrip("/") for o in _split(os.environ.get("ALLOWED_ORIGINS", ""))}
# Bearer token for read-only private routes called by another server (Dinner Log's "Pick for tonight").
# It never grants writes; empty = off.
READ_TOKEN = os.environ.get("READ_TOKEN", "").strip()
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
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  uid TEXT
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
  source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  uid TEXT
);
CREATE INDEX IF NOT EXISTS recipes_book ON recipes(book_id, page_sort);
"""
# Stable public ids. `id` is a SQLite rowid: deleting the newest book and adding another gives the new book the
# old number, so anything outside this database that remembers a book (Dinner Log's cookbook_book_id) can end up
# pointing at the wrong one. `uid` is 32 hex characters, set once and never reused. The triggers fill it on every
# insert path (editor, imports, seed, sqlite3 CLI) so no caller has to remember to; migrate_uids() backfills
# rows from before the column existed.
UIDS = """
CREATE UNIQUE INDEX IF NOT EXISTS books_uid ON books(uid) WHERE uid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS recipes_uid ON recipes(uid) WHERE uid IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS books_uid_ai AFTER INSERT ON books WHEN new.uid IS NULL BEGIN
  UPDATE books SET uid=lower(hex(randomblob(16))) WHERE id=new.id;
END;
CREATE TRIGGER IF NOT EXISTS recipes_uid_ai AFTER INSERT ON recipes WHEN new.uid IS NULL BEGIN
  UPDATE recipes SET uid=lower(hex(randomblob(16))) WHERE id=new.id;
END;
"""
UID_RE = re.compile(r"[0-9a-f]{32}")


def migrate_uids(con):
    """Add `uid` to books and recipes on databases from before it existed, and give every row one."""
    for table in ("books", "recipes"):
        if "uid" not in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
            con.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
        con.execute(f"UPDATE {table} SET uid=lower(hex(randomblob(16))) WHERE uid IS NULL")
    con.executescript(UIDS)


# Shelf locations: a tree (room > area > shelf, any depth). A book is in at most one (`books.location_id`; the
# location SET NULLs when deleted, and a location's children go with it). `books.location` is the free text this
# replaced: it stays, frozen, so the previous release still runs if you roll back, and nothing reads or writes it.
LOCATIONS = """
CREATE TABLE IF NOT EXISTS locations (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES locations(id) ON DELETE CASCADE,
  name TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS locations_parent ON locations(parent_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
LOCATION_SEP = " › "  # the breadcrumb separator, "Kitchen › Pantry › Shelf 2"; exports use it and imports split on it
PATHY = ",;/>›|-"  # characters that hint a flat name is really a path. The migration reports these; it never splits on them


def loc_name(v):
    """A location name as stored: composed accents, runs of whitespace collapsed to one space, trimmed. None if empty."""
    if v is None:
        return None
    return " ".join((clean(v if isinstance(v, str) else str(v)) or "").split()) or None


def migrate_locations(con):
    """Add the locations table and `books.location_id`, and (once) turn the old free-text `location` into root
    locations: one per distinct text, where text that differs only in case, accents or spacing counts as the same,
    named with its commonest spelling (a tie goes to the lowest book id). Text is never split on commas, slashes or
    arrows ("Office, top shelf" is one location); the names that look like paths are recorded for review instead.
    The `locations_migrated` marker in `meta` makes this run once: without it, a book whose shelf was cleared in the
    editor would be put back from the frozen text on the next start."""
    con.executescript(LOCATIONS)
    cols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    if "location_id" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL")
    con.execute("CREATE INDEX IF NOT EXISTS books_location ON books(location_id)")
    if con.execute("SELECT 1 FROM meta WHERE key='locations_migrated'").fetchone():
        return
    groups = {}  # folded name -> {spelling: how many books}, in book id order
    members = {}  # folded name -> book ids
    if "location" in cols:
        for bid, raw in con.execute("SELECT id, location FROM books WHERE location IS NOT NULL AND location_id IS NULL "
                                    "ORDER BY id").fetchall():
            name = loc_name(raw)
            if name:
                spell = groups.setdefault(fold(name), {})
                spell[name] = spell.get(name, 0) + 1
                members.setdefault(fold(name), []).append(bid)
    names = {}
    for order, key in enumerate(sorted(groups)):
        name = max(groups[key], key=groups[key].get)  # max keeps the first of equals: the lowest book id's spelling
        lid = con.execute("INSERT INTO locations(parent_id, name, sort_order) VALUES (NULL, ?, ?)", (name, order)).lastrowid
        con.executemany("UPDATE books SET location_id=? WHERE id=?", [(lid, b) for b in members[key]])
        names[lid] = name
    info = {"at": time.strftime("%Y-%m-%d %H:%M"), "locations": len(names), "books": sum(map(len, members.values())),
            "delimited": sorted(n for n in names.values() if any(ch in n for ch in PATHY))}
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('locations_migrated', ?)", (json.dumps(info),))
    if names:
        app.logger.info("shelf locations: %d locations from the old text for %d books; %d look like paths (%s)",
                        info["locations"], info["books"], len(info["delimited"]), "; ".join(info["delimited"]) or "none")


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
    con.execute("PRAGMA busy_timeout=15000")
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    lock_f = open(DATA_DIR / ".init.lock", "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        _init_db_locked()
    finally:
        lock_f.close()


def _init_db_locked():
    global HAS_FTS
    con = connect()
    con.executescript(SCHEMA)
    import extras, eyb, ingredients, photos, online, ask
    online.migrate(con)
    extras.migrate(con)
    eyb.migrate(con)
    ingredients.migrate(con)
    photos.migrate(con)
    ask.migrate(con)
    migrate_uids(con)
    migrate_locations(con)
    old = con.execute("SELECT sql FROM sqlite_master WHERE name='recipes_fts'").fetchone()
    if old and "content=" in old[0]:  # the old index read unfolded text straight from recipes; replace it
        con.executescript("DROP TRIGGER IF EXISTS recipes_ai; DROP TRIGGER IF EXISTS recipes_ad; "
                          "DROP TRIGGER IF EXISTS recipes_au; DROP TABLE recipes_fts;")
    try:
        con.executescript(FTS)
    except sqlite3.OperationalError:
        HAS_FTS = False  # sqlite built without FTS5: search falls back to LIKE
    merged = eyb.merge_garbled(con)  # recipes imported with "�" for lost letters, merged into their good copy
    if merged:  # shown on Needs review
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('garbled_merged', ?)",
                    (json.dumps({"n": merged, "at": time.strftime("%Y-%m-%d %H:%M")}),))
    empty = con.execute("SELECT count(*) FROM books").fetchone()[0] == 0
    seed = APP_DIR / "seed.json"
    # SKIP_SEED=1 is for importer.py (a restore into a new database must not start from the seed catalog). It is
    # read at call time, and the service never sets it, so it is not in setup-head.sh's export line.
    if empty and seed.exists() and not _flag("SKIP_SEED"):
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
def env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        app.logger.warning("%s is not a whole number; using %s", name, default)
        return default


def local_midnight():
    """Unix time of the most recent local midnight: daily limits reset then."""
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


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
    if isinstance(q, (list, tuple)):
        q = " ".join(q)
    return " ".join(f'"{t}"*' for t in re.findall(r"\w+", fold(q)))


# Letters that aren't an accent on a plain letter, so stripping accents leaves them alone.
FOLD_EXTRA = str.maketrans({"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i",
                            "ħ": "h", "ŧ": "t", "ŋ": "n"})
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
# Scripts written without spaces between words: a word search can't find text in the middle of a run.
UNSPACED = re.compile(r"[\u0e00-\u0eff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def fold(s):
    """Search and matching key: "Crème Brûlée" -> "creme brulee", "Œufs" -> "oeufs", "Straße" -> "strasse"."""
    if not s:
        return ""
    if s.isascii():  # most names: nothing to fold but case
        return s.lower()
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c)).translate(FOLD_EXTRA)


def search_words(q):
    """Split a search into (words for the full-text index, runs of Chinese/Japanese/Korean/Thai text). Those
    scripts don't put spaces between words, so they're matched as substrings rather than word prefixes."""
    words, runs = [], []
    for t in re.findall(r"\w+", unicodedata.normalize("NFKC", q or "")):
        if not re.findall(r"\w+", fold(t)):
            continue
        (runs if UNSPACED.search(t) else words).append(t)
    return words, runs


PUBLIC_BOOK = ("id", "uid", "title", "subtitle", "isbn", "publisher", "year", "cuisine", "description", "cover", "signed",
               "format")
PUBLIC_RECIPE = ("id", "uid", "book_id", "name", "heading", "page")
# Dinner Log history on the public book page: how often a recipe was made, never when or how it was rated.
PUBLIC_RECIPE_COOKED = ("cooked_count",)
PUBLIC_BOOKS = "b.kind IS NULL"  # cookbooks; recipe websites (kind 'online') are for editors only


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
    paths = location_paths(db())
    out = []
    for r in rows:
        d = dict(r) if private else {k: r[k] for k in PUBLIC_BOOK}
        lid = r["location_id"] if "location_id" in r.keys() else None
        d["location_id"], d["location_path"] = lid, paths.get(lid)  # the shelf is public on purpose (see get_locations)
        if private:  # `location` used to be free text; it is now the same path, so exports and the MCP keep their key
            d["location"] = d["location_path"]
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
            was_cached = _jwks is not None
            if _jwks is None:
                _jwks = jwt.PyJWKClient(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs", cache_keys=True)
            try:
                key = _jwks.get_signing_key_from_jwt(token).key
            except Exception:  # noqa: BLE001 — retry once on key rotation if _jwks was cached
                if not was_cached:
                    raise
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
    if via_cloudflare:
        g.auth_reason = ("No Cloudflare Access sign-in reached the app. Check that the Access application for "
                         "this site covers the path 'admin'.")
        return None
    return lan_identity()


def _host_only(netloc):
    """'192.168.1.5:8767' -> '192.168.1.5', '[::1]:8767' -> '::1', 'Cookbooks.lan' -> 'cookbooks.lan'."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[1:netloc.find("]")] if "]" in netloc else ""
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _is_ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def lan_identity():
    """A request that didn't come through Cloudflare. It may edit only when LAN_EDIT=1, it comes from a
    LAN_CIDRS address, and its Host is an IP address or a LAN_HOSTS name. The Host rule stops DNS
    rebinding: a page on attacker.example re-pointed at this server still sends Host: attacker.example.
    request.remote_addr is the socket peer (no proxy headers are trusted), so it can't be spoofed by headers."""
    if not LAN_EDIT:
        g.auth_reason = "Editing from the LAN is turned off (LAN_EDIT=0)."
        return None
    try:
        peer = ipaddress.ip_address((request.remote_addr or "").split("%")[0])
    except ValueError:
        peer = None
    if peer is not None and peer.version == 6 and peer.ipv4_mapped:
        peer = peer.ipv4_mapped  # a dual-stack bind ([::]) reports IPv4 clients as ::ffff:a.b.c.d
    if peer is None or not any(peer in n for n in LAN_CIDRS):
        g.auth_reason = f"LAN editing isn't allowed from {request.remote_addr} (not in LAN_CIDRS)."
        return None
    host = _host_only(request.host)
    if not (_is_ip(host) or host in LAN_HOSTS):
        g.auth_reason = (f"LAN editing needs the server's IP address in the URL, not '{host}'. "
                         "To use a name, add it to LAN_HOSTS.")
        return None
    return "lan"


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def cross_site_reason():
    """For edits: None if the request came from this site's own pages (or from no browser at all),
    else why it's refused. Browsers always send Origin on cross-origin POST/PUT/DELETE, including the
    'simple' text/plain and form posts that skip CORS preflight, so checking it blocks CSRF."""
    if request.method not in UNSAFE_METHODS:
        return None
    origin = (request.headers.get("Origin") or "").strip().lower().rstrip("/")
    if not origin:
        site = (request.headers.get("Sec-Fetch-Site") or "").lower()
        return None if site in ("", "same-origin", "none") else f"Cross-site request refused ({site})."
    if origin in ALLOWED_ORIGINS:
        return None
    parts = urllib.parse.urlsplit(origin)
    hosts = {request.host.lower()}
    via_cloudflare = request.headers.get("Cf-Ray") or request.headers.get("Cf-Connecting-Ip")
    if via_cloudflare:
        hosts.add((request.headers.get("X-Forwarded-Host") or "").lower())
    # Through Cloudflare the site is only served over HTTPS, so an http:// origin of the same name isn't ours.
    scheme_ok = parts.scheme == "https" if via_cloudflare else parts.scheme in ("http", "https")
    if origin != "null" and scheme_ok and parts.netloc and parts.netloc in hosts:
        return None
    return f"Cross-site request refused (Origin {origin}). Add it to ALLOWED_ORIGINS if it's yours."


def editor_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        bad = cross_site_reason()
        if bad:
            return jsonify(error="Refused", reason=bad), 403
        who = access_identity()
        if not who:
            return jsonify(error="Sign in to edit", reason=g.auth_reason), 401
        g.editor = who
        return fn(*a, **kw)
    return inner


def reader_required(fn):
    """For private routes that change nothing: an editor, or a server presenting
    `Authorization: Bearer <READ_TOKEN>`. Never put this on a write route."""
    import hmac
    guarded = editor_required(fn)

    @wraps(fn)
    def inner(*a, **kw):
        auth = request.headers.get("Authorization", "")
        if READ_TOKEN and auth[:7].lower() == "bearer " and \
                hmac.compare_digest(auth[7:].strip().encode(), READ_TOKEN.encode()):
            g.editor = None
            return fn(*a, **kw)
        return guarded(*a, **kw)
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


def cuisine_condition(name):
    """Books of this cuisine: their own, or a tag with the cuisine's name ("Portuguese" on a New England book
    whose cuisine is American). That gives a book a second cuisine, while stats count it once, under its own."""
    return ("(b.cuisine=? OR b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
            "WHERE fold(t.name)=fold(?)))", [name, name])


# ------------------------------------------------------------- locations
def location_tree(con):
    """Every location, depth first (siblings by sort_order, then folded name), as
    {id, parent_id, name, sort_order, path, depth}. A location whose parent is missing, or that sits in a loop
    (only possible by editing the database by hand), is listed as a root rather than lost."""
    rows = con.execute("SELECT id, parent_id, name, sort_order FROM locations").fetchall()
    kids = {}
    for id_, pid, name, order in rows:
        kids.setdefault(pid, []).append((order, fold(name), id_, name))
    out, seen = [], set()

    def walk(pid, prefix, depth):
        for order, _, id_, name in sorted(kids.get(pid, [])):
            if id_ in seen:
                continue
            seen.add(id_)
            out.append({"id": id_, "parent_id": pid, "name": name, "sort_order": order, "path": prefix + name,
                        "depth": depth})
            walk(id_, prefix + name + LOCATION_SEP, depth + 1)
    walk(None, "", 0)
    for id_, pid, name, order in sorted(rows, key=lambda r: (r[3], fold(r[2]), r[0])):
        if id_ not in seen:
            seen.add(id_)
            out.append({"id": id_, "parent_id": pid, "name": name, "sort_order": order, "path": name, "depth": 0})
            walk(id_, name + LOCATION_SEP, 1)
    return out


def get_locations(con, public=False):
    """The location tree with book counts: `direct` (books shelved right here) and `n` (here or anywhere below, which
    is what filtering by it returns). public=True counts only cookbooks and lists only the locations that hold at
    least one, themselves or below, so no empty rooms and nothing from editor-only recipe websites is exposed."""
    tree = location_tree(con)
    direct = dict(con.execute("SELECT b.location_id, count(*) FROM books b WHERE b.location_id IS NOT NULL"
                              + (f" AND {PUBLIC_BOOKS}" if public else "") + " GROUP BY b.location_id").fetchall())
    n = {l["id"]: direct.get(l["id"], 0) for l in tree}
    for l in reversed(tree):  # a child comes after its parent, so this adds each subtree into its parent once
        if l["parent_id"] in n and l["parent_id"] != l["id"]:
            n[l["parent_id"]] += n[l["id"]]
    out = [{**l, "n": n[l["id"]], "direct": direct.get(l["id"], 0)} for l in tree]
    return [l for l in out if l["n"] > 0] if public else out


def location_paths(con):
    """{location id: "Kitchen › Pantry › Shelf 2"}"""
    return {l["id"]: l["path"] for l in location_tree(con)}


def descendant_location_ids(con, location_id):
    """`location_id` and every location below it."""
    kids = {}
    for id_, pid in con.execute("SELECT id, parent_id FROM locations").fetchall():
        kids.setdefault(pid, []).append(id_)
    found, todo = set(), [location_id]
    while todo:
        cur = todo.pop()
        if cur not in found:
            found.add(cur)
            todo += kids.get(cur, [])
    return found


def location_arg(raw, what="location_id"):
    """An id from a request: None for nothing (null or ""), an int for a whole number, otherwise a 400."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and re.fullmatch(r"\s*\d+\s*", raw):
        return int(raw)
    abort(make_err(400, f"{what} must be a whole number"))


def locations_list(private):
    return jsonify(locations=get_locations(db(), public=not private))


# ------------------------------------------------------------- read API
def list_books(private):
    q = request.args.get("q", "").strip()
    sql, args = "SELECT b.* FROM books b WHERE 1=1", []
    if not private:
        sql += " AND " + PUBLIC_BOOKS
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
        cond, a = cuisine_condition(request.args["cuisine"])
        sql += " AND " + cond
        args += a
    if request.args.get("tag"):
        sql += " AND b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)"
        args.append(request.args["tag"])
    if request.args.get("author"):
        sql += " AND b.id IN (SELECT ba.book_id FROM book_authors ba JOIN authors a ON a.id=ba.author_id WHERE a.name=?)"
        args.append(request.args["author"])
    if request.args.get("publisher"):
        sql += " AND fold(b.publisher) = fold(?)"
        args.append(request.args["publisher"])
    shelf = location_arg(request.args.get("location_id"))  # a location includes everything shelved below it
    if shelf is not None:
        ids = sorted(descendant_location_ids(db(), shelf))
        sql += f" AND b.location_id IN ({','.join('?' * len(ids))})"
        args += ids
    if request.args.get("ingredient"):
        import ingredients
        cond, a = ingredients.ingredient_condition(db(), request.args["ingredient"])
        sql += f" AND b.id IN (SELECT r.book_id FROM recipes r WHERE {cond})"
        args += a
    if private and request.args.get("status"):
        sql += " AND b.status = ?"
        args.append(request.args["status"])
    if private and request.args.get("unindexed"):  # cookbooks on hand with no recipes indexed yet
        sql += (" AND b.kind IS NULL AND b.status NOT IN ('gone','wanted')"
                " AND NOT EXISTS (SELECT 1 FROM recipes r WHERE r.book_id=b.id)")
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
    return jsonify(books=books, total=db().execute(
        "SELECT count(*) FROM books b" + ("" if private else " WHERE " + PUBLIC_BOOKS)).fetchone()[0])


def get_book(book_id, private):
    row = db().execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row or (not private and row["kind"]):
        abort(404)
    book = book_dicts([row], private)[0]
    recs = db().execute("SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort IS NULL, page_sort, name",
                        (book_id,)).fetchall()
    book["recipes"] = [recipe_dict(r, private) for r in recs]
    import extras
    # Public pages sync too, so their counts stay current; a pull is leased, at most one a minute (with
    # backoff after a failure), and nothing from the request reaches Dinner Log.
    extras.ensure_synced()
    cr = extras.cooked_by_recipe(book_id)
    for r in book["recipes"]:
        stats = cr.get(r["id"], {})
        r.update(stats if private else {k: stats[k] for k in PUBLIC_RECIPE_COOKED if k in stats})
    if private:
        book.update(extras.cooked_by_book([book_id]).get(book_id, {}))
        book["wishes"] = extras.wishes_for_book(book_id)
        import photos
        book["photos"] = photos.for_book(book_id)
    return jsonify(book)


def get_book_by_uid(uid, private):
    """The same as get_book, found by the stable uid (what Dinner Log should store and send back)."""
    if not UID_RE.fullmatch(uid or ""):
        abort(404)
    row = db().execute("SELECT id FROM books WHERE uid=?", (uid,)).fetchone()
    if not row:
        abort(404)
    return get_book(row["id"], private)


def recipe_filter(params, private):
    """WHERE clause + args for recipe search. Each word of q must match the recipe's name/index heading or
    one of its ingredient tags; ingredient= limits to recipes that use it (see ingredients.ingredient_condition)."""
    import ingredients
    where, args = ([] if private else [PUBLIC_BOOKS]), []
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
        cond, a = cuisine_condition(params["cuisine"])
        where.append(cond)
        args += a
    if params.get("tag"):
        where.append("b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE t.name=?)")
        args.append(params["tag"])
    if params.get("publisher"):
        where.append("fold(b.publisher) = fold(?)")
        args.append(params["publisher"])
    shelf = location_arg(params.get("location_id"))  # the books on that shelf, or anywhere below it
    if shelf is not None:
        ids = sorted(descendant_location_ids(db(), shelf))
        where.append(f"b.location_id IN ({','.join('?' * len(ids))})")
        args += ids
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
    import ingredients
    where, args = recipe_filter(p, private)
    base = " FROM recipes r JOIN books b ON b.id=r.book_id WHERE 1=1" + "".join(" AND " + w for w in where)
    # best matches first, each book's recipes kept together for the headings on the results page
    order = ingredients.ranked(db(), base, args, ingredients.search_terms(p.get("q", ""), p.get("ingredient")),
                               by_book=True)
    rows = recipes_by_id([x["id"] for x in order[offset:offset + limit]])
    return jsonify(recipes=[recipe_dict(r, private) for r in rows], count=len(order))


def recipes_by_id(ids):
    """Full recipe rows (with book_title) for these ids, in this order."""
    if not ids:
        return []
    got = {r["id"]: r for r in db().execute(
        f"SELECT r.*, b.title AS book_title FROM recipes r JOIN books b ON b.id=r.book_id "
        f"WHERE r.id IN ({','.join('?' * len(ids))})", ids)}
    return [got[i] for i in ids if i in got]


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
    pub = "" if private else f" WHERE {PUBLIC_BOOKS}"
    pub_and = "" if private else f" AND {PUBLIC_BOOKS}"
    return jsonify(
        cuisines=by_name(d.execute(
            "SELECT c.name, count(*) AS n FROM (SELECT DISTINCT cuisine AS name FROM books b WHERE cuisine IS NOT NULL"
            + pub_and + ") c JOIN books b ON (b.cuisine=c.name OR b.id IN (SELECT bt.book_id FROM book_tags bt "
            "JOIN tags t ON t.id=bt.tag_id WHERE fold(t.name)=fold(c.name)))" + pub + " GROUP BY c.name")),
        tags=by_name(d.execute(
            "SELECT t.name, count(*) AS n FROM tags t JOIN book_tags bt ON bt.tag_id=t.id "
            "JOIN books b ON b.id=bt.book_id" + pub + " GROUP BY t.id")),
        authors=by_name(d.execute(
            "SELECT a.name, count(*) AS n FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
            "JOIN books b ON b.id=ba.book_id" + pub + " GROUP BY a.id")),
        publishers=sorted((r[0] for r in d.execute(
            "SELECT DISTINCT publisher FROM books WHERE publisher IS NOT NULL")), key=fold) if private else [],
        locations=get_locations(d, public=not private),  # editors get empty ones too, for the picker
        stats={"books": d.execute(f"SELECT count(*) FROM books b WHERE {PUBLIC_BOOKS}").fetchone()[0],  # cookbooks
               "recipes": d.execute("SELECT count(*) FROM recipes r JOIN books b ON b.id=r.book_id"
                                    + pub).fetchone()[0]},
    )


for prefix, private in (("/api", False), ("/admin/api", True)):
    def _wrap(fn, private=private):
        f = (lambda *a, **kw: fn(*a, private=private, **kw))
        return editor_required(f) if private else f
    app.add_url_rule(f"{prefix}/books", f"books_{private}", _wrap(list_books))
    app.add_url_rule(f"{prefix}/books/<int:book_id>", f"book_{private}", _wrap(get_book))
    app.add_url_rule(f"{prefix}/books/by-uid/<uid>", f"book_uid_{private}", _wrap(get_book_by_uid))
    app.add_url_rule(f"{prefix}/recipes", f"recipes_{private}", _wrap(search_recipes))
    app.add_url_rule(f"{prefix}/recipes/books", f"recipe_books_{private}", _wrap(recipe_books))
    app.add_url_rule(f"{prefix}/facets", f"facets_{private}", _wrap(facets))
    app.add_url_rule(f"{prefix}/locations", f"locations_{private}", _wrap(locations_list))


@app.get("/api/me")
def me_public():
    return jsonify(editor=bool(access_identity()))


@app.get("/admin/api/me")
@editor_required
def me():
    return jsonify(editor=True, email=g.editor)


# ------------------------------------------------------------- write API
BOOK_FIELDS = ("title", "subtitle", "isbn", "publisher", "year", "cuisine", "format", "description", "location_id",
               "status", "loaned_to", "signed", "private_notes", "needs_review", "rating")  # not `location`: see migrate_locations


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
        elif k == "location_id":
            v = location_arg(v)
            if v is not None and not db().execute("SELECT 1 FROM locations WHERE id=?", (v,)).fetchone():
                abort(make_err(400, "That shelf location doesn't exist"))
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
    row = con.execute("SELECT kind FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    data = request.get_json(force=True)
    vals = book_values(data)
    if vals.get("location_id") is not None and row["kind"]:
        return make_err(400, "A recipe website isn't on a shelf")
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
    reindex_book(con, book_id)
    con.commit()
    if data.get("cover_url"):
        try:
            save_cover_from_url(book_id, data["cover_url"])
            con.commit()
        except Exception as e:  # noqa: BLE001
            return make_err(502, f"Saved, but couldn't fetch that cover: {e}")
    return get_book(book_id, private=True)


@app.delete("/admin/api/books/<int:book_id>")
@editor_required
def delete_book(book_id):
    con = db()
    row = con.execute("SELECT cover FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    import photos
    photo_files = photos.files_where(con, "recipe_id IN (SELECT id FROM recipes WHERE book_id=?)", book_id)
    con.execute("DELETE FROM books WHERE id=?", (book_id,))
    if HAS_FTS:
        con.execute("DELETE FROM books_fts WHERE rowid=?", (book_id,))
    con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
    con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
    con.commit()
    if row["cover"]:
        (COVER_DIR / row["cover"]).unlink(missing_ok=True)
    photos.unlink(photo_files)
    return jsonify(ok=True)


# ---- shelf locations (editors only; the public read side is /api/locations and the catalog filter)
def location_clash(con, parent_id, name, exclude=None):
    """The sibling already called `name` (case, accents and spacing don't count), or None. Two shelves of one name
    in one place would make a path ("Kitchen › Shelf 2") ambiguous, and an import couldn't tell them apart."""
    key = fold(name)
    for id_, other in con.execute("SELECT id, name FROM locations WHERE parent_id IS ? AND id IS NOT ?",
                                  (parent_id, exclude)).fetchall():
        if fold(other) == key:
            return id_
    return None


def next_sort_order(con, parent_id):
    return con.execute("SELECT coalesce(max(sort_order) + 1, 0) FROM locations WHERE parent_id IS ?", (parent_id,)).fetchone()[0]


def one_location(con, loc_id):
    return next(l for l in get_locations(con) if l["id"] == loc_id)


@app.post("/admin/api/locations")
@editor_required
def add_location():
    data = request.get_json(silent=True) or {}
    name = loc_name(data.get("name"))
    if not name:
        return make_err(400, "A location needs a name")
    con = db()
    parent = location_arg(data.get("parent_id"), "parent_id")
    if parent is not None and not con.execute("SELECT 1 FROM locations WHERE id=?", (parent,)).fetchone():
        return make_err(400, "That parent location doesn't exist")
    if location_clash(con, parent, name):
        return make_err(409, f"There is already a “{name}” there")
    order = location_arg(data.get("sort_order"), "sort_order")
    if order is None:
        order = next_sort_order(con, parent)
    lid = con.execute("INSERT INTO locations(parent_id, name, sort_order) VALUES (?,?,?)", (parent, name, order)).lastrowid
    con.commit()
    return jsonify(ok=True, location=one_location(con, lid)), 201


@app.put("/admin/api/locations/<int:loc_id>")
@editor_required
def edit_location(loc_id):
    """Rename, move (parent_id; null = a root) and/or set sort_order. A move goes last among its new siblings
    unless sort_order says otherwise. A location can't go under itself or anything below it."""
    con = db()
    cur = con.execute("SELECT parent_id, name, sort_order FROM locations WHERE id=?", (loc_id,)).fetchone()
    if not cur:
        return make_err(404, "No such location")
    data = request.get_json(silent=True) or {}
    name = loc_name(data["name"]) if "name" in data else cur["name"]
    if not name:
        return make_err(400, "A location needs a name")
    parent, order = cur["parent_id"], cur["sort_order"]
    if "parent_id" in data:
        parent = location_arg(data["parent_id"], "parent_id")
        if parent is not None:
            if parent in descendant_location_ids(con, loc_id):  # includes itself
                return make_err(400, "A location can't go inside itself or anything below it")
            if not con.execute("SELECT 1 FROM locations WHERE id=?", (parent,)).fetchone():
                return make_err(400, "That parent location doesn't exist")
        if parent != cur["parent_id"]:
            order = next_sort_order(con, parent)
    given = location_arg(data.get("sort_order"), "sort_order")
    if given is not None:
        order = given
    if location_clash(con, parent, name, exclude=loc_id):
        return make_err(409, f"There is already a “{name}” there")
    con.execute("UPDATE locations SET name=?, parent_id=?, sort_order=? WHERE id=?", (name, parent, order, loc_id))
    con.commit()
    return jsonify(ok=True, location=one_location(con, loc_id))


@app.delete("/admin/api/locations/<int:loc_id>")
@editor_required
def delete_location(loc_id):
    """Deletes the location and everything below it; the books that were on any of them become unshelved."""
    con = db()
    if not con.execute("SELECT 1 FROM locations WHERE id=?", (loc_id,)).fetchone():
        return make_err(404, "No such location")
    ids = sorted(descendant_location_ids(con, loc_id))
    qs = ",".join("?" * len(ids))
    unshelved = con.execute(f"SELECT count(*) FROM books WHERE location_id IN ({qs})", ids).fetchone()[0]
    con.execute("DELETE FROM locations WHERE id=?", (loc_id,))  # the children cascade, the books' ids go NULL
    con.commit()
    return jsonify(ok=True, deleted_id=loc_id, locations_deleted=len(ids), books_unshelved=unshelved)


@app.post("/admin/api/locations/reorder")
@editor_required
def reorder_locations():
    """{parent_id, ids}: the order the children of that parent (null = the roots) should be in. `ids` must be
    exactly those children, so a stale screen can't drop or duplicate one."""
    data = request.get_json(silent=True) or {}
    con = db()
    parent = location_arg(data.get("parent_id"), "parent_id")
    if parent is not None and not con.execute("SELECT 1 FROM locations WHERE id=?", (parent,)).fetchone():
        return make_err(400, "That parent location doesn't exist")
    ids = data.get("ids")
    if not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        return make_err(400, "ids must be a list of location ids")
    have = {r[0] for r in con.execute("SELECT id FROM locations WHERE parent_id IS ?", (parent,))}
    if len(ids) != len(set(ids)) or set(ids) != have:
        return make_err(400, "ids must be exactly the locations under that parent")
    con.executemany("UPDATE locations SET sort_order=? WHERE id=?", [(i, lid) for i, lid in enumerate(ids)])
    con.commit()
    return jsonify(ok=True, locations=get_locations(con))


# ---- covers
# The only image formats uploads may be in. Phones and cover services produce these; Pillow's other decoders
# (PSD, TIFF, BMP, FITS, EPS…) have had memory-safety bugs and nothing here needs them. HEIF is only
# available when pillow-heif is installed (photos.py registers it), so the list is checked against Pillow's
# registry at call time.
IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "HEIF")


def open_image(raw):
    """Decode an upload in one of IMAGE_FORMATS; anything else is a ValueError the routes turn into a 400."""
    from PIL import Image, UnidentifiedImageError
    Image.init()
    formats = [f for f in IMAGE_FORMATS if f in Image.OPEN]
    try:
        return Image.open(io.BytesIO(raw), formats=formats)
    except UnidentifiedImageError:
        raise ValueError("not a JPEG, PNG, WebP or HEIC image") from None


def store_cover(book_id, raw):
    from PIL import ImageOps
    img = open_image(raw)
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


def _allowed_cover_url(url):
    p = urllib.parse.urlparse(url)
    host = p.hostname or ""
    return p.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in COVER_HOSTS)


def save_cover_from_url(book_id, url):
    if not _allowed_cover_url(url):
        raise ValueError("cover host not allowed")
    host = urllib.parse.urlparse(url).hostname or ""
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
    import photos
    files = photos.files_where(db(), "recipe_id=?", rid)
    db().execute("DELETE FROM recipes WHERE id=?", (rid,))
    db().commit()
    photos.unlink(files)
    return jsonify(ok=True)


# ---- index import
PAGES = r"\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*"
PAGE_PREFIX = r"(?:\b(?i:pag|pp?)\.?\s*)?"  # Italian and other indexes write "p. 42", "pag. 42", "pp. 42-44"
# Front-matter and plate pages: roman numerals up to xlix (so an ordinary word made of roman letters, like
# "mix", can't pass) and a letter suffix ("12a"). They're kept but flagged, since page_sort() can't order them.
ODD_PAGE = r"(?i:(?=[ivxl])(?:xl|x{0,3})(?:ix|iv|v?i{0,3})|\d+[a-z])"
ENTRY_RE = re.compile(rf"^(?P<name>.*?\S)[\s,.;:]+{PAGE_PREFIX}(?P<pages>{PAGES})\.?$")
ODD_ENTRY_RE = re.compile(rf"^(?P<name>.*?\S)[,.;:]\s*{PAGE_PREFIX}(?P<pages>{ODD_PAGE})\.?$")  # needs punctuation
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
        m, warn = ENTRY_RE.match(s), None
        if not m:
            m, warn = ODD_ENTRY_RE.match(s), "page"
        if not m:
            heading = s.rstrip(":").strip()
            continue
        name = m.group("name").strip(" ,;:")
        pages = re.sub(r"\s*[-—]\s*", "–", re.sub(r"\s*,\s*", ", ", m.group("pages")))
        if fold(name) == "index":
            continue
        is_sub = heading is not None and (indented or name[:1].islower())
        row = {"heading": heading if is_sub else None, "name": name, "page": pages}
        if warn:
            row["warn"] = warn  # an unusual page form: shown in the review for a second look
        rows.append(row)
        if not is_sub:
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


class _CoverRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_cover_url(newurl):
            raise ValueError("cover redirect host not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---- metadata lookup (Open Library, then Google Books)
def http_get(url, binary=False, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    opener = urllib.request.build_opener(_CoverRedirectHandler) if binary else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as r:
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
# Shape version of export.json, read by importer.py, which refuses a file newer than it knows. Bump it only for a
# change an older importer would misread (a renamed or removed key); adding a key is not one.
EXPORT_VERSION = 1


@app.get("/admin/api/export.json")
@editor_required
def export_json():
    rows = db().execute("SELECT * FROM books ORDER BY id").fetchall()
    books = book_dicts(rows, True)
    for b in books:  # the shelf travels as the `location` path; ids belong to this database only (see importer._shelf)
        b.pop("location_id", None)
        b.pop("location_path", None)
    tags = {}  # recipe id -> its ingredient tag names; [] in the file means untagged, a missing key an older export
    for rid, name in db().execute("SELECT ri.recipe_id, i.name FROM recipe_ingredients ri "
                                  "JOIN ingredients i ON i.id=ri.ingredient_id ORDER BY i.name"):
        tags.setdefault(rid, []).append(name)
    hints = [r[0] for r in db().execute("SELECT name FROM ingredients WHERE hint=1 ORDER BY name")]
    for b in books:
        b["recipes"] = [dict(r) for r in db().execute("SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort",
                                                      (b["id"],))]
        for r in b["recipes"]:
            r["ingredients"] = tags.get(r["id"], [])
        photos = {}  # file names in DATA_DIR/photos
        for p in db().execute("SELECT p.recipe_id, p.file, p.taken_on, p.caption FROM recipe_photos p "
                              "JOIN recipes r ON r.id=p.recipe_id WHERE r.book_id=?", (b["id"],)):
            photos.setdefault(p["recipe_id"], []).append({k: p[k] for k in ("file", "taken_on", "caption")})
        for r in b["recipes"]:
            if r["id"] in photos:
                r["photos"] = photos[r["id"]]
    body = json.dumps({"version": EXPORT_VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),  # local time; kept for older readers
                       "ingredient_hints": hints,  # tags that are search hints (no chip on the catalog page)
                       "books": books}, ensure_ascii=False, indent=1)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=cookbooks.json"})


@app.get("/admin/api/export.csv")
@editor_required
def export_csv():
    rows = book_dicts(db().execute("SELECT * FROM books ORDER BY id").fetchall(), True)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "uid", "title", "subtitle", "authors", "publisher", "year", "isbn", "cuisine", "tags", "status",
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
    import photos  # noqa: E402,F401 — photos of dishes cooked from the books
    import online  # noqa: E402,F401 — recipes from websites
    import ask  # noqa: E402,F401 — AI search
    init_db()
