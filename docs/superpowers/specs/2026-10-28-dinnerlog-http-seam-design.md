# Dinner Log seam over HTTP — design

Status: approved design, 2026-10-28. Spans two repos: `cookbook-collection` (consumer) and `dinner-log`
(producer). One PR per repo; Dinner Log merges and deploys first.

## Goal

Cookbook Collection stops opening Dinner Log's SQLite file. Dinner Log records which catalog recipe a meal
came from when it knows, and serves a read-only, token-protected history endpoint that Cookbook Collection
pulls. Fuzzy title/page matching remains only for meals without stamped ids (legacy rows and hand-typed
meals).

Success criteria:

- Cookbook Collection's service user needs no filesystem access to Dinner Log's data.
- Meals logged from a catalog recipe are matched by id, not by fuzzy title/page guess.
- Public pages and MCP never wait on Dinner Log; Dinner Log being down degrades to "history as of the last
  good pull", never to an error or an empty history.
- No data loss or rewrite in either database: changes are additive.

## Decisions

| Question | Decision |
|---|---|
| Which logging paths stamp ids | All three: "Pick for tonight" card, Cookbook Collection's 🍽 link, the form's recipe picker |
| Transition | Hard cutover: the SQLite file reader, `DINNERLOG_DB` and the shared group are removed in the same PR |
| When Cookbook Collection pulls | Stale-while-revalidate: requests serve the current `cooked`; a stale snapshot (> 60 s) starts a background pull under a DB lease |
| Endpoint shape | Full snapshot with a content ETag (304 when unchanged) |
| Backfill legacy rows | None: they keep NULL ids, `dl_link` overrides and fuzzy matching |

## Dinner Log (producer)

### Schema

Added in the existing upgrade block in `app.py`, guarded by `PRAGMA table_info(meals)` like `rating`:

```sql
ALTER TABLE meals ADD COLUMN cookbook_book_id INTEGER;    -- Cookbook Collection books.id
ALTER TABLE meals ADD COLUMN cookbook_recipe_id INTEGER;  -- Cookbook Collection recipes.id
```

Plain integers, not foreign keys (they point into another database). No index. Migration impact: existing
rows get NULL; nothing is rewritten. The new columns are also added to `SCHEMA` for fresh databases.

### Stamping ids

`/new` accepts two optional fields, `cookbook_book_id` and `cookbook_recipe_id`, as query params (prefill)
and as hidden form fields (save). `parse_meal_params` accepts each only as a positive decimal integer
(`str.isdigit()` and > 0); anything else becomes NULL. Both are set to NULL unless `source_type` is
`cookbook` or `online`.

Sources:

1. **Pick card.** `catalog_dish()` keeps `id` → `cookbook_recipe_id` and `book_id` → `cookbook_book_id`
   from `/admin/api/pick`. The "Log tonight" `url_for` in `templates/index.html` passes them. Local dishes
   (not from the catalog) carry none.
2. **🍽 link** (Cookbook Collection `static/app.js`). Appends
   `&cookbook_book_id=<book id>&cookbook_recipe_id=<r.id>` to the existing `/new?…` link.
3. **Form recipe picker.** `cookbook_recipes()` keeps each recipe's `id` and returns the catalog `book_id`;
   `/cookbook/recipes` includes them. Choosing a recipe in the form sets both hidden fields.

Clearing stale ids: in `form.html`, an `input` handler on name, cookbook title and page/detail, and a
`change` handler on source type, clears both hidden fields once the user edits after ids were set. The
server-side rule above covers non-catalog source types. Known gap: an edit with JavaScript disabled keeps
stale ids; Cookbook Collection's manual `dl_link` override still wins, so it is correctable.

Editing an existing meal: the edit form renders the stored ids into the hidden fields, so a save without
relevant edits keeps them.

### `GET /api/history`

- Configured by `HISTORY_TOKEN`. Unset or empty: the route returns 404 (off by default).
- Requires `Authorization: Bearer <HISTORY_TOKEN>`, compared with `hmac.compare_digest` on bytes.
  Missing or wrong: 401 with a generic body.
- Body: `{"version": 1, "meals": [...]}`, meals ordered by `id`, each:

  ```json
  {"id": 412, "day": "2026-10-21", "name": "Chicken with 40 cloves of garlic",
   "source_type": "cookbook", "source_title": "The Zuni Cafe Cookbook", "source_detail": "p. 342",
   "rating": 8, "cookbook_book_id": 17, "cookbook_recipe_id": 2203}
  ```

