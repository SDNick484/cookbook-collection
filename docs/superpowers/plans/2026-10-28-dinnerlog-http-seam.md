# Dinner Log Seam over HTTP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Cookbook Collection's direct read of Dinner Log's SQLite file with a token-protected HTTP
history endpoint, and stamp catalog ids on meals at logging time.

**Architecture:** Dinner Log gains two nullable id columns, fills them from the three catalog entry points,
and serves `GET /api/history` (full snapshot, content ETag). Cookbook Collection stores the snapshot in
`dl_meals`, rebuilds `cooked` locally with id-first precedence, and pulls in a background thread under a
DB lease (stale-while-revalidate).

**Tech Stack:** Python 3 / Flask / SQLite (both apps), stdlib `urllib` and `hmac`, pytest. Alpine/OpenRC
installers in POSIX sh.

**Spec:** `docs/superpowers/specs/2026-10-28-dinnerlog-http-seam-design.md` (this repo). Executors read both.

## Global Constraints

- Two repos, two branches, two PRs: `dinner-log` branch `feat/history-endpoint` (Tasks 1–3), merged and
  deployed first; `cookbook-collection` branch `feat/dinnerlog-http-seam` (Tasks 4–6).
- Tasks 1–3 need a clone of `dinner-log` with push access; Tasks 4–6 run in this repo.
- Response envelope: `{"version": 1, "meals": [...]}`; consumer rejects any other `version`.
- Timings (Cookbook Collection): pull when snapshot older than **60 s**; backoff **300 s** after a failed
  pull; lease expires after **120 s**; request timeout **5 s**; response body cap **10 MB** (10485760 bytes).
- Tokens compared with `hmac.compare_digest` on bytes; env values `.strip()`ped; URLs `.rstrip("/")`.
- Never log or store the token or a response body; `dl_error` holds a short message only.
- No new dependencies in either app. No state in process memory between requests (gunicorn `--workers 2`).
- Cookbook Collection conventions (CLAUDE.md): new env vars go in `installer/setup-head.sh` `add_setting`
  and the `export` line; bump `?v=N` on both asset links in `static/index.html` after `static/` changes;
  `tests/test_hygiene.py` must pass; use `cookbooks.example.com`/`dinners.example.com`, never real hosts.
- Every commit ends with the session's `Co-Authored-By` and `Claude-Session` trailers.

## Review Focus

1. **A 200 with no `ETag` header** — store `dl_etag=""`, rebuild, and send no `If-None-Match` next time
   (an empty string must not count as "never pulled" or block rebuilds). Test in Task 4.
2. **The background pull raises something unexpected** (e.g. `sqlite3.OperationalError` while storing) —
   the lease is released in `finally` and `dl_error` is recorded; pulls resume after backoff. Test in Task 6.
3. **History rows with NULL or missing optional fields** (`rating`, `source_detail`, ids absent from an
   older producer) — stored as NULL; `rebuild_cooked` doesn't crash. Test in Task 4.
4. **`DINNERLOG_API_URL` with a trailing slash** — requests go to `…/api/history`, not `…//api/history`.
   Test in Task 4.
5. **A meal deleted in Dinner Log** — after the next 200, it is gone from `dl_meals` and `cooked`, and its
   `dl_link` row stays inert (no resurrection). Test in Task 5.

---

## Dinner Log (repo `dinner-log`, branch `feat/history-endpoint`)

### Task 1: Id columns and saving them

**Files:**
- Modify: `app.py` (`SCHEMA`, upgrade block after it, `parse_meal_params`, `new_meal`, `edit_meal`)
- Modify: `templates/form.html` (two hidden inputs)
- Test: `tests/test_history.py` (new)

**Interfaces:**
- Produces: `parse_cookbook_id(raw) -> int | None`; `parse_meal_params(...)` result gains keys
  `cookbook_book_id`, `cookbook_recipe_id`; hidden inputs `<input type="hidden" name="cookbook_book_id"
  id="cookbook-book-id">` and `name="cookbook_recipe_id" id="cookbook-recipe-id"` in the form.

- [ ] **Step 1: Write the failing tests** (`load_app` from `test_app`)

