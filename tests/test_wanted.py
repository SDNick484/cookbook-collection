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


# ---------- Task 3: off the editor catalog, facets and searches ----------

def _ids(r):
    return {b["id"] for b in r.json["books"]}


def test_editor_catalog_and_search_skip_wanted(c):
    bid, _ = add_wanted("Qwfp Wanted Volume")
    assert bid not in _ids(c.get("/admin/api/books"))
    assert bid not in _ids(c.get("/admin/api/books?q=qwfp"))
    assert bid in _ids(c.get("/admin/api/books?status=wanted"))


def test_editor_recipe_search_skips_wanted_books(c):
    bid, _ = add_wanted("Wanted With Recipes")
    con = A.connect()
    con.execute("INSERT INTO recipes(book_id, name) VALUES (?, 'Xylophone Lasagne')", (bid,))
    con.commit()
    con.close()
    assert c.get("/admin/api/recipes?q=xylophone").json["count"] == 0
    assert c.get("/admin/api/recipes/books?q=xylophone").json["count"] == 0


def test_editor_facets_skip_wanted(c):
    add_wanted("Facet Wanted", cuisine="Zanzibari", publisher="Qqq Wanted Press")
    f = c.get("/admin/api/facets").json
    assert all(x["name"] != "Zanzibari" for x in f["cuisines"])
    assert "Qqq Wanted Press" not in f["publishers"]


def test_private_mcp_search_books_skips_wanted(c, monkeypatch):
    import mcp as MC
    monkeypatch.setattr(MC, "MCP_TOKEN", "t0k")
    add_wanted("Mcp Wanted Qzx")
    books = mcp(c, "search_books", {"query": "qzx"}, token="t0k")["structuredContent"]["books"]
    assert books == []


# ---------- Task 4: the Wanted list and Bought it ----------

WANTED_KEYS = {"id", "uid", "title", "authors", "publisher", "isbn", "cover_url", "price_cents", "price_currency",
               "release_date", "released", "buy_url", "want_priority", "on_wish_lists", "created_at"}


def a_location(c):
    row = q("SELECT id FROM locations LIMIT 1")
    if row:
        return row[0][0]
    return c.post("/admin/api/locations", json={"name": "Test shelf"}).json["location"]["id"]


def test_wanted_api_lists_only_wanted(c):
    bid, uid = add_wanted("Api Wanted", price_cents=3500, price_currency="USD", release_date="2099-01",
                          buy_url="https://publisher.example/a", want_priority=1)
    books = c.get("/admin/api/wanted").json["books"]
    b = next(x for x in books if x["id"] == bid)
    assert b["uid"] == uid and b["released"] is False and b["want_priority"] == 1
    assert set(b) == WANTED_KEYS
    assert all(x["id"] not in _ids(c.get("/admin/api/books")) for x in books)
    assert {x["id"] for x in books} == {r[0] for r in q("SELECT id FROM books WHERE status='wanted'")}


def test_wanted_api_search(c):
    bid, _ = add_wanted("Searchable Vvwanted Book")
    add_wanted("Something Else Wanted")
    assert [b["id"] for b in c.get("/admin/api/wanted?q=vvwanted").json["books"]] == [bid]


def test_wanted_api_is_editor_only(c):
    assert c.get("/admin/api/wanted", environ_base=ANON).status_code in (401, 403)


def test_wanted_api_survives_imported_junk(c):
    bid, _ = add_wanted("Junk Import", release_date="Spring 2027", buy_url="javascript:alert(1)")
    r = c.get("/admin/api/wanted")
    assert r.status_code == 200 and next(x for x in r.json["books"] if x["id"] == bid)["released"] is True


def test_bought_it_flips_in_place_and_undoes(c):
    bid, uid = add_wanted("Bought Book")
    loc = a_location(c)
    r = c.post(f"/admin/api/books/{bid}/bought", json={"location_id": loc})
    assert r.status_code == 200 and r.json["status"] == "owned" and r.json["uid"] == uid
    assert r.json["location_id"] == loc
    assert bid in _ids(c.get("/admin/api/books"))
    assert c.get(f"/api/books/{bid}", environ_base=ANON).status_code == 200
    change = c.get("/admin/api/changes").json["changes"][0]
    assert change["op"] == "bought"
    assert c.post(f"/admin/api/changes/{change['id']}/undo").status_code == 200
    assert q("SELECT status, location_id FROM books WHERE id=?", bid)[0][:] == ("wanted", None)


def test_bought_twice_is_409_and_records_once(c):
    bid, _ = add_wanted("Double Tap")
    before = q("SELECT count(*) FROM changes")[0][0]
    assert c.post(f"/admin/api/books/{bid}/bought", json={}).status_code == 200
    r = c.post(f"/admin/api/books/{bid}/bought", json={})
    assert r.status_code == 409 and "wanted" in r.json["error"]
    assert q("SELECT count(*) FROM changes")[0][0] == before + 1


def test_bought_bad_location_is_400(c):
    bid, _ = add_wanted("Nowhere Book")
    assert c.post(f"/admin/api/books/{bid}/bought", json={"location_id": 987654}).status_code == 400
    assert q("SELECT status FROM books WHERE id=?", bid)[0][0] == "wanted"


