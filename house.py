"""House recipes: full recipes the family develops, kept in one house book (books.kind='house').

The house book is an ordinary row in `books`, so the catalog, search, Cook, Dinner Log matching and the MCP all
see it like any other book. What a cookbook index doesn't have lives beside the recipe row:

  recipe_details           1:1 with recipes: description, yield, servings, times, methods, equipment, notes, source
  recipe_ingredient_lines  ordered ingredient lines (the name recipe_ingredients is the EYB tag table)
  recipe_steps             ordered steps, each with an optional timer

Only recipes in the house book carry details (see write routes). `books.visibility` ('public' | 'members') is
the gate for anonymous readers: the house book is created 'members', every existing book stays 'public'. Every
anonymous read path filters with app.PUBLIC_BOOKS or app.book_public().
"""
import json, os, re, unicodedata, urllib.parse

from flask import abort, g, jsonify, request

from app import UID_RE, app, book_public, clean, db, editor_required, fold, make_err, reader_required

HOUSE_BOOK_TITLE = (os.environ.get("HOUSE_BOOK_TITLE") or "").strip().strip("'\"").strip() or "House Recipes"
# Origins the recipe editor may send the browser back to after a save (?return=), e.g. Dinner Log's public address.
# Exact scheme://host[:port] matches only; relative paths on this site are always allowed. Empty = only those.
RETURN_URL_ORIGINS_RAW = os.environ.get("RETURN_URL_ORIGINS", "")
VISIBILITIES = ("public", "members")

SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS books_one_house ON books(kind) WHERE kind = 'house';
CREATE TABLE IF NOT EXISTS recipe_details (
  recipe_id INTEGER PRIMARY KEY REFERENCES recipes(id) ON DELETE CASCADE,
  description TEXT,
  yield_text TEXT,
  servings INTEGER,
  prep_minutes INTEGER, cook_minutes INTEGER, total_minutes INTEGER,
  methods TEXT,      -- JSON array of strings: "braise", "grill"
  equipment TEXT,    -- JSON array of strings: "Dutch oven"
  notes TEXT,
  inspiration TEXT,  -- where the idea came from: a person, a restaurant, a book and page
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS recipe_ingredient_lines (
  id INTEGER PRIMARY KEY,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  section TEXT,          -- "For the sauce"
  quantity_text TEXT,    -- as typed: "1 1/2", "½", "2-3", "a pinch"
  quantity_num REAL,     -- parsed, for scaling; NULL when quantity_text isn't a number
  quantity_max REAL,     -- the top of a range ("2-3" -> 2 and 3); NULL otherwise
  unit TEXT,
  item TEXT NOT NULL,
  prep_note TEXT,        -- "finely chopped"
  optional INTEGER NOT NULL DEFAULT 0,
  UNIQUE (recipe_id, position));
CREATE TABLE IF NOT EXISTS recipe_steps (
  id INTEGER PRIMARY KEY,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  section TEXT,
  text TEXT NOT NULL,
  timer_minutes INTEGER,
  UNIQUE (recipe_id, position));
"""


def migrate(con):
    """Additive only: one column on books, three new tables, one partial index. Safe to run on every start."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    if "visibility" not in cols:
        # Existing rows take the default, so everything already in the catalog stays exactly as public as it was.
        con.execute("ALTER TABLE books ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public' "
                    "CHECK (visibility IN ('public', 'members'))")
    con.executescript(SCHEMA)


def ensure_house_book(con):
    """Create the house book if there isn't one; return its id. Runs after seeding (an empty catalog is seeded only
    while it has no books at all) under init_db's file lock, and the partial unique index makes a second house book
    impossible even if two processes raced. The title is used only when the book is created: renaming it in the
    editor sticks, and changing HOUSE_BOOK_TITLE later doesn't rename it."""
    row = con.execute("SELECT id FROM books WHERE kind='house'").fetchone()
    if row:
        return row[0]
    import app as core
    bid = con.execute("INSERT INTO books(title, kind, status, visibility, source) "
                      "VALUES (?, 'house', 'owned', 'members', 'house')", (clean(HOUSE_BOOK_TITLE),)).lastrowid
    core.reindex_book(con, bid)
    app.logger.info("created the house book (id %s)", bid)
    return bid


def house_book_id(con):
    row = con.execute("SELECT id FROM books WHERE kind='house'").fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ quantities
# quantity_text is kept exactly as typed; quantity_num (and quantity_max for a range) is what scaling multiplies.
# Anything that isn't plainly a number ("a pinch", "1 (14-oz) can", "to taste") is stored as text with no number,
# never refused.
VULGAR = {"¼": "1/4", "½": "1/2", "¾": "3/4", "⅐": "1/7", "⅑": "1/9", "⅒": "1/10", "⅓": "1/3", "⅔": "2/3",
          "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8",
          "⅝": "5/8", "⅞": "7/8"}
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "half": 0.5, "dozen": 12}
QUALIFIER = re.compile(r"^(?:about|approx(?:\.|imately)?|around|roughly|ca\.?|~)\s*")
AMOUNT = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|[a-z]+)"
QUANTITY = re.compile(rf"(?P<a>{AMOUNT})(?:\s*(?:-|to|or)\s*(?P<b>{AMOUNT}))?")
QUANTITY_MAX = 1_000_000


