"""Garbled duplicate recipes ("tud�usi" next to "tudòusi"), and what an ingredient filter matches."""
import io, json, os, sys, tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import eyb as E  # noqa: E402
import ingredients as I  # noqa: E402

G = "�"


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture()
def book(c):
    bid = c.post("/admin/api/books", json={"title": "Garbled Test Book"}).json["id"]
    yield bid
    c.delete(f"/admin/api/books/{bid}")


def upload(c, url, raw, **form):
    return c.post(url, data={"file": (io.BytesIO(raw), "x.csv"), **form}, content_type="multipart/form-data")


def test_garbled_twin():
    assert E.garbled_twin(f"Qingjiāo chao tud{G}usi", "Qingjiāo chao tudòusi")
    assert E.garbled_twin(f"Napa cabbage (Li{G}ngb{G}n d{G}ugān)", "Napa cabbage (Liángbàn dòugān)")
    assert E.garbled_twin(f"Cr{G}pes", "Crêpes") and E.garbled_twin(f"cr{G}pes", "Crêpes")  # case doesn't matter
    assert not E.garbled_twin(f"Cr{G}pes", "Crumpets")                                    # too many letters lost
    assert not E.garbled_twin(f"Cr{G}pes", f"Cr{G}pes")                                   # twin must be good
    assert not E.garbled_twin("Crêpes", "Crêpes")                                         # nothing garbled


def test_merge_keeps_tags_star_and_dinners(c, book):
    add = lambda name, page, **kw: c.post(f"/admin/api/books/{book}/recipes", json={"name": name, "page": page, **kw}).json["id"]
    good = add("Stir-fried potato threads (Tudòusi)", "39")
    bad = add(f"Stir-fried potato threads (Tud{G}usi)", "39", want_to_try=True, rating=8, notes="extra chili")
    lonely = add(f"Soymilk (D{G}ujiāng)", "64")                     # no good copy: stays, listed for review
    other_page = add(f"Crêpes (Jiānbing {G})", "366")                # different page from any twin
    con = A.connect()
    iid = A.name_id(con, "ingredients", "Potatoes")
    con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (bad, iid))
    con.execute("INSERT INTO cooked(meal_id, day, meal_name, book_id, recipe_id, how) VALUES (987654, '2026-10-01', 'x', ?, ?, 'manual')",
                (book, bad))
    con.commit()
    assert E.merge_garbled(con) == 1
    con.commit()
    left = {r[0] for r in con.execute("SELECT id FROM recipes WHERE book_id=?", (book,))}
    assert left == {good, lonely, other_page}
    g = con.execute("SELECT want_to_try, rating, notes FROM recipes WHERE id=?", (good,)).fetchone()
    assert tuple(g) == (1, 8, "extra chili")
    assert con.execute("SELECT 1 FROM recipe_ingredients WHERE recipe_id=? AND ingredient_id=?", (good, iid)).fetchone()
    assert con.execute("SELECT recipe_id FROM cooked WHERE meal_id=987654").fetchone()[0] == good
    assert E.merge_garbled(con) == 0                                 # nothing more to do
    con.execute("DELETE FROM cooked WHERE meal_id=987654")
    con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
    con.commit()
    con.close()
    listed = {r["id"] for r in c.get("/admin/api/recipes/garbled").json["recipes"]}
    assert {lonely, other_page} <= listed and good not in listed
    assert c.get("/admin/api/recipes/garbled", headers={"Cf-Ray": "x"}).status_code == 401


@pytest.fixture()
def no_pork():
    yield
    con = A.connect()
    con.execute("DELETE FROM ingredients WHERE name='Pork'")
    con.commit()
    con.close()


def test_imports_dont_add_garbled_copies(c, book, no_pork):
    title = "Garbled Test Book"
    c.post(f"/admin/api/books/{book}/recipes", json={"name": "Zhajiang noodles (Zhájiàng miàn)", "page": "43"})
    raw = ("Recipe Title,Book Title,Author,Page No.\r\n"
           f"Zhajiang noodles (Zh{G}ji{G}ng mi{G}n),{title},Carolyn Phillips,(page 43)\r\n").encode("utf-8")
    # recipe import: the garbled row is a duplicate
    r = upload(c, "/admin/api/eyb/commit", raw, decisions=json.dumps({title: book})).json
    assert r["added"] == 0 and r["duplicates"] == 1
    # ingredient import: the tag goes on the good copy
    r = upload(c, "/admin/api/ingredients/commit", raw, ingredient="Pork", decisions=json.dumps({title: book})).json
    assert r["added_recipes"] == 0 and r["tagged"] == 1
    names = [x["name"] for x in c.get(f"/api/books/{book}").json["recipes"]]
    assert names == ["Zhajiang noodles (Zhájiàng miàn)"]
    assert c.get(f"/api/recipes?ingredient=Pork&book={book}").json["count"] == 1
    # a good copy imported after a garbled one replaces it
    c.post(f"/admin/api/books/{book}/recipes", json={"name": f"Dalu noodle soup (Dalu mi{G}n)", "page": "44"})
    good = ("Recipe Title,Book Title,Author,Page No.\r\n"
            f"Dalu noodle soup (Dalu miàn),{title},Carolyn Phillips,(page 44)\r\n").encode("utf-8")
    assert upload(c, "/admin/api/eyb/commit", good, decisions=json.dumps({title: book})).json["added"] == 1
    names = [x["name"] for x in c.get(f"/api/books/{book}").json["recipes"]]
    assert names == ["Zhajiang noodles (Zhájiàng miàn)", "Dalu noodle soup (Dalu miàn)"]


