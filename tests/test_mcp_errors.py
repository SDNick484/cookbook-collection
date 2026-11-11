"""MCP robustness: tools/list survives odd tag names, tool errors read well and never leak internals,
and arguments are checked against each tool's inputSchema before the tool runs."""
import logging

import pytest

import app as A
import mcp as MC

TOKEN = "errors-test-token"
PRIVATE = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(MC, "MCP_TOKEN", TOKEN)
    monkeypatch.setattr(MC, "MCP_PUBLIC", True)
    monkeypatch.setattr(MC, "MCP_WRITE", True)  # PRIVATE is a header token, so it gets the write tools
    return A.app.test_client()


def rpc(c, method, params=None, headers=None):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                  headers=headers or {})


def call(c, name, args, headers=None):
    return rpc(c, "tools/call", {"name": name, "arguments": args}, headers).json["result"]


def text(res):
    return res["content"][0]["text"]


# ------------------------------------------------------------ tools/list with odd tag names
@pytest.mark.parametrize("tag", ["Back\\slash", "Tab\there", 'Say "cheese"'])
def test_tools_list_survives_special_characters_in_tag_names(c, tag):
    con = A.connect()
    con.execute("INSERT INTO ingredients(name, hint) VALUES (?, 0)", (tag,))
    iid = con.execute("SELECT id FROM ingredients WHERE name=?", (tag,)).fetchone()[0]
    rid = con.execute("SELECT id FROM recipes LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO recipe_ingredients(recipe_id, ingredient_id) VALUES (?, ?)", (rid, iid))
    con.commit()
    try:
        r = rpc(c, "tools/list")
        assert r.status_code == 200
        search = next(t for t in r.json["result"]["tools"] if t["name"] == "search_recipes")
        assert tag in search["description"]
        assert tag in search["inputSchema"]["properties"]["ingredient"]["description"]
    finally:
        con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        con.commit()


# ------------------------------------------------------------ errors meant for the model
def test_missing_required_argument_is_named(c):
    res = call(c, "set_want_to_try", {}, PRIVATE)
    assert res["isError"] and "recipe_id" in text(res) and "required" in text(res)


def test_non_integer_argument_is_explained(c):
    res = call(c, "search_recipes", {"query": "chicken", "limit": "ten"})
    assert res["isError"]
    assert "limit" in text(res) and "invalid literal" not in text(res)


def test_numeric_string_integer_is_still_accepted(c):
    res = call(c, "search_recipes", {"query": "chicken", "limit": "2"})
    assert not res.get("isError") and res["structuredContent"]["count"] <= 2


def test_enum_argument_lists_the_choices(c):
    res = call(c, "suggest_recipes", {"mode": "bogus"}, PRIVATE)
    assert res["isError"] and "favorites" in text(res)


def test_wrong_type_argument_is_explained(c):
    res = call(c, "search_books", {"fields": [{"x": 1}]})
    assert res["isError"] and "fields" in text(res)


def test_arguments_must_be_an_object(c):
    res = call(c, "search_books", ["not", "an", "object"])
    assert res["isError"] and text(res) == "arguments must be an object"


def test_tool_messages_written_for_the_model_pass_through(c):
    assert text(call(c, "get_book", {})) == "book_id or title is required"
    assert text(call(c, "get_book", {"book_id": 999999})) == "No such book"


# ------------------------------------------------------------ unexpected errors: generic + logged
def test_internal_error_is_logged_not_shown(c, monkeypatch, caplog):
    def boom(a, private):
        raise RuntimeError("no such column: secret_internal_col")

    monkeypatch.setattr(MC, "TOOLS", [t if t[0] != "search_books" else (t[0], t[1], boom, t[3], t[4])
                                     for t in MC.TOOLS])
    with caplog.at_level(logging.ERROR, logger=A.app.logger.name):
        res = call(c, "search_books", {"query": "zuni"})
    assert res["isError"]
    assert "secret_internal_col" not in text(res) and "RuntimeError" not in text(res)
    ref = text(res).split("ref ")[1].split(")")[0]
    logged = [r for r in caplog.records if ref in r.getMessage()]
    assert logged and logged[0].exc_info and "secret_internal_col" in str(logged[0].exc_info[1])