def _amount(tok):
    """'1 1/2' -> 1.5, '3/4' -> 0.75, '1,5' -> 1.5, '1,500' -> 1500.0, 'two' -> 2.0; None if it isn't one."""
    if tok in WORDS:
        return float(WORDS[tok])
    m = re.fullmatch(r"(?:(\d+)\s+)?(\d+)/(\d+)", tok)
    if m:
        whole, n, d = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return whole + n / d if d else None
    if re.fullmatch(r"\d+,\d{3}", tok):  # a thousands separator: "1,500" (grams)
        return float(tok.replace(",", ""))
    if re.fullmatch(r"\d+(?:[.,]\d+)?", tok):  # "1.5", or "1,5" as Italian and most of Europe write it
        return float(tok.replace(",", "."))
    return None


def parse_quantity(text):
    """(quantity_num, quantity_max) for a typed quantity, or (None, None) when it isn't plainly a number.

        "2" -> (2, None)    "1 1/2", "1½", "1-1/2", "1.5", "1,5" -> (1.5, None)    "½" -> (0.5, None)
        "2-3", "2–3", "2 to 3", "2 or 3" -> (2, 3)    "about 2" -> (2, None)    "a pinch", "1 (14-oz)" -> (None, None)
    A "range" whose top is a fraction below its bottom is a mixed number written with a hyphen ("1-1/2")."""
    s = clean(text)
    if not s or len(s) > 40:
        return None, None
    s = unicodedata.normalize("NFC", s).lower().replace("⁄", "/").replace("∕", "/")
    s = re.sub(r"[‐-―−]", "-", s)  # en/em dashes, minus
    for ch, frac in VULGAR.items():
        s = s.replace(ch, f" {frac} ")
    s = " ".join(QUALIFIER.sub("", s.strip()).split()).rstrip(".").strip()
    m = QUANTITY.fullmatch(s)
    if not m:
        return None, None
    lo = _amount(m.group("a"))
    hi = _amount(m.group("b")) if m.group("b") else None
    if lo is None or (m.group("b") and hi is None):
        return None, None
    if hi is not None and hi < lo:
        mixed = re.fullmatch(r"(\d+)\s*-\s*(\d+)/(\d+)", s)
        if mixed and int(mixed.group(2)) < int(mixed.group(3)):
            lo, hi = int(mixed.group(1)) + hi, None
        else:
            return None, None
    if hi == lo:
        hi = None
    if not 0 < lo <= QUANTITY_MAX or (hi is not None and hi > QUANTITY_MAX):
        return None, None
    return round(lo, 4), (round(hi, 4) if hi is not None else None)