@pytest.fixture()
def tags():
    con = A.connect()
    ids = {n: A.name_id(con, "ingredients", n) for n in ("Beef", "Eggs", "Tomatoes", "Chicken")}
    con.commit()
    yield ids
    for iid in ids.values():
        con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
    con.commit()
    con.close()


def test_ingredient_resolves_to_major_tags(c, book, tags):
    con = A.connect()
    names = lambda q: [t for _, t in I.resolve(con, q)]
    assert names("ground beef") == ["Beef"] and names("Beef, ground") == ["Beef"] and names("minced beef") == ["Beef"]
    assert names("egg") == ["Eggs"] and names("cherry tomatoes") == ["Tomatoes"] and names("BEEF") == ["Beef"]
    assert names("eggplant") == [] and names("chickpeas") == []      # whole words only
    assert sorted(names("chicken and eggs")) == ["Chicken", "Eggs"]
    add = lambda name: c.post(f"/admin/api/books/{book}/recipes", json={"name": name}).json["id"]
    tagged = add("Texas red")                                          # tagged Beef, title doesn't say beef
    titled = add("Best ground beef chili")                             # untagged, title does
    other = add("Turkey chili")
    con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (tagged, tags["Beef"]))
    con.commit()
    ids = lambda **q: {r["id"] for r in c.get("/api/recipes", query_string={"book": book, "limit": 500, **q}).json["recipes"]}
    assert ids(ingredient="Beef") == {tagged, titled}
    assert ids(ingredient="ground beef", q="chili") == {titled}        # what the MCP test asked for
    assert ids(ingredient="turkey") == {other}                         # no tag: titles only
    assert ids(ingredient="lamb") == set()
    # the MCP says what it used, and lists the real tags when there's no match
    mcp = lambda args: A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "search_recipes", "arguments": args}}).json["result"]["structuredContent"]
    r = mcp({"query": "chili", "ingredient": "ground beef", "book_id": book})
    assert r["count"] == 1 and r["ingredient_tags"] == ["Beef"] and "Beef" in r["ingredient_note"]
    r = mcp({"ingredient": "turkey", "book_id": book})
    assert r["count"] == 1 and r["ingredient_tags"] == [] and "titles only" in r["ingredient_note"]
    listed = A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json
    desc = next(t for t in listed["result"]["tools"] if t["name"] == "search_recipes")
    assert "Beef" in desc["description"] and "Beef" in desc["inputSchema"]["properties"]["ingredient"]["description"]
    assert "{tags}" not in json.dumps(listed)
    con.close()


def test_startup_merges_and_reports(c, book):
    good = c.post(f"/admin/api/books/{book}/recipes", json={"name": "Mapo doufu (Mápo dòufu)", "page": "268"}).json["id"]
    c.post(f"/admin/api/books/{book}/recipes", json={"name": f"Mapo doufu (M{G}po d{G}ufu)", "page": "268"})
    A.init_db()  # what happens when the upgraded app starts
    assert [r["id"] for r in c.get(f"/api/books/{book}").json["recipes"]] == [good]
    merged = c.get("/admin/api/recipes/garbled").json["merged"]
    assert merged["n"] >= 1 and merged["at"]


def test_more_specific_tag_wins(c, book):
    con = A.connect()
    ids = {n: A.name_id(con, "ingredients", n) for n in ("Potatoes", "Sweet potatoes", "Beans", "Green beans", "Chicken")}
    con.execute("UPDATE ingredients SET hint=1 WHERE name IN ('Sweet potatoes', 'Green beans')")
    con.commit()
    try:
        names = lambda q: sorted(t for _, t in I.resolve(con, q))
        assert names("sweet potatoes") == ["Sweet potatoes"] and names("Sweet potato") == ["Sweet potatoes"]
        assert names("potatoes") == ["Potatoes"] and names("mashed potatoes") == ["Potatoes"]
        assert names("green beans") == ["Green beans"] and names("beans") == ["Beans"]
        assert names("chicken and sweet potatoes") == ["Chicken", "Sweet potatoes"]   # unrelated tags: both
        # a sweet potato recipe tagged only Sweet potatoes is found (the Potatoes tag isn't also required)
        rid = c.post(f"/admin/api/books/{book}/recipes", json={"name": "Roasted tubers with miso"}).json["id"]
        con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid, ids["Sweet potatoes"]))
        con.commit()
        got = {r["id"] for r in c.get("/api/recipes", query_string={"ingredient": "sweet potatoes", "book": book}).json["recipes"]}
        assert got == {rid}
    finally:
        for iid in ids.values():
            con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        con.commit()
        con.close()


