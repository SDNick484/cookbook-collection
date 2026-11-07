"""Ask (AI search): config, thread validation, daily cap and the route's guards. No network."""
import time

import pytest

import app as A
import extras as X
import ask

OK = {"thread": [{"q": "chicken thighs"}]}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(ask, "SEARCH_KEY", "test-key")


@pytest.fixture()
def c():
    return A.app.test_client()


def ask_rows():
    con = A.connect()
    try:
        return con.execute("SELECT * FROM ask_log ORDER BY id").fetchall()
    finally:
        con.close()


def midnight():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def test_no_identity_401(c, monkeypatch):
    monkeypatch.setattr(A, "LAN_EDIT", False)
    assert c.post("/admin/api/ask", json=OK).status_code == 401


def test_cross_site_403(c):
    r = c.post("/admin/api/ask", json=OK, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_no_key_503_and_flag(c, monkeypatch):
    monkeypatch.setattr(ask, "SEARCH_KEY", "")
    monkeypatch.setattr(X, "ANTHROPIC_API_KEY", "")
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 503
    assert r.get_json()["error"] == "AI search isn't set up (no API key)."
    assert c.get("/admin/api/features").get_json()["ask"] is False
    monkeypatch.setattr(ask, "SEARCH_KEY", "k")
    assert c.get("/admin/api/features").get_json()["ask"] is True


def test_falls_back_to_api_key(monkeypatch):
    monkeypatch.setattr(ask, "SEARCH_KEY", "")
    monkeypatch.setattr(X, "ANTHROPIC_API_KEY", "k2")
    assert ask.search_key() == "k2"
    assert ask.enabled()


BAD = [
    [],
    [{"a": "x"}],
    [{"q": "x"}, {"q": "y"}],
    [{"q": "x"}, {"a": "y"}],
    [{"q": "x"}, {"a": "y"}] * 5 + [{"q": "z"}],  # 6 q
    [{"q": "x" * 501}],
    [{"q": "   "}],
    [{"q": "x"}, {"a": "y" * 1001}, {"q": "z"}],
    [{"q": "x", "a": "y"}],
    [{"q": 5}],
    [{"role": "user", "content": "x"}],
    [{"tool_result": "x"}],
    [{"system": "x"}],
]


@pytest.mark.parametrize("thread", BAD)
def test_bad_thread_400(c, thread):
    r = c.post("/admin/api/ask", json={"thread": thread})
    assert r.status_code == 400
    assert "error" in r.get_json()


@pytest.mark.parametrize("kw", [dict(data="hi", content_type="text/plain"), dict(json=[]), dict(json=None),
                                dict(json={}), dict(json={"thread": "x"})])
def test_non_object_body_400(c, kw):
    r = c.post("/admin/api/ask", **kw)
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_validate_thread_builds_messages():
    out = ask.validate_thread([{"q": "  café ideas "}, {"a": "Six."}, {"q": "only new"}])
    assert out == [{"role": "user", "content": "café ideas"}, {"role": "assistant", "content": "Six."},
                   {"role": "user", "content": "only new"}]


def test_validate_thread_five_questions_ok():
    t = [{"q": "x"}, {"a": "y"}] * 4 + [{"q": "z"}]
    assert len(ask.validate_thread(t)) == 9


def test_daily_cap(c, monkeypatch):
    monkeypatch.setattr(ask, "DAILY_LIMIT", 2)
    con = A.connect()
    now = time.time()
    for ts, st in [(now, "ok"), (now, "ok"), (midnight() - 60, "ok"), (now, "capped")]:
        con.execute("INSERT INTO ask_log(ts, editor, rounds, status) VALUES (?, 'x', 1, ?)", (ts, st))
    con.commit()
    before = len(ask_rows())
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 429
    assert r.get_json()["error"] == "Daily AI search limit reached; try again tomorrow."
    rows = ask_rows()
    assert len(rows) == before + 1
    assert rows[-1]["status"] == "capped" and rows[-1]["editor"] == "lan"
    con.execute("DELETE FROM ask_log WHERE id=(SELECT MIN(id) FROM ask_log WHERE status='ok' AND ts>=?)", (midnight(),))
    con.commit()
    with A.app.app_context():
        assert ask.asked_today(A.db()) == 1
    con.close()


def test_migration_is_one_table():
    con = A.connect()
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE name IN ('ask_log','ask_log_ts')")}
    assert names == {"ask_log", "ask_log_ts"}
    ask.migrate(con)
    ask.migrate(con)
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ask_log%'").fetchone()[0] == 2
    con.close()
