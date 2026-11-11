"""Non-integer values in integer query parameters are the caller's mistake: 400, never a 500.

Walks every GET route (public and editor) with junk in each whole-number parameter the app reads, so a new
route that does int(request.args[...]) without a guard shows up here.
"""
import pytest

import app as A
import dinnerlog as DL

JUNK = {"limit": "lots", "offset": "1e3", "n": "ten", "rest": "x", "count": "1.5", "book": "abc", "page": "p",
        "location_id": "shelf", "days": "-", "year": "nineteen", "id": "1;2", "seed": "s"}


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(A, "http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in tests")))
    monkeypatch.setattr(DL, "start_background", lambda *a, **k: None)
    return A.app.test_client()


def _get_routes():
    con = A.connect()
    sample = {"book_id": "1", "rid": str(con.execute("SELECT id FROM recipes LIMIT 1").fetchone()[0]),
              "uid": con.execute("SELECT uid FROM books LIMIT 1").fetchone()[0],
              "job_id": "1", "scan_id": "1", "raw": "9780306406157", "name": "x.jpg", "p": "app.js"}
    out = []
    for r in A.app.url_map.iter_rules():
        if "GET" not in r.methods or r.rule.startswith("/static") or r.rule == "/mcp":
            continue
        path = r.rule
        for arg in r.arguments:
            path = path.replace(f"<int:{arg}>", sample[arg]).replace(f"<path:{arg}>", sample[arg]) \
                       .replace(f"<{arg}>", sample[arg])
        out.append(path)
    return sorted(set(out))


@pytest.mark.parametrize("name,value", sorted(JUNK.items()))
def test_no_route_500s_on_a_junk_integer(c, name, value):
    bad = []
    for path in _get_routes():
        q = {name: value, "q": "chicken"}  # q: so search routes get past their "nothing to search" shortcut
        r = c.get(path, query_string=q)
        if r.status_code == 500:  # 502 = an upstream (Open Library, wish list) the tests keep unreachable
            bad.append((path, r.status_code))
    assert not bad, f"?{name}={value} -> {bad}"


@pytest.mark.parametrize("path", ["/api/recipes", "/admin/api/recipes"])
def test_recipe_search_rejects_junk_paging(c, path):
    for qs in ({"q": "chicken", "limit": "x"}, {"q": "chicken", "offset": "1.5"}, {"q": "chicken", "book": "abc"}):
        r = c.get(path, query_string=qs)
        assert r.status_code == 400 and "whole number" in r.json["error"], (path, qs)
    assert c.get(path, query_string={"q": "chicken", "limit": " 5 ", "offset": "0"}).status_code == 200


def test_arg_int_bounds():
    with A.app.test_request_context():
        assert A.arg_int({}, "limit", 100, 1, 500) == 100
        assert A.arg_int({"limit": ""}, "limit", 100, 1, 500) == 100
        assert A.arg_int({"limit": "9999"}, "limit", 100, 1, 500) == 500
        assert A.arg_int({"limit": "-4"}, "limit", 100, 1, 500) == 1
        assert A.arg_int({"limit": 7}, "limit", 100) == 7
        for junk in ("x", "1.0", "1e3", "99999999999999999999", True):
            with pytest.raises(Exception) as e:
                A.arg_int({"limit": junk}, "limit", 100)
            assert getattr(e.value, "code", None) == 400 or getattr(getattr(e.value, "response", None), "status_code", None) == 400
