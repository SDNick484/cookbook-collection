"""Ingredient tags from EYB ingredient exports, and ingredient-aware search."""
import io, json, os, sys, tempfile
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
    only = c.get("/api/recipes?ingredient=Chicken&limit=500").json
    assert only["count"] == 4 and "Bread soup" in {x["name"] for x in only["recipes"]}
    assert c.get("/api/recipes?ingredient=Chicken&q=soup").json["count"] == 1   # combine ingredient + words
    assert c.get("/api/recipes?q=chicken soup").json["count"] >= 1              # word via tag + word via title
    page2 = c.get("/api/recipes?ingredient=Chicken&limit=2&offset=2").json
    assert page2["count"] == 4 and len(page2["recipes"]) == 2
    books = {b["title"] for b in c.get("/api/books?ingredient=Chicken").json["books"]}
    assert books == {"The Zuni Cafe Cookbook", "Pozole"}
    ing = c.get("/api/ingredients").json["ingredients"]
    assert ing == [{"id": ing[0]["id"], "name": "Chicken", "n": 4}]
    # importing again changes nothing
    r2 = up(c, "/admin/api/ingredients/commit", ingredient="Chicken", decisions=json.dumps(dec)).json
    assert r2["tagged"] == 0 and r2["added_recipes"] == 0 and r2["total"] == 4
    pv2 = up(c, "/admin/api/ingredients/preview", ingredient="Chicken").json
    assert sum(x["already"] for x in pv2["groups"]) == 4 and sum(x["new"] for x in pv2["groups"]) == 0
    # MCP search sees tags too
    res = A.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "search_recipes", "arguments": {"ingredient": "Chicken"}}}).json["result"]["structuredContent"]
    assert res["count"] == 4
    # removing the tag keeps the recipes
    assert c.delete(f"/admin/api/ingredients/{ing[0]['id']}").status_code == 200
    assert c.get("/api/recipes?ingredient=Chicken").json["count"] == 0
    assert c.get("/api/recipes?q=bread soup").json["count"] == 1
    assert c.delete(f"/admin/api/ingredients/{ing[0]['id']}", headers={"Cf-Ray": "x"}).status_code == 401


def test_validation(c):
    assert up(c, "/admin/api/ingredients/commit", decisions="{}").status_code == 400            # no name
    shelf = b"Book Title,Author,ISBN,Book Format,Indexing Status\r\nX,Y, 9780306406157 ,Hardcover ,\r\n"
    assert up(c, "/admin/api/ingredients/preview", raw=shelf, ingredient="Lamb").status_code == 400


def test_real_sample_file(c):
    path = Path(os.environ.get("EYB_SAMPLE_CSV", "/nonexistent/IngredientList.csv"))
    if not path.exists():
        pytest.skip("sample not available")
    pv = up(c, "/admin/api/ingredients/preview", raw=path.read_bytes(), name="IngredientList.csv", ingredient="Chicken").json
    assert pv["total"] == 200 and len(pv["groups"]) > 40
