# Cookbook Collection: notes for Claude

Flask + SQLite cookbook catalog. A public catalog at `/`, an editor UI at `/admin` (behind Cloudflare Access),
and an MCP server at `/mcp`. README.md is the user-facing guide; this file is how to work on the code.

## Map
| File | What's in it |
|---|---|
| `app.py` | Core: schema + `init_db`, `connect()`/`db()`, `fold()` (accent/case folding, also an SQL function), FTS (`books_fts`, `recipes_fts`), auth (`editor_required`, `access_identity`, the per-worker Access key cache `_signing_key`), security headers (`CSP`, `security_headers`) and `/robots.txt`, books/recipes/facets API, index paste import (`parse_index`, `mark_duplicates`, `index_commit`), `PUBLIC_BOOKS`, `cuisine_condition()`, `env_int`/`local_midnight`, `/healthz` (deep check: database, both FTS tables, DATA_DIR) |
| `dinnerlog.py` | Dinner Log history: pulled over HTTP into `dl_meals` and `dl_photos` (`pull_history`, `fetch_dl_photo`), matched to books and recipes and rebuilt into `cooked` (`rebuild_cooked`: uid, then ids, then title + page + name), `ensure_synced()` starts background pulls under a DB lease (`try_lease`, `background_pull`). Also `cooked_by_book` / `cooked_by_recipe` / `cooked_stats_for`, `meta_get` / `meta_set` for the `meta` table, the `/admin/api/dinnerlog*` routes |
| `cook.py` | `cook_rank` (the one ranking behind the Cook page `/admin/api/cook`, `/admin/api/suggest`, MCP `suggest_recipes` and Dinner Log's `/admin/api/pick`; those are thin wrappers, so change modes and ordering there), the collection stats, shelf (on loan) tracking |
| `scan.py` | Photo index import (Claude API, background jobs in `photo_jobs`; whole-index scans are a parent row with one child per page, `scan_state` derives the status, `merge_pages` joins them, `pages_today` enforces `PHOTO_DAILY_LIMIT`), `messages_api` (the Claude API call, also used by Ask), `model_call` (one request, effort fallback), and the job helpers capture.py shares: `photo_slot`, `finish_job`, `child_jobs`, `PARENT_STATUSES`. The `ANTHROPIC_*` settings |
| `mcp.py` | MCP server (`TOOLS`, `WRITE_TOOLS`, `handle_rpc`, `run_tool`, access levels, token handling and log redaction) and `/admin/api/features` |
| `wishlist.py` | The family wish list link (adds a book to a person's list over localhost, remembers what was added) |
| `ask.py` | AI search: `POST /admin/api/ask`, `ask_log`, tool loop over `run_tool`, `show_results` hydration |
| `ingredients.py` | Ingredient tags and search hints, CSV/zip import, `resolve()`, `ranked()` search ranking |
| `eyb.py` | Eat Your Books import, merging garbled duplicate recipes |
| `photos.py` | Photos of cooked dishes; `sync_dl_photos` imports and reconciles photos shared from Dinner Log (`dl_file`, `dl_photo_skip`) |
| `online.py` | Recipes from websites (NYT Cooking): `books.kind='online'`, editor-only |
| `house.py` | House recipes: the one `books.kind='house'` book (`ensure_house_book`, after seeding), `books.visibility`, `recipe_details` / `recipe_ingredient_lines` / `recipe_steps`, `parse_quantity`, `parse_doc`/`save_doc`, read/write routes, `safe_return_url` (`RETURN_URL_ORIGINS`), `ROLLBACK_SQL` |
| `capture.py` | Start a house recipe from 1-4 photos or pasted text: a `photo_jobs` parent with `status='capture'` and a child per photo; `parse_reply` (the model's labelled lines, `_from_json` fallback), `parse_recipe_text` (pasted text with Ingredients/Method headings, no model), `merge` (photo order, section carry, `CONTINUED:`), `to_doc` (fields cut to `house.LIMITS`, then `house.parse_doc`), `call_claude`, attach/discard. Kept colour copies in `DATA_DIR/capture/<job>.<n>.jpg`, removed by `drop_job` or `sweep`. No schema |
| `importer.py` | Loads the JSON export back in (`python3 -m importer`, also `import_data(con, data, ...)`): reconcile by uid, then ISBN, then title + first author; `--replace`, `--dry-run`, `--photos`. One transaction; needs `app.connect()` for `fold()`. `SKIP_SEED=1` stops `init_db` seeding an empty DB. No routes. Export carries `version` (`EXPORT_VERSION`, bump only for breaking shape changes) |
| `static/` | Single-page app (`app.js`, `style.css`, `index.html`) |
| `installer/` | `build.sh` -> `dist/setup-cookbooks.sh` (Alpine/OpenRC installer: `setup-head.sh` + base64 tarball). Also writes `/etc/logrotate.d/cookbooks` (only if absent) and installs `tzdata` |
| `ops/` | `app-backup` (nightly snapshots; warns when images have no `BACKUP_TARGET`), `app-restore` (`--list`, `--dry-run`, restore), `app-health`, `app-notify`, all POSIX `sh` for BusyBox (no bash); `migrate-ops` moves files from older container-named paths. `tests/test_app_backup.py` and `test_app_restore.py` run the real scripts against a fake root through env overrides (`LIB_DIR`, `LOCAL_DIR`, `RC_SERVICE`, ...) and need the `sqlite3` CLI |
| `tests/` | pytest; each file sets `DATA_DIR` to a temp dir and seeds from `seed.json`. `test_scan.py` covers scans, merging and the page-number forms; `test_capture.py` the recipe reader |
| `.github/` | `workflows/ci.yml`: `pytest` (Alpine's python3 and the current stable Python), `alpine` (apk packages, then the real installer under OpenRC, on the production release and `latest`), `ui-tests`, `upgrade-path`, `seam-contract`, `shellcheck`, `ruff`, `gitleaks`. `scripts/`: `install-openrc-stub.sh`, `run-ui-tests.sh`, `upgrade_path.py`. `dependabot.yml`: one weekly PR each for actions and pip. Actions are pinned by commit SHA |
| `requirements-dev.txt`, `requirements-ui.txt`, `ruff.toml` | CI-only pins (production installs apk packages, so a bump never reaches a deployment) and the ruff config (E9 and F only; each per-file ignore says why) |

Modules register routes onto `app.app` and add their tables in a `migrate(con)` that `init_db` calls.

## Commands
```sh
python -m pytest -q                      # full suite, ~10 s
sh installer/build.sh                    # -> dist/setup-cookbooks.sh
for f in installer/*.sh ops/* .github/scripts/*.sh; do sh -n "$f"; done   # one file per `sh -n`: it only parses the first
DATA_DIR=$(mktemp -d) LAN_EDIT=1 python3 app.py   # local server on :8767; /admin edits from localhost
```
The checks CI runs (see README, "Checks"):
```sh
ruff check .                              # E9 + F only; config in ruff.toml
shellcheck --shell=sh installer/*.sh ops/* .github/scripts/*.sh
sh .github/scripts/run-ui-tests.sh        # browser tests; fails on any skip (PYTHON=python3 to pick the interpreter)
python3 .github/scripts/upgrade_path.py   # database from b25b194 -> current code; needs full git history
gitleaks git --redact .                   # secrets in the history
```
Chromium for Playwright checks: `/opt/pw-browsers/chromium-*/chrome-linux/chrome` (cloud sessions).

## Conventions
- `static/index.html` links `/static/app.js` and `/static/style.css` without a version: `render_index()` adds `?v=<content hash>` at
  startup and those URLs are served `immutable` for a year (the page itself is `no-cache` with an ETag). Don't hand-write a
  `?v=`. A new asset file that the page links must be added to `ASSET_FILES` in `app.py`.
- A new top-level module must be added to the `cp` list in `installer/build.sh` and imported at the bottom of `app.py`.
  Route registration follows that import order and `init_db` runs each module's `migrate(con)` in the order it lists them,
  so add to both at the place you want. Call another module's functions module-qualified (`dinnerlog.ensure_synced()`),
  and make tests patch the module that defines the name (`monkeypatch.setattr(scan, "call_claude", ...)`): patching a
  `from x import y` copy leaves the real function in place.
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
- MCP tools: add to `TOOLS` in mcp.py as (name, private_only, fn, description, schema); writes go in
  `WRITE_TOOLS` and are private-only. Keep tool output compact: it stays in the assistant's context.
- MCP access (`mcp_auth`): public, `private` (read tools) or `write`. `WRITE_TOOLS` need `MCP_WRITE=1` *and* a Bearer
  header token; a `/mcp/<token>` path token is never more than `private`, and `MCP_URL_TOKEN=0` refuses it.
  `MCP_TOKEN` is comma-separated (rotation); `token_ok()` compares every candidate with no early exit. A wrong token
  is refused, never downgraded to public. Never log a token. `tests/test_mcp_access.py` holds the full matrix.
- Ask (`ask.py`) reuses `run_tool()`; it must never be offered `WRITE_TOOLS`. `run_tool`/`handle_rpc` take
  `write=False` by default, and Ask never passes it.
- Tests: commit the test's own connection before calling the app, or SQLite reports "database is locked".
- Photo jobs: never call Claude from tests (monkeypatch `call_claude` or `messages_api`; for capture, `capture.call_claude`). A scan page's thread waits on
  `_photo_sem` as `queued` and only becomes `running` (with `started`) when it gets a slot; stale checks use `started`.
  Finished jobs are deleted after `PHOTO_JOB_KEEP`; it must stay >= a day because `pages_today()` counts them.
  A new kind of parent row goes in `PARENT_STATUSES` (never counted, never served by `/index/photo/<id>`).
- Recipe capture: the draft is never written to the recipe tables; the editor saves it through the ordinary house
  routes. Model output is data: `capture.plain()` and `to_doc()` before anything else sees it, `esc()` in the browser.
  Discarding a capture keeps its child rows (blanked) so `PHOTO_DAILY_LIMIT` can't be reset by discarding.
- Dinner Log photos: `sync_dl_photos` runs only from `background_pull` (network, re-encoding), never from
  `rebuild_cooked` or a request. Removals are never capped. Never touch rows with `dl_file` NULL (uploads), never
  send `dl_file` to a client, and keep an editor's delete a permanent `dl_photo_skip` tombstone. Tests pass a fake
  `fetch`; pages that sync need `start_background` patched so no real pull starts.
- Every write route uses `@editor_required`, which refuses cross-site edits (Origin check) before auth.
  LAN editing (`lan_identity()`) trusts only the socket peer, never forwarded headers.
  `tests/test_security_headers.py` checks every write route answers 401 to an anonymous client.
- Access tokens: never verify one on a public route (`/api/me` is a constant). Signing keys come from
  `_signing_key()`: an unknown kid refetches at most once per `JWKS_REFRESH_SECONDS` per worker and nothing a
  client sends clears the cache. `g.auth_reason` reaches the browser, so it names settings, never their values
  (no AUD tags, no library error text); pass those as `_reject(..., detail)`, which only logs them. Tests sign
  tokens with `tests/access_jwt.py` (`AccessKeys`), which stubs `_fetch_jwks`.
- Integer query parameters go through `arg_int()` (or `location_arg()` for ids): junk is a 400, never a 500.
  `tests/test_int_params.py` walks every GET route with junk values.
- The Content-Security-Policy is enforced: no inline `<script>`, no `on…=` attributes in templates (attach
  listeners), no `eval`. Inline `style=` attributes are allowed. Images from a new external host need that host in
  `CSP` (cover hosts come from `COVER_HOSTS`). `tests/test_csp_browser.py` opens the main screens in Chromium and
  fails on any violation; run it after front-end changes.
- Don't `pkill -f`/`pgrep -f` a server by a pattern that also matches your own shell command.
- Shell scripts are POSIX sh (BusyBox ash on Alpine) and `shellcheck --shell=sh` stays clean. Disable a finding inline,
  with the reason in a comment on the line above the directive.
- The browser tests (`pytest.importorskip("playwright...")`) skip quietly without Chromium; CI's `ui-tests` job fails on
  any skip. A new browser test file must be added to the list in `.github/scripts/run-ui-tests.sh`.
- A schema or migration change must keep `upgrade_path.py` passing. If it adds something an old database has to gain
  (a backfilled column, a new table), add a check for it there.
- Keep the owner's domain, server names and personal setup out of the repo; use `cookbooks.example.com`.
  `tests/test_hygiene.py` fails on container/VM IDs and home-directory paths; names, hostnames and emails
  can't be listed there, so check diffs for them yourself.

## Release flow
Develop on a branch, run the tests and the checks above, open a PR, wait for every CI job to pass, merge it, then build
the installer and hand over `dist/setup-cookbooks.sh`. When production moves to a new Alpine release, change the `alpine`
matrix and the first Python in the `pytest` matrix in `ci.yml` (the `python3` that release ships) in the same PR.
The installer is safe to rerun. It keeps `/var/lib/cookbooks` and `/etc/conf.d/cookbooks` and backs up the DB first
(`app-restore --list` shows that copy as `cookbooks-before-upgrade-<time>`; the CI Alpine job runs a full
backup, change, `app-restore` cycle against the installed system).
