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