# ------------------------------------------------------------------ return URLs (?return= on the editor)
DEFAULT_PORTS = {"http": 80, "https": 443}
RETURN_URL_MAX = 2048


def _origin(scheme, host, port):
    return f"{scheme}://{host}" + (f":{port}" if port and port != DEFAULT_PORTS[scheme] else "")


def parse_origins(raw):
    """RETURN_URL_ORIGINS -> {"https://dinner.example.com", ...}. An entry with a path, credentials or another
    scheme is ignored (and logged), so a typo can't widen the list."""
    out = set()
    for entry in (x.strip() for x in (raw or "").strip().strip("'\"").split(",")):
        if not entry:
            continue
        try:
            p = urllib.parse.urlsplit(entry.rstrip("/"))
            port = p.port
        except ValueError:
            p, port = None, None
        if (not p or p.scheme not in DEFAULT_PORTS or not p.hostname or p.username or p.password or p.path
                or p.query or p.fragment):
            app.logger.warning("RETURN_URL_ORIGINS: ignoring %r (want scheme://host[:port])", entry)
            continue
        out.add(_origin(p.scheme, p.hostname, port))
    return out


RETURN_URL_ORIGINS = parse_origins(RETURN_URL_ORIGINS_RAW)


def safe_return_url(raw, origins=None):
    """The URL to send the browser to after a save, or None when it isn't allowed. Allowed: a path on this site
    ("/admin#/book/3"), or an absolute http(s) URL whose origin is exactly one in RETURN_URL_ORIGINS. Refused:
    "//host" and "/\\host" (browsers read both as another host), backslashes and control characters anywhere,
    credentials ("https://ok.example@evil.example"), other schemes (javascript:, data:). The URL returned is rebuilt
    from its parsed parts, so what the browser opens is what was checked."""
    origins = RETURN_URL_ORIGINS if origins is None else origins
    if not isinstance(raw, str) or not raw or len(raw) > RETURN_URL_MAX:
        return None
    if "\\" in raw or any(ord(ch) < 33 or ord(ch) == 127 for ch in raw):
        return None
    if raw.startswith("/"):
        if raw.startswith("//"):
            return None
        return raw
    try:
        p = urllib.parse.urlsplit(raw)
        port = p.port
    except ValueError:
        return None
    if p.scheme not in DEFAULT_PORTS or not p.hostname or p.username is not None or p.password is not None:
        return None
    if p.path and not p.path.startswith("/"):
        return None
    origin = _origin(p.scheme, p.hostname, port)
    if origin not in origins:
        return None
    return urllib.parse.urlunsplit((p.scheme, origin.split("://", 1)[1], p.path or "/", p.query, p.fragment))


def with_ids(url, book_uid, recipe_uid):
    """Add the new recipe's stable ids to the return URL's query, so the page that sent us here can link it."""
    p = urllib.parse.urlsplit(url)
    extra = urllib.parse.urlencode({"cookbook_book_uid": book_uid, "cookbook_recipe_uid": recipe_uid})
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, f"{p.query}&{extra}" if p.query else extra, p.fragment))


@app.get("/admin/api/house/return-check")
@editor_required
def return_check():
    """For the editor's "Save and go back" button: may ?return= be followed? Nothing is redirected here."""
    url = safe_return_url(request.args.get("url"))
    host = urllib.parse.urlsplit(url).hostname if url and not url.startswith("/") else None
    return jsonify(ok=bool(url), host=host)


# ------------------------------------------------------------------ the recipe document
class DocError(ValueError):
    """A recipe that can't be saved as sent; the message is for the editor (and the importer's report)."""


LIMITS = {"name": 200, "description": 4000, "yield_text": 100, "notes": 8000, "inspiration": 500, "section": 100,
          "quantity_text": 40, "unit": 30, "item": 200, "prep_note": 200, "text": 4000, "list_item": 60}
