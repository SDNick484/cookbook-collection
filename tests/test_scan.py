"""Whole-index scanning: a parent photo job with one child per page, merged across pages, plus the
page-number prefixes and odd page forms those indexes use, and the daily page limit."""
import io, json, os, sys, tempfile, time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
import app as A  # noqa: E402
import extras as X  # noqa: E402

LAN = dict(base_url="http://127.0.0.1:8767", environ_base={"REMOTE_ADDR": "127.0.0.1"})


@pytest.fixture()
def c():
    return A.app.test_client()


# ------------------------------------------------------------------ schema
def test_photo_jobs_has_scan_columns_and_single_page_rows_are_untouched():
    con = A.connect()
    cols = {r[1] for r in con.execute("PRAGMA table_info(photo_jobs)")}
    assert {"parent_id", "page_no", "started"} <= cols
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES ('single', 1, 'done', ?)", (time.time(),))
    con.commit()
    row = con.execute("SELECT parent_id, page_no, started FROM photo_jobs WHERE id='single'").fetchone()
    assert tuple(row) == (None, None, None)
    con.close()


def test_migrate_marks_queued_and_running_pages_dead_after_restart():
    con = A.connect()
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created, parent_id, page_no) VALUES ('q1', 1, 'queued', ?, 'p', 1)",
                (time.time(),))
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created, parent_id, page_no) VALUES ('r1', 1, 'running', ?, 'p', 2)",
                (time.time(),))
    con.commit()
    X.migrate(con)
    con.commit()
    rows = {r[0]: r[1] for r in con.execute("SELECT id, status FROM photo_jobs WHERE id IN ('q1', 'r1')")}
    assert rows == {"q1": "error", "r1": "error"}
    con.close()


# ------------------------------------------------------------------ page-number prefixes and odd pages
def test_parse_index_strips_page_prefixes():
    rows = A.parse_index("Zuppa di farro, p. 42\nRisotto\n  ai funghi, pag. 88\n  allo zafferano pp. 90-91\nPolenta P.12\nKetchup 42\n")
    got = [(r["heading"], r["name"], r["page"]) for r in rows]
    assert (None, "Zuppa di farro", "42") in got
    assert ("Risotto", "ai funghi", "88") in got
    assert ("Risotto", "allo zafferano", "90–91") in got
    assert (None, "Polenta", "12") in got
    assert (None, "Ketchup", "42") in got  # a name ending in p is not a prefix
    assert not any("warn" in r for r in rows)


def test_parse_index_keeps_roman_and_lettered_pages_with_a_warning():
    rows = A.parse_index("Introduzione, xii\nAbout this book, p. iv\nTagliatelle, 12a\nSpice mix\n  for lamb, 40\n")
    got = {r["name"]: r for r in rows}
    assert got["Introduzione"]["page"] == "xii" and got["Introduzione"]["warn"] == "page"
    assert got["About this book"]["page"] == "iv" and got["About this book"]["warn"] == "page"
    assert got["Tagliatelle"]["page"] == "12a" and got["Tagliatelle"]["warn"] == "page"
    assert "Spice" not in got                     # "mix" is a valid numeral (1009) but not a page
    assert got["for lamb"]["heading"] == "Spice mix" and "warn" not in got["for lamb"]
    assert "Spice mix" not in got                 # still a heading, as before


def test_parse_claude_rows_strips_prefixes_and_flags_odd_pages():
    rows = X.parse_claude_rows("Pasta | al pomodoro | p. 42\n | Pane | pag. 7\n | Dolci | pp. 100-102\n"
                               " | Prefazione | xii\n | Tabella | 12a\n | Nothing | see also\n")
    assert [(r["name"], r["page"]) for r in rows] == [
        ("al pomodoro", "42"), ("Pane", "7"), ("Dolci", "100–102"), ("Prefazione", "xii"), ("Tabella", "12a")]
    assert [r.get("warn") for r in rows] == [None, None, None, "page", "page"]
    # the JSON shape older prompts produced goes through the same cleanup
    rows = X.parse_claude_rows('{"entries": [{"heading": null, "name": "Lomo saltado", "page": "pag. 212"}]}')
    assert rows == [{"heading": None, "name": "Lomo saltado", "page": "212"}]


# ------------------------------------------------------------------ merging pages
def R(heading, name, page, **extra):
    return {"heading": heading, "name": name, "page": page, **extra}


