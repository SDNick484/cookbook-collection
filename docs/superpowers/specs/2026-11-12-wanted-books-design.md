# Wanted cookbooks: design

Date: 2026-11-12
Status: approved in conversation, awaiting written-spec review
Branch: `feat/wanted-books`

## Goal

Track cookbooks we don't own yet but want to buy: books already in print and upcoming releases or pre-orders.
Record author, publisher, price, release date, where to buy and a priority. Wanted books are a shopping list,
not part of the collection: they never appear on the main page for anyone, and the public never sees them at all.
Buying one turns it into an ordinary cookbook without re-entering anything. The family wish list integration stays
one-way now, with a documented read endpoint for a later pull from the wish list app.

### Success criteria

- An editor can add a wanted book (with ISBN lookup and cover fetch), see all wanted books on one screen sorted by
  priority, release date, price or date added, and see which are out now versus coming soon.
- No wanted book appears on the public catalog, any anonymous route, the public MCP, the editor main grid, editor
  search, facets, or the "N cookbooks" counts.
- "Bought it" turns a wanted book into an owned one in place (same row, same `uid`), recorded in the change history
  and undoable.
- Pushing a wanted book to the family wish list uses its buy URL, price and priority.

### Decisions made

| Question | Decision |
|---|---|
| Who sees the wanted list | Editors only |
| Storage | Existing `books` rows with `status='wanted'` (not a separate table, not a new `kind`) |
| Editor main-page search finds wanted books | No; only the Wanted screen's own search does |
| Writing purchase state into the wish list app | No (see "Wish list") |

### Out of scope

Price history or tracking, release notifications or any background job, currency conversion, a public wanted page,
MCP write tools for wanted books, any change to the family wish list app.

## Current state (main at 207743b)

- `books.status` already accepts `'wanted'` (UI label "Want to buy"); `importer.STATUSES` and `changes.STATUS` know it.
- `wishlist.py` pushes a wanted book to a person's list over localhost and records it in `wish_items`.
- `cook.OWNED`, Shelf check, the `unindexed` filter and `eyb` matching already exclude `wanted`.
- **Gap:** `list_books()` does not filter on status and `PUBLIC_BOOKS` filters only `kind` and `visibility`, so
  wanted books show on the public catalog today. The public MCP `search_books` excludes only `'gone'`, so they leak
  there too. This design closes both.

## 1. Data model

New module `wanted.py`, registered like the others: imported at the bottom of `app.py`, its `migrate(con)` called
from `init_db` after `house.migrate`, and added to the `cp` list in `installer/build.sh`.

```python
COLS = {
    "price_cents":    "INTEGER CHECK (price_cents IS NULL OR price_cents >= 0)",
    "price_currency": "TEXT",   # ISO 4217, upper-cased on write; NULL is shown as USD
    "release_date":   "TEXT",   # 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'
    "buy_url":        "TEXT",   # http(s) only
    "want_priority":  "INTEGER CHECK (want_priority IS NULL OR want_priority BETWEEN 1 AND 3)",
}

def migrate(con):
    """Additive only: five nullable columns on books. Safe on every start."""
    have = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    for col, decl in COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE books ADD COLUMN {col} {decl}")
```

Validation on every write route (`wanted.clean_fields(d)`, returning values or a 400 message):

| Field | Accepted | Rejected (400) |
|---|---|---|
| `price` (UI sends a decimal string) | `35`, `35.00`, `24,50` → `3500`, `2450`; empty → NULL | negative, more than 2 decimals, > 1,000,000 |
| `price_currency` | three letters, upper-cased; empty → NULL | anything else |
| `release_date` | `2027`, `2027-03`, `2027-03-17` (a real date) | `2027-13`, `2027-02-30`, `March`, year outside 1900–2100 |
| `buy_url` | `http://` or `https://`, at most 2,000 characters; empty → NULL | any other scheme, including `javascript:` |
| `want_priority` | 1, 2, 3; empty → NULL | anything else |

The fields can be set on any book but are only shown while `status='wanted'`.

Sample row:

```
title               status price_cents price_currency release_date buy_url                          want_priority
Example Pasta Book  wanted 3500        USD            2027-03      https://publisher.example/pasta  1
```

### Migration impact

Five nullable columns; existing rows, including existing `wanted` books, get NULLs. No backfill, no data rewrite.
The JSON export (`SELECT *`) and the importer (`ctx.cols["books"]`) carry the new columns without an
`EXPORT_VERSION` bump. `changes.py` snapshots pick up new `books` columns on their own. `upgrade_path.py` gains a
check (`.github/scripts/upgrade_path.py`) that the five columns exist after upgrading. Rolling back to older code is safe: it ignores the columns.

Imported data bypasses `clean_fields`, so the browser also renders `buy_url` as a link only when it matches
`^https?://`, and `release_date` values that don't parse are shown as text and treated as "no date".

