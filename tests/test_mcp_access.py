"""Who gets which MCP tools: public, a Bearer header token, or a /mcp/<token> path token, under MCP_WRITE and
MCP_URL_TOKEN. Also several tokens at once (rotation) and keeping a path token out of the logged environ."""
import hmac

import pytest
from werkzeug.test import EnvironBuilder

import app as A
import extras as X

TOKEN = "access-test-token_Q1"
OLD, NEW = "old-rotating-token", "new-rotating-token"

PUBLIC_TOOLS = {"search_recipes", "search_books", "get_book", "get_recipe", "list_categories"}
READ_TOOLS = PUBLIC_TOOLS | {"suggest_recipes", "cooking_stats"}  # private, read-only
WRITE = set(X.WRITE_TOOLS)


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", TOKEN)
    monkeypatch.setattr(X, "MCP_PUBLIC", True)
    monkeypatch.setattr(X, "MCP_WRITE", False)
    monkeypatch.setattr(X, "MCP_URL_TOKEN", True)
    return A.app.test_client()


def rpc(c, method, params=None, url="/mcp", headers=None):
    return c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                  headers=headers or {})


def request_for(caller, token=TOKEN):
    """(url, headers) for a caller kind."""
    return {"public": ("/mcp", {}),
            "header": ("/mcp", {"Authorization": f"Bearer {token}"}),
            "path": (f"/mcp/{token}", {})}[caller]


def want_flag(rid):
    con = A.connect()
    try:
        return con.execute("SELECT want_to_try FROM recipes WHERE id=?", (rid,)).fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def rid():
    """A recipe that isn't starred; put back afterward whatever a test did to it."""
    con = A.connect()
    r = con.execute("SELECT id FROM recipes WHERE want_to_try=0 LIMIT 1").fetchone()[0]
    con.close()
    yield r
    con = A.connect()
    con.execute("UPDATE recipes SET want_to_try=0 WHERE id=?", (r,))
    con.commit()
    con.close()


# caller, MCP_WRITE, MCP_URL_TOKEN -> HTTP status, the tools listed (None when refused)
MATRIX = [
    ("public", False, True, 200, PUBLIC_TOOLS),
    ("public", True, True, 200, PUBLIC_TOOLS),
    ("public", False, False, 200, PUBLIC_TOOLS),
    ("public", True, False, 200, PUBLIC_TOOLS),
    ("header", False, True, 200, READ_TOOLS),          # write tools off by default
    ("header", True, True, 200, READ_TOOLS | WRITE),   # the only way to the write tools
    ("header", False, False, 200, READ_TOOLS),
    ("header", True, False, 200, READ_TOOLS | WRITE),  # MCP_URL_TOKEN doesn't affect the header
    ("path", False, True, 200, READ_TOOLS),
    ("path", True, True, 200, READ_TOOLS),             # a path token is read-only whatever MCP_WRITE says
    ("path", False, False, 404, None),                 # MCP_URL_TOKEN=0 refuses the path form
    ("path", True, False, 404, None),
]
IDS = [f"{caller}-write{int(w)}-url{int(u)}" for caller, w, u, *_ in MATRIX]


@pytest.mark.parametrize("caller,write,url_token,status,expected", MATRIX, ids=IDS)
def test_tools_list(c, monkeypatch, caller, write, url_token, status, expected):
    monkeypatch.setattr(X, "MCP_WRITE", write)
    monkeypatch.setattr(X, "MCP_URL_TOKEN", url_token)
    url, headers = request_for(caller)
    r = rpc(c, "tools/list", url=url, headers=headers)
    assert r.status_code == status
    if expected is not None:
        assert {t["name"] for t in r.json["result"]["tools"]} == expected
    else:
        assert "result" not in (r.json or {})


