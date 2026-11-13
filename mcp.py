"""The MCP server (Streamable HTTP, stateless JSON) at /mcp, registered onto the core app.

Also /admin/api/features, which reports which of the optional integrations are configured. Tools, access levels
(public / private / write), token handling and the log redaction wrapper live here; Ask (ask.py) reuses run_tool().
"""
import hmac, json, logging, os, re, secrets

from flask import jsonify, request, Response

import app as core
from app import app, db, editor_required, fold, clean
import cook
import dinnerlog
import scan


MCP_TOKEN = os.environ.get("MCP_TOKEN", "").strip()  # one token, or several comma-separated (for rotation)
MCP_PUBLIC = os.environ.get("MCP_PUBLIC", "1") == "1"
MCP_WRITE = os.environ.get("MCP_WRITE", "0") == "1"          # 1 = write tools for a token sent as a Bearer header
MCP_URL_TOKEN = os.environ.get("MCP_URL_TOKEN", "1") == "1"  # 0 = refuse /mcp/<token>; it is always read-only


@app.get("/admin/api/features")
@editor_required
def features():
    import ask  # local: ask imports mcp
    import wishlist  # local: importing it registers its routes, which must come after /mcp's
    return jsonify(ask=ask.enabled(), photo_import=bool(scan.ANTHROPIC_API_KEY), dinnerlog=bool(dinnerlog.DINNERLOG_API_URL),
                   dinnerlog_url=dinnerlog.DINNERLOG_URL or None, wishlist=wishlist.wishlist_available(),
                   wishlist_url=wishlist.WISHLIST_PUBLIC_URL or None,
                   mcp_private=bool(mcp_tokens()), mcp_public=MCP_PUBLIC, mcp_write=MCP_WRITE,
                   mcp_url_token=MCP_URL_TOKEN)


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
                    "the stock itself); when next_offset is present there are more results to fetch. "
                    "Some recipes are our own (the house book): they have full ingredients and steps instead of a page, "
                    "full: true in search results, and 'ingredients' in match when a word is in their ingredient "
                    "list; get_recipe returns them.")

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
    keys = ["id", "name", "heading", "page", "url", "book_id", "book_title", "match", "full"]
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
    import house
    full = house.detail_ids(db(), [r["id"] for r in rows])
    for r in rows:
        r["match"] = match[r["id"]]
        r["full"] = r["id"] in full  # a house recipe: get_recipe has its ingredients and steps
    if private:
        dinnerlog.ensure_synced()
        st = dinnerlog.cooked_stats_for([r["id"] for r in rows])
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
    else:  # wanted books aren't the collection: list_wanted has them
        sql += " AND " + core.IN_COLLECTION
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
        dinnerlog.ensure_synced()
        cb = dinnerlog.cooked_by_book([b["id"] for b in books])
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
    if not row or (not private and not core.book_public(row)):
        raise ToolError("No such book")
    b = core.book_dicts([row], private)[0]
    # Paged: a well-indexed book has hundreds of recipes, and the whole list would go into the model's context.
    limit = max(1, min(int(a.get("limit") or GET_BOOK_LIMIT), GET_BOOK_MAX))
    offset = max(0, int(a.get("offset") or 0))
    recs = [dict(r) for r in con.execute(
        "SELECT * FROM recipes WHERE book_id=? ORDER BY page_sort IS NULL, page_sort, name LIMIT ? OFFSET ?",
        (b["id"], limit, offset))]
    if private:
        dinnerlog.ensure_synced()
        st = dinnerlog.cooked_stats_for([r["id"] for r in recs])
        for r in recs:
            r.update(st.get(r["id"], {}))
        b.update(dinnerlog.cooked_by_book([b["id"]]).get(b["id"], {}))
    out = _book_out(b, private)
    if b.get("description"):
        out["description"] = b["description"]
    out["recipes"] = [{k: v for k, v in _recipe_out(r, private).items() if k not in ("book_id", "book_title")} for r in recs]
    if offset:
        out["offset"] = offset
    if offset + len(recs) < (b.get("recipe_count") or 0):
        out["next_offset"] = offset + len(recs)  # more recipes: call again with this offset
    return out


def t_get_recipe(a, private):
    """One recipe in full: a house recipe's ingredients and steps, or a cookbook recipe's book and page."""
    import house
    con = db()
    if a.get("recipe_id") not in (None, ""):
        row = con.execute(house.RECIPE_SQL + " WHERE r.id=?", (int(a["recipe_id"]),)).fetchone()
        if row and not private and not core.book_public(row):
            row = None
    elif a.get("uid"):
        row = house.find_recipe(con, str(a["uid"]).strip().lower(), private)
    else:
        raise ToolError("recipe_id or uid is required")
    if not row:
        raise ToolError("No such recipe")
    return house.recipe_for_model(con, row, private)


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
        "totals": {"books": con.execute(f"SELECT count(*) FROM books b WHERE {core.COOKBOOKS}" + pub_and).fetchone()[0],
                   "recipes": con.execute("SELECT count(*) FROM recipes r JOIN books b ON b.id=r.book_id"
                                          + pub).fetchone()[0]},
    }


def t_suggest(a, private):
    res = cook.suggest(a.get("mode") or "want", a.get("cuisine"), a.get("tag"), max(1, min(int(a.get("count") or 5), 20)))
    res["recipes"] = [_recipe_out(r, True) for r in res["recipes"]]
    return res


SHELF_LIST_MAX = 25  # books in cooking_stats' shelf_check.lapsed; the tool output stays in the assistant's context


