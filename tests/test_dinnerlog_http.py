"""Cooking history from Dinner Log over HTTP: the pull, the rebuild into `cooked`, and when they run."""
import io
import json
import socket
import urllib.error

import pytest

import app as A
import extras as X

TOKEN = "dl-token-for-tests"
URL = "http://127.0.0.1:8000"


class Resp:
    """What dl_open returns for a 2xx: status, headers and a readable body."""

    def __init__(self, body=b"", status=200, headers=None):
        self.status, self.headers, self._body = status, headers or {}, io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def payload(meals, version=1):
    return json.dumps({"version": version, "meals": meals}).encode()


def http_error(code, body=b""):
    return urllib.error.HTTPError(URL + "/api/history", code, "x", {}, io.BytesIO(body))


class FakeDL:
    """Stands in for Dinner Log: records each request, answers from a queue."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def meal(id_, name="Roast chicken", title="", detail="", source_type="cookbook", **kw):
    return {"id": id_, "day": "2026-10-01", "name": name, "source_type": source_type, "source_title": title,
            "source_detail": detail, "rating": 8, "cookbook_book_id": None, "cookbook_recipe_id": None, **kw}


def meta(con, k):
    return X.meta_get(con, k)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(X, "DINNERLOG_API_URL", URL)
    monkeypatch.setattr(X, "DINNERLOG_TOKEN", TOKEN)
    con = A.connect()
    con.execute("DELETE FROM dl_meals")
    con.execute("DELETE FROM cooked")
    con.execute("DELETE FROM dl_link")
    con.execute("DELETE FROM meta WHERE key LIKE 'dl\\_%' ESCAPE '\\' AND key <> 'dl_lease'")
    con.execute("UPDATE meta SET value='0' WHERE key='dl_lease'")
    con.commit()
    yield con
    con.close()


def use(monkeypatch, *answers):
    fake = FakeDL(*answers)
    monkeypatch.setattr(X, "dl_open", fake)
    return fake


# ------------------------------------------------------------------ pull_history

def test_pull_200_stores_snapshot_and_etag(configured, monkeypatch):
    fake = use(monkeypatch, Resp(payload([meal(1), meal(2, "Soup")]), headers={"ETag": '"abc"'}))
    st = X.pull_history(configured, now=1000.0)
    assert st == {"ok": True, "changed": True, "error": None}
    req, timeout = fake.requests[0]
    assert req.full_url == URL + "/api/history" and timeout == X.PULL_TIMEOUT
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("If-none-match") is None                      # nothing pulled yet
    rows = configured.execute("SELECT id, name, rating FROM dl_meals ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "Roast chicken", 8), (2, "Soup", 8)]
    assert meta(configured, "dl_etag") == '"abc"'
    assert float(meta(configured, "dl_pulled_at")) == 1000.0


def test_pull_sends_if_none_match_and_handles_304(configured, monkeypatch):
    X.store_snapshot(configured, [meal(1)], '"abc"')
    fake = use(monkeypatch, http_error(304))
    st = X.pull_history(configured, now=2000.0)
    assert st == {"ok": True, "changed": False, "error": None}
    assert fake.requests[0][0].get_header("If-none-match") == '"abc"'
    assert configured.execute("SELECT count(*) FROM dl_meals").fetchone()[0] == 1
    assert float(meta(configured, "dl_pulled_at")) == 2000.0


@pytest.mark.parametrize("answer", [socket.timeout("timed out"), urllib.error.URLError("refused"),
                                    http_error(500), http_error(302), http_error(401)])
def test_pull_errors_keep_snapshot(configured, monkeypatch, answer):
    X.store_snapshot(configured, [meal(1)], '"abc"')
    use(monkeypatch, answer)
    st = X.pull_history(configured, now=3000.0)
    assert st["ok"] is False and st["error"]
    assert meta(configured, "dl_error") and float(meta(configured, "dl_failed_at")) == 3000.0
    assert configured.execute("SELECT count(*) FROM dl_meals").fetchone()[0] == 1
    assert meta(configured, "dl_etag") == '"abc"'


@pytest.mark.parametrize("body", [
    payload([meal(1)], version=2),
    json.dumps({"version": 1, "meals": {"id": 1}}).encode(),
    payload([{**meal(1), "id": "x"}]),
    payload([{**meal(1), "id": True}]),
    b"<html>login</html>",
    b"x" * (X.MAX_HISTORY_BYTES + 1),
])
def test_pull_rejects_bad_payload(configured, monkeypatch, body):
    X.store_snapshot(configured, [meal(1)], '"abc"')
    use(monkeypatch, Resp(body, headers={"ETag": '"new"'}))
    st = X.pull_history(configured, now=4000.0)
    assert st["ok"] is False and meta(configured, "dl_error")
    assert configured.execute("SELECT count(*) FROM dl_meals").fetchone()[0] == 1
    assert meta(configured, "dl_etag") == '"abc"'


def test_pull_200_without_etag(configured, monkeypatch):            # Review Focus 1
    fake = use(monkeypatch, Resp(payload([meal(1)])), http_error(304))
    assert X.pull_history(configured, now=1.0)["ok"]
    assert meta(configured, "dl_etag") == ""
    X.pull_history(configured, now=2.0)
    assert fake.requests[1][0].get_header("If-none-match") is None


def test_pull_tolerates_missing_fields(configured, monkeypatch):    # Review Focus 3
    use(monkeypatch, Resp(payload([{"id": 5, "day": "2026-10-01", "name": "X"}]), headers={"ETag": '"e"'}))
    assert X.pull_history(configured, now=1.0)["ok"]
    r = configured.execute("SELECT name, source_type, rating, cookbook_book_id FROM dl_meals WHERE id=5").fetchone()
    assert tuple(r) == ("X", None, None, None)


def test_pull_url_trailing_slash(configured, monkeypatch):          # Review Focus 4
    monkeypatch.setattr(X, "DINNERLOG_API_URL", X.clean_url(URL + "/"))
    fake = use(monkeypatch, Resp(payload([]), headers={"ETag": '"e"'}))
    X.pull_history(configured, now=1.0)
    assert fake.requests[0][0].full_url == URL + "/api/history"


def test_pull_unconfigured_is_noop(configured, monkeypatch):
    monkeypatch.setattr(X, "DINNERLOG_API_URL", "")
    fake = use(monkeypatch)
    st = X.pull_history(configured, now=1.0)
    assert st["ok"] is False and fake.requests == []
    assert meta(configured, "dl_error") is None and meta(configured, "dl_pulled_at") is None


def test_error_never_contains_token_or_body(configured, monkeypatch):
    use(monkeypatch, http_error(500, f"secret page with {TOKEN}".encode()))
    X.pull_history(configured, now=1.0)
    err = meta(configured, "dl_error")
    assert TOKEN not in err and "secret page" not in err


# ------------------------------------------------------------------ rebuild_cooked

ZUNI, DINING_IN = "The Zuni Cafe Cookbook", "Dining In"


def book_id(con, title):
    return con.execute("SELECT id FROM books WHERE title=?", (title,)).fetchone()[0]


def lone_recipes(con, bid, n=2):
    """Recipes alone on their page in a book: (id, name, page_sort)."""
    return [tuple(r) for r in con.execute(
        "SELECT min(id), min(name), page_sort FROM recipes WHERE book_id=? AND page_sort IS NOT NULL "
        "GROUP BY page_sort HAVING count(*)=1 ORDER BY page_sort LIMIT ?", (bid, n))]


def cooked(con):
    return {r["meal_id"]: (r["book_id"], r["recipe_id"], r["how"]) for r in con.execute("SELECT * FROM cooked")}


def test_precedence(configured):
    con = configured
    zuni, dining = book_id(con, ZUNI), book_id(con, DINING_IN)
    (r1, n1, p1), (r2, n2, p2) = lone_recipes(con, zuni)
    meals = [
        meal(1, "Dinner one", ZUNI, str(p1), cookbook_book_id=zuni, cookbook_recipe_id=r1),   # dl_link overrides
        meal(2, "Something", "garbled title", "", cookbook_book_id=dining, cookbook_recipe_id=r2),  # linked
        meal(3, n1, "unmatched title", str(p1), cookbook_book_id=zuni),                       # stamped book
        meal(4, n2, ZUNI, str(p2)),                                                           # fuzzy, as before
    ]
    X.store_snapshot(con, meals, '"v1"')
    con.execute("INSERT INTO dl_link VALUES (1, ?, ?)", (r2, X.meal_key("2026-10-01", "Dinner one")))
    con.commit()
    assert X.rebuild_cooked(con)["rebuilt"] is True
    assert cooked(con) == {1: (zuni, r2, "manual"), 2: (zuni, r2, "linked"),
                           3: (zuni, r1, "auto"), 4: (zuni, r2, "auto")}


def test_deleted_stamped_recipe_falls_through(configured):
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, n1, p1), = lone_recipes(con, zuni, 1)
    X.store_snapshot(con, [meal(1, n1, ZUNI, str(p1), cookbook_book_id=999999, cookbook_recipe_id=999999)], '"v1"')
    X.rebuild_cooked(con)
    assert cooked(con) == {1: (zuni, r1, "auto")}


def test_never_pulled_keeps_cooked(configured):
    con = configured
    con.execute("INSERT INTO cooked(meal_id, day, meal_name) VALUES (42, '2026-01-01', 'From before the upgrade')")
    con.commit()
    assert X.rebuild_cooked(con)["rebuilt"] is False
    assert X.rebuild_cooked(con, force=True)["rebuilt"] is False         # nothing pulled yet: keep what's there
    assert list(cooked(con)) == [42]


def test_rebuild_only_when_something_changed(configured, monkeypatch):
    con = configured
    X.store_snapshot(con, [meal(1, "x", ZUNI)], '"v1"')
    assert X.rebuild_cooked(con)["rebuilt"] is True
    assert X.rebuild_cooked(con)["rebuilt"] is False                     # same snapshot, same catalog
    use(monkeypatch, http_error(304))
    X.pull_history(con)                                                  # unchanged history
    assert X.rebuild_cooked(con)["rebuilt"] is False
    X.store_snapshot(con, [meal(1, "x", ZUNI), meal(2, "y", ZUNI)], '"v2"')
    assert X.rebuild_cooked(con)["rebuilt"] is True


def test_changed_history_without_etag_still_rebuilds(configured):    # Review Focus 1
    con = configured
    X.store_snapshot(con, [meal(1, "x", ZUNI)], "")
    assert X.rebuild_cooked(con)["rebuilt"] is True
    X.store_snapshot(con, [meal(1, "x", ZUNI), meal(2, "y", ZUNI)], "")
    assert X.rebuild_cooked(con)["rebuilt"] is True
    assert set(cooked(con)) == {1, 2}


def test_catalog_change_triggers_rebuild(configured):
    con = configured
    bid = con.execute("INSERT INTO books(title) VALUES ('Late Indexed Book')").lastrowid
    con.commit()
    X.store_snapshot(con, [meal(1, "Special Braised Short Ribs", "Late Indexed Book", "42")], '"v1"')
    X.rebuild_cooked(con)
    assert cooked(con) == {1: (bid, None, "book")}
    rid = con.execute("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?, 'Special Braised Short Ribs', "
                      "'42', 42)", (bid,)).lastrowid
    con.commit()
    assert X.rebuild_cooked(con)["rebuilt"] is True                      # no force, no new pull
    assert cooked(con) == {1: (bid, rid, "auto")}
    con.execute("DELETE FROM books WHERE id=?", (bid,))
    con.commit()


def test_deleted_meal_disappears(configured):                           # Review Focus 5
    con = configured
    zuni = book_id(con, ZUNI)
    (r1, n1, p1), (r2, n2, p2) = lone_recipes(con, zuni)
    X.store_snapshot(con, [meal(1, n1, ZUNI, str(p1)), meal(3, n2, ZUNI, str(p2))], '"v1"')
    con.execute("INSERT INTO dl_link VALUES (3, ?, ?)", (r1, X.meal_key("2026-10-01", n2)))
    con.commit()
    X.rebuild_cooked(con)
    assert set(cooked(con)) == {1, 3}
    X.store_snapshot(con, [meal(1, n1, ZUNI, str(p1))], '"v2"')
    X.rebuild_cooked(con)
    assert cooked(con) == {1: (zuni, r1, "auto")}                        # meal 3 gone; its link unused