def test_writes_are_logged(c, caplog):
    rid = A.connect().execute("SELECT id FROM recipes LIMIT 1").fetchone()[0]
    with caplog.at_level(logging.INFO, logger=A.app.logger.name):
        res = call(c, "set_want_to_try", {"recipe_id": rid, "want": False}, PRIVATE)
    assert not res.get("isError")
    assert any("set_want_to_try" in r.getMessage() and str(rid) in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------ bounded schemas
def test_paging_arguments_declare_their_bounds(c):
    tools = rpc(c, "tools/list", headers=PRIVATE).json["result"]["tools"]
    seen = 0
    for t in tools:
        for name, p in t["inputSchema"].get("properties", {}).items():
            if name in ("limit", "count", "offset"):
                seen += 1
                assert p["type"] == "integer" and p["minimum"] >= 0, (t["name"], name)
                if name != "offset":
                    assert p["maximum"] > p["default"] >= 1, (t["name"], name)
    assert seen == 6  # search_recipes limit+offset, search_books limit, get_book limit+offset, suggest_recipes count


def test_tool_annotations_describe_write_and_world_behavior(c):
    tools = {t["name"]: t["annotations"] for t in rpc(c, "tools/list", headers=PRIVATE).json["result"]["tools"]}
    for name in ("search_recipes", "search_books", "get_book", "list_categories", "suggest_recipes", "cooking_stats"):
        assert tools[name] == {"readOnlyHint": True, "openWorldHint": False}
    assert tools["set_want_to_try"] == {
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    assert tools["update_book"] == {
        "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


def test_online_only_ingredient_tag_hidden_from_public_schema_and_notes(c):
    con = A.connect()
    cur = con.execute("INSERT INTO books(title, kind) VALUES ('Secret Web Site', 'online')")
    bid = cur.lastrowid
    rid = con.execute("INSERT INTO recipes(book_id, name) VALUES (?, 'Web Dish')", (bid,)).lastrowid
    iid = con.execute("INSERT INTO ingredients(name, hint) VALUES ('UnicornMeat', 0)").lastrowid
    con.execute("INSERT INTO recipe_ingredients(recipe_id, ingredient_id) VALUES (?, ?)", (rid, iid))
    con.commit()
    try:
        pub_desc = next(t["description"] for t in rpc(c, "tools/list").json["result"]["tools"]
                        if t["name"] == "search_recipes")
        priv_desc = next(t["description"] for t in rpc(c, "tools/list", headers=PRIVATE).json["result"]["tools"]
                         if t["name"] == "search_recipes")
        assert "UnicornMeat" not in pub_desc
        assert "UnicornMeat" in priv_desc
        note = call(c, "search_recipes", {"ingredient": "no-such-ing"})["structuredContent"]["ingredient_note"]
        assert "UnicornMeat" not in note
    finally:
        con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        con.execute("DELETE FROM books WHERE id=?", (bid,))
        con.commit()


def test_get_book_ambiguous_title_lists_matches_instead_of_guessing(c):
    res = call(c, "get_book", {"title": "cookbook"})
    assert res["isError"]
    msg = text(res)
    assert "Multiple books match 'cookbook'" in msg and "My First Cookbook" in msg and "book_id=" in msg



def test_run_tool_success_and_errors(monkeypatch):
    with A.app.app_context():  # tools reach the DB through flask.g
        res, err = MC.run_tool("list_categories", {}, True)
        assert err is None and "cuisines" in res
        res, err = MC.run_tool("get_book", {}, True)
        assert res is None and err == "book_id or title is required"

    def boom(a, p): raise RuntimeError("SELECT secret FROM internals")
    monkeypatch.setattr(MC, "TOOLS", [("boom", False, boom, "d", MC._S())])
    res, err = MC.run_tool("boom", {}, True)
    assert res is None and "internal error (ref " in err and "secret" not in err


def test_run_tool_unknown_and_private_only():
    with pytest.raises(MC.UnknownTool):
        MC.run_tool("nope", {}, True)
    with pytest.raises(MC.UnknownTool):
        MC.run_tool("cooking_stats", {}, False)