@pytest.mark.parametrize("caller,write,url_token,status,expected", MATRIX, ids=IDS)
def test_tools_call(c, monkeypatch, rid, caller, write, url_token, status, expected):
    monkeypatch.setattr(X, "MCP_WRITE", write)
    monkeypatch.setattr(X, "MCP_URL_TOKEN", url_token)
    url, headers = request_for(caller)

    # a public read tool
    r = rpc(c, "tools/call", {"name": "list_categories", "arguments": {}}, url=url, headers=headers)
    assert r.status_code == status
    if expected is None:
        return
    assert "cuisines" in r.json["result"]["structuredContent"]

    # a private read tool
    r = rpc(c, "tools/call", {"name": "cooking_stats", "arguments": {}}, url=url, headers=headers).json
    if "cooking_stats" in expected:
        assert "result" in r and not r["result"].get("isError")
    else:
        assert r["error"]["code"] == -32602 and "Unknown tool" in r["error"]["message"]

    # a write tool: runs only when listed, and otherwise changes nothing
    r = rpc(c, "tools/call", {"name": "set_want_to_try", "arguments": {"recipe_id": rid, "want": True}},
            url=url, headers=headers).json
    if "set_want_to_try" in expected:
        assert not r["result"].get("isError") and want_flag(rid) == 1
    else:
        assert r["error"]["code"] == -32602 and want_flag(rid) == 0


def test_path_token_cannot_write_through_a_batch(c, monkeypatch, rid):
    monkeypatch.setattr(X, "MCP_WRITE", True)
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
              "params": {"name": n, "arguments": {"recipe_id": rid, "want": True}}}
             for i, n in enumerate(["set_want_to_try", "update_book"])]
    out = c.post(f"/mcp/{TOKEN}", json=batch).json
    assert [r["error"]["code"] for r in out] == [-32602, -32602] and want_flag(rid) == 0


