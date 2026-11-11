"""Cookbook Collection v2 features, registered onto the core app:

- Dinner Log link: pulls cooking history from Dinner Log's /api/history and matches
  dinners to books and recipes (by the ids Dinner Log stamped, else title + page +
  name, with manual overrides)
- Photo index import via the Claude API
- "What should we cook?" suggestions, stats, off-the-shelf tracking
- MCP endpoint (Streamable HTTP, stateless JSON) at /mcp
"""
import base64, hmac, io, json, logging, os, re, secrets, threading, time, urllib.error, urllib.request

from flask import abort, jsonify, request, Response

import app as core
from app import app, db, editor_required, fold, make_err, clean
import cook
import dinnerlog


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "low").strip()  # empty: the model's default


MCP_TOKEN = os.environ.get("MCP_TOKEN", "").strip()  # one token, or several comma-separated (for rotation)
MCP_PUBLIC = os.environ.get("MCP_PUBLIC", "1") == "1"
MCP_WRITE = os.environ.get("MCP_WRITE", "0") == "1"          # 1 = write tools for a token sent as a Bearer header
MCP_URL_TOKEN = os.environ.get("MCP_URL_TOKEN", "1") == "1"  # 0 = refuse /mcp/<token>; it is always read-only


def migrate(con):
    con.execute(WISH_SCHEMA)
    con.execute(PHOTO_SCHEMA)
    # Whole-index scans (one parent row, one child row per page). Single-page jobs leave all three NULL.
    photo_cols = [r[1] for r in con.execute("PRAGMA table_info(photo_jobs)")]
    for col, typ in (("parent_id", "TEXT"), ("page_no", "INTEGER"), ("started", "REAL")):
        if col not in photo_cols:
            con.execute(f"ALTER TABLE photo_jobs ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS photo_jobs_parent ON photo_jobs(parent_id)")
    con.execute("UPDATE photo_jobs SET status='error', "
                "error='Reading that photo stopped partway (the server restarted). Try it again.' "
                "WHERE status IN ('running', 'queued')")  # their threads died with the process


# ======================================================== photo index import
# A dense index page (three columns, 250+ entries) takes Claude a minute or two to transcribe: longer than
# Cloudflare lets a request wait. So a photo becomes a job: the upload returns at once, a thread does the work,
# and the browser checks back. Jobs live in the database because gunicorn runs more than one worker process.
PHOTO_PROMPT = """This is a photo of a page from a cookbook's index. Transcribe every index entry that has a page number. A full page can have 250 or more; don't skip any.

Rules:
- Read in the book's order: the whole first column top to bottom, then the next column.
- A main entry with no page number (e.g. "Chicken") is a heading; the indented entries under it are sub-entries. For each sub-entry, give that main entry as its heading. Headings carry over from the previous page/column when the first lines are indented continuations.
- A main entry that has its own page number has no heading; it may also be the heading for sub-entries below it.
- Some indexes put the first sub-entry on the heading's line after a colon: "abalone: abalone sarsa 52" is heading "abalone", name "abalone sarsa", and the indented lines below it share that heading.
- Join entries that wrap onto a second line, including a page number alone on the next line.
- Keep page numbers exactly as printed, including ranges and lists ("42", "342–44", "51, 88"). Drop "see also" cross-references and letter dividers.
- Keep the wording as printed; don't add or invent anything. If a word is unreadable, give your best reading.

Reply with one entry per line and nothing else, as: heading | name | page
Leave the heading empty when there is none. For example:
Chicken | braised with figs | 350
 | Ceviche, classic | 42"""

PHOTO_SCHEMA = """CREATE TABLE IF NOT EXISTS photo_jobs (
  id TEXT PRIMARY KEY, book_id INTEGER, status TEXT,  -- running | done | error; scans add queued and scan (parent)
  rows TEXT, warning TEXT, error TEXT, created REAL,
  parent_id TEXT, page_no INTEGER, started REAL)""" # pages of a scan; NULL on single-page jobs (migrate() adds them)
PHOTO_JOB_LIMIT = 600  # seconds; a job still "running" after this died with its worker
PHOTO_QUEUE_LIMIT = 7200  # seconds a scan page may wait for a slot before it's presumed dead
PHOTO_JOBS_MAX = 3     # reading photos at once across the server; more get 429 and the browser retries
PHOTO_JOB_KEEP = 86400  # seconds before finished jobs are deleted; must cover "since local midnight" (pages_today)
PHOTO_DAILY_LIMIT = core.env_int("PHOTO_DAILY_LIMIT", 100)  # pages per day, all editors together; each is one model call