## 2. Hiding wanted books

One predicate in `app.py`, used at two choke points:

```python
IN_COLLECTION = "b.status <> 'wanted'"  # status is NOT NULL DEFAULT 'owned'
PUBLIC_BOOKS = f"({IN_COLLECTION} AND (b.kind IS NULL OR b.kind = 'house') AND b.visibility = 'public')"

def book_public(row):
    return (row["status"] != "wanted" and row["kind"] in (None, "house")
            and row["visibility"] == "public")
```

- **Anonymous reads:** every anonymous read path already must use `PUBLIC_BOOKS` or `book_public` (CLAUDE.md), so
  the public catalog, `/api/books/<id>`, `#/uid/…`, recipe search, facets, counts and the public MCP stop showing
  wanted books with this one change. A wanted book's public detail URL answers 404, like a members-only book.
- **Editor main page:** `list_books(private=True)`, the editor facets and the editor recipe search add
  `AND IN_COLLECTION` unless the request has `status=wanted`.
- **Unchanged:** `cook.OWNED` and the `eyb` filters already exclude wanted. The editor book detail and edit form
  open a wanted book by id as today.
- **Payload:** the five want fields and `released` are added to `book_dicts` only in its `private` branch.

CLAUDE.md's anonymous-read convention gains one sentence: wanted books are excluded by `PUBLIC_BOOKS`, and editor
lists of "the collection" add `IN_COLLECTION`.

## 3. Wanted screen

Editors only, at `#/wanted`, reached from **More → Wanted (n)**. The phone nav keeps its six tabs.

API: `GET /admin/api/wanted` (`@editor_required`). Optional `q` filters by title or author (same FTS path as
`list_books`). The response is the stable shape in "Wish list". Sorting is done in the browser over the returned list
(a household has tens of wanted books, not thousands).

Layout: two groups, **Coming soon** then **Out now**, each a list of cards with cover, title, authors, publisher,
release date ("Mar 2027"), price (`Intl.NumberFormat` with the stored currency), priority stars, a "Buy page" link
(`target="_blank" rel="noopener noreferrer"`, text through `esc()`), and "On <name>'s wish list" from `wish_items`.
Sort control: Priority (then release date), Release date, Price, Added; the choice is kept per browser in
`localStorage`, reads and writes wrapped in try/catch.

### Released or not

`wanted.release_start(s)` pads a partial date to its first day: `2027` → `2027-01-01`, `2027-03` → `2027-03-01`.
`released = start is None or start <= today`, where today is the date in the server's `TZ` (the zone `local_midnight()` uses).
Computed once on the server and sent as `released`.

"Out since you added it" is shown when `created_at`'s date < `release_start` <= today.

Prices sort by cents within a currency, USD first, then other currencies alphabetically; no conversion.

### Adding and editing

- **Add screen:** a "Don't own it yet" checkbox. Checked: the shelf picker hides, the five want fields show, the
  book saves with `status='wanted'`. ISBN lookup and cover fetch are unchanged.
- **Edit form:** the want fields show while the Status select is "Want to buy".
- The existing create and update routes accept the five fields through `wanted.clean_fields`, inside the existing
  `changes.Recorder` transaction.

## 4. Bought it

A primary **Bought it** button on a wanted book's detail page opens a dialog with an optional shelf picker (the
existing location tree) and, when `wish_items` has rows for the book, the sentence "It's on <names>'s wish list.
Remove it there so no one buys a second copy." The sentence links to `WISHLIST_PUBLIC_URL` when that's set.

`POST /admin/api/books/<id>/bought` (`@editor_required`), body `{"location_id": <id or null>}`:

1. 404 if the book doesn't exist; 409 if its status isn't `wanted`.
2. In one transaction: `rec.book(id)`, set `status='owned'` and `location_id` (validated with `location_arg`),
   `updated_at`, `rec.finish()`, commit. Op name `bought`, so Recent changes reads "Want to buy → On the shelf".
3. The want fields are kept (a record of the list price and buy page) and stop being shown.

Undo through Recent changes restores `status='wanted'` and the previous location. The book keeps its `uid`, cover,
authors, recipes and notes.

## 5. Wish list

### Push (existing, improved)

`wishlist.add_to_wishlist` builds the item from the new fields when they're set:

