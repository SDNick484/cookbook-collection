"""Ingredient tags from EYB ingredient exports, and ingredient-aware search."""
import io, json, os, re, sys, tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import ingredients as I  # noqa: E402

RAW = ("Recipe Title,Book Title,Author,Page No.\r\n"
       "Roast chicken salad with peppers pine nuts olives and bitter greens,The Zuni Cafe Cookbook,Judy Rodgers,(page 346)\r\n"
       "Chicken braised with figs honey and vinegar,The Zuni Cafe Cookbook,Judy Rodgers,(page 350)\r\n"   # re-words sheet recipe
       "Bread soup,The Zuni Cafe Cookbook,Judy Rodgers,(page 160)\r\n"                                    # uses chicken stock
       "Pozole verde,Pozole,Steve Sando,(page 40)\r\n"
       "Banker's chicken,Jacques P\xe9pin Art of the Chicken,Jacques P\xe9pin,(page 148)\r\n"           # book not in catalog
       ",,,\r\n").encode("cp1252")


@pytest.fixture()
def c():
    return A.app.test_client()


def up(c, url, raw=RAW, name="x.csv", **form):
    return c.post(url, data={"file": (io.BytesIO(raw), name), **form}, content_type="multipart/form-data")


def book_id(c, title):
    return next(b["id"] for b in c.get("/admin/api/books?sort=title").json["books"] if b["title"] == title)


def test_guess_name():
    assert I.guess_name("IngredientList.csv") == ""
    assert I.guess_name("IngredientList-lamb.csv") == "Lamb"
    assert I.guess_name("chicken (2).csv") == "Chicken"
    assert I.guess_name("Chicken - all pages combined.csv") == "Chicken"
    assert I.guess_name("lamb pages 1-4.csv") == "Lamb"
    assert I.guess_name("pork_combined.csv") == "Pork"
    assert I.guess_name("Jalapeño part 2.csv") == "Jalapeño"  # accented letters kept


def test_tidy_names():
    con = A.connect()
    rid = con.execute("SELECT id FROM recipes LIMIT 1").fetchone()[0]
    ids = {}
    for n in ("Tidy test all pages combined", "Tidy dup", "Tidy dup - all pages combined", "Tidy 7-Up",
              "Tidy lamb (all pages combined)"):
        ids[n] = A.name_id(con, "ingredients", n)
        con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, ids[n]))
    rid2 = con.execute("SELECT id FROM recipes WHERE id<>? LIMIT 1", (rid,)).fetchone()[0]
    con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid2, ids["Tidy dup - all pages combined"]))
    I.tidy_names(con)
    name = lambda i: (con.execute("SELECT name FROM ingredients WHERE id=?", (i,)).fetchone() or [None])[0]
    assert name(ids["Tidy test all pages combined"]) == "Tidy test"
    # merged into the existing tag, keeping both recipes
    assert name(ids["Tidy dup - all pages combined"]) is None
    tagged = {r[0] for r in con.execute("SELECT recipe_id FROM recipe_ingredients WHERE ingredient_id=?", (ids["Tidy dup"],))}
    assert tagged == {rid, rid2}
    assert name(ids["Tidy 7-Up"]) == "Tidy 7-Up"  # ordinary names untouched
    assert name(ids["Tidy lamb (all pages combined)"]) == "Tidy lamb"
    I.tidy_names(con)  # idempotent
    assert name(ids["Tidy test all pages combined"]) == "Tidy test"
    for i in ids.values():
        con.execute("DELETE FROM ingredients WHERE id=?", (i,))
    con.commit()
    con.close()