class ClaudeAPIError(Exception):
    """The Messages API answered with an HTTP error status."""
    def __init__(self, status, detail):
        super().__init__(f"Claude API error {status}: {detail}")
        self.status, self.detail = status, detail


def messages_api(body, key, timeout):
    """POST body to the Messages API and return the parsed JSON. Only HTTP errors become
    ClaudeAPIError; timeouts and connection errors propagate unchanged."""
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ClaudeAPIError(e.code, e.read().decode(errors="replace")[:300])


def call_claude(image_jpeg, media_type="image/jpeg", effort=None):
    """Returns (text, stop_reason)."""
    effort = ANTHROPIC_EFFORT if effort is None else effort
    body = {
        "model": ANTHROPIC_MODEL, "max_tokens": 16000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image_jpeg).decode()}},
            {"type": "text", "text": PHOTO_PROMPT}]}],
    }
    if effort:  # transcription needs little thinking, and thinking is slow
        body["output_config"] = {"effort": effort}
    try:
        data = messages_api(body, ANTHROPIC_API_KEY, PHOTO_JOB_LIMIT - 30)
    except ClaudeAPIError as e:
        if e.status == 400 and effort and ("effort" in e.detail or "output_config" in e.detail):
            return call_claude(image_jpeg, media_type, effort="")  # a model without effort settings
        raise RuntimeError(f"Claude API error {e.status}: {e.detail}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, data.get("stop_reason")


_ODD_PAGE_RE = re.compile(rf"^{core.ODD_PAGE}$")


def _photo_row(heading, name, page):
    name, page = clean(str(name or "")), clean(str(page or ""))
    if not name or not page:
        return None
    page = re.sub(rf"^{core.PAGE_PREFIX}", "", page)  # "p. 42" -> "42"
    row = {"heading": clean(str(heading or "")) or None, "name": name, "page": page}
    if not re.search(r"\d", page):
        if not _ODD_PAGE_RE.match(page):
            return None
        row["warn"] = "page"  # a roman numeral: kept for the review, flagged
        return row
    row["page"] = re.sub(r"\s*[-—]\s*", "–", re.sub(r"\s*,\s*", ", ", page))
    if _ODD_PAGE_RE.match(page):
        row["warn"] = "page"  # "12a"
    return row


def parse_claude_rows(text, truncated=False):
    """Reads "heading | name | page" lines (or the JSON older prompts asked for). When the reply was cut off,
    its last line may be partial ("Ceviche | 34" of "342"), so it's dropped."""
    s = text.strip()
    if s.startswith(("{", "[")) or s.startswith("```json"):
        m = re.search(r"\{.*\}|\[.*\]", s, re.S)
        if m:
            try:
                data = json.loads(m.group())
            except ValueError:
                data = None
            if data is not None:
                entries = data.get("entries", []) if isinstance(data, dict) else data
                return [r for e in entries if isinstance(e, dict)
                        for r in [_photo_row(e.get("heading"), e.get("name"), e.get("page"))] if r]
    raw_lines = s.splitlines()
    if truncated and raw_lines and not text.endswith("\n"):
        raw_lines = raw_lines[:-1]
    lines = [ln for ln in raw_lines if "|" in ln]
    rows = []
    for ln in lines:
        parts = [p.strip() for p in ln.strip().split("|")]
        if len(parts) >= 5 and not parts[0] and not parts[-1]:
            parts = parts[1:-1]  # a markdown table row: | heading | name | page |
        heading, name, page = (None, parts[0], parts[1]) if len(parts) == 2 else (parts[0], "|".join(parts[1:-1]), parts[-1])
        if fold(name) == "name" and fold(page) == "page":
            continue  # a header line
        r = _photo_row(re.sub(r"^[-*•]\s+", "", heading or ""), name, page)
        if r:
            rows.append(r)
    return rows


OVERLAP_WINDOW = 60  # rows at the end of a page that the next photo can show again


def merge_pages(pages):
    """Rows of a whole-index scan, page by page in order (pages[i] is page i+1; an empty list is a page that
    failed or hasn't been read). Returns one list with page_no on every row, after:
    - carrying the last heading of a page into the headless rows at the top of the next page (a section
      continues across a page or column break, and the model can't see the previous page), marked carried;
    - dropping the leading run of rows on a page that repeat (folded name + page) rows near the end of
      the previous page: the photos overlapped. Same-name recipes elsewhere are kept."""
    out, prev = [], None  # prev: the previous page's rows, or None after a gap
    for i, rows in enumerate(pages):
        if not rows:
            prev = None
            continue
        carry = prev[-1]["heading"] if prev else None
        tail = {(fold(r["name"]), r["page"] or "") for r in prev[-OVERLAP_WINDOW:]} if prev else set()
        kept, overlapping = [], True
        for r in rows:
            r = {**r, "page_no": i + 1}
            overlapping = overlapping and (fold(r["name"]), r["page"] or "") in tail
            if overlapping:
                continue  # the leading run of repeats; a repeat further down is for mark_duplicates to flag
            if carry is not None:
                if r.get("heading") is None:
                    r["heading"], r["carried"] = carry, True
                else:
                    carry = None  # the leading headless run is over
            kept.append(r)
        out.extend(kept)
        prev = rows
    return out


def prepare_photo(raw):
    from PIL import ImageOps
    img = ImageOps.exif_transpose(core.open_image(raw)).convert("L")  # grayscale: text only, smaller
    # Current models read images up to 2576px on the long side; a dense three-column index needs all of it
    # (older models scale it down themselves).
    img.thumbnail((2576, 2576))
    img = ImageOps.autocontrast(img, cutoff=1)
    for q in (88, 75, 60):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=q)
        if out.tell() < 3_500_000:  # the API takes up to 5 MB of base64 per image
            break
    return out.getvalue()


