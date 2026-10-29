"""The private MCP token: accepted forms, bad input, and keeping it out of logs."""
import io

import pytest
from werkzeug.test import EnvironBuilder

import app as A
import extras as X

TOKEN = "s3cret-token_AbC-123"


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", TOKEN)
    monkeypatch.setattr(X, "MCP_PUBLIC", True)
    return A.app.test_client()


def tools(c, url="/mcp", headers=None):
    return c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers or {})


def private_tools(r):
    return "set_want_to_try" in {t["name"] for t in r.json["result"]["tools"]}


# ------------------------------------------------------------ accepted forms
def test_token_in_path(c):
    r = tools(c, f"/mcp/{TOKEN}")
    assert r.status_code == 200 and private_tools(r)


def test_token_in_path_with_trailing_slash(c):
    r = tools(c, f"/mcp/{TOKEN}/")
    assert r.status_code == 200 and private_tools(r)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_header_any_case(c, scheme):
    r = tools(c, headers={"Authorization": f"{scheme} {TOKEN}"})
    assert r.status_code == 200 and private_tools(r)


def test_public_without_token(c):
    r = tools(c)
    assert r.status_code == 200 and not private_tools(r)


# ------------------------------------------------------------ refused
@pytest.mark.parametrize("bad", ["wrong", TOKEN + "x", TOKEN[:-1], "é", "%C3%A9", "トークン", "a/b"])
def test_wrong_path_token_is_404(c, bad):
    assert tools(c, f"/mcp/{bad}").status_code == 404


@pytest.mark.parametrize("bad", ["wrong", "é", "トークン", ""])
def test_wrong_bearer_is_refused_not_500(c, monkeypatch, bad):
    monkeypatch.setattr(X, "MCP_PUBLIC", False)
    assert tools(c, headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_wrong_bearer_never_downgrades_to_public(c):
    """A bad token is refused outright, even when the public MCP is on."""
    assert tools(c, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_no_token_configured_refuses_any_token(c, monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", "")
    assert tools(c, "/mcp/anything").status_code == 404
    assert tools(c, headers={"Authorization": "Bearer "}).json["result"]  # empty bearer = public


# ------------------------------------------------------------ log redaction
def run_wsgi(path, query="", method="POST"):
    """Call the WSGI app the way gunicorn does and return the environ it logs from afterward."""
    env = EnvironBuilder(path=path, method=method, query_string=query,
                         data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                         content_type="application/json").get_environ()
    raw = path + ("?" + query if query else "")
    env["RAW_URI"] = env["REQUEST_URI"] = raw   # gunicorn sets both; access-log %(r)s uses RAW_URI
    status = {}
    body = b"".join(A.app.wsgi_app(env, lambda s, h, e=None: status.setdefault("s", s)))
    return env, status["s"], body


def test_token_is_redacted_from_logged_environ(monkeypatch):
    monkeypatch.setattr(X, "MCP_TOKEN", TOKEN)
    env, status, body = run_wsgi(f"/mcp/{TOKEN}", "trace=1")
    assert status.startswith("200") and b'"result"' in body
    for k, v in env.items():
        if isinstance(v, str):
            assert TOKEN not in v, k
    assert env["RAW_URI"] == "/mcp/<redacted>?trace=1"
    assert env["PATH_INFO"] == "/mcp"


def test_gunicorn_request_line_has_no_token(monkeypatch):
    glogging = pytest.importorskip("gunicorn.glogging")
    monkeypatch.setattr(X, "MCP_TOKEN", TOKEN)
    env, status, _ = run_wsgi(f"/mcp/{TOKEN}")
    resp = type("Resp", (), {"status": status, "sent": 10, "headers": []})()
    log = glogging.Logger.__new__(glogging.Logger)
    log.cfg = type("Cfg", (), {"forwarded_allow_ips": [], "access_log_format": ""})()
    req = type("Req", (), {"headers": []})()
    atoms = glogging.Logger.atoms(log, resp, req, env, type("T", (), {"seconds": 0, "microseconds": 1})())
    assert TOKEN not in atoms["r"] and TOKEN not in atoms["U"]
    assert "/mcp/<redacted>" in atoms["r"]


def test_other_paths_untouched():
    env, status, _ = run_wsgi("/healthz", method="GET")
    assert status.startswith("200") and env["RAW_URI"] == "/healthz" and X.MCP_TOKEN_KEY not in env


def test_installer_does_not_print_the_token():
    src = (A.APP_DIR / "installer" / "setup-head.sh").read_text()
    assert "s/^MCP_TOKEN=//p" not in src