def test_ranking_chicken_and_leeks(c, book):
    """The MCP test that prompted ranking: chicken + leek dishes first, stock-only ones last, and paging."""
    con = A.connect()
    ids = {n: A.name_id(con, "ingredients", n) for n in ("Chicken", "Chicken stock", "Chicken pieces", "Leeks")}
    con.execute("UPDATE ingredients SET hint=1 WHERE name IN ('Chicken stock', 'Chicken pieces', 'Leeks')")
    con.commit()  # before the app writes through its own connection
    recipes = {  # name: tags
        "Osso buco": ["Chicken", "Chicken stock", "Leeks"],
        "Leek and potato soup": ["Chicken", "Chicken stock"],
        "Coq au vin blanc": ["Chicken", "Chicken pieces", "Chicken stock", "Leeks"],
        "Stuffed chicken breasts": ["Chicken", "Chicken stock", "Leeks"],
        "No-fuss risotto with chicken, leek and garlic": [],
        "Eggplant gratin": ["Leeks"],                       # no chicken: not a match
    }
    rid = {}
    try:
        for name, tags in recipes.items():
            rid[name] = c.post(f"/admin/api/books/{book}/recipes", json={"name": name}).json["id"]
            for t in tags:
                con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid[name], ids[t]))
            con.commit()
        expect = ["No-fuss risotto with chicken, leek and garlic",   # both in the title
                  "Stuffed chicken breasts",                         # chicken in the title, leeks tagged
                  "Coq au vin blanc",                                # both from tags, real chicken (pieces)
                  "Leek and potato soup",                            # leeks in the title, chicken maybe only stock
                  "Osso buco"]                                       # leeks tagged, chicken maybe only stock
        web = [r["name"] for r in c.get("/api/recipes", query_string={"q": "chicken leeks", "book": book}).json["recipes"]]
        assert web == expect
        filt = [r["name"] for r in c.get("/api/recipes", query_string={"q": "leek", "ingredient": "chicken", "book": book}).json["recipes"]]
        assert filt == expect
        mcp = lambda args: A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "search_recipes", "arguments": {"book_id": book, **args}}}).json["result"]["structuredContent"]
        first = mcp({"query": "chicken leeks", "limit": 2})
        assert [r["name"] for r in first["recipes"]] == expect[:2] and first["total"] == 5 and first["next_offset"] == 2
        rest = mcp({"query": "chicken leeks", "limit": 10, "offset": first["next_offset"]})
        assert [r["name"] for r in rest["recipes"]] == expect[2:] and "next_offset" not in rest
        match = {r["name"]: r["match"] for r in first["recipes"] + rest["recipes"]}
        assert match["No-fuss risotto with chicken, leek and garlic"] == "title"
        assert match["Coq au vin blanc"] == "chicken: tag, leek: tag"
        assert match["Stuffed chicken breasts"] == "chicken: title, leek: tag"
        assert match["Osso buco"] == "chicken: may be only chicken stock, leek: tag"
        assert match["Leek and potato soup"] == "chicken: may be only chicken stock, leek: title"
        # whole words in titles: "egg" isn't in "Eggplant gratin"
        assert I.in_title("egg", "eggs benedict") and not I.in_title("egg", "eggplant gratin")
        assert I.in_title("cherry", "cherries jubilee") and I.in_title("leek", "leeks vinaigrette")
        # the MCP explains missing pages and ranking
        init = A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).json
        assert "without page numbers" in init["result"]["instructions"] and "next_offset" in init["result"]["instructions"]
    finally:
        for r in rid.values():
            c.delete(f"/admin/api/recipes/{r}")
        for iid in ids.values():
            con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        con.commit()
        con.close()


