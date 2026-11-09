# Cookbook Collection: notes for Claude

Flask + SQLite cookbook catalog. A public catalog at `/`, an editor UI at `/admin` (behind Cloudflare Access),
and an MCP server at `/mcp`. README.md is the user-facing guide; this file is how to work on the code.

## Map
| File | What's in it |
|---|---|
| `app.py` | Core: schema + `init_db`, `connect()`/`db()`, `fold()` (accent/case folding, also an SQL function), FTS (`books_fts`, `recipes_fts`), auth (`editor_required`), books/recipes/facets API, index paste import (`parse_index`, `mark_duplicates`, `index_commit`), `PUBLIC_BOOKS`, `cuisine_condition()`, `env_int`/`local_midnight` |
| `extras.py` | Dinner Log history (pulled over HTTP into `dl_meals`, rebuilt into `cooked`; `ensure_synced()` starts background pulls under a DB lease), suggestions, stats, photo index import (Claude API, background jobs in `photo_jobs`; whole-index scans are a parent row with one child per page, `scan_state` derives the status, `merge_pages` joins them, `pages_today` enforces `PHOTO_DAILY_LIMIT`), MCP server (`TOOLS`, `WRITE_TOOLS`, `handle_rpc`, `run_tool`), `messages_api` (Claude API call), wishlist |
| `ask.py` | AI search: `POST /admin/api/ask`, `ask_log`, tool loop over `run_tool`, `show_results` hydration |
| `ingredients.py` | Ingredient tags and search hints, CSV/zip import, `resolve()`, `ranked()` search ranking |
| `eyb.py` | Eat Your Books import, merging garbled duplicate recipes |
| `photos.py` | Photos of cooked dishes |
| `online.py` | Recipes from websites (NYT Cooking): `books.kind='online'`, editor-only |
| `importer.py` | Loads the JSON export back in (`python3 -m importer`, also `import_data(con, data, ...)`): reconcile by uid, then ISBN, then title + first author; `--replace`, `--dry-run`, `--photos`. One transaction; needs `app.connect()` for `fold()`. `SKIP_SEED=1` stops `init_db` seeding an empty DB. No routes. Export carries `version` (`EXPORT_VERSION`, bump only for breaking shape changes) |
| `static/` | Single-page app (`app.js`, `style.css`, `index.html`) |
| `installer/` | `build.sh` -> `dist/setup-cookbooks.sh` (Alpine/OpenRC installer: `setup-head.sh` + base64 tarball) |
| `ops/` | Backup and health-check scripts the installer sets up; `migrate-ops` moves files from older container-named paths |
| `tests/` | pytest; each file sets `DATA_DIR` to a temp dir and seeds from `seed.json`. `test_scan.py` covers scans, merging and the page-number forms |

Modules register routes onto `app.app` and add their tables in a `migrate(con)` that `init_db` calls.

## Commands
```sh
python -m pytest -q                      # full suite, ~10 s
sh installer/build.sh                    # -> dist/setup-cookbooks.sh
DATA_DIR=$(mktemp -d) LAN_EDIT=1 python3 app.py   # local server on :8767; /admin edits from localhost
```
Chromium for Playwright checks: `/opt/pw-browsers/chromium-*/chrome-linux/chrome` (cloud sessions).

## Conventions
- After a change to `static/`, bump `?v=N` on both asset links in `static/index.html` (browsers cache hard).
- A new top-level module must be added to the `cp` list in `installer/build.sh` and imported at the bottom of `app.py`.
- A new env var must be added to the `export` line in `installer/setup-head.sh`, or the service never sees it.
- Production runs gunicorn with `--workers 2`: no state in process memory between requests (use the DB).
  Long work (> ~90 s, Cloudflare's limit) goes in a background thread with a DB row the browser polls.
- Recipe text goes through `clean()`; comparisons go through `fold()`. Write recipes only through the app,
  since the FTS triggers call `fold()`.
- `kind='online'` books are editor-only; public queries filter with `PUBLIC_BOOKS`.
- `books.uid` / `recipes.uid` are the stable public ids (triggers fill them on insert; `id` is a reusable rowid).
  Anything stored outside this database, or sent to Dinner Log, should carry the uid.
- MCP tools: add to `TOOLS` in extras.py as (name, private_only, fn, description, schema); writes go in
  `WRITE_TOOLS` and are private-only. Keep tool output compact: it stays in the assistant's context.
- Ask (`ask.py`) reuses `run_tool()`; it must never be offered `WRITE_TOOLS`.
- Tests: commit the test's own connection before calling the app, or SQLite reports "database is locked".
- Photo jobs: never call Claude from tests (monkeypatch `call_claude` or `messages_api`). A scan page's thread waits on
  `_photo_sem` as `queued` and only becomes `running` (with `started`) when it gets a slot; stale checks use `started`.
  Finished jobs are deleted after `PHOTO_JOB_KEEP`; it must stay >= a day because `pages_today()` counts them.
- Every write route uses `@editor_required`, which refuses cross-site edits (Origin check) before auth.
  LAN editing (`lan_identity()`) trusts only the socket peer, never forwarded headers.
- Don't `pkill -f`/`pgrep -f` a server by a pattern that also matches your own shell command.
- Keep the owner's domain, server names and personal setup out of the repo; use `cookbooks.example.com`.
  `tests/test_hygiene.py` fails on container/VM IDs and home-directory paths; names, hostnames and emails
  can't be listed there, so check diffs for them yourself.

## Release flow
Develop on a branch, run the tests, open a PR, merge it, then build the installer and hand over `dist/setup-cookbooks.sh`.
The installer is safe to rerun. It keeps `/var/lib/cookbooks` and `/etc/conf.d/cookbooks` and backs up the DB first.
