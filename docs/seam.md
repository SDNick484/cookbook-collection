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

**Dinner Log `photos`:** `shared` (0/1, default 0) marks a photo offered to the catalog. It is set only from the
edit form's *Show on the cookbook page* box, and only counts while the dinner can resolve to a recipe: a cookbook or
website source, or a stamped uid (`SHARED_PHOTO_SQL`). Saving a dinner with any other source unshares its photos.

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
| `dl_photos` | each meal's shared photo names from the same pull (`dl_file` → `meal_id`; replaced with `dl_meals`) |
| `recipe_photos.dl_file` | on an imported copy, the Dinner Log file it came from (unique; NULL for uploads) |
| `dl_photo_skip` | Dinner Log files never to import: `deleted` (an editor deleted the copy; permanent) or `unreadable` |
| `cooked` | one row per matched dinner: `book_id`, `recipe_id`, `how`; rebuilt from `dl_meals` and the catalog |
| `dl_link`, `dl_alias` | editors' manual overrides: dinner → recipe, unknown title → book |

## Calls

| Caller | Call | Auth | Used for |
|---|---|---|---|
| Cookbook Collection | `GET {DINNERLOG_API_URL}/api/history` | `Authorization: Bearer <token>`; Dinner Log's `HISTORY_TOKEN` = Cookbook Collection's `DINNERLOG_TOKEN` | cooking history; `ETag` / `If-None-Match`, so an unchanged history is a 304 |
| Cookbook Collection | `GET {DINNERLOG_API_URL}/api/photos/<file>` | the same bearer token | one shared photo (the stored 1600 px JPEG). 404 for anything not shared right now: private, unshared, its dinner no longer eligible, a `-t.jpg` thumbnail, an unknown name |
| Dinner Log | `GET {COOKBOOK_API_URL}/api/books` and `/api/books/<id>` | none (public) | the form's recipe picker: titles, then a book's recipes with `id` and `uid` |
| Dinner Log | `GET {COOKBOOK_API_URL}/admin/api/pick` | `Authorization: Bearer <READ_TOKEN>` (Dinner Log's `COOKBOOK_TOKEN`) | "Pick for tonight"; each recipe has `id`, `uid`, `book_id`, `book_uid` |
| Dinner Log | `GET {COOKBOOK_API_URL}/admin/api/house/recipes[?q=]` and `/admin/api/house/recipes/<uid>` | `Authorization: Bearer <READ_TOKEN>` | house recipes ("our own"): `uid`, `title`, `updated_at`; one by uid with details, ingredients and steps. Members-only unless the house book is public, so never the public `/api` |
| browser | Dinner Log's "new house recipe" link → `{COOKBOOK_URL}/admin/recipe/new?title=…&return=…` | the editor login (Access) | the recipe editor, prefilled; after a save the browser goes to `return` with `cookbook_book_uid` and `cookbook_recipe_uid` added, only if `return`'s origin is in Cookbook Collection's `RETURN_URL_ORIGINS` |
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
   "cookbook_book_uid": "4f1c…(32 hex)", "cookbook_recipe_uid": "9a0e…(32 hex)",
   "photos": ["5d2e…(32 hex).jpg"]}
]}
```

`photos` lists the meal's shared photos by file name (Dinner Log's photo id order), `[]` when none. It was added
without a version bump; a catalog that predates it ignores it. Cookbook Collection keeps only names matching
`[0-9a-f]{32}\.jpg` and, the first time it stores them, forgets its ETag, since a 304 would otherwise hide them.

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

## Shared photos

After every pull that worked (a 304 included), `photos.sync_dl_photos` runs in the same background thread, under the
same lease. It is never part of `rebuild_cooked`, which also runs on the request path. A photo is *wanted* when its
meal is in `cooked` with a `recipe_id` (any resolution level above) and its file isn't in `dl_photo_skip`.

| Step | What | Limits |
|---|---|---|
| remove | imported copies no longer wanted: unshared, photo or dinner deleted, dinner no longer resolves to a recipe. Rows first, then files | none |
| move | the dinner now resolves to another recipe: `recipe_id` changes; caption and date stay | none |
| import | fetch, `store()` (decoded by `open_image()`, re-encoded to 2048 px + 480 px thumb, no EXIF), insert with `taken_on` = the dinner's day | newest dinner first, 8 per pull, 45 s; 10 MB per file |

A 404 on fetch is skipped for now (it was unshared since the pull); an unreadable or oversized file is recorded as
`unreadable`; any other error ends that pull's imports. The unique index on `dl_file` keeps one copy per file even
if two pulls overlap. An editor deleting an imported copy records `deleted`, permanently: sharing it again in Dinner
Log doesn't bring it back. Each sync logs one line when it did something:

```
Dinner Log photos: imported=2 moved=0 removed=1 skipped=0 pending=0
```

Imported copies follow the catalog's visibility like uploads (public on public books, editor-only on recipe websites
and private house books), and their files are served with a 10-minute cache lifetime instead of a day, so an unshare
doesn't linger in browsers or a CDN. `dl_file` never appears in API output; editors get `from_dinner_log`. The JSON
export carries `dl_file` on imported photos and the importer restores it, so a restore doesn't import them twice.

## What's public

Cookbook Collection's public field lists in `app.py` decide what anonymous visitors get:
`PUBLIC_RECIPE` for recipe fields and `PUBLIC_RECIPE_COOKED` (`cooked_count`) for cooking history. Last-made
dates, ratings and per-book totals are editor-only. The public MCP tools return no cooking history.