MAX_LIST, MAX_LINES, MAX_STEPS = 20, 200, 100
MAX_MINUTES = 7 * 24 * 60  # a week, for cures and ferments
DETAIL_TEXT = ("description", "yield_text", "notes", "inspiration")
DETAIL_INT = {"servings": (1, 1000), "prep_minutes": (0, MAX_MINUTES), "cook_minutes": (0, MAX_MINUTES),
              "total_minutes": (0, MAX_MINUTES)}
DETAIL_LIST = ("methods", "equipment")
DETAIL_FIELDS = DETAIL_TEXT + tuple(DETAIL_INT) + DETAIL_LIST


def _text(v, label, key):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        raise DocError(f"{label} must be text")
    v = clean(v)
    if v and len(v) > LIMITS[key]:
        raise DocError(f"{label} is too long (at most {LIMITS[key]} characters)")
    return v


def _int(v, label, lo, hi):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        v = int(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise DocError(f"{label} must be a whole number from {lo} to {hi}")
    return v


def _list(v, label):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        raise DocError(f"{label} must be a list")
    out = []
    for x in v:
        x = _text(x, f"each of {label}", "list_item")
        if x and fold(x) not in map(fold, out):
            out.append(x)
    if len(out) > MAX_LIST:
        raise DocError(f"{label}: at most {MAX_LIST}")
    return out


def parse_doc(data, need_name=True):
    """A recipe document from the editor (or an export) -> {"name", "detail", "ingredients", "steps"}.
    Lists are in display order; position is the index. Blank rows (an editor's empty last line) are dropped."""
    if not isinstance(data, dict):
        raise DocError("Send the recipe as a JSON object")
    name = _text(data.get("name", data.get("title")), "Name", "name")
    if need_name and not name:
        raise DocError("A recipe needs a name")
    detail = {k: _text(data.get(k), k.replace("_", " ").capitalize(), k) for k in DETAIL_TEXT}
    for k, (lo, hi) in DETAIL_INT.items():
        detail[k] = _int(data.get(k), k.replace("_", " ").capitalize(), lo, hi)
    for k in DETAIL_LIST:
        detail[k] = _list(data.get(k), k.capitalize())
    raw_lines = data.get("ingredients") or []
    raw_steps = data.get("steps") or []
    if not isinstance(raw_lines, list) or not isinstance(raw_steps, list):
        raise DocError("ingredients and steps must be lists")
    lines = []
    for n, x in enumerate(raw_lines, 1):
        if not isinstance(x, dict):
            raise DocError(f"Ingredient {n} isn't an object")
        line = {"section": _text(x.get("section"), f"Ingredient {n}: section", "section"),
                "quantity_text": _text(x.get("quantity_text", x.get("quantity")), f"Ingredient {n}: quantity",
                                       "quantity_text"),
                "unit": _text(x.get("unit"), f"Ingredient {n}: unit", "unit"),
                "item": _text(x.get("item"), f"Ingredient {n}: ingredient", "item"),
                "prep_note": _text(x.get("prep_note"), f"Ingredient {n}: note", "prep_note"),
                "optional": 1 if x.get("optional") in (True, 1, "1", "true", "yes") else 0}
        if not (line["quantity_text"] or line["unit"] or line["item"] or line["prep_note"]):
            continue  # an empty row
        if not line["item"]:
            raise DocError(f"Ingredient {n} needs an ingredient name")
        line["quantity_num"], line["quantity_max"] = parse_quantity(line["quantity_text"])
        lines.append(line)
    steps = []
    for n, x in enumerate(raw_steps, 1):
        if isinstance(x, str):
            x = {"text": x}
        if not isinstance(x, dict):
            raise DocError(f"Step {n} isn't an object")
        step = {"section": _text(x.get("section"), f"Step {n}: section", "section"),
                "text": _text(x.get("text"), f"Step {n}", "text"),
                "timer_minutes": _int(x.get("timer_minutes"), f"Step {n}: timer", 1, MAX_MINUTES)}
        if not step["text"]:
            if step["timer_minutes"]:
                raise DocError(f"Step {n} has a timer but no text")
            continue
        steps.append(step)
    if len(lines) > MAX_LINES or len(steps) > MAX_STEPS:
        raise DocError(f"At most {MAX_LINES} ingredients and {MAX_STEPS} steps")
    return {"name": name, "detail": detail, "ingredients": lines, "steps": steps}


def save_doc(con, rid, doc, updated_at=None):
    """Write a parsed document's details, lines and steps for recipe `rid`, replacing what was there."""
    d = {**doc["detail"]}
    for k in DETAIL_LIST:
        d[k] = json.dumps(d[k], ensure_ascii=False) if d[k] else None
    cols = list(DETAIL_FIELDS)
    con.execute(
        f"INSERT INTO recipe_details(recipe_id, {', '.join(cols)}, updated_at) "
        f"VALUES (?, {', '.join('?' * len(cols))}, coalesce(?, CURRENT_TIMESTAMP)) "
        f"ON CONFLICT(recipe_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}, "
        "updated_at=excluded.updated_at",
        [rid, *(d[c] for c in cols), updated_at])
    con.execute("DELETE FROM recipe_ingredient_lines WHERE recipe_id=?", (rid,))
    con.execute("DELETE FROM recipe_steps WHERE recipe_id=?", (rid,))
    con.executemany(
        "INSERT INTO recipe_ingredient_lines(recipe_id, position, section, quantity_text, quantity_num, quantity_max, "
        "unit, item, prep_note, optional) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(rid, i, x["section"], x["quantity_text"], x["quantity_num"], x["quantity_max"], x["unit"], x["item"],
          x["prep_note"], x["optional"]) for i, x in enumerate(doc["ingredients"])])
    con.executemany("INSERT INTO recipe_steps(recipe_id, position, section, text, timer_minutes) VALUES (?,?,?,?,?)",
                    [(rid, i, x["section"], x["text"], x["timer_minutes"]) for i, x in enumerate(doc["steps"])])


