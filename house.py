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
import os, re, unicodedata

from app import app, clean

HOUSE_BOOK_TITLE = (os.environ.get("HOUSE_BOOK_TITLE") or "").strip().strip("'\"").strip() or "House Recipes"
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
