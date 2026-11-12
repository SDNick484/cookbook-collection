# Wanted Cookbooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An editors-only list of cookbooks we want to buy (price, release date, buy link, priority), hidden from the
main catalog and from the public everywhere, with a "Bought it" step that turns one into an owned book in place.

**Architecture:** Wanted books stay `books` rows with `status='wanted'`. One SQL predicate, `IN_COLLECTION`, is
folded into `PUBLIC_BOOKS`/`book_public`/`COOKBOOKS` (anonymous reads and counts) and added to the editor catalog,
facets and searches. A new module `wanted.py` owns the five new columns, their validation, the release-date logic,
the Wanted API and the Bought-it route. The wish list push reads the new fields; nothing is ever read from or
written to the wish list app's purchase state.

**Tech Stack:** Python 3 / Flask / SQLite (FTS5), vanilla JS single-page app, pytest, Playwright (browser tests).

**Spec:** `docs/superpowers/specs/2026-11-12-wanted-books-design.md`

## Global Constraints

- New columns, exactly: `price_cents INTEGER CHECK (price_cents IS NULL OR price_cents >= 0)`, `price_currency TEXT`,
  `release_date TEXT`, `buy_url TEXT`, `want_priority INTEGER CHECK (want_priority IS NULL OR want_priority BETWEEN 1 AND 3)`.
  Additive only; no backfill; no `EXPORT_VERSION` bump.
- `IN_COLLECTION = "b.status <> 'wanted'"` (in `app.py`), the only spelling of the rule in SQL.
- Every new route is under `/admin/api` and has `@editor_required`. No anonymous path returns a wanted book.
- Never read or write purchase/reserve state in the wish list app.
- No new outbound network calls. One new optional setting: `WISHLIST_PUBLIC_URL` (display link only).
- New module `wanted.py`: imported at the bottom of `app.py` (after `wishlist`), `wanted.migrate(con)` called in
  `init_db` right after `house.migrate(con)`, added to the `cp` list in `installer/build.sh`.
- Writes to books go through `changes.Recorder` on the same connection, `rec.finish()` before `commit()`.
- Tests never touch the network (conftest enforces it); never call Claude or the wish list for real.
- Public-repo hygiene: placeholders only (`wishlist.example.com`, `publisher.example`); no real names in tests,
  fixtures, docs or commit messages. Use names like "Sam".
- Commit messages are conventional commits and end with the session's Co-Authored-By / Claude-Session trailer.
- Every task ends with `python -m pytest -q` and `ruff check .` green.

## Review Focus

1. **Imported rows bypass validation** (`python3 -m importer` writes columns generically): a `buy_url` of
   `javascript:alert(1)` or a `release_date` of `Spring 2027` must not break `/admin/api/wanted` or render as a link.
   Expected: `released` treats an unparseable date as "no date" (out now), and the browser links only `^https?://`.
   Tests: Task 2 (`test_unparseable_release_date_counts_as_released`), Task 4 (`test_wanted_api_survives_imported_junk`).
2. **Typing a price the way people do:** `$35`, ` 35 `, `€24,50`. Expected: accepted (a leading `$`, `€` or `£` and
   surrounding spaces are stripped). `1,234.50` (both separators) is refused rather than guessed.
   Test: Task 2 (`test_clean_fields_price_as_typed`).
3. **Double-tapping "Bought it", or Bought it on a book that isn't wanted.** Expected: the second call is 409 and
   writes no second change row. Test: Task 4 (`test_bought_twice_is_409_and_records_once`).
4. **Wanting a book we already own** (same ISBN). Expected: the existing 409 "Already in the catalog" with `book_id`,
   so the editor lands on the owned copy. Test: Task 2 (`test_wanting_an_owned_isbn_is_a_conflict`).
5. **Editing a wanted book without sending its want fields** (the edit form sends only what it shows). Expected:
   they're left unchanged, and switching status to owned keeps them. Test: Task 2
   (`test_partial_update_keeps_want_fields`).

---

## File map

| File | Change |
|---|---|
| `app.py` | `IN_COLLECTION`; `PUBLIC_BOOKS`, `book_public`, `COOKBOOKS` use it; `book_values` calls `wanted.clean_fields`; `list_books`/`recipe_filter`/`facets` private branches add it; `book_dicts` private adds `released`; import `wanted` at the bottom |
| `wanted.py` (new) | Columns + `migrate`, `clean_fields`, `release_start`, `is_released`, `today`, `wanted_payload`, routes `GET /admin/api/wanted`, `POST /admin/api/books/<id>/bought` |
| `wishlist.py` | Push uses `buy_url`/price/priority; `WISHLIST_PUBLIC_URL` |
| `mcp.py` | Private `search_books` adds `IN_COLLECTION`; new private tool `list_wanted`; `/admin/api/features` gains `wishlist_url` |
| `static/app.js`, `static/style.css` | More panel, `#/wanted` view, Add/Edit want fields, Bought-it dialog, wish dialog pre-fill |
| `installer/build.sh`, `installer/setup-head.sh` | `wanted.py` in `cp`; `WISHLIST_PUBLIC_URL` setting + export |
| `.github/scripts/upgrade_path.py`, `.github/scripts/run-ui-tests.sh` | Column check; new browser test file |
| `tests/test_wanted.py` (new), `tests/test_wanted_ui.py` (new), `tests/test_mcp_access.py`, `tests/test_csp_browser.py` | Tests |
| `README.md`, `CLAUDE.md`, `docs/seam.md` | Docs, each in the task that changes the behavior |

