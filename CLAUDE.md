# Cookbook Collection: notes for Claude

Flask + SQLite cookbook catalog. A public catalog at `/`, an editor UI at `/admin` (behind Cloudflare Access),
and an MCP server at `/mcp`. README.md is the user-facing guide; this file is how to work on the code.

## Map
| File | What's in it |
|---|---|
| `app.py` | Core: schema + `init_db`, `connect()`/`db()`, `fold()` (accent/case folding, also an SQL function), FTS (`books_fts`, `recipes_fts`), auth (`editor_required`), books/recipes/facets API, index paste import (`parse_index`, `mark_duplicates`, `index_commit`), `PUBLIC_BOOKS`, `cuisine_condition()`, `env_int`/`local_midnight`, `/healthz` (deep check: database, both FTS tables, DATA_DIR) |
| `extras.py` | Dinner Log history (pulled over HTTP into `dl_meals` and `dl_photos`, rebuilt into `cooked`; `fetch_dl_photo`; `ensure_synced()` starts background pulls under a DB lease), `cook_rank` (the one ranking behind the Cook page `/admin/api/cook`, `/admin/api/suggest`, MCP `suggest_recipes` and Dinner Log's `/admin/api/pick`; those are thin wrappers, so change modes and ordering there), stats, photo index import (Claude API, background jobs in `photo_jobs`; whole-index scans are a parent row with one child per page, `scan_state` derives the status, `merge_pages` joins them, `pages_today` enforces `PHOTO_DAILY_LIMIT`), MCP server (`TOOLS`, `WRITE_TOOLS`, `handle_rpc`, `run_tool`), `messages_api` (Claude API call), wishlist |
| `ask.py` | AI search: `POST /admin/api/ask`, `ask_log`, tool loop over `run_tool`, `show_results` hydration |
| `ingredients.py` | Ingredient tags and search hints, CSV/zip import, `resolve()`, `ranked()` search ranking |
| `eyb.py` | Eat Your Books import, merging garbled duplicate recipes |
| `photos.py` | Photos of cooked dishes; `sync_dl_photos` imports and reconciles photos shared from Dinner Log (`dl_file`, `dl_photo_skip`) |
| `online.py` | Recipes from websites (NYT Cooking): `books.kind='online'`, editor-only |
| `house.py` | House recipes: the one `books.kind='house'` book (`ensure_house_book`, after seeding), `books.visibility`, `recipe_details` / `recipe_ingredient_lines` / `recipe_steps`, `parse_quantity`, `parse_doc`/`save_doc`, read/write routes, `safe_return_url` (`RETURN_URL_ORIGINS`), `ROLLBACK_SQL` |
| `importer.py` | Loads the JSON export back in (`python3 -m importer`, also `import_data(con, data, ...)`): reconcile by uid, then ISBN, then title + first author; `--replace`, `--dry-run`, `--photos`. One transaction; needs `app.connect()` for `fold()`. `SKIP_SEED=1` stops `init_db` seeding an empty DB. No routes. Export carries `version` (`EXPORT_VERSION`, bump only for breaking shape changes) |
| `static/` | Single-page app (`app.js`, `style.css`, `index.html`) |
| `installer/` | `build.sh` -> `dist/setup-cookbooks.sh` (Alpine/OpenRC installer: `setup-head.sh` + base64 tarball). Also writes `/etc/logrotate.d/cookbooks` (only if absent) and installs `tzdata` |
| `ops/` | `app-backup` (nightly snapshots; warns when images have no `BACKUP_TARGET`), `app-restore` (`--list`, `--dry-run`, restore), `app-health`, `app-notify`, all POSIX `sh` for BusyBox (no bash); `migrate-ops` moves files from older container-named paths. `tests/test_app_backup.py` and `test_app_restore.py` run the real scripts against a fake root through env overrides (`LIB_DIR`, `LOCAL_DIR`, `RC_SERVICE`, ...) and need the `sqlite3` CLI |
| `tests/` | pytest; each file sets `DATA_DIR` to a temp dir and seeds from `seed.json`. `test_scan.py` covers scans, merging and the page-number forms |

Modules register routes onto `app.app` and add their tables in a `migrate(con)` that `init_db` calls.

## Commands
```sh
python -m pytest -q                      # full suite, ~10 s
sh installer/build.sh                    # -> dist/setup-cookbooks.sh
for f in installer/*.sh ops/* .github/scripts/*.sh; do sh -n "$f"; done   # one file per `sh -n`: it only parses the first
DATA_DIR=$(mktemp -d) LAN_EDIT=1 python3 app.py   # local server on :8767; /admin edits from localhost
```
Chromium for Playwright checks: `/opt/pw-browsers/chromium-*/chrome-linux/chrome` (cloud sessions).

## Conventions
- After a change to `static/`, bump `?v=N` on both asset links in `static/index.html` (browsers cache hard).
- A new top-level module must be added to the `cp` list in `installer/build.sh` and imported at the bottom of `app.py`.
- A new env var must be added to the `export` line in `installer/setup-head.sh`, or the service never sees it.
  `TZ` is the exception: it is exported on its own line only when non-empty, because an empty `TZ` means UTC and
  overrides `/etc/localtime`. It sets when `local_midnight()` (the Ask and photo daily limits) falls; the default is
  the machine's zone, which is UTC on a stock Alpine install.
- `/healthz` is public and unauthenticated. It returns only `ok`, `fts` and, on failure, a short `reason`
  (`db`, `fts`, `data-dir`) with 503; exception text goes to the log, never the response. It must not write
  (a public probe must not make the app touch the disk). `ops/app-health` only looks at the status code.
- The installer never overwrites a file the owner may have edited: `/etc/conf.d/*` only gains missing keys,
  `/etc/logrotate.d/cookbooks` is written only when absent. New ops files follow the same rule.
- Production runs gunicorn with `--workers 2`: no state in process memory between requests (use the DB).
  Long work (> ~90 s, Cloudflare's limit) goes in a background thread with a DB row the browser polls.
- Recipe text goes through `clean()`; comparisons go through `fold()`. Write recipes only through the app,
  since the FTS triggers call `fold()`.
- Anonymous reads filter with `PUBLIC_BOOKS` (SQL, alias `b`) or `book_public(row)`: an allow-list of kinds (NULL
  cookbooks, `'house'`) AND `visibility='public'`. `kind='online'` is never public. Every new read path (route, MCP
  tool, count) needs one of the two, and a test in `tests/test_house.py`. "How many cookbooks" counts use `COOKBOOKS`.
- `recipe_ingredients` is the EYB tag table; a house recipe's own lines are `recipe_ingredient_lines`. Only recipes in
  the house book carry details.
- `books.uid` / `recipes.uid` are the stable public ids (triggers fill them on insert; `id` is a reusable rowid).
  Anything stored outside this database, or sent to Dinner Log, should carry the uid.
- MCP tools: add to `TOOLS` in extras.py as (name, private_only, fn, description, schema); writes go in
  `WRITE_TOOLS` and are private-only. Keep tool output compact: it stays in the assistant's context.
- Ask (`ask.py`) reuses `run_tool()`; it must never be offered `WRITE_TOOLS`.
- Tests: commit the test's own connection before calling the app, or SQLite reports "database is locked".
- Photo jobs: never call Claude from tests (monkeypatch `call_claude` or `messages_api`). A scan page's thread waits on
  `_photo_sem` as `queued` and only becomes `running` (with `started`) when it gets a slot; stale checks use `started`.
  Finished jobs are deleted after `PHOTO_JOB_KEEP`; it must stay >= a day because `pages_today()` counts them.
- Dinner Log photos: `sync_dl_photos` runs only from `background_pull` (network, re-encoding), never from
  `rebuild_cooked` or a request. Removals are never capped. Never touch rows with `dl_file` NULL (uploads), never
  send `dl_file` to a client, and keep an editor's delete a permanent `dl_photo_skip` tombstone. Tests pass a fake
  `fetch`; pages that sync need `start_background` patched so no real pull starts.
- Every write route uses `@editor_required`, which refuses cross-site edits (Origin check) before auth.
  LAN editing (`lan_identity()`) trusts only the socket peer, never forwarded headers.
- Don't `pkill -f`/`pgrep -f` a server by a pattern that also matches your own shell command.
- Keep the owner's domain, server names and personal setup out of the repo; use `cookbooks.example.com`.
  `tests/test_hygiene.py` fails on container/VM IDs and home-directory paths; names, hostnames and emails
  can't be listed there, so check diffs for them yourself.

## Release flow
Develop on a branch, run the tests, open a PR, merge it, then build the installer and hand over `dist/setup-cookbooks.sh`.
The installer is safe to rerun. It keeps `/var/lib/cookbooks` and `/etc/conf.d/cookbooks` and backs up the DB first
(`app-restore --list` shows that copy as `cookbooks-before-upgrade-<time>`; the CI Alpine job runs a full
backup, change, `app-restore` cycle against the installed system).