def test_bought_unknown_book_is_404(c):
    assert c.post("/admin/api/books/999999/bought", json={}).status_code == 404


# ---------- Task 5: the wish list push ----------

def fake_wishlist(calls):
    def f(method, path, body=None, timeout=5):
        calls.append((method, path, body))
        if path == "/api/people":
            return [{"id": 2, "name": "Sam"}]
        if path == "/api/people/2/items":
            return {"id": 55}
        raise RuntimeError("unexpected call")
    return f


def test_push_uses_buy_link_price_and_priority(c, monkeypatch):
    import wishlist as WL
    calls = []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Push Me", buy_url="https://publisher.example/push", price_cents=2450,
                        price_currency="EUR", want_priority=1)
    assert c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2}).status_code == 200
    item = calls[-1][2]
    assert item["url"] == "https://publisher.example/push" and item["price"] == "24.50"
    assert item["priority"] == 1 and "EUR 24.50" in item["notes"]
    assert [w["on_wish_lists"] for w in c.get("/admin/api/wanted").json["books"] if w["id"] == bid] == [["Sam"]]


def test_push_usd_price_has_no_currency_note(c, monkeypatch):
    import wishlist as WL
    calls = []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Push Dollars", price_cents=3500, price_currency="USD")
    c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2})  # no price sent: the book's
    item = calls[-1][2]
    assert item["price"] == "35.00" and "USD" not in item["notes"]


def test_push_without_want_fields_keeps_old_behaviour(c, monkeypatch):
    import wishlist as WL
    calls = []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Plain Push")
    c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2, "price": "25.00", "priority": 3})
    item = calls[-1][2]
    assert item["url"].startswith("https://www.amazon.com/s?k=") and item["price"] == "25.00" and item["priority"] == 3


def test_push_ignores_an_imported_non_http_buy_link(c, monkeypatch):
    import wishlist as WL
    calls = []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Junk Link Push", buy_url="javascript:alert(1)")
    c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2})
    assert calls[-1][2]["url"].startswith("https://www.amazon.com/s?k=")


def test_features_carry_the_wish_list_link(c, monkeypatch):
    import wishlist as WL
    monkeypatch.setattr(WL, "WISHLIST_PUBLIC_URL", "https://wishlist.example.com")
    assert c.get("/admin/api/features").json["wishlist_url"] == "https://wishlist.example.com"
    monkeypatch.setattr(WL, "WISHLIST_PUBLIC_URL", "")
    assert c.get("/admin/api/features").json["wishlist_url"] is None


# ---------- Task 6: the private list_wanted MCP tool ----------

def test_list_wanted_is_private_and_compact(c, monkeypatch):
    import mcp as MC
    monkeypatch.setattr(MC, "MCP_TOKEN", "t0k")
    add_wanted("Mcp Listed", price_cents=2450, price_currency="EUR", want_priority=1, release_date="2099")
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "list_wanted", "arguments": {}}}).json
    assert r["error"]["code"] == -32602 and "Unknown tool" in r["error"]["message"]  # public: not offered
    res = mcp(c, "list_wanted", {}, token="t0k")["structuredContent"]
    b = next(x for x in res["books"] if x["title"] == "Mcp Listed")
    assert set(b) == {"title", "authors", "price", "release_date", "released", "priority"}
    assert b["price"] == "€24.50" and b["released"] is False and b["priority"] == 1
    assert res["total"] == q("SELECT count(*) FROM books WHERE status='wanted'")[0][0]


@pytest.mark.parametrize("cents,cur,out", [(3500, "USD", "$35.00"), (3500, None, "$35.00"), (999, "GBP", "£9.99"),
                                           (1200, "CHF", "CHF 12.00"), (None, "EUR", None)])
def test_list_wanted_price_text(cents, cur, out):
    import mcp as MC
    assert MC.price_text(cents, cur) == out


def test_list_wanted_order_priority_then_release():
    import mcp as MC
    books = [{"title": "c", "want_priority": None, "release_date": "2026"},
             {"title": "b", "want_priority": 2, "release_date": None},
             {"title": "a2", "want_priority": 1, "release_date": "2028"},
             {"title": "a1", "want_priority": 1, "release_date": "2027-05"}]
    assert [b["title"] for b in MC.wanted_order(books)] == ["a1", "a2", "b", "c"]


# ---------- final review fixes ----------

def test_fingerprint_ignores_unset_want_columns():
    """A change recorded before the upgrade (no want columns in its snapshot) must still undo after it."""
    import changes as CH
    import wanted as W
    bid = q("SELECT id FROM books WHERE kind IS NULL LIMIT 1")[0][0]
    con = A.connect()
    now = CH.STATE["book"](con, bid)
    con.close()
    before_upgrade = {**now, "row": {k: v for k, v in now["row"].items() if k not in W.COLS}}
    assert all(now["row"][k] is None for k in W.COLS)
    assert CH.fingerprint("book", before_upgrade) == CH.fingerprint("book", now)
    set_ = {**now, "row": {**now["row"], "price_cents": 100}}
    assert CH.fingerprint("book", set_) != CH.fingerprint("book", now)  # a set want field is still a change