- Scope: `source_type IN ('cookbook','online')`, or `source_detail`/`source_title` containing `://`, or
  either id non-NULL. Restaurant and "our own" meals without a URL are never sent.
- `ETag`: quoted hex SHA-256 of the serialized body. `If-None-Match` equal to it → 304 with no body.
- `Cache-Control: private, no-store`.
- GET only; the cross-site write guard does not apply. Intended to be reached on a private address;
  through the public hostname the authenticating proxy blocks it anyway.

### Config and docs

- `deploy/install-alpine.sh`: `set_conf "export HISTORY_TOKEN" "" "Token Cookbook Collection uses to read
  cooking history (optional)"` (conf file is already mode 600).
- `docker-compose.yml` / `.env.example` where present: `HISTORY_TOKEN=` placeholder.
- README: "Linking the two apps" — generate with `openssl rand -hex 32`, set the same value as
  Cookbook Collection's `DINNERLOG_TOKEN`; note the two new columns.

## Cookbook Collection (consumer)

### Config

- Removed: `DINNERLOG_DB`.
- Added: `DINNERLOG_API_URL` (Dinner Log's private address, e.g. `http://127.0.0.1:<port>`) and
  `DINNERLOG_TOKEN`. Both go in `add_setting` and the `export` line in `installer/setup-head.sh`.
- Installer: remove the `addgroup "$SVC_USER" dinnerlog` block; add
  `delgroup "$SVC_USER" dinnerlog 2>/dev/null || true` (BusyBox syntax) so upgrades lose the access.
  Update the post-install hint that reads `/admin/api/dinnerlog`. A leftover `DINNERLOG_DB=` line in an
  existing conf file is ignored.
- `DINNERLOG_URL` (public, for 🍽 links) is unchanged.

### Storage

```sql
CREATE TABLE IF NOT EXISTS dl_meals (
  id INTEGER PRIMARY KEY,               -- Dinner Log meals.id
  day TEXT, name TEXT, source_type TEXT, source_title TEXT, source_detail TEXT,
  rating REAL, cookbook_book_id INTEGER, cookbook_recipe_id INTEGER);
```

The raw snapshot is stored because a 304 carries no body, and `cooked` must be rebuilt when the local
catalog changes even if Dinner Log did not.

Meta keys: `dl_etag`, `dl_pulled_at`, `dl_lease`, `dl_error`, `dl_failed_at`, `dl_cat_sig`. `dl_sig` is
deleted on migration. `dl_synced` keeps its meaning (time of the last `cooked` rebuild).

Migration impact: new empty table and meta keys; `cooked`, `dl_link`, `dl_alias` schemas unchanged;
`cooked.how` gains the value `'linked'`. Until the first successful pull, `cooked` keeps its pre-upgrade
rows (no rebuild runs from an empty snapshot that has never been pulled: `dl_etag` absent).

### `pull_history(con)` — network only

- `GET {DINNERLOG_API_URL}/api/history` with `Authorization: Bearer`, `If-None-Match: <dl_etag>` when
  known, timeout 5 s, through an opener that does not follow redirects (a 3xx is an error, so the token
  never goes elsewhere).
- 200: body read with a 10 MB cap (larger → error); must parse as JSON with `version == 1` and a `meals`
  list whose items have an integer `id`. Then, in one transaction: replace `dl_meals`, set `dl_etag`,
  `dl_pulled_at`, clear `dl_error`/`dl_failed_at`.
- 304: set `dl_pulled_at`, clear `dl_error`/`dl_failed_at`.
- Anything else (timeout, connection error, 4xx/5xx, 3xx, bad payload): set `dl_error` (short message, no
  token, no body) and `dl_failed_at`; leave `dl_meals` untouched. Logged at warning level.
- Not configured (`DINNERLOG_API_URL` empty): returns without touching anything.

### `rebuild_cooked(con)` — local only

Runs when `dl_etag` changed since the last rebuild or the catalog signature (the `b:`/`r:` part of today's
`dl_signature`) differs from `dl_cat_sig`. Same `DELETE`/`INSERT` of `cooked` as today, under `_sync_lock`.
Per meal in `dl_meals`, first rule that applies:

1. Manual `dl_link` for the meal (same `meal_key` check as today) → `how='manual'`.
2. `cookbook_recipe_id` exists in `recipes` → that recipe; book from the recipe → `how='linked'`.
3. `cookbook_book_id` exists in `books` → that book; `match_recipe` within it → `how='auto'` or `'book'`.
4. Online URL match (`online.match_meal`) → `how='auto'`.
5. `match_book` then `match_recipe` → `how='auto'`, `'book'`, or skipped as today (non-cookbook meals
   without a web match are not inserted).

Stamped ids that no longer exist fall through silently.

### Request path

`ensure_synced()` (once per request, as today):

1. If the catalog signature differs from `dl_cat_sig`, `rebuild_cooked()` inline (local, fast).
2. If configured, `dl_pulled_at` is older than 60 s, and `dl_failed_at` is not within the last 300 s, try
   the lease: `UPDATE meta SET value=:now WHERE key='dl_lease' AND CAST(value AS REAL) < :now - 120`
   (row seeded at migration). On `rowcount == 1`, commit and start a daemon thread that opens its own
   connection, runs `pull_history()` then `rebuild_cooked()`, and releases the lease (`value='0'`). The
   request does not wait.

Inline (synchronous) pull + rebuild: `/admin/api/dinnerlog?force=1`, and after `/admin/api/dinnerlog/alias`
and `/admin/api/dinnerlog/link` (rebuild only for those two; the snapshot didn't change).

### Status endpoint

`GET /admin/api/dinnerlog` (editor-only) returns `available` (configured and last pull succeeded),
`source` (`DINNERLOG_API_URL`), `pulled`, `synced`, `error`, `meals`, `linked`, `recipe_matched`,
`book_only`, `unmatched_titles`. `path` is removed. `static/app.js` admin view is updated to match.

## Security

- Removing the shared group removes Cookbook Collection's read access to every file Dinner Log owns.
- `HISTORY_TOKEN` is read-only and single-purpose. The endpoint 404s when unset, so installs that don't
  link the apps expose nothing.
- Meal names, dates and ratings are only reachable with the token. Responses are `private, no-store`.
- The consumer never follows redirects with the token and never logs it or response bodies.
- The token travels in plain HTTP only on the private address; docs say to keep `DINNERLOG_API_URL` on
  loopback or a private network, or use https.

## Testing

Dinner Log (`tests/test_history.py`, additions to `tests/test_recipe_picker.py`):

- Upgrade of a pre-change DB adds both columns; existing rows read back NULL.
- `parse_meal_params`: `"17"` → 17; `"x"`, `"0"`, `"-3"`, `"1e3"`, `""` → NULL; ids dropped for
  `restaurant` and `original`.
- Pick card "Log tonight" URL and `/cookbook/recipes` carry the ids (stubbed catalog).
- Saving `/new` with ids stores them; editing without changes keeps them.
- `/api/history`: 404 with `HISTORY_TOKEN` unset; 401 missing/wrong token; 200 correct token; restaurant
  and "our own" rows excluded; matching `If-None-Match` → 304; ETag changes after an edit and a delete.

Cookbook Collection (`tests/test_dinnerlog_http.py`, opener and thread starter stubbed; no network):

- Pull: 200 stores snapshot and ETag; 304 keeps snapshot, bumps `dl_pulled_at`; timeout and 5xx set
  backoff and keep `dl_meals`/`cooked`; wrong `version`, oversized body, 3xx rejected.
- Precedence: one meal per rule 1–5, plus a deleted stamped recipe id falling through to fuzzy.
- Never-pulled snapshot does not empty `cooked`.
- Lease: second acquire within the window fails; expired lease is re-acquired.
- `ensure_synced()` starts the pull without waiting; a catalog change rebuilds without a pull; backoff
  suppresses pulls.
- Existing tests that build a fake `meals.db` are converted to seed `dl_meals`, keeping their
  `match_book`/`match_recipe` assertions.
- Full suite stays green (baseline 246 passed, 2 skipped); `test_hygiene` passes.

## Rollout

1. Merge and deploy Dinner Log; set `HISTORY_TOKEN`. Nothing calls it yet.
2. Merge and deploy Cookbook Collection; set `DINNERLOG_API_URL` and `DINNERLOG_TOKEN`.
3. Check `/admin/api/dinnerlog?force=1`: `available: true`, meal counts consistent with before.

Rollback: reinstall the previous Cookbook Collection build (its installer re-adds the group and path).
Dinner Log's columns and endpoint are inert without a caller.

## Out of scope

- Backfilling ids into legacy Dinner Log rows.
- Removing fuzzy matching or `dl_link`.
- Incremental sync, push notifications from Dinner Log.
- Splitting `extras.py` (backlog item 4); new code goes in `extras.py` for now.
