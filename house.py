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
import os

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