def test_merge_carries_the_last_heading_into_headless_rows_at_the_top_of_the_next_page():
    page1 = [R(None, "Almonds, toasted", "72"), R("Chicken", "braised with figs", "350"), R("Chicken", "roast", "342")]
    page2 = [R(None, "with bread salad", "344"), R(None, "stock", "12"), R("Chickpeas", "with chard", "100"), R(None, "Polenta", "88")]
    out = X.merge_pages([page1, page2])
    assert [(r["heading"], r["name"]) for r in out] == [
        (None, "Almonds, toasted"), ("Chicken", "braised with figs"), ("Chicken", "roast"),
        ("Chicken", "with bread salad"), ("Chicken", "stock"), ("Chickpeas", "with chard"), (None, "Polenta")]
    assert [r.get("carried") for r in out] == [None, None, None, True, True, None, None]
    assert [r["page_no"] for r in out] == [1, 1, 1, 2, 2, 2, 2]


def test_merge_does_not_invent_a_heading_from_a_headless_last_row():
    out = X.merge_pages([[R(None, "Zuppa", "10")], [R(None, "Risotto", "20")]])
    assert out[1]["heading"] is None and "carried" not in out[1]


def test_merge_drops_overlap_between_the_end_of_one_page_and_the_start_of_the_next():
    page1 = [R("Pasta", "al pomodoro", "42"), R("Pasta", "alla gricia", "43"), R(None, "Pane", "50")]
    page2 = [R(None, "alla gricia", "43"), R(None, "Pane", "50"), R(None, "Pane", "51"), R(None, "Pane", "50")]
    out = X.merge_pages([page1, page2])
    assert [(r["name"], r["page"], r["page_no"]) for r in out] == [
        ("al pomodoro", "42", 1), ("alla gricia", "43", 1), ("Pane", "50", 1), ("Pane", "51", 2), ("Pane", "50", 2)]
    # same name on another page survives; a repeat deeper into page 2 is for mark_duplicates, not the overlap rule


def test_merge_overlap_matches_on_folded_name():
    out = X.merge_pages([[R(None, "Ají amarillo", "51")], [R(None, "AJI AMARILLO", "51"), R(None, "Causa", "52")]])
    assert [r["name"] for r in out] == ["Ají amarillo", "Causa"]


def test_merge_does_not_carry_across_a_missing_page():
    # page 2 failed (or hasn't been read yet): nothing is known about where its headings end
    out = X.merge_pages([[R("A", "one", "1")], [], [R(None, "two", "2")]])
    assert [(r["name"], r["page_no"], r["heading"]) for r in out] == [("one", 1, "A"), ("two", 3, None)]


# ------------------------------------------------------------------ scan jobs
def jpeg(width):
    """A page photo whose width tells the fake model which page it is."""
    buf = io.BytesIO()
    Image.new("L", (width, 600), 200).save(buf, "JPEG")
    return buf.getvalue()


PAGE_TEXT = {
    1: "Chicken | braised with figs | 350\nChicken | roast | 342\n",
    2: " | with bread salad | 344\n | Pane | 50\nPasta | al pomodoro | 42\n",
    3: " | al pomodoro | 42\n | Polenta | p. 88\n | Prefazione | xii\n",
}


def fake_claude(pages=PAGE_TEXT, fail=()):
    def fake(img, media_type="image/jpeg", effort=None):
        n = Image.open(io.BytesIO(img)).size[0] - 400
        if n in fail:
            raise RuntimeError("Claude API error 529: overloaded")
        return pages[n], "end_turn"
    return fake