def test_import_tags_and_search(c):
    zuni = book_id(c, "The Zuni Cafe Cookbook")
    before = c.get("/api/recipes?q=chicken&limit=500").json["count"]
    pv = up(c, "/admin/api/ingredients/preview", ingredient="chicken").json
    assert pv["ingredient"] == "Chicken" and pv["total"] == 5
    g = {x["eyb_title"]: x for x in pv["groups"]}
    assert g["The Zuni Cafe Cookbook"]["existing"] == 1 and g["The Zuni Cafe Cookbook"]["new"] == 2
    assert g["Jacques Pépin Art of the Chicken"]["match"] is None
    assert pv["groups"][0]["match"] is None                                   # unmatched listed first
    dec = {t: (x["match"]["id"] if x["match"] else "skip") for t, x in g.items()}
    r = up(c, "/admin/api/ingredients/commit", ingredient="chicken", decisions=json.dumps(dec)).json
    assert r == {"kind": "ingredient", "ingredient": "Chicken", "tagged": 4, "added_recipes": 3, "skipped": 1,
                 "created_books": 0, "total": 4}
    # the sheet's starred "Chicken Braised with Figs, Honey, & Vinegar" was tagged, not duplicated
    p350 = [x for x in c.get(f"/admin/api/books/{zuni}").json["recipes"] if x["page"] == "350"]
    assert len(p350) == 1 and p350[0]["want_to_try"] == 1
    # search: "chicken" now also finds Bread soup and Pozole verde; ingredient filter lists exactly the tagged ones
    hits = c.get("/api/recipes?q=chicken&limit=500").json
    names = {x["name"] for x in hits["recipes"]}
    assert {"Bread soup", "Pozole verde"} <= names and hits["count"] >= before + 2
    # the ingredient filter: tagged Chicken, or "chicken" as a word in the title (only some recipes carry tags)
    con = A.connect()
    rows = con.execute("SELECT r.id, r.name, r.heading, r.book_id, b.title FROM recipes r JOIN books b ON b.id=r.book_id").fetchall()
    tagged = {x[0] for x in con.execute("SELECT recipe_id FROM recipe_ingredients")}
    con.close()
    titled = {x[0] for x in rows if re.search(r"\bchickens?\b", A.fold(f"{x[1]} {x[2] or ''}"))}
    expect = tagged | titled
    assert len(tagged) == 4 and titled - tagged                                # the title adds untagged ones
    only = c.get("/api/recipes?ingredient=Chicken&limit=500").json
    assert {x["id"] for x in only["recipes"]} == expect and only["count"] == len(expect)
    assert "Bread soup" in {x["name"] for x in only["recipes"]}                # tagged, no "chicken" in the title
    soup = {x["id"] for x in rows if "soup" in A.fold(x[1])} & expect
    assert c.get("/api/recipes?ingredient=Chicken&q=soup&limit=500").json["count"] == len(soup)  # + words
    assert c.get("/api/recipes?q=chicken soup").json["count"] >= 1              # word via tag + word via title
    page2 = c.get("/api/recipes?ingredient=Chicken&limit=2&offset=2").json
    assert page2["count"] == len(expect) and len(page2["recipes"]) == 2
    per_book = {}
    for x in rows:
        if x[0] in expect:
            per_book[(x[3], x[4])] = per_book.get((x[3], x[4]), 0) + 1
    books = {b["title"] for b in c.get("/api/books?ingredient=Chicken").json["books"]}
    assert books == {t for _, t in per_book} and {"The Zuni Cafe Cookbook", "Pozole"} <= books
    # the per-book list shown when an ingredient is picked: titles A–Z ignoring "The", with counts
    bb = c.get("/api/recipes/books?ingredient=Chicken").json
    sort_key = lambda t: A.fold(re.sub(r"^(the|a|an)\s+", "", t, flags=re.I))
    assert [(b["title"], b["n"]) for b in bb["books"]] == sorted(((t, n) for (_, t), n in per_book.items()),
                                                                  key=lambda x: sort_key(x[0]))
    assert bb["count"] == len(expect)
    assert c.get(f"/api/recipes?ingredient=Chicken&book={zuni}&limit=500").json["count"] == per_book[(zuni, "The Zuni Cafe Cookbook")]
    assert c.get("/api/recipes/books").json == {"books": [], "count": 0}
    ing = c.get("/api/ingredients").json["ingredients"]
    assert ing == [{"id": ing[0]["id"], "name": "Chicken", "n": len(expect)}]  # the chip counts what it lists
    # importing again changes nothing
    r2 = up(c, "/admin/api/ingredients/commit", ingredient="Chicken", decisions=json.dumps(dec)).json
    assert r2["tagged"] == 0 and r2["added_recipes"] == 0 and r2["total"] == 4
    pv2 = up(c, "/admin/api/ingredients/preview", ingredient="Chicken").json
    assert sum(x["already"] for x in pv2["groups"]) == 4 and sum(x["new"] for x in pv2["groups"]) == 0
    # MCP search uses the same rule and says which tag it used
    res = A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "search_recipes", "arguments": {"ingredient": "Chicken", "limit": 100}}}).json["result"]["structuredContent"]
    assert res["count"] == len(expect) and res["ingredient_tags"] == ["Chicken"]
    # removing the tag keeps the recipes; the title still finds the ones named for chicken
    assert c.delete(f"/admin/api/ingredients/{ing[0]['id']}").status_code == 200
    assert c.get("/api/recipes?ingredient=Chicken&limit=500").json["count"] == len(titled)
    assert c.get("/api/recipes?q=bread soup").json["count"] == 1
    assert c.delete(f"/admin/api/ingredients/{ing[0]['id']}", headers={"Cf-Ray": "x"}).status_code == 401


def test_validation(c):
    assert up(c, "/admin/api/ingredients/commit", decisions="{}").status_code == 400            # no name
    shelf = b"Book Title,Author,ISBN,Book Format,Indexing Status\r\nX,Y, 9780306406157 ,Hardcover ,\r\n"
    assert up(c, "/admin/api/ingredients/preview", raw=shelf, ingredient="Lamb").status_code == 400


def test_real_sample_file(c):
    path = Path("/home/claude/eyb-in3/IngredientList.csv")
    if not path.exists():
        pytest.skip("sample not available")
    pv = up(c, "/admin/api/ingredients/preview", raw=path.read_bytes(), name="IngredientList.csv", ingredient="Chicken").json
    assert pv["total"] == 200 and len(pv["groups"]) > 40
