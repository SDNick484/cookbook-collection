"""Wanted books (status 'wanted'): a shopping list kept off the collection. Hidden from every anonymous read and
from the editor catalog; their own fields (price, release date, buy link, priority); the Wanted list and Bought it;
the wish list push; the private list_wanted MCP tool."""
import datetime as dt

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


# ---------- Task 2: the want fields ----------

def test_migrate_is_idempotent():
    import wanted as W
    con = A.connect()
    W.migrate(con)
    W.migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    con.close()
    assert set(W.COLS) <= cols


@pytest.mark.parametrize("raw,cents", [("35", 3500), ("35.00", 3500), ("24,50", 2450), ("", None), (None, None)])
def test_clean_fields_price(raw, cents):
    import wanted as W
    assert W.clean_fields({"price": raw}) == {"price_cents": cents}


@pytest.mark.parametrize("raw,cents", [("$35", 3500), (" 35 ", 3500), ("€24,50", 2450), ("£9.99", 999), (35, 3500)])
def test_clean_fields_price_as_typed(raw, cents):
    import wanted as W
    assert W.clean_fields({"price": raw})["price_cents"] == cents


@pytest.mark.parametrize("raw", ["-1", "1.234", "1,234.50", "1000000.01", "abc"])
def test_clean_fields_price_rejects(raw):
    import wanted as W
    with pytest.raises(ValueError):
        W.clean_fields({"price": raw})


@pytest.mark.parametrize("data,out", [
    ({"price_currency": "eur"}, {"price_currency": "EUR"}),
    ({"release_date": "2027"}, {"release_date": "2027"}),
    ({"release_date": "2027-03"}, {"release_date": "2027-03"}),
    ({"release_date": "2027-03-17"}, {"release_date": "2027-03-17"}),
    ({"buy_url": "https://publisher.example/pasta"}, {"buy_url": "https://publisher.example/pasta"}),
    ({"want_priority": "1"}, {"want_priority": 1}),
    ({"want_priority": ""}, {"want_priority": None}),
    ({"title": "not a want field"}, {}),
])
def test_clean_fields_accepts(data, out):
    import wanted as W
    assert W.clean_fields(data) == out


@pytest.mark.parametrize("data", [
    {"price_currency": "EURO"}, {"price_currency": "E1"},
    {"release_date": "2027-13"}, {"release_date": "2027-02-30"}, {"release_date": "March"}, {"release_date": "1899"},
    {"release_date": "2101"}, {"buy_url": "javascript:alert(1)"}, {"buy_url": "ftp://x.example/a"},
    {"buy_url": "https://x.example/" + "a" * 2000}, {"want_priority": 4}, {"want_priority": "high"},
])
def test_clean_fields_rejects(data):
    import wanted as W
    with pytest.raises(ValueError):
        W.clean_fields(data)


@pytest.mark.parametrize("s,start", [("2027", dt.date(2027, 1, 1)), ("2027-03", dt.date(2027, 3, 1)),
                                     ("2027-03-17", dt.date(2027, 3, 17)), (None, None), ("Spring 2027", None)])
def test_release_start(s, start):
    import wanted as W
    assert W.release_start(s) == start


def test_is_released_boundaries():
    import wanted as W
    d = dt.date(2027, 3, 1)
    assert W.is_released("2027-03", d) and not W.is_released("2027-03", d - dt.timedelta(days=1))
    assert W.is_released(None, d)


def test_unparseable_release_date_counts_as_released():
    import wanted as W
    assert W.is_released("Spring 2027", dt.date(2026, 11, 12))


def test_create_wanted_book_with_fields(c):
    r = c.post("/admin/api/books", json={"title": "Example Pasta Book", "authors": ["A. Author"], "status": "wanted",
               "price": "35.00", "price_currency": "usd", "release_date": "2027-03",
               "buy_url": "https://publisher.example/pasta", "want_priority": 1})
    assert r.status_code == 201
    b = r.json
    assert (b["price_cents"], b["price_currency"], b["release_date"], b["want_priority"]) == (3500, "USD", "2027-03", 1)
    assert b["buy_url"] == "https://publisher.example/pasta" and "released" in b


def test_bad_want_field_is_400(c):
    r = c.post("/admin/api/books", json={"title": "X", "status": "wanted", "buy_url": "javascript:alert(1)"})
    assert r.status_code == 400 and "link" in r.json["error"].lower()
    bid = c.post("/admin/api/books", json={"title": "Edit Me", "status": "wanted"}).json["id"]
    assert c.put(f"/admin/api/books/{bid}", json={"want_priority": 9}).status_code == 400


def test_wanting_an_owned_isbn_is_a_conflict(c):
    owned = c.post("/admin/api/books", json={"title": "Owned One", "isbn": "9780306406157"}).json["id"]
    r = c.post("/admin/api/books", json={"title": "Owned One", "isbn": "9780306406157", "status": "wanted"})
    assert r.status_code == 409 and r.json["book_id"] == owned


def test_partial_update_keeps_want_fields(c):
    bid = c.post("/admin/api/books", json={"title": "Keep Fields", "status": "wanted", "price": "20",
                                           "want_priority": 2}).json["id"]
    c.put(f"/admin/api/books/{bid}", json={"subtitle": "new"})
    c.put(f"/admin/api/books/{bid}", json={"status": "owned"})
    b = c.get(f"/admin/api/books/{bid}").json
    assert (b["price_cents"], b["want_priority"], b["status"]) == (2000, 2, "owned")


def test_public_book_payload_has_no_want_fields(c):
    import wanted as W
    bid = c.post("/admin/api/books", json={"title": "Public Owned", "price": "9"}).json["id"]
    pub = c.get(f"/api/books/{bid}", environ_base=ANON).json
    assert not set(W.COLS) & set(pub) and "released" not in pub
    listed = next(b for b in c.get("/api/books?q=public owned", environ_base=ANON).json["books"] if b["id"] == bid)
    assert not set(W.COLS) & set(listed)