---

### Task 1: Keep wanted books off every public read and out of the counts

**Files:**
- Modify: `app.py:422-431` (`PUBLIC_BOOKS`, `COOKBOOKS`, `book_public`)
- Create: `tests/test_wanted.py`
- Modify: `CLAUDE.md` (the anonymous-reads convention)

**Interfaces:**
- Produces: `app.IN_COLLECTION: str` (SQL, alias `b`); `PUBLIC_BOOKS` and `COOKBOOKS` now include it;
  `book_public(row)` is false when `row["status"] == "wanted"`.
- Produces (test helpers in `tests/test_wanted.py`, reused by later tasks): `ANON = {"REMOTE_ADDR": "203.0.113.9"}`,
  fixture `c()` → `A.app.test_client()` (an editor, via `LAN_EDIT=1` on loopback), `add_wanted(title, **cols) -> (id, uid)`
  inserting through `A.connect()` with `status='wanted'`, `visibility='public'`, committing, and calling
  `A.reindex_book(con, id)`; `mcp(c, name, args, token=None)` copied from `tests/test_house.py:209`.

- [ ] **Step 1: Write the failing tests**

```python
TITLE = "Zzyzx Wanted Tome"

def test_wanted_book_is_not_public():
    bid, _ = add_wanted(TITLE)
    row = q("SELECT * FROM books WHERE id=?", bid)[0]
    assert A.book_public(row) is False

def test_no_anonymous_get_route_shows_a_wanted_book(c, monkeypatch):
    bid, uid = add_wanted(TITLE)
    # every GET rule not under /admin, /static or /mcp, with <book_id>/<int:…> -> bid and <uid> -> uid
    for path in anon_get_paths(book_id=bid, uid=uid):
        r = c.get(path, environ_base=ANON)
        assert TITLE.encode() not in r.data, path

def test_wanted_book_detail_is_404_anonymously(c):
    bid, _ = add_wanted(TITLE)
    assert c.get(f"/api/books/{bid}", environ_base=ANON).status_code == 404

def test_public_mcp_never_returns_a_wanted_book(c):
    add_wanted(TITLE)
    books = mcp(c, "search_books", {"query": "zzyzx"})["structuredContent"]["books"]
    assert books == []

def test_cookbook_counts_exclude_wanted(c):
    before = c.get("/api/facets", environ_base=ANON).json["stats"]["books"]
    add_wanted("Another Wanted Book")
    assert c.get("/api/facets", environ_base=ANON).json["stats"]["books"] == before
    assert c.get("/admin/api/facets").json["stats"]["books"] == q(
        f"SELECT count(*) FROM books b WHERE {A.COOKBOOKS}")[0][0]
```