def t_stats(a, private):
    res = cook.stats()
    sc = res["shelf_check"]
    sc["lapsed_total"], sc["lapsed"] = len(sc["lapsed"]), sc["lapsed"][:SHELF_LIST_MAX]  # longest ago first
    return res


def t_set_want(a, private):
    rid = int(a["recipe_id"])
    con = db()
    if not con.execute("SELECT 1 FROM recipes WHERE id=?", (rid,)).fetchone():
        raise ToolError("No such recipe")
    want = a.get("want", True) not in (False, 0, "false", "False", "0", "no")
    import changes
    rec = changes.Recorder(con, op="MCP", actor="mcp")
    rec.recipe(rid)
    con.execute("UPDATE recipes SET want_to_try=? WHERE id=?", (1 if want else 0, rid))
    rec.finish()
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
    import changes
    rec = changes.Recorder(con, op="MCP", actor="mcp")
    rec.book(bid)
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
    rec.finish()
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
    ("get_recipe", False, t_get_recipe,
     "One recipe by recipe_id (from search_recipes or get_book) or uid. For our own recipes (full: true): description, "
     "yield, servings, times, methods, equipment, notes, the ingredient list (in order, with sections) and the steps "
     "(with timers). For a cookbook recipe: its book and page, since the text is in the book.",
     _S(recipe_id={"type": "integer"}, uid={"type": "string", "description": "The recipe's 32-character uid"})),
    ("list_categories", False, t_list_categories, "Cuisines and tags used in the collection, with book counts.", _S()),
    ("suggest_recipes", True, t_suggest,
     "Random dinner ideas from the collection. Modes: want (starred, never cooked), untried (never cooked), "
     "neglected (from books not cooked from in a year), favorites (rated 8+, not made in 3 months).",
     _S(mode={"type": "string", "enum": list(cook.SUGGEST_MODES)}, cuisine={"type": "string"}, tag={"type": "string"},
        count={"type": "integer", "minimum": 1, "maximum": 20, "default": 5,
               "description": "How many ideas, default 5, at most 20"})),
    ("cooking_stats", True, t_stats,
     "What we've cooked from our cookbooks (from Dinner Log): most-used books, top-rated recipes, books never cooked from. "
     "shelf_check says what the collection is neglecting: lapsed (cooked from before, not in a year, longest ago first, "
     f"at most {SHELF_LIST_MAX} of lapsed_total), unrated_most_cooked (books we cook from most that have no rating) "
     "and index (cookbooks with and without a recipe index).",
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
    app returns, so the log never sees the secret. (Cloudflare's own logs still see the path, which is why a
    path token is read-only and MCP_URL_TOKEN=0 turns it off; clients that can send an Authorization header
    should use `Bearer <MCP_TOKEN>` instead.)"""
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


def mcp_tokens():
    """The accepted private tokens: MCP_TOKEN split on commas, blanks dropped. Several at once let a token be
    rotated without downtime (add the new one, move the clients, remove the old one)."""
    return [t for t in (p.strip() for p in MCP_TOKEN.split(",")) if t]


def token_ok(supplied):
    """True if `supplied` is one of mcp_tokens(). Every candidate is compared, in constant time, with no early
    exit, so the time taken doesn't say which token (or how many) matched."""
    ok = False
    for t in mcp_tokens():
        # compare bytes: compare_digest raises TypeError on non-ASCII str
        ok |= hmac.compare_digest(supplied.encode(), t.encode())
    return ok


def mcp_auth(path_token):
    """Returns 'write', 'private', 'public' or None (refused).

    - No token: 'public' if MCP_PUBLIC, else refused.
    - Token in the path (/mcp/<token>): refused when MCP_URL_TOKEN is off; otherwise at most 'private'. A URL ends
      up in proxy logs, browser history and pasted links, so it never unlocks the write tools.
    - Token in an Authorization: Bearer header: 'write' when MCP_WRITE is on, else 'private'.
    - A wrong token is refused, never downgraded to the public view."""
    if path_token:
        if not MCP_URL_TOKEN:
            return None
        return "private" if token_ok(path_token) else None
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if bearer:
        if not token_ok(bearer):
            return None
        return "write" if MCP_WRITE else "private"
    return "public" if MCP_PUBLIC else None


def rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


class UnknownTool(LookupError):
    """No tool by that name is available to this caller (including private-only tools when not private)."""


def tool_allowed(name, private_only, private, write):
    """Private-only tools need `private`; WRITE_TOOLS also need `write` (and are private-only)."""
    if name in WRITE_TOOLS:
        return private and write
    return private or not private_only


def run_tool(name, args, private, write=False):
    """Run one MCP tool: (result, None) on success, (None, error_text) for a ToolError or a bug (redacted).
    Raises UnknownTool when no tool matches or the caller may not use it. Shared by handle_rpc and the Ask
    search; Ask never passes `write`, so WRITE_TOOLS can't run from it."""
    tool = next((t for t in TOOLS if t[0] == name and tool_allowed(t[0], t[1], private, write)), None)
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


def handle_rpc(msg, private, write=False):
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
            for n, priv_only, _, d, s in TOOLS if tool_allowed(n, priv_only, private, write)]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            res, err_text = run_tool(name, args, private, write)
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
    private, write = level in ("private", "write"), level == "write"
    if isinstance(payload, list):
        if len(payload) > MCP_BATCH_MAX:
            return jsonify(rpc_error(None, -32600, f"Batch too large: at most {MCP_BATCH_MAX} requests")), 400
        out = [r for r in (handle_rpc(m, private, write) for m in payload) if r is not None]
        return (jsonify(out), 200) if out else Response(status=202)
    res = handle_rpc(payload, private, write)
    return (jsonify(res), 200) if res is not None else Response(status=202)