_photo_sem = threading.Semaphore(2)


def run_photo_job(job_id, jpeg):
    res_rows, warning, err_msg = None, None, None
    try:
        with _photo_sem:
            con = core.connect()  # a scan page waited for this slot: now it's running, and the clock starts
            try:
                con.execute("UPDATE photo_jobs SET status='running', started=? WHERE id=? AND status='queued'",
                            (time.time(), job_id))
                con.commit()
            finally:
                con.close()
            text, stop = call_claude(jpeg)
        cut = stop == "max_tokens"
        rows = parse_claude_rows(text, truncated=cut)
        if not rows:
            if stop == "refusal":
                raise RuntimeError("Claude declined to read that photo")
            raise RuntimeError("no entries with page numbers found" + (f": {clean(text)[:200]}" if text.strip() else ""))
        warning = (f"This page was cut off after {len(rows)} entries. Photograph the rest of the page "
                   "(a column or two at a time) to get the remaining entries.") if cut else None
        res_rows = json.dumps(rows)
    except Exception as e:  # noqa: BLE001
        err_msg = f"Couldn't read the index from that photo: {e}"
    con = core.connect()
    try:
        if err_msg is not None:
            con.execute("UPDATE photo_jobs SET status='error', error=? WHERE id=?", (err_msg, job_id))
        else:
            con.execute("UPDATE photo_jobs SET status='done', rows=?, warning=? WHERE id=?",
                        (res_rows, warning, job_id))
        con.commit()
    finally:
        con.close()


@app.post("/admin/api/books/<int:book_id>/index/photo")
@editor_required
def index_photo(book_id):
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    if not ANTHROPIC_API_KEY:
        return make_err(503, "Photo import needs ANTHROPIC_API_KEY in /etc/conf.d/cookbooks")
    f = request.files.get("file")
    if not f:
        return make_err(400, "No photo")
    try:
        jpeg = prepare_photo(f.read())
    except Exception as e:  # noqa: BLE001
        return make_err(400, f"Couldn't read that image: {e}")
    job_id = secrets.token_urlsafe(12)
    con.commit()
    con.execute("BEGIN IMMEDIATE")  # one writer at a time, so the count and the insert agree across workers
    capped = daily_limit_reached(con, 1)
    if capped:
        con.rollback()
        return capped
    active = con.execute("SELECT count(*) FROM photo_jobs WHERE status='running' AND created > ?",
                         (time.time() - PHOTO_JOB_LIMIT,)).fetchone()[0]
    if active >= PHOTO_JOBS_MAX:
        con.rollback()
        resp = make_err(429, "Too many photos are being read right now. Trying again shortly.")
        resp.headers["Retry-After"] = "5"
        return resp
    con.execute("DELETE FROM photo_jobs WHERE created < ?", (time.time() - PHOTO_JOB_KEEP,))
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES (?,?,'running',?)",
                (job_id, book_id, time.time()))
    con.commit()
    threading.Thread(target=run_photo_job, args=(job_id, jpeg), daemon=True).start()
    return jsonify(job=job_id), 202