def test_path_token_wins_over_a_header(c, monkeypatch):
    """Both supplied: the path decides, so a valid header can't lift a path request to write."""
    monkeypatch.setattr(X, "MCP_WRITE", True)
    r = rpc(c, "tools/list", url=f"/mcp/{TOKEN}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert {t["name"] for t in r.json["result"]["tools"]} == READ_TOOLS


# ------------------------------------------------------------ refused, never downgraded
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("url_token", [False, True])
def test_wrong_token_is_refused_not_public(c, monkeypatch, write, url_token):
    monkeypatch.setattr(X, "MCP_WRITE", write)
    monkeypatch.setattr(X, "MCP_URL_TOKEN", url_token)
    assert rpc(c, "tools/list", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert rpc(c, "tools/list", url="/mcp/wrong").status_code == 404


def test_url_token_off_refuses_before_comparing(c, monkeypatch):
    monkeypatch.setattr(X, "MCP_URL_TOKEN", False)
    calls = []
    monkeypatch.setattr(X, "token_ok", lambda s: calls.append(s) or True)
    assert rpc(c, "tools/list", url=f"/mcp/{TOKEN}").status_code == 404 and calls == []


# ------------------------------------------------------------ several tokens (rotation)
@pytest.mark.parametrize("configured", [f"{OLD},{NEW}", f" {OLD} , {NEW} ", f",{OLD},,{NEW},"])
@pytest.mark.parametrize("caller", ["header", "path"])
def test_each_listed_token_is_accepted(c, monkeypatch, configured, caller):
    monkeypatch.setattr(X, "MCP_TOKEN", configured)
    for tok in (OLD, NEW):
        url, headers = request_for(caller, tok)
        r = rpc(c, "tools/list", url=url, headers=headers)
        assert r.status_code == 200 and "cooking_stats" in {t["name"] for t in r.json["result"]["tools"]}


@pytest.mark.parametrize("bad", [f"{OLD},{NEW}", OLD + NEW, OLD[:-1], ",", " "])
def test_the_list_itself_or_a_fragment_is_not_a_token(c, monkeypatch, bad):
    monkeypatch.setattr(X, "MCP_TOKEN", f"{OLD},{NEW}")
    monkeypatch.setattr(X, "MCP_PUBLIC", False)
    r = rpc(c, "tools/list", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_rotation_add_move_remove(c, monkeypatch):
    hdr = lambda t: {"Authorization": f"Bearer {t}"}
    monkeypatch.setattr(X, "MCP_TOKEN", OLD)
    assert rpc(c, "tools/list", headers=hdr(NEW)).status_code == 401
    monkeypatch.setattr(X, "MCP_TOKEN", f"{OLD},{NEW}")  # add the new one
    assert rpc(c, "tools/list", headers=hdr(OLD)).status_code == 200
    assert rpc(c, "tools/list", headers=hdr(NEW)).status_code == 200
    monkeypatch.setattr(X, "MCP_TOKEN", NEW)  # clients moved: remove the old one
    assert rpc(c, "tools/list", headers=hdr(OLD)).status_code == 401
    assert rpc(c, "tools/list", headers=hdr(NEW)).status_code == 200


def test_mcp_tokens_parsing(monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", " a ,, b ,")
    assert X.mcp_tokens() == ["a", "b"]
    monkeypatch.setattr(X, "MCP_TOKEN", "")
    assert X.mcp_tokens() == [] and X.token_ok("") is False and X.token_ok("anything") is False


@pytest.mark.parametrize("supplied", ["t1", "t2", "t3", "nope"])
def test_every_candidate_is_compared(monkeypatch, supplied):
    """No early exit on a match: the time doesn't say which token matched or how many there are."""
    monkeypatch.setattr(X, "MCP_TOKEN", "t1,t2,t3")
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(X.hmac, "compare_digest", lambda a, b: seen.append(b) or real(a, b))
    assert X.token_ok(supplied) is (supplied != "nope")
    assert seen == [b"t1", b"t2", b"t3"]


def test_non_ascii_token_is_refused_not_500(c, monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", f"{OLD},トークン")
    assert X.token_ok("トークン") is True
    monkeypatch.setattr(X, "MCP_PUBLIC", False)
    assert rpc(c, "tools/list", headers={"Authorization": "Bearer é"}).status_code == 401


# ------------------------------------------------------------ logs
def run_wsgi(path):
    env = EnvironBuilder(path=path, method="POST", data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                         content_type="application/json").get_environ()
    env["RAW_URI"] = env["REQUEST_URI"] = path
    status = {}
    b"".join(A.app.wsgi_app(env, lambda s, h, e=None: status.setdefault("s", s)))
    return env, status["s"]


@pytest.mark.parametrize("url_token", [True, False])
def test_path_token_is_redacted_whether_accepted_or_refused(monkeypatch, url_token):
    monkeypatch.setattr(X, "MCP_TOKEN", f"{OLD},{NEW}")
    monkeypatch.setattr(X, "MCP_URL_TOKEN", url_token)
    env, status = run_wsgi(f"/mcp/{NEW}")
    assert status.startswith("200" if url_token else "404")
    assert env["RAW_URI"] == "/mcp/<redacted>" and X.MCP_TOKEN_KEY not in env
    assert not any(NEW in v for v in env.values() if isinstance(v, str))


def test_write_tool_log_line_has_no_token(c, monkeypatch, rid, caplog):
    import logging
    monkeypatch.setattr(X, "MCP_WRITE", True)
    with caplog.at_level(logging.DEBUG):
        rpc(c, "tools/call", {"name": "set_want_to_try", "arguments": {"recipe_id": rid, "want": True}},
            headers={"Authorization": f"Bearer {TOKEN}"})
    assert any("MCP write set_want_to_try" in r.getMessage() for r in caplog.records)
    assert not any(TOKEN in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------ settings reach the service
def test_settings_are_installed_exported_and_documented():
    head = (A.APP_DIR / "installer" / "setup-head.sh").read_text()
    export = head.split("export DATA_DIR", 1)[1].split("\n")[0]
    env_example = (A.APP_DIR / ".env.example").read_text()
    readme = (A.APP_DIR / "README.md").read_text()
    for var in ("MCP_WRITE", "MCP_URL_TOKEN"):
        assert f'add_setting "$CONF" {var} ' in head
        assert f" {var}" in export
        assert f"\n{var}=" in env_example
        assert f"`{var}" in readme