LINE_KEYS = ("section", "quantity_text", "quantity_num", "quantity_max", "unit", "item", "prep_note", "optional")
STEP_KEYS = ("section", "text", "timer_minutes")


def load_doc(con, rid):
    """{"detail": {...} or None, "ingredients": [...], "steps": [...]} for one recipe."""
    row = con.execute("SELECT * FROM recipe_details WHERE recipe_id=?", (rid,)).fetchone()
    detail = None
    if row:
        detail = {k: row[k] for k in DETAIL_FIELDS + ("updated_at",)}
        for k in DETAIL_LIST:
            try:
                detail[k] = json.loads(row[k]) if row[k] else []
            except ValueError:
                detail[k] = []
    lines = [{k: (bool(r[k]) if k == "optional" else r[k]) for k in LINE_KEYS} for r in con.execute(
        "SELECT * FROM recipe_ingredient_lines WHERE recipe_id=? ORDER BY position", (rid,))]
    steps = [{k: r[k] for k in STEP_KEYS} for r in con.execute(
        "SELECT * FROM recipe_steps WHERE recipe_id=? ORDER BY position", (rid,))]
    return {"detail": detail, "ingredients": lines, "steps": steps}


def detail_ids(con, ids):
    """The recipes among `ids` that have details (the catalog links those to their recipe page)."""
    ids = list(ids)
    out = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(r[0] for r in con.execute(
            f"SELECT recipe_id FROM recipe_details WHERE recipe_id IN ({','.join('?' * len(chunk))})", chunk))
    return out


# ------------------------------------------------------------------ read API
PRIVATE_EXTRA = ("want_to_try", "rating", "notes", "last_made")  # the recipe row's own private fields


def recipe_full(con, row, private):
    """One recipe with its book and details, as the recipe page, the Dinner Log API and the MCP send it."""
    doc = load_doc(con, row["id"])
    out = {"id": row["id"], "uid": row["uid"], "title": row["name"], "name": row["name"], "heading": row["heading"],
           "page": row["page"], "book": {"id": row["book_id"], "uid": row["book_uid"], "title": row["book_title"],
                                         "kind": row["book_kind"]},
           "updated_at": (doc["detail"] or {}).get("updated_at") or row["created_at"], **doc}
    if private:
        out.update({k: row[k] for k in PRIVATE_EXTRA})
    return out


