"""Recipes from websites (NYT Cooking, Serious Eats, ...), indexed like a cookbook: a name and a link.

Each site is a book with kind='online' (created the first time one of its links is added), and its recipes
have a url instead of a page. Only the link is stored: the title comes from the link itself, so no page is
fetched, and opening a recipe uses your own signed-in browser. Online sources are for editors only.
"""
import re, urllib.parse

from flask import jsonify, request

import app as core
from app import app, db, editor_required, make_err, clean

SITES = {"cooking.nytimes.com": "NYT Cooking", "seriouseats.com": "Serious Eats", "bonappetit.com": "Bon Appétit",
         "epicurious.com": "Epicurious", "food52.com": "Food52", "smittenkitchen.com": "Smitten Kitchen",
         "kingarthurbaking.com": "King Arthur Baking", "americastestkitchen.com": "America's Test Kitchen",
         "cooksillustrated.com": "Cook's Illustrated", "cookscountry.com": "Cook's Country",
         "budgetbytes.com": "Budget Bytes", "thekitchn.com": "The Kitchn", "allrecipes.com": "Allrecipes",
         "foodandwine.com": "Food & Wine", "bbcgoodfood.com": "BBC Good Food", "simplyrecipes.com": "Simply Recipes",
         "food.com": "Food.com", "foodnetwork.com": "Food Network", "eatingwell.com": "EatingWell"}
URL_RE = re.compile(r"https?://[^\s<>\"'\]\[]+")
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with"}


def migrate(con):
    books = [r[1] for r in con.execute("PRAGMA table_info(books)")]
    if "kind" not in books:
        con.execute("ALTER TABLE books ADD COLUMN kind TEXT")  # NULL = a cookbook; 'online' = a recipe website
    if "site" not in books:
        con.execute("ALTER TABLE books ADD COLUMN site TEXT")
    recipes = [r[1] for r in con.execute("PRAGMA table_info(recipes)")]
    if "url" not in recipes:
        con.execute("ALTER TABLE recipes ADD COLUMN url TEXT")
    if "ext_id" not in recipes:
        con.execute("ALTER TABLE recipes ADD COLUMN ext_id TEXT")  # the site's id for the recipe: no duplicates
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS recipes_ext ON recipes(book_id, ext_id) WHERE ext_id IS NOT NULL")


def title_from_slug(slug):
    """'french-onion-soup-with-gruyere' -> 'French Onion Soup with Gruyere'."""
    words = [w for w in re.split(r"[-_+\s]+", slug) if w]
    return " ".join(w if i and w in SMALL else w[:1].upper() + w[1:] for i, w in enumerate(words))


def parse_link(url):
    """A recipe link -> {url, site, site_name, ext_id, title}, or None if it isn't one we can index."""
    try:
        u = urllib.parse.urlsplit(url.strip().rstrip(").,;!"))
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = re.sub(r"^(www|m|amp)\.", "", u.hostname.lower())
    path = re.sub(r"/+$", "", u.path)
    if host == "cooking.nytimes.com":
        m = re.fullmatch(r"/recipes/(\d+)(?:-([^/]+))?", path)
        if not m:  # recipe box, collections, search: not a recipe
            return None
        ext, slug = m.group(1), m.group(2) or ""
        canon = f"https://cooking.nytimes.com/recipes/{ext}" + (f"-{slug}" if slug else "")
    else:
        segs = [urllib.parse.unquote(s) for s in path.split("/") if s]
        if not segs:
            return None
        ext, canon = host + path.lower(), f"https://{u.hostname.lower()}{path}"
        slug = re.sub(r"\.(html?|php|aspx?)$", "", segs[-1], flags=re.I)
        if re.fullmatch(r"\d*", slug) and len(segs) > 1:  # .../best-chili/12345
            slug = segs[-2]
        slug = re.sub(r"^(recipe-)|^\d+-|-(recipes?)$|-\d+$", "", slug.lower())
        slug = re.sub(r"-(recipes?)$", "", slug)
    title = title_from_slug(urllib.parse.unquote(slug))
    return {"url": canon, "site": host, "site_name": SITES.get(host, host), "ext_id": ext, "title": title or host}


def links_in(text):
    """Every recipe link in pasted text, in order, without repeats; plus the links that weren't recipes."""
    found, seen, skipped = [], set(), []
    for raw in URL_RE.findall(text or ""):
        item = parse_link(raw)
        if not item:
            skipped.append(raw)
        elif (item["site"], item["ext_id"]) not in seen:
            seen.add((item["site"], item["ext_id"]))
            found.append(item)
    return found, skipped


def site_book(con, item, create=True):
    row = con.execute("SELECT id FROM books WHERE kind='online' AND site=?", (item["site"],)).fetchone()
    if row or not create:
        return row[0] if row else None
    bid = con.execute("INSERT INTO books(title, kind, site, status, source) VALUES (?, 'online', ?, 'owned', 'online')",
                      (item["site_name"], item["site"])).lastrowid
    core.reindex_book(con, bid)
    return bid


def existing(con, item):
    bid = site_book(con, item, create=False)
    row = bid and con.execute("SELECT id FROM recipes WHERE book_id=? AND ext_id=?", (bid, item["ext_id"])).fetchone()
    return row[0] if row else None


def match_meal(con, *texts):
    """(book_id, recipe_id) for a Dinner Log meal whose source holds a link to a recipe saved here."""
    for t in texts:
        for raw in URL_RE.findall(t or ""):
            item = parse_link(raw)
            rid = item and existing(con, item)
            if rid:
                return con.execute("SELECT book_id FROM recipes WHERE id=?", (rid,)).fetchone()[0], rid
    return None, None


@app.post("/admin/api/online/preview")
@editor_required
def online_preview():
    con = db()
    found, skipped = links_in((request.get_json(force=True) or {}).get("text"))
    for item in found:
        item["recipe_id"] = existing(con, item)
    return jsonify(items=found, skipped=skipped)


@app.post("/admin/api/online/commit")
@editor_required
def online_commit():
    """Save recipe links: [{url, title?, want_to_try?}]. The link is parsed again here; a title typed in
    the preview replaces the one taken from the link. Links already saved are left as they are."""
    con = db()
    added, duplicates, books = 0, 0, set()
    for row in (request.get_json(force=True) or {}).get("items", []):
        item = parse_link(str(row.get("url") or ""))
        if not item:
            continue
        if existing(con, item):
            duplicates += 1
            continue
        bid = site_book(con, item)
        books.add(bid)
        con.execute("INSERT INTO recipes(book_id, name, url, ext_id, want_to_try, source) VALUES (?,?,?,?,?, 'online')",
                    (bid, clean(row.get("title")) or item["title"], item["url"], item["ext_id"],
                     1 if row.get("want_to_try") else 0))
        added += 1
    if not added and not duplicates:
        return make_err(400, "No recipe links to save")
    con.commit()
    return jsonify(added=added, duplicates=duplicates, books=[
        {"id": bid, "title": con.execute("SELECT title FROM books WHERE id=?", (bid,)).fetchone()[0]} for bid in sorted(books)])