@app.get("/admin/api/index/photo/<job_id>")
@editor_required
def index_photo_job(job_id):
    j = db().execute("SELECT * FROM photo_jobs WHERE id=?", (job_id,)).fetchone()
    if not j or j["status"] == "scan":  # a whole-index scan is polled at /index/scan/<id>
        abort(404)
    elapsed = int(time.time() - j["created"])
    if j["status"] == "running":
        if elapsed > PHOTO_JOB_LIMIT:
            msg = "Reading that photo stopped partway (the server may have restarted). Try it again."
            db().execute("UPDATE photo_jobs SET status='error', error=? WHERE id=? AND status='running'", (msg, job_id))
            db().commit()
            return jsonify(status="error", error=msg)
        return jsonify(status="running", elapsed=elapsed)
    if j["status"] == "error":
        return jsonify(status="error", error=j["error"])
    return jsonify(status="done", warning=j["warning"], rows=core.mark_duplicates(j["book_id"], json.loads(j["rows"])))


# ---- whole-index scans: one parent job, one child job per page, merged for a single review
# The parent row (status 'scan') only ties the pages together; its state is derived from its children on every
# poll, so it's the same from either gunicorn worker. Pages wait for a _photo_sem slot in their own thread
# ('queued'), then run through the single-photo path above.
def pages_today(con):
    """Pages (single photos and scan pages) accepted since local midnight. Counted from photo_jobs itself,
    which the cleanup keeps for PHOTO_JOB_KEEP >= a day."""
    return con.execute("SELECT count(*) FROM photo_jobs WHERE status != 'scan' AND created >= ?",
                       (core.local_midnight(),)).fetchone()[0]


def daily_limit_reached(con, n):
    """A 429 response if accepting n more pages would pass PHOTO_DAILY_LIMIT, else None. No Retry-After:
    unlike the concurrency 429, waiting a few seconds won't help."""
    used = pages_today(con)
    if used + n <= PHOTO_DAILY_LIMIT:
        return None
    return make_err(429, f"Today's limit of {PHOTO_DAILY_LIMIT} pages is used up ({used} read so far"
                    f"{f', {n} more asked for' if n > 1 else ''}). It resets at midnight; PHOTO_DAILY_LIMIT in "
                    "/etc/conf.d/cookbooks raises it.", daily_limit=True)


def _scan_row(con, scan_id):
    j = con.execute("SELECT * FROM photo_jobs WHERE id=? AND status='scan'", (scan_id,)).fetchone()
    if not j:
        abort(404)
    return j


def _queue_pages(con, parent, files, page_nos):
    """Insert a child row per file (queued, or error if the image can't be read) and return the threads to
    start once the rows are committed: a worker finishing before its row exists would update nothing."""
    threads = []
    for f, page_no in zip(files, page_nos):
        job_id = secrets.token_urlsafe(12)
        try:
            jpeg = prepare_photo(f.read())
        except Exception as e:  # noqa: BLE001
            con.execute("INSERT INTO photo_jobs (id, book_id, status, error, created, parent_id, page_no) "
                        "VALUES (?,?,'error',?,?,?,?)",
                        (job_id, parent["book_id"], f"Couldn't read that image: {e}", time.time(), parent["id"], page_no))
            continue
        con.execute("INSERT INTO photo_jobs (id, book_id, status, created, parent_id, page_no) VALUES (?,?,'queued',?,?,?)",
                    (job_id, parent["book_id"], time.time(), parent["id"], page_no))
        threads.append(threading.Thread(target=run_photo_job, args=(job_id, jpeg), daemon=True))
    return threads