RECIPE_SQL = ("SELECT r.*, b.uid AS book_uid, b.title AS book_title, b.kind AS book_kind, b.visibility AS visibility, "
              "b.kind AS kind FROM recipes r JOIN books b ON b.id=r.book_id")


def find_recipe(con, uid, private, house_only=False):
    """The recipe with this uid, if this caller may read it; else None. A recipe in a book the caller can't see
    is indistinguishable from one that doesn't exist."""
    if not UID_RE.fullmatch(uid or ""):
        return None
    row = con.execute(RECIPE_SQL + " WHERE r.uid=?", (uid,)).fetchone()
    if not row or (house_only and row["book_kind"] != "house") or (not private and not book_public(row)):
        return None
    return row


def recipe_by_uid(uid, private, house_only=False):
    con = db()
    row = find_recipe(con, uid, private, house_only)
    if not row:
        abort(make_err(404, "No such recipe"))
    return jsonify(recipe_full(con, row, private))


def house_book(con, private):
    """The house book row if this caller may read it, else None."""
    row = con.execute("SELECT * FROM books WHERE kind='house'").fetchone()
    return row if row and (private or book_public(row)) else None


LIST_MAX = 200


def house_list(private):
    """House recipes, newest change first (or best match first with ?q=): uid, title, updated_at. For Dinner Log's
    "our own" picker. ?q= searches like the catalog does, ingredient lines included."""
    import app as core, ingredients
    con = db()
    book = house_book(con, private)
    if not book:
        abort(make_err(404, "No such book"))
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), LIST_MAX))
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        abort(make_err(400, "limit and offset must be whole numbers"))
    q = (request.args.get("q") or "").strip()
    updated = "coalesce(d.updated_at, r.created_at)"
    if q:
        where, args = core.recipe_filter({"q": q, "book": book["id"]}, True)
        base = " FROM recipes r JOIN books b ON b.id=r.book_id WHERE 1=1" + "".join(" AND " + w for w in where)
        ids = [x["id"] for x in ingredients.ranked(con, base, args, ingredients.search_terms(q, None))]
    else:
        ids = [r[0] for r in con.execute(
            f"SELECT r.id FROM recipes r LEFT JOIN recipe_details d ON d.recipe_id=r.id WHERE r.book_id=? "
            f"ORDER BY {updated} DESC, r.id DESC", (book["id"],))]
    page = ids[offset:offset + limit]
    rows = {r["id"]: r for r in con.execute(
        f"SELECT r.id, r.uid, r.name, {updated} AS updated_at FROM recipes r "
        f"LEFT JOIN recipe_details d ON d.recipe_id=r.id WHERE r.id IN ({','.join('?' * len(page)) or 'NULL'})", page)}
    out = {"book": {"id": book["id"], "uid": book["uid"], "title": book["title"]}, "count": len(ids),
           "recipes": [{"id": i, "uid": rows[i]["uid"], "title": rows[i]["name"], "updated_at": rows[i]["updated_at"]}
                       for i in page if i in rows]}
    if offset + len(page) < len(ids):
        out["next_offset"] = offset + len(page)
    return jsonify(out)


def house_info(private):
    book = house_book(db(), private)
    if not book:
        abort(make_err(404, "No such book"))
    return jsonify(id=book["id"], uid=book["uid"], title=book["title"],
                   **({"visibility": book["visibility"]} if private else {}))