def test_minor_forms_rank_low(c, book):
    """Tomato paste, rice wine, chicken stock…: a recipe that may only use a minor form of the ingredient ranks
    after ones that clearly use it, unless the search asks for that form."""
    con = A.connect()
    names = ("Tomatoes", "Tomato paste", "Rice", "Rice wine", "Rice vinegar", "Chicken", "Chicken stock", "Duck", "Duck fat")
    ids = {n: A.name_id(con, "ingredients", n) for n in names}
    con.execute(f"UPDATE ingredients SET hint=1 WHERE id IN ({','.join('?' * 5)})",
                [ids[n] for n in ("Tomato paste", "Rice wine", "Rice vinegar", "Chicken stock", "Duck fat")])
    con.commit()
    recipes = {
        "Panzanella": ["Tomatoes"],                                   # really tomatoes
        "Braised short ribs": ["Tomatoes", "Tomato paste"],           # a spoonful of paste
        "Red-cooked pork": ["Rice", "Rice wine"],                     # Shaoxing wine, no rice
        "Sushi rice salad": ["Rice", "Rice vinegar"],                 # rice in the title: real rice
        "Clay pot rice": ["Rice", "Rice wine"],                       # rice in the title: real rice
        "Hot and sour soup": ["Chicken", "Chicken stock", "Rice", "Rice vinegar"],
    }
    rid = {}
    try:
        for name, tags in recipes.items():
            rid[name] = c.post(f"/admin/api/books/{book}/recipes", json={"name": name}).json["id"]
            for t in tags:
                con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid[name], ids[t]))
            con.commit()
        order = lambda **q: [r["name"] for r in c.get("/api/recipes", query_string={"book": book, **q}).json["recipes"]]
        assert order(q="tomatoes") == ["Panzanella", "Braised short ribs"]
        assert order(q="tomato paste") == ["Braised short ribs"]          # asking for the paste: it counts fully
        rice = order(q="rice")
        assert rice[:2] == ["Clay pot rice", "Sushi rice salad"] and set(rice[2:]) == {"Hot and sour soup", "Red-cooked pork"}
        # asking for rice wine: both recipes that use it, nothing that only has rice or vinegar
        assert set(order(q="rice wine")) == {"Red-cooked pork", "Clay pot rice"}
        assert set(order(ingredient="rice wine")) == {"Red-cooked pork", "Clay pot rice"}  # Rice wine, not Rice
        mcp = lambda q: {r["name"]: r["match"] for r in A.app.test_client().post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_recipes", "arguments": {
                "query": q, "book_id": book}}}).json["result"]["structuredContent"]["recipes"]}
        assert mcp("tomatoes")["Braised short ribs"] == "tomato: may be only tomato paste"
        assert mcp("rice")["Hot and sour soup"] == "rice: may be only rice vinegar"
        assert mcp("chicken rice")["Hot and sour soup"] == "chicken: may be only chicken stock, rice: may be only rice vinegar"
        assert mcp("chicken stock")["Hot and sour soup"] == "chicken: tag, stock: tag"
    finally:
        for r in rid.values():
            c.delete(f"/admin/api/recipes/{r}")
        for iid in ids.values():
            con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        con.commit()
        con.close()


def test_recipes_for_stock_rank_low(c, book):
    """'Chicken stock' or 'Basic chicken broth' has chicken in its title, but it's the stock, not a chicken dish."""
    con = A.connect()
    ids = {n: A.name_id(con, "ingredients", n) for n in ("Leeks", "Duck")}
    con.execute("UPDATE ingredients SET hint=1 WHERE id=?", (ids["Leeks"],))
    con.commit()
    recipes = {"Chicken stock": ["Leeks"], "Basic chicken broth": ["Leeks"], "Braised chicken with leeks": [],
               "Chicken pie": ["Leeks"], "Duck-fat potatoes": ["Duck"], "Roast duck": ["Duck"]}
    rid = {}
    try:
        for name, tags in recipes.items():
            rid[name] = c.post(f"/admin/api/books/{book}/recipes", json={"name": name}).json["id"]
            for t in tags:
                con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid[name], ids[t]))
            con.commit()
        order = lambda q: [r["name"] for r in c.get("/api/recipes", query_string={"book": book, "q": q}).json["recipes"]]
        assert order("chicken leeks") == ["Braised chicken with leeks", "Chicken pie", "Basic chicken broth", "Chicken stock"]
        assert order("chicken stock") == ["Chicken stock"]          # asking for the stock: it's the best match
        assert order("duck") == ["Roast duck", "Duck-fat potatoes"]
        mcp = {r["name"]: r["match"] for r in A.app.test_client().post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_recipes", "arguments": {
                "query": "chicken leeks", "book_id": book}}}).json["result"]["structuredContent"]["recipes"]}
        assert mcp["Chicken stock"] == "chicken: only as “chicken stock” in the title, leek: tag"
        assert mcp["Chicken pie"] == "chicken: title, leek: tag"
        assert mcp["Braised chicken with leeks"] == "title"
    finally:
        for r in rid.values():
            c.delete(f"/admin/api/recipes/{r}")
        for iid in ids.values():
            con.execute("DELETE FROM ingredients WHERE id=?", (iid,))
        con.commit()
        con.close()