@app.post("/admin/api/books/<int:book_id>/index/scan")
@editor_required
def index_scan_start(book_id):
    con = db()
    if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
        abort(404)
    if not ANTHROPIC_API_KEY:
        return make_err(503, "Photo import needs ANTHROPIC_API_KEY in /etc/conf.d/cookbooks")
    scan_id = secrets.token_urlsafe(12)
    con.execute("DELETE FROM photo_jobs WHERE created < ?", (time.time() - PHOTO_JOB_KEEP,))
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES (?,?,'scan',?)", (scan_id, book_id, time.time()))
    con.commit()
    return jsonify(scan=scan_id), 202


@app.post("/admin/api/index/scan/<scan_id>/pages")
@editor_required
def index_scan_pages(scan_id):
    con = db()
    parent = _scan_row(con, scan_id)
    files = request.files.getlist("file")
    if not files:
        return make_err(400, "No photos")
    con.commit()
    con.execute("BEGIN IMMEDIATE")  # the daily count and the inserts agree across workers
    capped = daily_limit_reached(con, len(files))
    if capped:
        con.rollback()
        return capped
    first = (con.execute("SELECT coalesce(max(page_no), 0) FROM photo_jobs WHERE parent_id=?", (scan_id,)).fetchone()[0] or 0) + 1
    page_nos = list(range(first, first + len(files)))
    threads = _queue_pages(con, parent, files, page_nos)
    con.commit()
    for t in threads:
        t.start()
    return jsonify(pages=page_nos), 202


@app.put("/admin/api/index/scan/<scan_id>/pages/<int:page_no>")
@editor_required
def index_scan_retry(scan_id, page_no):
    """Read one page again from a new upload (the browser still has the photo). Replaces that page's row."""
    con = db()
    parent = _scan_row(con, scan_id)
    old = con.execute("SELECT * FROM photo_jobs WHERE parent_id=? AND page_no=?", (scan_id, page_no)).fetchone()
    if not old:
        abort(404)
    if old["status"] in ("queued", "running"):
        return make_err(409, "That page is still being read.")
    f = request.files.get("file")
    if not f:
        return make_err(400, "No photo")
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    con.execute("DELETE FROM photo_jobs WHERE id=?", (old["id"],))  # a retry replaces the page, so it isn't counted twice
    capped = daily_limit_reached(con, 1)
    if capped:
        con.rollback()
        return capped
    threads = _queue_pages(con, parent, [f], [page_no])
    con.commit()
    for t in threads:
        t.start()
    return jsonify(pages=[page_no]), 202


def scan_state(con, parent):
    """The parent's status, derived from its pages, with the merged rows of the pages read so far."""
    now = time.time()
    kids = con.execute("SELECT * FROM photo_jobs WHERE parent_id=? ORDER BY page_no", (parent["id"],)).fetchall()
    stale = [k["id"] for k in kids if (k["status"] == "running" and (k["started"] or k["created"]) < now - PHOTO_JOB_LIMIT)
             or (k["status"] == "queued" and k["created"] < now - PHOTO_QUEUE_LIMIT)]
    if stale:
        msg = "Reading that page stopped partway (the server may have restarted). Retry the page."
        con.executemany("UPDATE photo_jobs SET status='error', error=? WHERE id=? AND status IN ('running', 'queued')",
                        [(msg, i) for i in stale])
        con.commit()
        kids = con.execute("SELECT * FROM photo_jobs WHERE parent_id=? ORDER BY page_no", (parent["id"],)).fetchall()
    pages, by_no = [], {}
    for k in kids:
        rows = json.loads(k["rows"]) if k["status"] == "done" and k["rows"] else None
        by_no[k["page_no"]] = rows or []
        pages.append({"page_no": k["page_no"], "status": k["status"], "rows": len(rows) if rows is not None else None,
                      "error": k["error"], "warning": k["warning"],
                      "elapsed": int(now - (k["started"] or k["created"])) if k["status"] in ("queued", "running") else None})
    if not kids:
        status = "empty"
    elif any(k["status"] in ("queued", "running") for k in kids):
        status = "running"
    else:
        status = "done"
    merged = merge_pages([by_no.get(n, []) for n in range(1, max(by_no, default=0) + 1)])
    merged = core.mark_duplicates(parent["book_id"], merged)
    present = sum(1 for r in merged if r["duplicate"])
    return {"status": status, "book_id": parent["book_id"], "pages": pages, "rows": merged,
            "added": len(merged) - present, "present": present}


@app.get("/admin/api/index/scan/<scan_id>")
@editor_required
def index_scan_status(scan_id):
    con = db()
    return jsonify(scan_state(con, _scan_row(con, scan_id)))