```python
def test_upgrade_adds_id_columns(tmp_path):
    # old schema: create meals without the columns, insert a row, then load the app on that DATA_DIR
    ...
    cols = {r[1] for r in con.execute("PRAGMA table_info(meals)")}
    assert {"cookbook_book_id", "cookbook_recipe_id"} <= cols
    assert con.execute("SELECT cookbook_book_id, cookbook_recipe_id FROM meals").fetchone() == (None, None)

@pytest.mark.parametrize("raw,want", [("17", 17), ("x", None), ("0", None), ("-3", None),
                                       ("1e3", None), ("", None), (None, None)])
def test_parse_cookbook_id(tmp_path, raw, want):
    assert load_app(tmp_path).parse_cookbook_id(raw) == want

def test_ids_dropped_for_non_catalog_sources(tmp_path):
    a = load_app(tmp_path)
    for st in ("restaurant", "original"):
        v = a.parse_meal_params({"source_type": st, "cookbook_book_id": "3", "cookbook_recipe_id": "4"})
        assert (v["cookbook_book_id"], v["cookbook_recipe_id"]) == (None, None)
    v = a.parse_meal_params({"source_type": "online", "cookbook_book_id": "3", "cookbook_recipe_id": "4"})
    assert (v["cookbook_book_id"], v["cookbook_recipe_id"]) == (3, 4)

def test_new_saves_ids_and_edit_keeps_them(tmp_path):
    # POST /new with source_type=cookbook, ids 7/1 -> row has 7/1
    # GET /meal/<id>/edit renders value="7" and value="1" in the hidden inputs
    # POST /meal/<id>/edit with the same fields -> row still 7/1
```

- [ ] **Step 2:** Run `python -m pytest -q tests/test_history.py` — expect FAIL (`parse_cookbook_id` missing).
- [ ] **Step 3:** Implement. `parse_cookbook_id`: `str(raw).strip()`, accept only `isdigit()` and `> 0`.
  Columns added to `SCHEMA` and via `ALTER TABLE` guarded by `PRAGMA table_info(meals)` (like `rating`).
  `parse_meal_params` sets both to `None` unless `stype in ("cookbook", "online")`. Both INSERT and UPDATE
  include the two columns. `form.html` renders `meal.cookbook_book_id or ''` into the hidden inputs.
- [ ] **Step 4:** Run the file, then the full suite `python -m pytest -q` — expect all PASS.
- [ ] **Step 5:** Commit: `feat(meals): store Cookbook Collection book and recipe ids`

### Task 2: Stamp ids from the three entry points

**Files:**
- Modify: `app.py` (`catalog_dish`, `cookbook_recipes`, `cookbook_recipes_route`)
- Modify: `templates/index.html` (Log tonight `url_for`), `templates/form.html` (picker JS)
- Test: `tests/test_recipe_picker.py`, `tests/test_suggest.py`

**Interfaces:**
- Consumes: Task 1 hidden inputs and param names.
- Produces: `catalog_dish(r, today)` result gains `cookbook_book_id` (from `r["book_id"]`) and
  `cookbook_recipe_id` (from `r["id"]`); `cookbook_recipes(title)` returns
  `(title, book_id, [{"id", "name", "page"}])` — note the new middle element; `/cookbook/recipes` JSON
  gains `"book_id"` and each recipe `"id"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_catalog_dish_keeps_ids(tmp_path):            # test_suggest.py
    d = load_app(tmp_path).catalog_dish({"id": 2203, "book_id": 17, "name": "X", "book_title": "Zuni",
                                          "page": "342"}, datetime.date(2026, 10, 28))
    assert (d["cookbook_book_id"], d["cookbook_recipe_id"]) == (17, 2203)

def test_log_tonight_link_carries_ids(...):          # index page with a stubbed catalog pick
    assert "cookbook_book_id=17" in html and "cookbook_recipe_id=2203" in html

def test_recipe_picker_returns_ids(monkeypatch, tmp_path):   # test_recipe_picker.py, existing ZUNI fixture
    data = client.get("/cookbook/recipes?title=zuni cafe cookbook").get_json()
    assert data["book_id"] == 7 and data["recipes"][0]["id"] == 1
```

Update existing assertions that unpack `cookbook_recipes()` as a 2-tuple.

- [ ] **Step 2:** Run both files — expect FAIL.
- [ ] **Step 3:** Implement the Python changes. Form JS: `pickRecipe()` sets `#cookbook-recipe-id` to the
  recipe's `id` and `#cookbook-book-id` to the response's `book_id`; an `input` listener on the name,
  cookbook-title and detail inputs, and a `change` listener on source type, clear both hidden inputs when
  the edit isn't the picker's own write (track with the existing `autoName`/`autoPage` values). Local
  dishes (not `from_catalog`) pass no ids.
- [ ] **Step 4:** Full suite PASS. Manually check in a browser (`DATA_DIR=$(mktemp -d) python3 app.py`)
  that picking sets and editing the title clears the hidden fields (devtools).