def test_editor_total_counts_the_collection_only(c):
    add_wanted("Total Wanted Book")
    r = c.get("/admin/api/books").json
    assert len(r["books"]) == r["total"]
    r = c.get("/admin/api/books?status=wanted").json
    assert r["total"] == q("SELECT count(*) FROM books WHERE status='wanted'")[0][0]


def test_shelf_counts_skip_wanted_books(c):
    loc = a_location(c)
    before = next(x for x in c.get("/admin/api/facets").json["locations"] if x["id"] == loc)["direct"]
    add_wanted("Shelved Wanted Book", location_id=loc)
    after = next(x for x in c.get("/admin/api/facets").json["locations"] if x["id"] == loc)["direct"]
    assert after == before


def test_becoming_wanted_takes_a_book_off_its_shelf(c):
    loc = a_location(c)
    bid = c.post("/admin/api/books", json={"title": "Shelf Then Wanted", "location_id": loc}).json["id"]
    b = c.put(f"/admin/api/books/{bid}", json={"status": "wanted", "location_id": loc}).json
    assert b["status"] == "wanted" and b["location_id"] is None


def test_private_list_categories_skip_wanted(c, monkeypatch):
    import mcp as MC
    monkeypatch.setattr(MC, "MCP_TOKEN", "t0k")
    add_wanted("Cuisine Wanted Book", cuisine="Wantedonian")
    cats = mcp(c, "list_categories", {}, token="t0k")["structuredContent"]
    assert all(x["name"] != "Wantedonian" for x in cats["cuisines"])


def test_push_prefers_what_the_dialog_sends(c, monkeypatch):
    import wishlist as WL
    calls = []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    bid, _ = add_wanted("Dialog Wins", price_cents=2450, price_currency="EUR", want_priority=1)
    c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2, "price": "30.00", "priority": 3})
    item = calls[-1][2]
    assert item["price"] == "30.00" and item["priority"] == 3 and "EUR 30.00" in item["notes"]


# ---------- the cover goes with a wish list item ----------

def a_cover(name="wish-cover.jpg"):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (30, 40), (200, 80, 40)).save(buf, "JPEG")
    A.COVER_DIR.mkdir(parents=True, exist_ok=True)
    (A.COVER_DIR / name).write_bytes(buf.getvalue())
    return name, buf.getvalue()


def test_multipart_body_is_a_valid_upload():
    import wishlist as WL
    from werkzeug.test import EnvironBuilder
    from werkzeug.wrappers import Request
    ctype, body = WL.multipart_body("photo", "cover.jpg", "image/jpeg", b"\xff\xd8 bytes \r\n--x")
    env = EnvironBuilder(method="POST", input_stream=__import__("io").BytesIO(body), content_type=ctype,
                         content_length=len(body)).get_environ()
    f = Request(env).files["photo"]
    assert f.filename == "cover.jpg" and f.mimetype == "image/jpeg" and f.read() == b"\xff\xd8 bytes \r\n--x"


def test_push_sends_the_cover(c, monkeypatch):
    import wishlist as WL
    calls, uploads = [], []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    monkeypatch.setattr(WL, "wishlist_upload", lambda path, field, filename, ctype, data, timeout=15:
                        uploads.append((path, field, filename, ctype, data)) or {"id": 55})
    name, data = a_cover()
    bid, _ = add_wanted("Push With Cover", cover=name)
    r = c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2})
    assert r.status_code == 200 and r.json["cover"] is True
    assert uploads == [("/api/items/55/photo", "photo", "cover.jpg", "image/jpeg", data)]


def test_push_without_a_cover_sends_none(c, monkeypatch):
    import wishlist as WL
    calls, uploads = [], []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))
    monkeypatch.setattr(WL, "wishlist_upload", lambda *a, **k: uploads.append(a))
    bid, _ = add_wanted("Push No Cover")
    gone, _ = add_wanted("Push Missing Cover File", cover="not-on-disk.jpg")
    assert c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2}).json["cover"] is None
    assert c.post(f"/admin/api/books/{gone}/wishlist", json={"person_id": 2}).json["cover"] is None
    assert uploads == []


def test_push_still_adds_when_the_cover_fails(c, monkeypatch):
    import wishlist as WL
    calls = []
    monkeypatch.setattr(WL, "wishlist_call", fake_wishlist(calls))

    def boom(*a, **k):
        raise RuntimeError("wish list answered 503")
    monkeypatch.setattr(WL, "wishlist_upload", boom)
    name, _ = a_cover("wish-cover-2.jpg")
    bid, _ = add_wanted("Push Cover Fails", cover=name)
    r = c.post(f"/admin/api/books/{bid}/wishlist", json={"person_id": 2})
    assert r.status_code == 200 and r.json["cover"] is False and r.json["person"] == "Sam"
    assert q("SELECT count(*) FROM wish_items WHERE book_id=?", bid)[0][0] == 1