@app.get("/admin/api/features")
@editor_required
def features():
    import ask  # local: ask imports extras
    return jsonify(ask=ask.enabled(), photo_import=bool(ANTHROPIC_API_KEY), dinnerlog=bool(dinnerlog.DINNERLOG_API_URL),
                   dinnerlog_url=dinnerlog.DINNERLOG_URL or None, wishlist=wishlist_available(),
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


def t_stats(a, private):
    return cook.stats()


def t_set_want(a, private):
    rid = int(a["recipe_id"])
    con = db()
    if not con.execute("SELECT 1 FROM recipes WHERE id=?", (rid,)).fetchone():
        raise ToolError("No such recipe")
    want = a.get("want", True) not in (False, 0, "false", "False", "0", "no")
    con.execute("UPDATE recipes SET want_to_try=? WHERE id=?", (1 if want else 0, rid))
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
     "What we've cooked from our cookbooks (from Dinner Log): most-used books, top-rated recipes, books never cooked from.",
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


WISHLIST_URL = os.environ.get("WISHLIST_URL", "http://127.0.0.1:8766").rstrip("/")
WISHLIST_BOOK_URL = os.environ.get("WISHLIST_BOOK_URL", "https://www.amazon.com/s?k={q}")


WISH_SCHEMA = """CREATE TABLE IF NOT EXISTS wish_items (
  id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  person_id INTEGER, person_name TEXT, item_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def wishlist_call(method, path, body=None, timeout=5):
    """Talk to the wish list app over localhost (it treats loopback requests as LAN editors)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(WISHLIST_URL + path, data=data, method=method, headers={
        "X-Wishlist": "1", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        raise RuntimeError(msg or f"wish list answered {e.code}")
    except OSError as e:
        raise RuntimeError(f"wish list isn't reachable at {WISHLIST_URL} ({e})")


def wishlist_available():
    """True only when the wish list app answers on WISHLIST_URL. The URL is set by default even when the app
    isn't installed, so checking the URL alone would show a button that always fails. A refused localhost
    connection returns at once, so checking on each features request is cheap."""
    if not WISHLIST_URL:
        return False
    try:
        wishlist_call("GET", "/api/people", timeout=1)
        return True
    except RuntimeError:
        return False


@app.get("/admin/api/wishlist/people")
@editor_required
def wishlist_people():
    try:
        people = wishlist_call("GET", "/api/people")
    except RuntimeError as e:
        return make_err(502, str(e))
    return jsonify(people=[{"id": p["id"], "name": p["name"], "color": p.get("color")} for p in people])


@app.post("/admin/api/books/<int:book_id>/wishlist")
@editor_required
def add_to_wishlist(book_id):
    d = request.get_json(force=True)
    con = db()
    row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    b = core.book_dicts([row], True)[0]
    pid = int(d.get("person_id") or 0)
    by = f" by {', '.join(b['authors'][:2])}" if b["authors"] else ""
    q = b["isbn"] or f"{b['title']} {' '.join(b['authors'][:1])}"
    notes = ["Cookbook"]
    if b["isbn"]:
        notes.append(f"ISBN {b['isbn']}")
    if d.get("notes"):
        notes.append(str(d["notes"])[:300])
    item = {"name": f"{b['title']}{by}"[:200], "kind": "online",
            "url": WISHLIST_BOOK_URL.replace("{q}", urllib.parse.quote_plus(q)),
            "notes": " · ".join(notes), "priority": int(d.get("priority") or 2)}
    if d.get("price") not in (None, ""):
        item["price"] = d["price"]
    try:
        people = {p["id"]: p["name"] for p in wishlist_call("GET", "/api/people")}
        if pid not in people:
            return make_err(400, "Pick whose wish list")
        created = wishlist_call("POST", f"/api/people/{pid}/items", item)
    except RuntimeError as e:
        return make_err(502, str(e))
    con.execute("INSERT INTO wish_items(book_id, person_id, person_name, item_id) VALUES (?,?,?,?)",
                (book_id, pid, people[pid], created.get("id")))
    con.commit()
    return jsonify(ok=True, person=people[pid], item_id=created.get("id"))


def wishes_for_book(book_id):
    return [dict(r) for r in db().execute(
        "SELECT person_name, created_at FROM wish_items WHERE book_id=? ORDER BY id", (book_id,))]
