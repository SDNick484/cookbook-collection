"""Ask (AI search): config, thread validation, daily cap, the tool loop and result hydration.

No network: `extras.messages_api` is replaced by a scripted fake that returns canned responses and records
the request bodies it was given."""
import copy, json, time

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
    before = len(ask_rows())
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 503
    assert len(ask_rows()) == before  # no question was asked
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
    [{"q": "x"}, {"a": "  "}, {"q": "z"}],
    [{"q": "x"}, {"a": ""}, {"q": "z"}],
    [{"q": "x", "a": "y"}],
    [{"q": 5}],
    [{"role": "user", "content": "x"}],
    [{"tool_result": "x"}],
    [{"system": "x"}],
]


@pytest.mark.parametrize("thread", BAD)
def test_bad_thread_400(c, thread):
    before = len(ask_rows())
    r = c.post("/admin/api/ask", json={"thread": thread})
    assert r.status_code == 400
    assert "error" in r.get_json()
    assert len(ask_rows()) == before


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


def test_daily_limit_env(monkeypatch):
    monkeypatch.setenv("ASK_DAILY_LIMIT", "lots")
    assert ask.env_int("ASK_DAILY_LIMIT", 200) == 200
    monkeypatch.setenv("ASK_DAILY_LIMIT", " 7 ")
    assert ask.env_int("ASK_DAILY_LIMIT", 200) == 7
    monkeypatch.delenv("ASK_DAILY_LIMIT")
    assert ask.env_int("ASK_DAILY_LIMIT", 200) == 200


# ------------------------------------------------------------------ the tool loop (scripted model)
class Script:
    """Stands in for extras.messages_api. Each item is a response dict, an exception to raise, or a
    function of the request body returning a response (to use ids the tools really returned)."""
    def __init__(self, items):
        self.items, self.bodies, self.timeouts = list(items), [], []

    def __call__(self, body, key, timeout):
        assert key == "test-key"
        self.bodies.append(copy.deepcopy(body))
        self.timeouts.append(timeout)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item(body) if callable(item) else item


def tool_use(name, input, id="tu1"):
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def text(t):
    return {"type": "text", "text": t}