| Wish list item field | From | Fallback (today's behavior) |
|---|---|---|
| `url` | `buy_url` | `WISHLIST_BOOK_URL` search on ISBN or title + author |
| `price` | `price_cents` / 100, with currency in the notes when not USD | the dialog's price field |
| `priority` | `want_priority` | the dialog's priority, default 2 |

The dialog pre-fills price and priority from the book, so it is one tap when they're set. Revised in the final review: what the dialog sends wins, since the editor may have changed the pre-filled values; the book's values apply only when the dialog sends none.

### No write-back, no read-back

Cookbooks never writes purchase or reserve state into the wish list app and never reads it. Reasons, from the wish
list app's code:

- Cookbooks calls it as a loopback LAN editor, not as the list's owner, so `POST /api/items/<id>/purchase` would show
  family a purchase by a generic editor.
- When someone has reserved the item, that call fails with a message naming the reserver: a spoiler.
- `DELETE` from a non-owner removes the item outright, dropping the buyer's record; the owner's soft delete
  (`removed_by_owner`) isn't reachable from loopback.
- Reading purchase state would let a wanted book vanish from the Wanted screen when a relative buys it: a spoiler.

### Seam for later

`GET /admin/api/wanted` is documented in `docs/seam.md` as a stable, uid-keyed contract, so the wish list app can
later pull from it ("Add a cookbook from the Wanted list") and keep surprise-sensitive decisions on its side:

```json
{"books": [{"uid": "…", "title": "Example Pasta Book", "authors": ["A. Author"], "publisher": "Publisher",
            "isbn": "9780000000000", "cover_url": "/covers/…", "price_cents": 3500, "price_currency": "USD",
            "release_date": "2027-03", "released": false, "buy_url": "https://publisher.example/pasta",
            "want_priority": 1, "on_wish_lists": ["Sam"], "created_at": "2026-11-12 09:30:00"}]}
```

Fields are only ever added, never renamed or removed, without a version note in `docs/seam.md`.

### New setting

`WISHLIST_PUBLIC_URL` (optional): the wish list app's browser URL, used only as a link. Added to the `export` line in
`installer/setup-head.sh`, the README settings table and the example config with `https://wishlist.example.com`.

## 6. MCP and Ask

- **Public MCP:** wanted books disappear through `PUBLIC_BOOKS`.
- **Private `search_books`:** adds `IN_COLLECTION`, so Ask doesn't treat a wanted book as one we own.
- **New tool `list_wanted`:** `private_only=True`, read-only, not in `WRITE_TOOLS`. No arguments. Returns per book:
  title, first two authors, price with currency, release date, `released`, priority. At most 100 books, compact.
  Ask gets it through `run_tool` with no other change.

## 7. Security

- Every new route (`/admin/api/wanted`, `/admin/api/books/<id>/bought`) is under `/admin/api` with
  `@editor_required` (Access, Origin check). `tests/test_security_headers.py`'s write-route walk covers the POST.
- No anonymous path can return a wanted book or its want fields (choke point plus private-only serialization).
- `buy_url` is scheme-checked on write and on render. No CSP change: it's a navigation link, not an embed.
- No new outbound network calls. One new env var, display only.
- `list_wanted` is added to the access matrix in `tests/test_mcp_access.py`: public token refused.

## 8. Testing

New `tests/test_wanted.py`:

- `migrate` is idempotent; an old database gains the five columns (also in `upgrade_path.py`).
- `clean_fields`: every accepted and rejected case in the validation table.
- `release_start` / `released`: `2027`, `2027-03`, `2027-03-17`, NULL, today exactly, the day before.
- Hiding: a seeded wanted book with a distinctive title is absent from every anonymous GET route (reusing the route
  walk from `test_int_params.py`), the public MCP tools, the editor `list_books`, editor facets and editor search,
  and the cookbook count; present in `/admin/api/wanted`.
- `book_public` is false for a wanted book; its public detail URL is 404.
- Bought it: status flips, location set, one change recorded, undo restores `wanted`; 409 when not wanted; the book
  then appears in `list_books` and, if public, on the public catalog.
- Push payload: uses `buy_url`, price and priority when set, falls back otherwise (fake `wishlist_call`).
- `list_wanted`: refused on public access, compact shape on private.

Browser: one Playwright test (More → Wanted renders both groups, Add with "Don't own it yet" saves a wanted book),
added to `.github/scripts/run-ui-tests.sh`; `tests/test_csp_browser.py` opens `#/wanted`.

## 9. Docs

README: a "Wanted books" section and the `WISHLIST_PUBLIC_URL` setting. CLAUDE.md: the map row for `wanted.py` and
the `IN_COLLECTION` sentence. `docs/seam.md`: the `/admin/api/wanted` contract.

## Commit plan (for the implementation plan)

1. `fix(privacy): keep wanted books off public reads` (predicate, `PUBLIC_BOOKS`, `book_public`, tests)
2. `feat(wanted): price, release date, buy link and priority on books` (`wanted.py`, migration, validation, routes)
3. `feat(wanted): hide wanted books from the editor catalog and search`
4. `feat(wanted): Wanted screen and Bought it`
5. `feat(wishlist): push buy link, price and priority`
6. `feat(mcp): private list_wanted tool`
7. `docs: wanted books, seam contract, settings`
