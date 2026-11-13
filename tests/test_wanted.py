"""Wanted books (status 'wanted'): a shopping list kept off the collection. Hidden from every anonymous read and
from the editor catalog; their own fields (price, release date, buy link, priority); the Wanted list and Bought it;
the wish list push; the private list_wanted MCP tool."""
import pytest

import app as A

ANON = {"REMOTE_ADDR": "203.0.113.9"}  # not on the LAN and no Access token: not an editor
TITLE = "Zzyzx Wanted Tome"


@pytest.fixture()
def c():
    return A.app.test_client()  # loopback with LAN_EDIT=1 (conftest): an editor


def q(sql, *args):
    con = A.connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def add_wanted(title, **cols):
    """A wanted, public cookbook written straight to the database. Returns (id, uid)."""
    con = A.connect()
    fields = {"title": title, "status": "wanted", "visibility": "public", **cols}
    bid = con.execute(f"INSERT INTO books({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                      list(fields.values())).lastrowid
    A.reindex_book(con, bid)
    con.commit()
    uid = con.execute("SELECT uid FROM books WHERE id=?", (bid,)).fetchone()[0]
    con.close()
    return bid, uid


def mcp(c, name, args, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, headers=headers)
    return r.json["result"]


def anon_get_paths(book_id, uid):
    """Every GET route an anonymous visitor can reach, with this book's id and uid filled in."""
    con = A.connect()
    rid = str(con.execute("SELECT id FROM recipes LIMIT 1").fetchone()[0])
    con.close()
    sample = {"book_id": str(book_id), "uid": uid, "rid": rid, "p": "app.js"}
    out = []
    for r in A.app.url_map.iter_rules():
        if ("GET" not in r.methods or r.rule.startswith(("/admin", "/static")) or r.rule.startswith("/mcp")
                or r.rule == "/covers/<path:p>"):
            continue
        path = r.rule
        for arg in r.arguments:
            path = (path.replace(f"<int:{arg}>", sample.get(arg, "1")).replace(f"<path:{arg}>", sample.get(arg, "x"))
                    .replace(f"<{arg}>", sample.get(arg, "x")))
        out.append(path)
    # the catalog and searches, asked for this book by name
    out += ["/api/books?q=zzyzx", "/api/recipes?q=zzyzx", "/api/recipes/books?q=zzyzx"]
    return sorted(set(out))


# ---------- Task 1: hidden from every public read ----------

def test_wanted_book_is_not_public():
    bid, _ = add_wanted(TITLE)
    row = q("SELECT * FROM books WHERE id=?", bid)[0]
    assert A.book_public(row) is False


def test_no_anonymous_get_route_shows_a_wanted_book(c):
    bid, uid = add_wanted(TITLE)
    paths = anon_get_paths(book_id=bid, uid=uid)
    assert "/api/books" in paths and f"/api/books/{bid}" in paths
    for path in paths:
        r = c.get(path, environ_base=ANON)
        assert TITLE.encode() not in r.data, path


def test_wanted_book_detail_is_404_anonymously(c):
    bid, uid = add_wanted(TITLE)
    assert c.get(f"/api/books/{bid}", environ_base=ANON).status_code == 404
    assert c.get(f"/api/books/by-uid/{uid}", environ_base=ANON).status_code == 404


def test_public_mcp_never_returns_a_wanted_book(c):
    add_wanted(TITLE)
    books = mcp(c, "search_books", {"query": "zzyzx"})["structuredContent"]["books"]
    assert books == []


def test_cookbook_counts_exclude_wanted(c):
    before = c.get("/api/facets", environ_base=ANON).json["stats"]["books"]
    add_wanted("Another Wanted Book")
    assert c.get("/api/facets", environ_base=ANON).json["stats"]["books"] == before
    assert c.get("/admin/api/facets").json["stats"]["books"] == q(
        f"SELECT count(*) FROM books b WHERE {A.COOKBOOKS}")[0][0]
    cats = mcp(c, "list_categories", {})["structuredContent"]
    assert cats["totals"]["books"] == before