# The admin side takes READ_TOKEN as well as an editor's sign-in: Dinner Log reads house recipes server-to-server.
# reader_required never allows a write; every write below is @editor_required.
for _prefix, _private in (("/api", False), ("/admin/api", True)):
    _guard = reader_required if _private else (lambda f: f)
    app.add_url_rule(f"{_prefix}/house", f"house_info_{_private}", _guard(lambda p=_private: house_info(p)))
    app.add_url_rule(f"{_prefix}/house/recipes", f"house_list_{_private}", _guard(lambda p=_private: house_list(p)))
    app.add_url_rule(f"{_prefix}/house/recipes/<uid>", f"house_recipe_{_private}",
                     _guard(lambda uid, p=_private: recipe_by_uid(uid, p, house_only=True)))
    app.add_url_rule(f"{_prefix}/recipes/by-uid/<uid>", f"recipe_uid_{_private}",
                     _guard(lambda uid, p=_private: recipe_by_uid(uid, p)))


# ------------------------------------------------------------------ write API (editors only)
def _doc_or_400(data):
    try:
        return parse_doc(data)
    except DocError as e:
        abort(make_err(400, str(e)))


def _saved(con, rid, data, status):
    """The response to a save: the recipe, plus where to send the browser if ?return= was given and allowed."""
    row = con.execute(RECIPE_SQL + " WHERE r.id=?", (rid,)).fetchone()
    out = recipe_full(con, row, True)
    raw = data.get("return") if isinstance(data, dict) else None
    if raw:
        url = safe_return_url(raw)
        out["return_url"] = with_ids(url, row["book_uid"], row["uid"]) if url else None
        out["return_refused"] = url is None
        if url is None:
            app.logger.warning("house recipe saved; return URL refused (not in RETURN_URL_ORIGINS): %.200r", raw)
    return jsonify(out), status


@app.post("/admin/api/house/recipes")
@editor_required
def create_house_recipe():
    data = request.get_json(silent=True)
    doc = _doc_or_400(data)
    con = db()
    bid = house_book_id(con) or ensure_house_book(con)
    rid = con.execute("INSERT INTO recipes(book_id, name, source) VALUES (?, ?, 'house')", (bid, doc["name"])).lastrowid
    save_doc(con, rid, doc)
    con.commit()
    app.logger.info("house recipe %s created by %s", rid, g.editor)
    return _saved(con, rid, data, 201)


@app.put("/admin/api/house/recipes/<int:rid>")
@editor_required
def update_house_recipe(rid):
    """Replace a house recipe's name, details, ingredients and steps with what was sent (the editor sends it all,
    in order, so reordering and removing rows need nothing else)."""
    con = db()
    row = con.execute("SELECT b.kind FROM recipes r JOIN books b ON b.id=r.book_id WHERE r.id=?", (rid,)).fetchone()
    if not row:
        return make_err(404, "No such recipe")
    if row["kind"] != "house":
        return make_err(400, "Only recipes in the house book have ingredients and steps")
    data = request.get_json(silent=True)
    doc = _doc_or_400(data)
    con.execute("UPDATE recipes SET name=? WHERE id=?", (doc["name"], rid))
    save_doc(con, rid, doc)
    con.commit()
    return _saved(con, rid, data, 200)


# ------------------------------------------------------------------ export / import
def export_detail(con, rid):
    """A recipe's document for export.json, or None when it has none (the key is then left out)."""
    doc = load_doc(con, rid)
    if not doc["detail"] and not doc["ingredients"] and not doc["steps"]:
        return None
    detail = dict(doc["detail"] or {})
    return {**detail, "ingredients": [{k: x[k] for k in ("section", "quantity_text", "unit", "item", "prep_note",
                                                         "optional")} for x in doc["ingredients"]],
            "steps": doc["steps"]}


# Undo this release's schema (see the PR / README "Rolling back"). Deletes the house book and its recipes, so export
# first. Not needed just to run the previous release: it ignores the new column and tables, and its kind checks
# keep the house book out of every public view.
ROLLBACK_SQL = """
PRAGMA foreign_keys=ON;
DELETE FROM books WHERE kind = 'house';
DROP TABLE IF EXISTS recipe_steps;
DROP TABLE IF EXISTS recipe_ingredient_lines;
DROP TABLE IF EXISTS recipe_details;
DROP INDEX IF EXISTS books_one_house;
ALTER TABLE books DROP COLUMN visibility;
"""