@pytest.fixture()
def scan_env(monkeypatch):
    monkeypatch.setattr(X, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(X, "call_claude", fake_claude())
    con = A.connect()
    con.execute("DELETE FROM photo_jobs")
    con.commit()
    con.close()


def new_book(c, title="Scan Book"):
    return c.post("/admin/api/books", json={"title": title, "authors": ["A"]}, **LAN).json["id"]


def add_pages(c, scan, *page_nos):
    data = {"file": [(io.BytesIO(jpeg(400 + n)), f"p{n}.jpg") for n in page_nos]}
    return c.post(f"/admin/api/index/scan/{scan}/pages", data=data, content_type="multipart/form-data", **LAN)


def wait_scan(c, scan, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/admin/api/index/scan/{scan}", **LAN).json
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("scan never finished")


def test_scan_pages_run_as_child_jobs_and_merge(c, scan_env):
    bid = new_book(c)
    r = c.post(f"/admin/api/books/{bid}/index/scan", **LAN)
    assert r.status_code == 202
    scan = r.json["scan"]
    assert c.get(f"/admin/api/index/scan/{scan}", **LAN).json["status"] == "empty"
    r = add_pages(c, scan, 1, 2)
    assert r.status_code == 202 and r.json["pages"] == [1, 2]
    j = wait_scan(c, scan)
    assert j["status"] == "done" and j["book_id"] == bid
    assert [(p["page_no"], p["status"], p["rows"]) for p in j["pages"]] == [(1, "done", 2), (2, "done", 3)]
    assert [(x["heading"], x["name"], x["page_no"]) for x in j["rows"]] == [
        ("Chicken", "braised with figs", 1), ("Chicken", "roast", 1),
        ("Chicken", "with bread salad", 2), ("Chicken", "Pane", 2), ("Pasta", "al pomodoro", 2)]
    assert j["rows"][2]["carried"] is True and j["added"] == 5 and j["present"] == 0
    # a third page later: the overlap with page 2 is dropped, prefixes stripped, roman page flagged
    assert add_pages(c, scan, 3).json["pages"] == [3]
    j = wait_scan(c, scan)
    assert [x["name"] for x in j["rows"] if x["page_no"] == 3] == ["Polenta", "Prefazione"]
    assert [x.get("warn") for x in j["rows"] if x["page_no"] == 3] == [None, "page"]
    # the parent row never counts as a running photo job
    con = A.connect()
    assert con.execute("SELECT status FROM photo_jobs WHERE id=?", (scan,)).fetchone()[0] == "scan"
    assert con.execute("SELECT count(*) FROM photo_jobs WHERE parent_id=?", (scan,)).fetchone()[0] == 3
    con.close()
    # commit the merged rows through the existing endpoint
    r = c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": j["rows"]}, **LAN)
    assert r.json == {"added": 7, "skipped": 0}
    assert wait_scan(c, scan)["present"] == 7


def test_scan_reports_a_failed_page_and_retries_it_alone(c, scan_env, monkeypatch):
    monkeypatch.setattr(X, "call_claude", fake_claude(fail={2}))
    bid = new_book(c)
    scan = c.post(f"/admin/api/books/{bid}/index/scan", **LAN).json["scan"]
    add_pages(c, scan, 1, 2, 3)
    j = wait_scan(c, scan)
    assert j["status"] == "done"
    assert [(p["page_no"], p["status"]) for p in j["pages"]] == [(1, "done"), (2, "error"), (3, "done")]
    assert "529" in j["pages"][1]["error"]
    # pages 1 and 3 are reviewable now; nothing is carried across the gap
    assert [(x["name"], x["page_no"], x.get("carried")) for x in j["rows"]] == [
        ("braised with figs", 1, None), ("roast", 1, None), ("al pomodoro", 3, None), ("Polenta", 3, None), ("Prefazione", 3, None)]
    monkeypatch.setattr(X, "call_claude", fake_claude())
    r = c.put(f"/admin/api/index/scan/{scan}/pages/2", data={"file": (io.BytesIO(jpeg(402)), "p2.jpg")},
              content_type="multipart/form-data", **LAN)
    assert r.status_code == 202
    j = wait_scan(c, scan)
    assert [(p["page_no"], p["status"]) for p in j["pages"]] == [(1, "done"), (2, "done"), (3, "done")]
    assert [x["name"] for x in j["rows"]] == ["braised with figs", "roast", "with bread salad", "Pane", "al pomodoro", "Polenta", "Prefazione"]
    assert c.put(f"/admin/api/index/scan/{scan}/pages/9", data={"file": (io.BytesIO(jpeg(402)), "p.jpg")},
                 content_type="multipart/form-data", **LAN).status_code == 404


def test_scan_rejects_an_unreadable_image_as_a_failed_page(c, scan_env):
    bid = new_book(c)
    scan = c.post(f"/admin/api/books/{bid}/index/scan", **LAN).json["scan"]
    r = c.post(f"/admin/api/index/scan/{scan}/pages", data={"file": [(io.BytesIO(b"not an image"), "x.jpg"),
               (io.BytesIO(jpeg(401)), "p1.jpg")]}, content_type="multipart/form-data", **LAN)
    assert r.status_code == 202 and r.json["pages"] == [1, 2]
    j = wait_scan(c, scan)
    assert j["pages"][0]["status"] == "error" and "Couldn't read that image" in j["pages"][0]["error"]
    assert j["pages"][1]["status"] == "done"


def test_scan_status_is_derived_from_its_pages(c, scan_env, monkeypatch):
    monkeypatch.setattr(X, "run_photo_job", lambda *a, **k: None)  # pages stay queued
    bid = new_book(c)
    scan = c.post(f"/admin/api/books/{bid}/index/scan", **LAN).json["scan"]
    add_pages(c, scan, 1, 2)
    j = c.get(f"/admin/api/index/scan/{scan}", **LAN).json
    assert j["status"] == "running" and [p["status"] for p in j["pages"]] == ["queued", "queued"]
    con = A.connect()
    con.execute("UPDATE photo_jobs SET status='done', rows='[]' WHERE parent_id=? AND page_no=1", (scan,))
    con.commit()
    assert c.get(f"/admin/api/index/scan/{scan}", **LAN).json["status"] == "running"
    # a page that started long ago and never finished is dead; one merely waiting its turn is not
    con.execute("UPDATE photo_jobs SET status='running', started=? WHERE parent_id=? AND page_no=2",
                (time.time() - X.PHOTO_JOB_LIMIT - 5, scan))
    con.commit()
    con.close()
    j = c.get(f"/admin/api/index/scan/{scan}", **LAN).json
    assert j["status"] == "done" and j["pages"][1]["status"] == "error" and "stopped partway" in j["pages"][1]["error"]
    assert c.get("/admin/api/index/scan/nope", **LAN).status_code == 404
    assert c.get(f"/admin/api/index/photo/{scan}", **LAN).status_code == 404  # a scan isn't a single-photo job


def test_scan_retry_refuses_a_page_still_being_read(c, scan_env, monkeypatch):
    monkeypatch.setattr(X, "run_photo_job", lambda *a, **k: None)
    bid = new_book(c)
    scan = c.post(f"/admin/api/books/{bid}/index/scan", **LAN).json["scan"]
    add_pages(c, scan, 1)
    r = c.put(f"/admin/api/index/scan/{scan}/pages/1", data={"file": (io.BytesIO(jpeg(401)), "p.jpg")},
              content_type="multipart/form-data", **LAN)
    assert r.status_code == 409


def test_scan_needs_an_api_key_and_a_book(c, scan_env, monkeypatch):
    assert c.post("/admin/api/books/999999/index/scan", **LAN).status_code == 404
    bid = new_book(c)
    monkeypatch.setattr(X, "ANTHROPIC_API_KEY", "")
    assert c.post(f"/admin/api/books/{bid}/index/scan", **LAN).status_code == 503


def test_single_page_jobs_still_work_alongside_scans(c, scan_env):
    bid = new_book(c)
    r = c.post(f"/admin/api/books/{bid}/index/photo", data={"file": (io.BytesIO(jpeg(401)), "p.jpg")},
               content_type="multipart/form-data", **LAN)
    assert r.status_code == 202
    t0 = time.time()
    while time.time() - t0 < 10:
        j = c.get(f"/admin/api/index/photo/{r.json['job']}", **LAN).json
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done" and [x["name"] for x in j["rows"]] == ["braised with figs", "roast"]
    row = A.connect().execute("SELECT parent_id, page_no FROM photo_jobs WHERE id=?", (r.json["job"],)).fetchone()
    assert tuple(row) == (None, None)


# ------------------------------------------------------------------ daily page limit
def test_daily_page_limit_counts_pages_since_midnight(c, scan_env, monkeypatch):
    monkeypatch.setattr(X, "PHOTO_DAILY_LIMIT", 3)
    monkeypatch.setattr(X, "run_photo_job", lambda *a, **k: None)
    bid = new_book(c)
    con = A.connect()
    # yesterday's pages don't count; a parent row never counts
    con.execute("INSERT INTO photo_jobs (id, book_id, status, created) VALUES ('old', ?, 'done', ?)", (bid, A.local_midnight() - 60))
    con.commit()
    con.close()
    scan = c.post(f"/admin/api/books/{bid}/index/scan", **LAN).json["scan"]
    assert add_pages(c, scan, 1, 2).status_code == 202
    assert X.pages_today(A.connect()) == 2
    r = add_pages(c, scan, 3, 1)  # would make 4: refused whole, not partly
    assert r.status_code == 429 and r.json["daily_limit"] is True and "3 pages" in r.json["error"]
    assert "Retry-After" not in r.headers
    assert add_pages(c, scan, 3).status_code == 202
    r = c.post(f"/admin/api/books/{bid}/index/photo", data={"file": (io.BytesIO(jpeg(401)), "p.jpg")},
               content_type="multipart/form-data", **LAN)
    assert r.status_code == 429 and r.json["daily_limit"] is True  # single photos count too
    # a retry replaces a page and so doesn't count twice
    con = A.connect()
    con.execute("UPDATE photo_jobs SET status='error' WHERE parent_id=? AND page_no=3", (scan,))
    con.commit()
    con.close()
    r = c.put(f"/admin/api/index/scan/{scan}/pages/3", data={"file": (io.BytesIO(jpeg(403)), "p.jpg")},
              content_type="multipart/form-data", **LAN)
    assert r.status_code == 202 and X.pages_today(A.connect()) == 3


def test_photo_job_cleanup_keeps_today_s_rows():
    """pages_today() counts photo_jobs rows, so the cleanup that trims old jobs must keep everything since
    local midnight. 24 h always does; this pins it."""
    assert X.PHOTO_JOB_KEEP >= 86400