`anon_get_paths(book_id, uid)` lives in the test file: walk `A.app.url_map`, keep GET rules not starting with
`/admin`, `/static` or equal to `/mcp`, substitute every `<…>` argument (`book_id`/`int` args → `book_id`,
`uid` → `uid`, `rid` → any recipe id, `path` args → `"app.js"`), as `tests/test_int_params.py:_get_routes` does.
Check the public facets/list routes' exact paths with `grep -n '@app.get("/api' app.py` and use those.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python -m pytest tests/test_wanted.py -q`
Expected: FAIL. `book_public` is True; the title appears in `/api/books` output.

- [ ] **Step 3: Implement in `app.py`**

```python
IN_COLLECTION = "b.status <> 'wanted'"  # wanted books are a shopping list, not the collection (wanted.py)
PUBLIC_BOOKS = f"({IN_COLLECTION} AND (b.kind IS NULL OR b.kind = 'house') AND b.visibility = 'public')"
COOKBOOKS = f"(b.kind IS NULL AND {IN_COLLECTION})"
```

`book_public(row)` adds `row["status"] != "wanted" and …`. Update the comment above `PUBLIC_BOOKS` to say wanted
books are excluded. Leave `mcp.py:195`'s `'gone'` filter alone.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q && ruff check .`
Expected: all pass. If `tests/test_house.py`'s count asserts change, they compare against `A.COOKBOOKS` and pass.

- [ ] **Step 5: CLAUDE.md**

In the "Anonymous reads filter with `PUBLIC_BOOKS`…" bullet, add: "`PUBLIC_BOOKS` and `COOKBOOKS` also exclude
`status='wanted'` (`IN_COLLECTION`); editor lists of the collection add `IN_COLLECTION` themselves (see wanted.py)."

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_wanted.py CLAUDE.md
git commit -m "fix(privacy): keep wanted books off public reads"
```

---

### Task 2: Want fields on books (schema, validation, release logic)

**Files:**
- Create: `wanted.py`
- Modify: `app.py` (`book_values` at :1185, `book_dicts` private branch at :~452, bottom-of-file imports, `init_db` at :245)
- Modify: `installer/build.sh:10`, `.github/scripts/upgrade_path.py` (in `verify`), `CLAUDE.md` (map row)
- Test: `tests/test_wanted.py`

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces:
  - `wanted.COLS: dict[str, str]` (the five column declarations, verbatim from Global Constraints)
  - `wanted.migrate(con) -> None`
  - `wanted.clean_fields(data: dict) -> dict` — reads only keys present among `price`, `price_currency`,
    `release_date`, `buy_url`, `want_priority`; returns DB column → value (`price` → `price_cents`); raises
    `ValueError(message)` on bad input. Empty string or None → NULL.
  - `wanted.release_start(s: str | None) -> datetime.date | None` — None for None or unparseable.
  - `wanted.today() -> datetime.date` — `datetime.date.today()` (process `TZ`); tests monkeypatch it.
  - `wanted.is_released(s: str | None, today: datetime.date) -> bool` — `start is None or start <= today`.
  - Private book dicts carry `released: bool` for every book (cheap; the UI only uses it for wanted ones).

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt
import wanted as W

def test_migrate_is_idempotent():
    con = A.connect(); W.migrate(con); W.migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(books)")}; con.close()
    assert set(W.COLS) <= cols

@pytest.mark.parametrize("raw,cents", [("35", 3500), ("35.00", 3500), ("24,50", 2450), ("", None), (None, None)])
def test_clean_fields_price(raw, cents):
    assert W.clean_fields({"price": raw}) == {"price_cents": cents}

@pytest.mark.parametrize("raw,cents", [("$35", 3500), (" 35 ", 3500), ("€24,50", 2450), ("£9.99", 999)])
def test_clean_fields_price_as_typed(raw, cents):
    assert W.clean_fields({"price": raw})["price_cents"] == cents

@pytest.mark.parametrize("raw", ["-1", "1.234", "1,234.50", "1000000.01", "abc"])
def test_clean_fields_price_rejects(raw):
    with pytest.raises(ValueError):
        W.clean_fields({"price": raw})

@pytest.mark.parametrize("data,out", [
    ({"price_currency": "eur"}, {"price_currency": "EUR"}),
    ({"release_date": "2027"}, {"release_date": "2027"}),
    ({"release_date": "2027-03"}, {"release_date": "2027-03"}),
    ({"release_date": "2027-03-17"}, {"release_date": "2027-03-17"}),
    ({"buy_url": "https://publisher.example/pasta"}, {"buy_url": "https://publisher.example/pasta"}),
    ({"want_priority": "1"}, {"want_priority": 1}),
    ({"want_priority": ""}, {"want_priority": None}),
])
def test_clean_fields_accepts(data, out):
    assert W.clean_fields(data) == out

@pytest.mark.parametrize("data", [
    {"price_currency": "EURO"}, {"price_currency": "E1"},
    {"release_date": "2027-13"}, {"release_date": "2027-02-30"}, {"release_date": "March"}, {"release_date": "1899"},
    {"release_date": "2101"}, {"buy_url": "javascript:alert(1)"}, {"buy_url": "ftp://x.example/a"},
    {"buy_url": "https://x.example/" + "a" * 2000}, {"want_priority": 4}, {"want_priority": "high"},
])
def test_clean_fields_rejects(data):
    with pytest.raises(ValueError):
        W.clean_fields(data)

@pytest.mark.parametrize("s,start", [("2027", dt.date(2027, 1, 1)), ("2027-03", dt.date(2027, 3, 1)),
                                     ("2027-03-17", dt.date(2027, 3, 17)), (None, None), ("Spring 2027", None)])
def test_release_start(s, start):
    assert W.release_start(s) == start

def test_is_released_boundaries():
    d = dt.date(2027, 3, 1)
    assert W.is_released("2027-03", d) and not W.is_released("2027-03", d - dt.timedelta(days=1))
    assert W.is_released(None, d)

def test_unparseable_release_date_counts_as_released():
    assert W.is_released("Spring 2027", dt.date(2026, 11, 12))

def test_create_wanted_book_with_fields(c):
    r = c.post("/admin/api/books", json={"title": "Example Pasta Book", "authors": ["A. Author"], "status": "wanted",
               "price": "35.00", "price_currency": "usd", "release_date": "2027-03",
               "buy_url": "https://publisher.example/pasta", "want_priority": 1})
    assert r.status_code == 201
    b = r.json
    assert (b["price_cents"], b["price_currency"], b["release_date"], b["want_priority"]) == (3500, "USD", "2027-03", 1)
    assert "released" in b

def test_bad_want_field_is_400(c):
    r = c.post("/admin/api/books", json={"title": "X", "status": "wanted", "buy_url": "javascript:alert(1)"})
    assert r.status_code == 400 and "link" in r.json["error"].lower()

def test_wanting_an_owned_isbn_is_a_conflict(c):
    owned = c.post("/admin/api/books", json={"title": "Owned One", "isbn": "9780306406157"}).json["id"]
    r = c.post("/admin/api/books", json={"title": "Owned One", "isbn": "9780306406157", "status": "wanted"})
    assert r.status_code == 409 and r.json["book_id"] == owned

def test_partial_update_keeps_want_fields(c):
    bid = c.post("/admin/api/books", json={"title": "Keep Fields", "status": "wanted", "price": "20",
                                           "want_priority": 2}).json["id"]
    c.put(f"/admin/api/books/{bid}", json={"subtitle": "new"})
    c.put(f"/admin/api/books/{bid}", json={"status": "owned"})
    b = c.get(f"/admin/api/books/{bid}").json
    assert (b["price_cents"], b["want_priority"], b["status"]) == (2000, 2, "owned")

def test_public_book_payload_has_no_want_fields(c):
    bid = c.post("/admin/api/books", json={"title": "Public Owned", "price": "9"}).json["id"]
    pub = c.get(f"/api/books/{bid}", environ_base=ANON).json
    assert not set(W.COLS) & set(pub) and "released" not in pub
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `python -m pytest tests/test_wanted.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wanted'`.

- [ ] **Step 3: Create `wanted.py`**

Module docstring: "Cookbooks we want to buy: five columns on books, their validation, release dates, the Wanted
list and Bought it." `from app import app, db, editor_required, make_err` and `import app as core`, like `wishlist.py`.

`clean_fields` rules (the spec's validation table plus Review Focus 2), with these exact 400 messages so the UI can
show them as-is:

| Key | Rule | Message |
|---|---|---|
| `price` | strip spaces and one leading `$`/`€`/`£`; refuse if it has both `,` and `.`; `,` → `.`; `^\d+(\.\d{1,2})?$`; ≤ 1,000,000.00; cents via `Decimal`, not float | "Price is a number like 35 or 24.50" |
| `price_currency` | `^[A-Za-z]{3}$`, upper-cased | "Currency is a three-letter code, like USD or EUR" |
| `release_date` | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, a real date, year 1900–2100 | "Release date is a year, year-month or full date, like 2027-03" |
| `buy_url` | starts `http://` or `https://` (case-insensitive), ≤ 2,000 characters | "The buy link must be an http(s) link" |
| `want_priority` | int 1–3 | "Priority is 1, 2 or 3" |

- [ ] **Step 4: Wire it into `app.py`**

- `book_values(data)`: after the `BOOK_FIELDS` loop, `try: vals.update(wanted.clean_fields(data))` /
  `except ValueError as e: abort(make_err(400, str(e)))`. `create_book`/`update_book` need no change: they write
  whatever `book_values` returns, inside the existing `Recorder`.
- `book_dicts` private branch: `d["released"] = wanted.is_released(r["release_date"], wanted.today())`, computing
  `wanted.today()` once per call.
- `init_db`: `wanted.migrate(con)` right after `house.migrate(con)`. Bottom of file: `import wanted  # noqa: E402,F401`
  after `wishlist`.
- `installer/build.sh:10`: add `wanted.py` after `wishlist.py`.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_wanted.py -q`
Expected: PASS.

- [ ] **Step 6: Upgrade path and CLAUDE.md**

- `.github/scripts/upgrade_path.py`, in `verify()` after the FTS checks: read `PRAGMA table_info(books)` on the new
  database and `check(set(WANT_COLS) <= cols, "books gained the wanted columns")`, with
  `WANT_COLS = ("price_cents", "price_currency", "release_date", "buy_url", "want_priority")` at module top (the
  script runs the old code, so it can't import `wanted`).
- CLAUDE.md map: a `wanted.py` row: "Wanted books (`status='wanted'`): the five want columns (`COLS`, `migrate`),
  `clean_fields` (write validation), `release_start`/`is_released`/`today`, the Wanted list and Bought it routes."

- [ ] **Step 7: Verify everything**

Run: `python -m pytest -q && ruff check . && sh installer/build.sh && python3 .github/scripts/upgrade_path.py`
Expected: all pass; the build lists `wanted.py`; the upgrade script ends `upgrade path OK`.

- [ ] **Step 8: Commit**

```bash
git add wanted.py app.py installer/build.sh .github/scripts/upgrade_path.py CLAUDE.md tests/test_wanted.py
git commit -m "feat(wanted): price, release date, buy link and priority on books"
```

---

### Task 3: Hide wanted books from the editor catalog, facets and searches

**Files:**
- Modify: `app.py` (`list_books` :922, `recipe_filter` :1045, `facets` :1130)
- Modify: `mcp.py:192-195` (`t_search_books`)
- Test: `tests/test_wanted.py`

**Interfaces:**
- Consumes: `app.IN_COLLECTION` (Task 1).
- Produces: `GET /admin/api/books?status=wanted` still returns wanted books (the only editor list that does).

- [ ] **Step 1: Write the failing tests**

```python
def test_editor_catalog_and_search_skip_wanted(c):
    bid, _ = add_wanted("Qwfp Wanted Volume")
    ids = lambda r: {b["id"] for b in r.json["books"]}
    assert bid not in ids(c.get("/admin/api/books"))
    assert bid not in ids(c.get("/admin/api/books?q=qwfp"))
    assert bid in ids(c.get("/admin/api/books?status=wanted"))

def test_editor_recipe_search_skips_wanted_books(c):
    bid, _ = add_wanted("Wanted With Recipes")
    con = A.connect()
    con.execute("INSERT INTO recipes(book_id, name) VALUES (?, 'Xylophone Lasagne')", (bid,)); con.commit(); con.close()
    assert c.get("/admin/api/recipes?q=xylophone").json["count"] == 0

def test_editor_facets_skip_wanted(c):
    add_wanted("Facet Wanted", cuisine="Zanzibari")
    assert all(x["name"] != "Zanzibari" for x in c.get("/admin/api/facets").json["cuisines"])

def test_private_mcp_search_books_skips_wanted(c, monkeypatch):
    monkeypatch.setattr(MC, "MCP_TOKEN", "t0k")
    add_wanted("Mcp Wanted Qzx")
    books = mcp(c, "search_books", {"query": "qzx"}, token="t0k")["structuredContent"]["books"]
    assert books == []
```

Use the editor list/recipe/facet paths as registered (`grep -n '@app.get("/admin/api' app.py`); adjust the
assertions' paths if they differ, not the behavior.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python -m pytest tests/test_wanted.py -q -k "editor or private_mcp"`
Expected: FAIL (wanted book listed).

- [ ] **Step 3: Implement**

- `list_books`: in the `private` path, `if request.args.get("status") != "wanted": sql += " AND " + IN_COLLECTION`.
- `recipe_filter`: `where = [PUBLIC_BOOKS] if not private else [IN_COLLECTION]`.
- `facets`: `pub = f" WHERE {IN_COLLECTION}" if private else f" WHERE {PUBLIC_BOOKS}"`, and `pub_and` likewise;
  the `publishers` query gains `AND b.status <> 'wanted'` (alias it `b`).
- `mcp.t_search_books`: `if private: sql += " AND " + core.IN_COLLECTION`.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q && ruff check .`
Expected: PASS. If an existing test listed a `wanted` book on the editor catalog, change it to use `?status=wanted`
and say so in the commit body.

- [ ] **Step 5: Commit**

```bash
git add app.py mcp.py tests/test_wanted.py
git commit -m "feat(wanted): hide wanted books from the editor catalog and search"
```

---

### Task 4: Wanted list API and Bought it

**Files:**
- Modify: `wanted.py`
- Modify: `docs/seam.md`
- Test: `tests/test_wanted.py`

**Interfaces:**
- Consumes: `wanted.is_released`, `wanted.today` (Task 2); `wishlist.wishes_for_book(book_id) -> list[dict]` (exists:
  `person_name`, `created_at`); `core.book_dicts(rows, True)`; `core.location_arg(raw)`; `changes.Recorder`.
- Produces:
  - `wanted.wanted_payload(rows) -> list[dict]` — one dict per book, keys exactly: `id`, `uid`, `title`, `authors`,
    `publisher`, `isbn`, `cover_url` (`/covers/<cover>` or None), `price_cents`, `price_currency`, `release_date`,
    `released`, `buy_url`, `want_priority`, `on_wish_lists` (person names), `created_at`.
  - `GET /admin/api/wanted[?q=]` → `{"books": wanted_payload(...)}`; `q` uses the same FTS/LIKE path as `list_books`
    (reuse `core.search_words`/`core.fts_query`).
  - `POST /admin/api/books/<int:book_id>/bought`, body `{"location_id": int|null}` → the private book dict (200);
    404 unknown book; 409 `{"error": "That book isn't on the wanted list"}` when status isn't `wanted`.
  - "Out since you added it" is computed in the browser from `created_at`, `release_date` and `released` (Task 7).

- [ ] **Step 1: Write the failing tests**

```python
def test_wanted_api_lists_only_wanted(c):
    bid, uid = add_wanted("Api Wanted", price_cents=3500, price_currency="USD", release_date="2099-01",
                          buy_url="https://publisher.example/a", want_priority=1)
    books = c.get("/admin/api/wanted").json["books"]
    b = next(x for x in books if x["id"] == bid)
    assert b["uid"] == uid and b["released"] is False and b["want_priority"] == 1
    assert set(b) == {"id", "uid", "title", "authors", "publisher", "isbn", "cover_url", "price_cents",
                      "price_currency", "release_date", "released", "buy_url", "want_priority",
                      "on_wish_lists", "created_at"}
    assert all(x["id"] != bid for x in c.get("/admin/api/books").json["books"])

def test_wanted_api_is_editor_only(c):
    assert c.get("/admin/api/wanted", environ_base=ANON).status_code in (401, 403)

def test_wanted_api_survives_imported_junk(c):
    bid, _ = add_wanted("Junk Import", release_date="Spring 2027", buy_url="javascript:alert(1)")
    r = c.get("/admin/api/wanted")
    assert r.status_code == 200 and next(x for x in r.json["books"] if x["id"] == bid)["released"] is True

def test_bought_it_flips_in_place_and_undoes(c):
    bid, uid = add_wanted("Bought Book")
    loc = q("SELECT id FROM locations LIMIT 1")[0][0]
    r = c.post(f"/admin/api/books/{bid}/bought", json={"location_id": loc})
    assert r.status_code == 200 and r.json["status"] == "owned" and r.json["uid"] == uid
    assert bid in {b["id"] for b in c.get("/admin/api/books").json["books"]}
    assert c.get(f"/api/books/{bid}", environ_base=ANON).status_code == 200
    cid = c.get("/admin/api/changes").json["changes"][0]["id"]
    assert c.post(f"/admin/api/changes/{cid}/undo").status_code == 200
    assert q("SELECT status FROM books WHERE id=?", bid)[0][0] == "wanted"

def test_bought_twice_is_409_and_records_once(c):
    bid, _ = add_wanted("Double Tap")
    before = q("SELECT count(*) FROM changes")[0][0]
    assert c.post(f"/admin/api/books/{bid}/bought", json={}).status_code == 200
    assert c.post(f"/admin/api/books/{bid}/bought", json={}).status_code == 409
    assert q("SELECT count(*) FROM changes")[0][0] == before + 1

def test_bought_unknown_book_is_404(c):
    assert c.post("/admin/api/books/999999/bought", json={}).status_code == 404
```

If `seed.json` has no locations, create one in the test through `POST /admin/api/locations` (check the route with
`grep -n 'locations' app.py`). Check the changes list's JSON key (`changes` vs `rows`) in `changes.py:818` and use it.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python -m pytest tests/test_wanted.py -q -k "wanted_api or bought"`
Expected: FAIL with 404 on the new routes.

- [ ] **Step 3: Implement in `wanted.py`**

- `GET /admin/api/wanted` (`@editor_required`): `SELECT b.* FROM books b WHERE b.status='wanted'` plus the optional
  `q` filter, then `wanted_payload`. Wanted books are tens, not thousands: no paging.
- `bought(book_id)` (`@editor_required`): one connection; read the row (404); `status != 'wanted'` → 409;
  `loc = core.location_arg(d.get("location_id"))`, refused with the same 400 as `book_values` if it doesn't exist;
  `rec = changes.Recorder(con, op="bought")`, `rec.book(book_id)`,
  `UPDATE books SET status='owned', location_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='wanted'`;
  if `rowcount == 0` roll back and 409 (a concurrent tap); `rec.finish()`; `commit()`; return
  `core.get_book(book_id, private=True)`. The want columns are left as they are.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_wanted.py -q && python -m pytest tests/test_security_headers.py -q`
Expected: PASS. The write-route walk now covers `/admin/api/books/<id>/bought` and gets 401 anonymously.

- [ ] **Step 5: `docs/seam.md`**

Add a "Wanted books (for the family wish list app)" section: the endpoint, editor auth (same as other
`/admin/api` routes), the JSON example from the spec's "Seam for later", key meanings (`released` computed in the
server's `TZ`; `release_date` is `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, or legacy text), and the rule that fields are only
ever added, never renamed or removed, without a version note here.

- [ ] **Step 6: Commit**

```bash
git add wanted.py docs/seam.md tests/test_wanted.py
git commit -m "feat(wanted): Wanted list API and Bought it"
```

---

### Task 5: Wish list push uses the want fields; wish list link setting

**Files:**
- Modify: `wishlist.py` (`add_to_wishlist` :67-100)
- Modify: `mcp.py` (`/admin/api/features`)
- Modify: `installer/setup-head.sh:110-111` and `:137`
- Test: `tests/test_wanted.py`

**Interfaces:**
- Consumes: the five columns (Task 2).
- Produces: `wishlist.WISHLIST_PUBLIC_URL: str` (env, default `""`, trailing `/` stripped); `/admin/api/features`
  JSON gains `wishlist_url` (that string, editors only, as the rest of features); the push payload rules below.

- [ ] **Step 1: Write the failing tests**

```python
import wishlist as WL

def fake_wishlist(calls):
    def f(method, path, body=None, timeout=5):
        calls.append((method, path, body))
        if path == "/api/people":
            return [{"id": 2, "name": "Sam"}]
        if path == "/api/people/2/items":
            return {"id": 55}
        raise RuntimeError("unexpected call")
    return f

def test_push_uses_buy_link_price_and_priority(c, monkeypatch):
    calls = []; monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Push Me", buy_url="https://publisher.example/push", price_cents=2450,
                        price_currency="EUR", want_priority=1)
    assert c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2}).status_code == 200
    item = calls[-1][2]
    assert item["url"] == "https://publisher.example/push" and item["price"] == "24.50"
    assert item["priority"] == 1 and "EUR 24.50" in item["notes"]

def test_push_without_want_fields_keeps_old_behaviour(c, monkeypatch):
    calls = []; monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Plain Push")
    c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2, "price": "25.00", "priority": 3})
    item = calls[-1][2]
    assert item["url"].startswith("https://www.amazon.com/s?k=") and item["price"] == "25.00" and item["priority"] == 3

def test_features_carry_the_wish_list_link(c, monkeypatch):
    monkeypatch.setattr(WL, "WISHLIST_PUBLIC_URL", "https://wishlist.example.com")
    assert c.get("/admin/api/features").json["wishlist_url"] == "https://wishlist.example.com"
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `python -m pytest tests/test_wanted.py -q -k "push or features"`
Expected: FAIL (`url` is the Amazon search; no `wishlist_url`).

- [ ] **Step 3: Implement**

In `add_to_wishlist`, after loading `b` (private dict, so it has the columns):
- `url`: `b["buy_url"]` if set, else today's `WISHLIST_BOOK_URL` search.
- `price`: if `b["price_cents"]` is set, `f"{b['price_cents'] / 100:.2f}"` (string, as the dialog sends today); else the
  request's `price` as today.
- When the currency is set and isn't `USD`, append `f"{currency} {price}"` to `notes`.
- `priority`: `b["want_priority"]` if set, else the request's `priority`, default 2.

`WISHLIST_PUBLIC_URL = os.environ.get("WISHLIST_PUBLIC_URL", "").rstrip("/")` in `wishlist.py`; add
`wishlist_url=wishlist.WISHLIST_PUBLIC_URL` to the features response in `mcp.py`.

`installer/setup-head.sh`: after line 111,
`add_setting "$CONF" WISHLIST_PUBLIC_URL "" "Browser link to the wish list app, shown after Bought it, e.g. https://wishlist.example.com"`;
append `WISHLIST_PUBLIC_URL` to the `export` line.

- [ ] **Step 4: Verify**

Run: `python -m pytest -q && ruff check . && sh -n installer/setup-head.sh && shellcheck --shell=sh installer/*.sh`
Expected: PASS, shellcheck clean. `tests/test_v2.py::test_wishlist_endpoints_mocked` still passes (its fake takes
`body=None`; it's called without `timeout`).

- [ ] **Step 5: Commit**

```bash
git add wishlist.py mcp.py installer/setup-head.sh tests/test_wanted.py
git commit -m "feat(wishlist): push buy link, price and priority"
```

---

### Task 6: Private MCP tool `list_wanted`

**Files:**
- Modify: `mcp.py` (`TOOLS` :397)
- Modify: `tests/test_mcp_access.py:14-15` (`READ_TOOLS`)
- Test: `tests/test_wanted.py`

**Interfaces:**
- Consumes: `wanted.wanted_payload` (Task 4).
- Produces: tool `("list_wanted", True, t_list_wanted, <description>, _S())`; result
  `{"books": [{"title", "authors" (first two), "price" ("€24.50" or null), "release_date", "released", "priority"}],
  "total": n}`, at most 100 books, ordered by priority (nulls last) then release start.

- [ ] **Step 1: Write the failing tests**

```python
def test_list_wanted_is_private_and_compact(c, monkeypatch):
    monkeypatch.setattr(MC, "MCP_TOKEN", "t0k")
    add_wanted("Mcp Listed", price_cents=2450, price_currency="EUR", want_priority=1, release_date="2099")
    assert mcp(c, "list_wanted", {})["isError"]                       # public: refused
    res = mcp(c, "list_wanted", {}, token="t0k")["structuredContent"]
    b = next(x for x in res["books"] if x["title"] == "Mcp Listed")
    assert set(b) == {"title", "authors", "price", "release_date", "released", "priority"}
    assert b["price"] == "€24.50" and b["released"] is False
```

And in `tests/test_mcp_access.py`: `READ_TOOLS = PUBLIC_TOOLS | {"suggest_recipes", "cooking_stats", "list_wanted"}`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python -m pytest tests/test_wanted.py -k list_wanted tests/test_mcp_access.py -q`
Expected: FAIL (unknown tool).

- [ ] **Step 3: Implement `t_list_wanted(a, private) -> dict` in `mcp.py`**

Built on `wanted.wanted_payload`. Price formatting: symbol for USD `$`, EUR `€`, GBP `£`, otherwise `"<CODE> 24.50"`;
`null` when there's no price. Description: "Cookbooks we want to buy but don't own yet (editors only): title,
authors, price, release date, whether it's out, priority 1 (most wanted)–3. Not part of the collection: never
suggest cooking from these." Not in `WRITE_TOOLS`.

- [ ] **Step 4: Verify**

Run: `python -m pytest -q && ruff check .`
Expected: PASS, including the full access matrix and `tests/test_ask*.py` (Ask sees the tool through `run_tool`).

- [ ] **Step 5: Commit**

```bash
git add mcp.py tests/test_wanted.py tests/test_mcp_access.py
git commit -m "feat(mcp): private list_wanted tool"
```

---

### Task 7: Wanted screen, Add/Edit fields, Bought it dialog, README

**Files:**
- Modify: `static/app.js` (`route()` :141, `moreView` :1627, `bookView` :396/:441, `editView` :1314, `addView` :1361,
  `wishDialog` :1754)
- Modify: `static/style.css`
- Create: `tests/test_wanted_ui.py`
- Modify: `.github/scripts/run-ui-tests.sh:16`, `tests/test_csp_browser.py` (screen list near :331)
- Modify: `README.md` (near :204, and the settings table)

**Interfaces:**
- Consumes: `GET /admin/api/wanted`, `POST /admin/api/books/<id>/bought` (Task 4); the five fields on
  create/update and `released` in book dicts (Task 2); `feat.wishlist_url` (Task 5); `book.wishes` (exists).
- Produces: route `#/wanted` (editors only); element ids used by the browser test: `#wanted-coming`, `#wanted-out`,
  `#wanted-sort`, `#bought`, `#want-fields`, `#dont-own`.

UI rules (from the spec; the implementer builds the markup in the file's existing style):
- More: a "Wanted" panel ("Cookbooks we want to buy. They stay off the catalog until you mark them bought.") with
  `<a class="btn primary" href="#/wanted">Open the wanted list</a>`; the count comes from `/admin/api/wanted`.
- `#/wanted`: search box (sends `q`), sort select `#wanted-sort` (Priority, Release date, Price, Added; key
  `wantedSort` in `localStorage`, every access in try/catch); sections `#wanted-coming` ("Coming soon",
  `released === false`) then `#wanted-out` ("Out now"). Card: cover, title (links to `#/book/<id>`), authors,
  publisher, release date as "Mar 2027" / "2027" / "17 Mar 2027" (raw text if it doesn't parse), price via
  `Intl.NumberFormat(undefined, {style: "currency", currency: price_currency || "USD"})`, priority as 1–3 stars,
  "On <names>'s wish list", "Out since you added it" when `released` and `created_at` date < release start, and a
  "Buy page" link **only** when `buy_url` matches `/^https?:\/\//i`, with `target="_blank" rel="noopener noreferrer"`.
  Empty state: "Nothing on the wanted list. Add a book and tick “Don't own it yet”."
- Sorting: Priority = priority (nulls last), then release start; Release date = start (nulls last); Price = by
  currency (USD first, then alphabetical), then cents; Added = `created_at` descending.
- Add screen: checkbox `#dont-own` ("Don't own it yet"). Checked → hide the shelf picker, show `#want-fields`
  (Price, Currency defaulting to USD, Release date with placeholder "2027-03", Buy link, Priority 1–3), send
  `status: "wanted"`.
- Edit form: `#want-fields` shown while the Status select is `wanted`.
- Book detail, wanted books only: the four values under the title, a primary `#bought` "Bought it" button next to
  the existing "Add to a wish list…". Its dialog: the existing location picker (optional), and, when `b.wishes` is
  non-empty, "It's on <names>'s wish list. Remove it there so no one buys a second copy." with the names as a link to
  `feat.wishlist_url` when set. Confirm → `POST bought` → toast "On the shelf" → `bookView(id)`.
- `wishDialog(b)`: pre-fill price (`price_cents / 100`) and priority (`want_priority`) when set.
- No inline `<script>` or `on…=` attributes (CSP); text through `esc()`; tap targets ≥ 44 px (existing CSS classes).

- [ ] **Step 1: Write the failing browser test** (`tests/test_wanted_ui.py`; copy the server/browser fixtures and
  `importorskip` from `tests/test_uid_route.py`)

```python
def test_add_wanted_then_see_it_and_buy_it(server, page):
    page.goto(f"{server}/admin#/add")
    page.fill("input[name=title]", "Browser Wanted Book")
    page.check("#dont-own")
    assert page.is_visible("#want-fields")
    page.fill("input[name=release_date]", "2099-05")
    page.fill("input[name=price]", "35")
    page.click("button[type=submit]")
    page.goto(f"{server}/admin#/wanted")
    assert "Browser Wanted Book" in page.inner_text("#wanted-coming")
    page.goto(f"{server}/admin#/")
    assert "Browser Wanted Book" not in page.inner_text("main")
    page.goto(f"{server}/admin#/wanted"); page.click("text=Browser Wanted Book")
    page.click("#bought"); page.click("dialog button.primary")
    page.goto(f"{server}/admin#/")
    page.wait_for_selector("text=Browser Wanted Book")
```

Match the Add form's real field names and submit button (`grep -n 'name="title"\|type="submit"' static/app.js`)
and the main view's container id; keep the test's steps.

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHON=python3 sh .github/scripts/run-ui-tests.sh` (after adding `tests/test_wanted_ui.py` to its list)
Expected: FAIL (`#dont-own` not found). The script fails on skips, so Chromium must be present.

- [ ] **Step 3: Implement the UI** per the rules above; add `#/wanted` to `route()` beside the other `ADMIN` routes,
  and `#/wanted` to the screens `tests/test_csp_browser.py` opens.

- [ ] **Step 4: Run the browser tests and the CSP check**

Run: `PYTHON=python3 sh .github/scripts/run-ui-tests.sh`
Expected: all pass, no skips, no CSP violations, tap-target test passes on the new screen.

- [ ] **Step 5: README**

Replace the wish list paragraph near :204 with a "Wanted books" section: how to add one ("Don't own it yet"), the
Wanted screen (More → Wanted), what's hidden and from whom, Bought it, the wish list push (now using the buy link,
price and priority), and why purchase status is never read or written (no spoilers). Add `WISHLIST_PUBLIC_URL` to the
settings table with `https://wishlist.example.com`.

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/style.css tests/test_wanted_ui.py tests/test_csp_browser.py .github/scripts/run-ui-tests.sh README.md
git commit -m "feat(wanted): Wanted screen and Bought it"
```

---

## Final verification (before opening the PR)

Run every check CI runs (CLAUDE.md, "Commands"):

```sh
python -m pytest -q
ruff check .
for f in installer/*.sh ops/* .github/scripts/*.sh; do sh -n "$f"; done
shellcheck --shell=sh installer/*.sh ops/* .github/scripts/*.sh
PYTHON=python3 sh .github/scripts/run-ui-tests.sh
python3 .github/scripts/upgrade_path.py
gitleaks git --redact .
sh installer/build.sh
```

Expected: all green; `upgrade path OK`; the built `dist/setup-cookbooks.sh` contains `wanted.py`. Then
`git diff main --stat` and a read of the diff for names, hostnames or emails (`tests/test_hygiene.py` can't catch
those). Don't push or open a PR until asked.
