"""Photo index import, registered onto the core app.

A photo of a cookbook's index page is read by the Claude API into recipe rows. A photo is a job (a thread plus a
photo_jobs row the browser polls), and a whole-index scan is a parent row with one child per page. Owns the
ANTHROPIC_* settings and the Messages API call (messages_api), which Ask also uses.
"""
import base64, io, json, os, re, secrets, threading, time, urllib.error, urllib.request

from flask import abort, jsonify, request

import app as core
from app import app, db, editor_required, fold, make_err, clean


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "low").strip()  # empty: the model's default


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


def migrate(con):
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