- [ ] **Step 5:** Commit: `feat(form): carry catalog ids from picks, links and the recipe picker`

### Task 3: `GET /api/history`

**Files:**
- Modify: `app.py` (new `HISTORY_TOKEN`, `history_rows`, route)
- Modify: `deploy/install-alpine.sh`, `docker-compose.yml` / `.env.example` if present, `README.md`
- Test: `tests/test_history.py`

**Interfaces:**
- Produces: `HISTORY_TOKEN: str` (env, stripped); `history_rows(con) -> list[dict]` ordered by `id`, keys
  `id, day, name, source_type, source_title, source_detail, rating, cookbook_book_id, cookbook_recipe_id`;
  route `GET /api/history` → 404 if unset, 401 bad/missing bearer, else 200 JSON `{"version": 1, "meals"}`
  with `ETag: "<sha256 hex>"` and `Cache-Control: private, no-store`; 304 on matching `If-None-Match`.

- [ ] **Step 1: Write the failing tests**

```python
def test_history_off_without_token(client_no_token):        assert get("/api/history").status_code == 404
def test_history_rejects_bad_token(client):                 # none -> 401, "Bearer nope" -> 401
def test_history_scope(client):
    # meals: cookbook, online, restaurant w/o URL, original w/o URL, restaurant with "https://x" detail,
    # original with ids stamped directly in SQL -> returned names exclude only the two plain ones
def test_history_etag(client):
    r = get(auth); assert r.status_code == 200 and r.headers["Cache-Control"] == "private, no-store"
    assert get(auth, etag=r.headers["ETag"]).status_code == 304
    # edit one meal's rating -> ETag differs; delete a meal -> ETag differs again
```

Set `HISTORY_TOKEN` via `monkeypatch.setattr(a, "HISTORY_TOKEN", "t0ken")` after `load_app`.

- [ ] **Step 2:** Run — expect FAIL (404 for every case).
- [ ] **Step 3:** Implement. Scope `WHERE` clause: `source_type IN ('cookbook','online') OR source_detail
  LIKE '%://%' OR source_title LIKE '%://%' OR cookbook_book_id IS NOT NULL OR cookbook_recipe_id IS NOT
  NULL`. Serialize with `json.dumps(..., separators=(",", ":"))`, hash that exact byte string, compare
  `If-None-Match` to the quoted ETag. Installer: `set_conf "export HISTORY_TOKEN" "" "Token Cookbook
  Collection uses to read cooking history (optional)" ""`. README "Linking the two apps": `openssl rand
  -hex 32`, same value as Cookbook Collection's `DINNERLOG_TOKEN`, keep the URL private or https; mention
  the two new columns.
- [ ] **Step 4:** Full suite PASS; `sh -n deploy/install-alpine.sh` clean.
- [ ] **Step 5:** Commit: `feat(api): token-protected cooking history endpoint`
- [ ] **Step 6:** Request code review (superpowers:requesting-code-review), address findings, verify
  (superpowers:verification-before-completion), push, open PR linking this repo's spec URL.

---

## Cookbook Collection (this repo, branch `feat/dinnerlog-http-seam`)

### Task 4: Snapshot storage and `pull_history`

**Files:**
- Modify: `extras.py` (config, `MIGRATIONS`, `migrate`, new functions)
- Modify: `installer/setup-head.sh` (settings, export line, group handling, post-install hint)
- Test: `tests/test_dinnerlog_http.py` (new)

**Interfaces:**
- Produces (all in `extras.py`):
  - `DINNERLOG_API_URL: str`, `DINNERLOG_TOKEN: str`; constants `PULL_EVERY=60`, `PULL_BACKOFF=300`,
    `LEASE_TTL=120`, `PULL_TIMEOUT=5`, `MAX_HISTORY_BYTES=10485760`.
  - `dl_open(req, timeout)` — module-level function using an opener whose redirect handler refuses
    redirects; tests monkeypatch it.
  - `store_snapshot(con, meals: list[dict], etag: str) -> None` — replaces `dl_meals`, sets `dl_etag`,
    `dl_pulled_at`, clears `dl_error`/`dl_failed_at`; commits. Used by tests to seed history.
  - `pull_history(con, now: float | None = None) -> dict` — `{"ok": bool, "changed": bool, "error": str|None}`.
- Migration: `dl_meals` table (spec DDL), `INSERT OR IGNORE INTO meta VALUES ('dl_lease','0')`,
  `DELETE FROM meta WHERE key='dl_sig'`.

- [ ] **Step 1: Write the failing tests** (stub `X.dl_open` with a fake returning status/headers/body or
  raising `urllib.error.HTTPError` / `URLError` / `socket.timeout`)