def resp(*blocks, usage=(10, 5)):
    return {"content": list(blocks), "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks)
            else "end_turn", "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}


def show(answer="Here you go.", recipe_ids=(), book_ids=(), id="tu9"):
    return resp(tool_use("show_results", {"answer": answer, "recipe_ids": list(recipe_ids),
                                          "book_ids": list(book_ids)}, id))


def results(body):
    """The tool results the model was sent in this request's last message, decoded."""
    return [json.loads(b["content"]) if not b.get("is_error") else b for b in body["messages"][-1]["content"]]


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setattr(ask.time, "sleep", lambda s: None)

    def install(*items):
        fake = Script(items)
        monkeypatch.setattr(X, "messages_api", fake)
        return fake
    return install


def db_one(sql, *args):
    con = A.connect()
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def test_tools_sent_are_read_only_plus_show_results(c, model):
    fake = model(resp(text("Nothing.")))
    assert c.post("/admin/api/ask", json=OK).status_code == 200
    body = fake.bodies[0]
    names = {t["name"] for t in body["tools"]}
    assert not names & set(X.WRITE_TOOLS)
    assert "show_results" in names and "search_recipes" in names
    assert "{tags}" not in json.dumps(body["tools"])
    assert body["model"] == ask.SEARCH_MODEL and body["max_tokens"] == 2048
    assert body["tool_choice"] == {"type": "auto"}
    assert "url instead of a page" in body["system"] and "show_results" in body["system"]
    assert body["messages"] == [{"role": "user", "content": "chicken thighs"}]
    assert fake.timeouts[0] <= ask.CALL_TIMEOUT_S


def test_happy_path_hydrates_from_db(c, model):
    row = db_one("SELECT r.id, r.name, r.page, r.book_id, b.title FROM recipes r JOIN books b ON b.id=r.book_id "
                 "WHERE r.name LIKE '%Raita%'")
    rid, bid = row["id"], row["book_id"]

    def finish(body):
        assert any(r["id"] == rid for r in results(body)[0]["recipes"])
        return show("Try Fake Name p. 999", [rid, 999999], [bid])
    fake = model(resp(tool_use("search_recipes", {"query": "raita"})), finish)
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200
    out = r.get_json()
    assert out["answer"] == "Try Fake Name p. 999"
    assert out["turns_left"] == 4
    rec, book = out["items"]
    assert rec["kind"] == "recipe" and rec["id"] == rid
    assert (rec["name"], rec["page"], rec["book_title"]) == (row["name"], row["page"], row["title"])
    assert book["kind"] == "book" and book["id"] == bid and book["title"] == row["title"]
    assert all(i.get("id") != 999999 for i in out["items"])
    # the second request carries the first response and its tool result
    m = fake.bodies[1]["messages"]
    assert m[1] == {"role": "assistant", "content": [tool_use("search_recipes", {"query": "raita"})]}
    assert m[2]["role"] == "user" and m[2]["content"][0]["type"] == "tool_result"
    assert m[2]["content"][0]["tool_use_id"] == "tu1" and not m[2]["content"][0].get("is_error")


def test_turns_left_counts_thread_questions(c, model):
    model(resp(text("Ok.")))
    t = [{"q": "x"}, {"a": "y"}, {"q": "z"}]
    assert c.post("/admin/api/ask", json={"thread": t}).get_json()["turns_left"] == 3


def test_ids_as_strings_dupes_and_deleted(c, model):
    def finish(body):
        found = [r["id"] for r in results(body)[0]["recipes"]]
        assert len(found) >= 2
        keep, gone = found[0], found[1]
        A.db().execute("DELETE FROM recipes WHERE id=?", (gone,))  # deleted after the search saw it
        return show(recipe_ids=[str(keep), keep, float(keep), gone, "abc", None, True])
    model(resp(tool_use("search_recipes", {"query": "chicken"})), finish)
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "recipe"


def test_hydrate_coerces_and_orders():
    with A.app.test_request_context():
        rows = A.db().execute("SELECT id FROM recipes ORDER BY id LIMIT 3").fetchall()
        a, b, c3 = (r[0] for r in rows)
        items, dropped = ask.hydrate([c3, " %d " % a, b, "x", "\u00b2", 12345678], "nope", {a, b, c3}, set())
        assert [i["id"] for i in items] == [c3, a, b]
        assert dropped == 3
        items, dropped = ask.hydrate([a] * 30 + list(range(1, 40)), [], set(range(1, 40)), set())
        assert len(items) == 25  # maxItems


def test_show_results_alongside_search_ignores_the_search(c, model):
    bid = db_one("SELECT id FROM books WHERE title='Franklin BBQ'")[0]
    fake = model(resp(tool_use("search_books", {"query": "Franklin"}, "a"),
                      tool_use("show_results", {"answer": "Found it.", "book_ids": [bid]}, "b")))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200
    assert r.get_json() == {"answer": "Found it.", "items": [], "turns_left": 4}
    assert len(fake.bodies) == 1


@pytest.fixture()
def clean_online():
    yield
    con = A.connect()
    con.execute("DELETE FROM books WHERE kind='online'")
    con.commit()
    con.close()


def test_web_recipes_included_for_editors(c, model, clean_online):
    nyt = "https://cooking.nytimes.com/recipes/1017256-french-onion-soup"
    assert c.post("/admin/api/online/commit", json={"items": [{"url": nyt}]}).status_code == 200
    rid = db_one("SELECT id FROM recipes WHERE url=?", nyt)[0]

    def finish(body):
        assert any(r["id"] == rid for r in results(body)[0]["recipes"])
        return show(recipe_ids=[rid])
    model(resp(tool_use("search_recipes", {"query": "french onion soup"})), finish)
    items = c.post("/admin/api/ask", json=OK).get_json()["items"]
    assert [(i["id"], i["url"], i["book_title"]) for i in items] == [(rid, nyt, "NYT Cooking")]


def test_ids_in_book_results_count_as_seen(c, model):
    row = db_one("SELECT r.id, r.book_id FROM recipes r WHERE r.name LIKE '%Raita%'")
    model(resp(tool_use("get_book", {"book_id": row["book_id"]})),
          show(recipe_ids=[row["id"]], book_ids=[row["book_id"]]))
    items = c.post("/admin/api/ask", json=OK).get_json()["items"]
    assert [(i["kind"], i["id"]) for i in items] == [("recipe", row["id"]), ("book", row["book_id"])]


def test_collect_ids():
    r, b = set(), set()
    ask.collect_ids({"books": [{"id": 1, "title": "T"}],
                     "recipes": [{"id": 2, "name": "N", "page": None}, {"id": 3, "name": "N", "book_id": 4}],
                     "top_recipes": [{"id": 5, "name": "N", "heading": "H"}],
                     "cuisines": [{"name": "Thai", "books": 3}], "ingredients": [{"id": 9, "name": "Eggs"}],
                     "ref": {"book_id": 7}, "odd": {"book_id": "8"}}, r, b)
    assert (r, b) == ({2, 3, 5}, {1, 4, 7})


def test_fifth_round_forces_show_results(c, model):
    lc = resp(tool_use("list_categories", {}))
    fake = model(lc, lc, lc, lc, show("Done."))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200 and r.get_json()["answer"] == "Done."
    assert [b["tool_choice"] for b in fake.bodies] == [{"type": "auto"}] * 4 + [
        {"type": "tool", "name": "show_results"}]
    assert ask_rows()[-1]["rounds"] == 5


def test_fifth_round_without_show_results(c, model):
    lc = resp(tool_use("list_categories", {}))
    model(lc, lc, lc, lc, resp(text("Still looking."), tool_use("list_categories", {})))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200
    assert r.get_json()["items"] == [] and r.get_json()["answer"] == "Still looking."
    assert ask_rows()[-1]["status"] == "ok"


def test_text_only_answer(c, model):
    model(resp(text("No recipes "), text("match that.")))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200
    assert r.get_json() == {"answer": "No recipes match that.", "items": [], "turns_left": 4}


def test_answer_truncated_to_600(c, model):
    model(show("x" * 700))
    assert c.post("/admin/api/ask", json=OK).get_json()["answer"] == "x" * 600
    model(resp(text("y" * 700)))
    assert c.post("/admin/api/ask", json=OK).get_json()["answer"] == "y" * 600


def test_tool_errors_reach_model(c, model, monkeypatch):
    def boom(a, private):
        raise RuntimeError("SQL internals")
    monkeypatch.setattr(X, "TOOLS", X.TOOLS + [("boom", False, boom, "Breaks.", {"type": "object", "properties": {}})])
    fake = model(resp(tool_use("get_book", {}, "t1"), tool_use("boom", {}, "t2"), tool_use("nope", {}, "t3")),
                 show())
    assert c.post("/admin/api/ask", json=OK).status_code == 200
    blocks = fake.bodies[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t1", "t2", "t3"]
    assert all(b["type"] == "tool_result" and b["is_error"] is True for b in blocks)
    assert blocks[0]["content"] == "book_id or title is required"
    assert "internal error (ref " in blocks[1]["content"] and "SQL internals" not in blocks[1]["content"]
    assert blocks[2]["content"] == "Unknown tool: nope"


def test_write_tools_refused_if_called(c, model):
    rid = db_one("SELECT id FROM recipes WHERE want_to_try=0 LIMIT 1")[0]
    fake = model(resp(tool_use("set_want_to_try", {"recipe_id": rid})), show())
    assert c.post("/admin/api/ask", json=OK).status_code == 200
    assert fake.bodies[1]["messages"][-1]["content"][0]["content"] == "Unknown tool: set_want_to_try"
    assert db_one("SELECT want_to_try FROM recipes WHERE id=?", rid)[0] == 0


GENERIC = {"error": "AI search is unavailable right now, try again"}


def test_api_500_is_502_and_logged(c, model):
    model(X.ClaudeAPIError(500, "boom"))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502
    assert r.get_json() == GENERIC and b"boom" not in r.data
    assert ask_rows()[-1]["status"] == "api_error"


def test_529_retried_once_then_ok(c, model, monkeypatch):
    slept = []
    model(X.ClaudeAPIError(529, ""), show("ok"))
    monkeypatch.setattr(ask.time, "sleep", slept.append)
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200 and r.get_json()["answer"] == "ok" and slept == [2]
    model(X.ClaudeAPIError(529, ""), X.ClaudeAPIError(429, ""))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502 and r.get_json() == GENERIC
    assert ask_rows()[-1]["status"] == "api_error"
    fake = model(X.ClaudeAPIError(400, "bad"), show())
    assert c.post("/admin/api/ask", json=OK).status_code == 502
    assert len(fake.bodies) == 1  # only 429 and 529 are retried


def test_timeout(c, model, monkeypatch):
    model(TimeoutError())
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502 and r.get_json() == GENERIC
    assert ask_rows()[-1]["status"] == "timeout"
    monkeypatch.setattr(ask, "BUDGET_S", -1)
    fake = model(show())
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502 and r.get_json() == GENERIC
    assert fake.bodies == [] and ask_rows()[-1]["status"] == "timeout"


def test_connection_error_is_502(c, model):
    import urllib.error
    model(urllib.error.URLError("refused"))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502 and ask_rows()[-1]["status"] == "api_error"


def test_non_object_response_is_502(c, model):
    for bad in ([], None, "x"):
        model(bad)
        r = c.post("/admin/api/ask", json=OK)
        assert r.status_code == 502 and r.get_json() == GENERIC
        assert ask_rows()[-1]["status"] == "api_error"


def test_truncated_response_is_502(c, model):
    import http.client
    model(http.client.IncompleteRead(b""))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502 and r.get_json() == GENERIC
    assert ask_rows()[-1]["status"] == "api_error"


def test_usage_summed(c, model):
    lc = tool_use("list_categories", {})
    fake = model(resp(lc, usage=(100, 10)), resp(lc, usage=(200, 20)),
                 resp(tool_use("show_results", {"answer": "a"}), usage=(300, 30)))
    assert c.post("/admin/api/ask", json=OK).status_code == 200
    row = ask_rows()[-1]
    assert (row["rounds"], row["input_tokens"], row["output_tokens"]) == (3, 600, 60)
    assert row["status"] == "ok" and row["editor"] == "lan"
    assert len(fake.bodies) == 3


def test_question_text_not_stored(c, model):
    q = "zanzibar quokka marmalade"
    model(resp(text("Nothing.")))
    assert c.post("/admin/api/ask", json={"thread": [{"q": q}]}).status_code == 200
    for row in ask_rows():
        assert all(q not in str(v) for v in tuple(row))


def test_huge_tool_result_truncated(c, model, monkeypatch):
    def huge(a, private):
        return {"blob": "x" * 50000}
    monkeypatch.setattr(X, "TOOLS", X.TOOLS + [("huge", False, huge, "Big.", {"type": "object", "properties": {}})])
    fake = model(resp(tool_use("huge", {}), tool_use("list_categories", {}, "tu2")), show())
    assert c.post("/admin/api/ask", json=OK).status_code == 200
    big, small = fake.bodies[1]["messages"][-1]["content"]
    assert len(big["content"]) == ask.MAX_TOOL_RESULT_CHARS + len(ask.TRUNCATED_NOTE)
    assert big["content"].endswith(ask.TRUNCATED_NOTE) and "narrow the search" in big["content"]
    assert not small["content"].endswith(ask.TRUNCATED_NOTE)
    json.loads(small["content"])  # small results are sent whole


def test_input_token_ceiling_forces_finish(c, model):
    lc = tool_use("list_categories", {})
    fake = model(resp(lc, usage=(ask.INPUT_TOKEN_CEILING + 1, 10)), show("Done."))
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200 and r.get_json()["answer"] == "Done."
    assert [b["tool_choice"] for b in fake.bodies] == [{"type": "auto"}, {"type": "tool", "name": "show_results"}]
    # under the ceiling the next round stays free to search
    fake = model(resp(lc, usage=(ask.INPUT_TOKEN_CEILING, 10)), show())
    assert c.post("/admin/api/ask", json=OK).status_code == 200
    assert fake.bodies[1]["tool_choice"] == {"type": "auto"}


def test_empty_answer_falls_back(c, model):
    bid = db_one("SELECT id FROM books WHERE title='Franklin BBQ'")[0]
    model(resp(tool_use("search_books", {"query": "Franklin"})), show("  ", book_ids=[bid]))
    out = c.post("/admin/api/ask", json=OK).get_json()
    assert out["answer"] == "Here's what I found." and [i["id"] for i in out["items"]] == [bid]
    model(show(""))
    assert c.post("/admin/api/ask", json=OK).get_json()["answer"] == "I couldn't find anything for that."
    model({"content": [], "stop_reason": "end_turn"})
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 200
    assert r.get_json() == {"answer": "I couldn't find anything for that.", "items": [], "turns_left": 4}


def test_unexpected_error_is_json_502(c, model):
    model({"content": 5, "usage": {"input_tokens": 1, "output_tokens": 1}})
    r = c.post("/admin/api/ask", json=OK)
    assert r.status_code == 502 and r.get_json() == GENERIC
    assert ask_rows()[-1]["status"] == "error"


def test_book_ids_seen_without_title(c, model):
    bid = db_one("SELECT id FROM books WHERE title='Franklin BBQ'")[0]

    def finish(body):
        books = results(body)[0]["books"]
        assert [x["id"] for x in books] == [bid] and "title" not in books[0]
        return show(book_ids=[bid])
    model(resp(tool_use("search_books", {"query": "Franklin", "fields": ["year"]})), finish)
    items = c.post("/admin/api/ask", json=OK).get_json()["items"]
    assert [(i["kind"], i["id"]) for i in items] == [("book", bid)]


def test_collect_ids_books_without_title():
    r, b = set(), set()
    ask.collect_ids({"count": 2, "books": [{"id": 11}, {"id": 12, "year": 2001}, "x", {"id": "13"}]}, r, b)
    assert (r, b) == (set(), {11, 12})


def test_rpc_empty_tool_error_unchanged(monkeypatch):
    monkeypatch.setattr(X, "run_tool", lambda name, args, private: (None, ""))
    with A.app.test_request_context():
        out = X.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "search_books", "arguments": {}}}, True)
    res = out["result"]
    assert res["isError"] is True and res["content"] == [{"type": "text", "text": ""}]
