# The Dinner Log seam

Cookbook Collection and Dinner Log are separate apps with separate SQLite databases. They meet at a few
HTTP calls and a handful of fields. This is the contract; each README covers setup.

Examples use `cookbooks.example.com` / `dinner.example.com` for public addresses and loopback for private ones.

## Identifiers

| | Cookbook Collection | Stable? |
|---|---|---|
| `books.id`, `recipes.id` | integer, the SQLite rowid | **No.** A deleted row's id can be handed to the next insert |
| `books.uid`, `recipes.uid` | 32 lowercase hex characters, set on insert by a trigger | Yes. Never reused; kept by the JSON export/import |

Anything stored outside Cookbook Collection's database should hold the uid. The ints are still sent and
stored so that either app can be upgraded first.

## What each side stores

**Dinner Log `meals`** (one row per dinner; only meals from a cookbook or website keep these):

| Column | Meaning |
|---|---|
| `cookbook_book_uid`, `cookbook_recipe_uid` | the catalog's uids, when the dinner was picked from it |
| `cookbook_book_id`, `cookbook_recipe_id` | the catalog's ints, same occasions (legacy) |
| `source_title`, `source_detail`, `name` | cookbook title, page (or URL), dish name, as typed or filled in |

Typing over the name, cookbook or page in Dinner Log's form clears all four ids. Meals logged before uids
existed get them from `python3 -m backfill_uids` (see Dinner Log's README), which stamps a uid only when the
catalog still agrees with the meal's title and page/name.

**Cookbook Collection:**

| Table | Holds |
|---|---|
| `dl_meals` | the last history pulled, as sent (a cache; replaced on every pull) |
| `cooked` | one row per matched dinner: `book_id`, `recipe_id`, `how`; rebuilt from `dl_meals` and the catalog |
| `dl_link`, `dl_alias` | editors' manual overrides: dinner → recipe, unknown title → book |

## Calls

| Caller | Call | Auth | Used for |
|---|---|---|---|
| Cookbook Collection | `GET {DINNERLOG_API_URL}/api/history` | `Authorization: Bearer <token>`; Dinner Log's `HISTORY_TOKEN` = Cookbook Collection's `DINNERLOG_TOKEN` | cooking history; `ETag` / `If-None-Match`, so an unchanged history is a 304 |
| Dinner Log | `GET {COOKBOOK_API_URL}/api/books` and `/api/books/<id>` | none (public) | the form's recipe picker: titles, then a book's recipes with `id` and `uid` |
| Dinner Log | `GET {COOKBOOK_API_URL}/admin/api/pick` | `Authorization: Bearer <READ_TOKEN>` (Dinner Log's `COOKBOOK_TOKEN`) | "Pick for tonight"; each recipe has `id`, `uid`, `book_id`, `book_uid` |
| browser | Cookbook Collection's 🍽 link → `{DINNERLOG_URL}/new?…` | whatever login sits in front of Dinner Log (it has none of its own) | prefilled form; carries `cookbook_book_id`, `cookbook_recipe_id`, `cookbook_book_uid`, `cookbook_recipe_uid` |
| browser | Dinner Log's links → `{COOKBOOK_URL}/#/uid/<book uid>[/<recipe uid>]` | none for the public catalog; `/admin#/uid/…` for recipe websites (`kind='online'`), behind the editor login | opens the book (and highlights the recipe) without knowing the integer id; the page resolves the uid through `/api/books/by-uid/<uid>` and replaces itself with `#/book/<id>` |

Both tokens are bearer secrets. Point `DINNERLOG_API_URL` and `COOKBOOK_API_URL` at private addresses
(e.g. `http://127.0.0.1:8000` and `http://127.0.0.1:8767` on one host), or use https. Neither side follows
redirects with a token attached, and Cookbook Collection ignores proxy environment variables for this call.
`/api/books/by-uid/<uid>` exists for callers that store only uids.

### `/api/history` body

```json
{"version": 1, "meals": [
  {"id": 12, "day": "2026-10-12", "name": "Roast chicken", "source_type": "cookbook",
   "source_title": "The Zuni Cafe Cookbook", "source_detail": "342", "rating": 8,
   "cookbook_book_id": 57, "cookbook_recipe_id": 53,
   "cookbook_book_uid": "4f1c…(32 hex)", "cookbook_recipe_uid": "9a0e…(32 hex)"}
]}
```

Sent: meals from a cookbook or website, meals whose source holds a link, and meals stamped with any id or
uid. Restaurant and home-recipe dinners without a link are never sent. New fields are additive and keep
`version` 1; Cookbook Collection rejects any other version, so bump it only for a breaking change and
upgrade Cookbook Collection first when you do.

## Resolution order

`rebuild_cooked` matches each dinner once per new history or catalog change. First match wins:

| Level | Rule | `cooked.how` |
|---|---|---|
| `manual` | an editor's link for this dinner (`dl_link`, checked against day + name) | `manual` |
| `recipe_uid` | `cookbook_recipe_uid` is a recipe here | `linked` |
| `book_uid` | `cookbook_book_uid` is a book here; then page, then name, within it | `auto`, or `book` if no recipe matched |
| `int` | **only if the dinner has no uid at all:** `cookbook_recipe_id`, then `cookbook_book_id` + page/name | `linked` / `auto` / `book` |
| `web` | the source is a link to a recipe saved from a website | `auto` |
| `text` | cookbook title (with `dl_alias`), then page, then name (fuzzy) | `auto` / `book` / `none` |
| `skipped` | not a cookbook dinner and no saved website matched | (no row) |

A uid that doesn't resolve (its row was deleted) skips the `int` level: the int may now belong to a different
book or recipe. Malformed uids count as absent. Each rebuild logs the count per level, plus `uid_missed`
(dinners whose uids all failed), as one line:

```
Dinner Log history matched: 120 meals; manual=2 recipe_uid=96 book_uid=3 int=0 web=4 text=12 skipped=3 uid_missed=1
```

The same counts are in `GET /admin/api/dinnerlog` as `resolved`. A rising `text` or `uid_missed` count after
an upgrade means uids aren't arriving or don't match this catalog.

## What's public

Cookbook Collection's public field lists in `app.py` decide what anonymous visitors get:
`PUBLIC_RECIPE` for recipe fields and `PUBLIC_RECIPE_COOKED` (`cooked_count`) for cooking history. Last-made
dates, ratings and per-book totals are editor-only. The public MCP tools return no cooking history.