```python
def test_pull_200_stores_snapshot_and_etag(...):  rows in dl_meals == payload; meta dl_etag == '"abc"'
def test_pull_sends_if_none_match_and_handles_304(...):  header sent; dl_meals unchanged; dl_pulled_at bumped
def test_pull_errors_keep_snapshot(...):  for timeout, 500, 302: ok False, dl_error set, dl_failed_at set, dl_meals unchanged
def test_pull_rejects_bad_payload(...):  version 2; meals not a list; id "x"; body > MAX_HISTORY_BYTES -> ok False
def test_pull_200_without_etag(...):  dl_etag == "" ; next pull sends no If-None-Match           # Review Focus 1
def test_pull_tolerates_missing_fields(...):  meal {"id": 5, "day": "2026-10-01", "name": "X"} stored, NULLs  # RF 3
def test_pull_url_trailing_slash(...):  DINNERLOG_API_URL "http://h:1/" -> request URL "http://h:1/api/history"  # RF 4
def test_pull_unconfigured_is_noop(...):  DINNERLOG_API_URL "" -> ok False, no meta writes
def test_error_never_contains_token(...):  HTTPError body containing the token -> token not in dl_error
```

- [ ] **Step 2:** Run `python -m pytest -q tests/test_dinnerlog_http.py` — expect FAIL.
- [ ] **Step 3:** Implement. Read at most `MAX_HISTORY_BYTES + 1` bytes; over the cap is an error. Store
  `dl_pulled_at`/`dl_failed_at` as `f"{now:.3f}"` epoch strings. Installer: remove the
  `addgroup "$SVC_USER" dinnerlog` block and `DINNERLOG_DB` setting; add `delgroup "$SVC_USER" dinnerlog
  2>/dev/null || true`; `add_setting` for `DINNERLOG_API_URL` ("Dinner Log's private address, e.g.
  http://127.0.0.1:8000 — reads cooking history") and `DINNERLOG_TOKEN` ("Dinner Log's HISTORY_TOKEN");
  replace `DINNERLOG_DB` with both in the `export` line; post-install hint mentions the two settings.
- [ ] **Step 4:** New file PASS; full suite PASS (old file-reader tests still pass — the reader is removed
  in Task 5); `sh -n installer/setup-head.sh` clean.
- [ ] **Step 5:** Commit: `feat(dinnerlog): pull cooking history over HTTP into a local snapshot`

### Task 5: `rebuild_cooked` with id-first precedence; remove the file reader

**Files:**
- Modify: `extras.py` (replace `sync_dinnerlog`/`dl_signature` with `catalog_sig` + `rebuild_cooked`;
  delete `DINNERLOG_DB` and the SQLite read)
- Modify: `tests/test_online.py:123-136`, `tests/test_pick.py:43-48`, `tests/test_v2.py:32-36,97,471` —
  fixtures seed via `store_snapshot` + `rebuild_cooked(con, force=True)` instead of writing `meals.db`;
  keep every existing assertion.
- Test: `tests/test_dinnerlog_http.py`

**Interfaces:**
- Consumes: `store_snapshot` (Task 4).
- Produces: `catalog_sig(con) -> str` (today's `b:`/`r:` parts); `rebuild_cooked(con, force=False) -> dict`
  `{"rebuilt": bool, "meals": int}` — no-op unless `force`, or `dl_etag` differs from meta
  `dl_built_etag`, or `catalog_sig` differs from `dl_cat_sig`; never runs while `dl_etag` is absent
  (never pulled). Writes `dl_built_etag` (added to the spec's meta keys: the ETag `cooked` was last built from), `dl_cat_sig`,
  `dl_synced`. `cooked.how` values:
  `manual | linked | auto | book | none`.

- [ ] **Step 1: Write the failing tests**

```python
def test_precedence(...):   # one dl_meals row per rule, catalog from seed.json
    # manual dl_link beats a stamped recipe id          -> how 'manual', recipe from dl_link
    # stamped recipe id that exists                     -> how 'linked', book_id from the recipe (ignores stamped book)
    # stamped book only + page in detail                -> how 'auto', recipe on that page in that book
    # online URL matching a saved online recipe         -> how 'auto'
    # no ids, fuzzy title + page                        -> how 'auto' (same result as before the change)
def test_deleted_stamped_recipe_falls_through(...):   # stamped recipe id 999999 -> fuzzy result, no error
def test_never_pulled_keeps_cooked(...):   # cooked has a row, dl_etag absent -> rebuild_cooked returns rebuilt False, row kept
def test_catalog_change_triggers_rebuild(...):   # add recipe on page 42 -> rebuild_cooked() (no force) matches it
def test_deleted_meal_disappears(...):   # snapshot without meal 3 -> not in cooked; its dl_link row unused   # RF 5
```

- [ ] **Step 2:** Run — expect FAIL.
- [ ] **Step 3:** Implement per spec precedence. Reuse `match_book`, `match_recipe`, `meal_key`,
  `online.match_meal` unchanged. Convert the three fixture files.
- [ ] **Step 4:** Full suite PASS (count ≥ 246 passed + new tests); `grep -n DINNERLOG_DB -r . --include=*.py`
  returns nothing.
- [ ] **Step 5:** Commit: `refactor(dinnerlog)!: rebuild cooked from the HTTP snapshot; drop the file reader`
  with a `BREAKING CHANGE:` footer naming `DINNERLOG_DB` → `DINNERLOG_API_URL`/`DINNERLOG_TOKEN`.

### Task 6: Stale-while-revalidate, status endpoint, links and docs

**Files:**
- Modify: `extras.py` (`ensure_synced`, lease helpers, `/admin/api/dinnerlog`, alias/link routes)
- Modify: `static/app.js` (🍽 link ids; admin status fields), `static/index.html` (`?v=N` bump)
- Modify: `README.md` (Dinner Log section), `CLAUDE.md` (`extras.py` row)
- Test: `tests/test_dinnerlog_http.py`

**Interfaces:**
- Consumes: `pull_history`, `rebuild_cooked`, `catalog_sig` (Tasks 4–5).
- Produces: `try_lease(con, now: float) -> bool`; `release_lease(con) -> None`;
  `start_background(fn) -> None` (wraps `threading.Thread(daemon=True).start()`; tests monkeypatch it);
  `background_pull() -> None` (own connection; pull, rebuild, `finally: release_lease`);
  status JSON keys `available, source, pulled, synced, error, meals, linked, recipe_matched, book_only,
  unmatched_titles`.

- [ ] **Step 1: Write the failing tests**

```python
def test_lease_single_holder(...):   try_lease(now) True; try_lease(now+1) False; try_lease(now+121) True
def test_ensure_synced_starts_pull_without_waiting(...):   stale dl_pulled_at -> start_background called once, dl_open not called inline
def test_fresh_snapshot_no_pull(...):   dl_pulled_at = now-10 -> start_background not called
def test_backoff_suppresses_pull(...):   dl_failed_at = now-100 -> not called; now-301 -> called
def test_background_pull_releases_lease_on_exception(...):   dl_open raises sqlite3.OperationalError -> lease '0', dl_error set   # RF 2
def test_status_force_pulls_inline(...):   GET /admin/api/dinnerlog?force=1 (editor) -> dl_open called; keys as above; no "path"
def test_status_requires_editor(...):   anonymous -> 401/403 as other admin routes
def test_alias_and_link_rebuild_without_pull(...):   dl_open not called; cooked reflects the change
```

- [ ] **Step 2:** Run — expect FAIL.
- [ ] **Step 3:** Implement. `ensure_synced`: once per request (`g._dl_synced`), rebuild inline if
  `catalog_sig` changed; if configured, stale (>`PULL_EVERY`), outside backoff, and `try_lease` → commit,
  `start_background(background_pull)`. Lease SQL from the spec, with `CAST(value AS REAL)`. 🍽 link in
  `app.js` appends `cookbook_book_id` (the open book's id, already held by the book view) and `cookbook_recipe_id`
  (`r.id`); admin status view shows `source`, `pulled`, `error`, `linked`. README: replace the
  read-only-file paragraph with "Linking the two apps" (placeholders, `openssl rand -hex 32`, keep the URL
  on loopback/private network or https, deploy Dinner Log first). CLAUDE.md `extras.py` row: "Dinner Log
  history pull (HTTP snapshot in `dl_meals`, rebuild into `cooked`)".
- [ ] **Step 4:** Full suite PASS; `tests/test_hygiene.py` PASS; `sh installer/build.sh` succeeds; start
  locally against the Task 3 Dinner Log running on another port with `HISTORY_TOKEN` set, and confirm
  `/admin/api/dinnerlog?force=1` shows `available: true` and the expected counts.
- [ ] **Step 5:** Commit: `feat(dinnerlog): background history pull with DB lease; status and links`
- [ ] **Step 6:** Request code review, address findings, verify, push, open PR (link the Dinner Log PR and
  state the deploy order and rollback from the spec's Rollout section).
