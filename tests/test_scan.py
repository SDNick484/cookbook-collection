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
