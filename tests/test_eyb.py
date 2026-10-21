"""Eat Your Books import: parsing, matching, commit, re-import, and bulk cover lookup."""
import io, json, os, sys, tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import eyb as E  # noqa: E402

# Same shape as EYB's export: Windows-1252, %uXXXX for characters outside it, padded ISBNs, blank rows.
BOOKS = ("Book Title,Author,ISBN,Book Format,Indexing Status\r\n"
         "Deep Run Roots,Vivian Howard, 9780316381109                       ,Hardcover ,Indexed\r\n"
         "Mastering the Art of French Cooking Volume Two,Julia Child and Simone Beck, 9780394721774   ,Paperback ,Indexed\r\n"
         "Mastering the Art of French Cooking Volume One,Julia Child and Louisette Bertholle and Simone Beck, 9780394721781 ,Paperback ,Indexed\r\n"
         "Julia's Kitchen Wisdom,Julia Child and David Nussbaum, 9780375411519 ,Hardcover ,Indexed\r\n"
         "Complete Cook%u2019s Country TV Show Cookbook,America's Test Kitchen, 9781954210547 ,Hardcover ,\r\n"
         "Proven\xe7al Cookbook,Gui Gedda and Marie Pierre Moine, 9781405345873 ,Hardcover ,Indexed\r\n"
         "Burma - Rivers of Flavor,Naomi Duguid, 9781579654139 ,Hardcover ,Indexed\r\n"
         "Taste of Persia,Naomi Duguid, 9781579655488 ,Hardcover ,Indexed\r\n"
         "Miracle of Salt,Naomi Duguid, 9781579659448 ,Hardcover ,Indexed\r\n"
         "Hot Sour Salty Sweet,Jeffrey Alford and Naomi Duguid, 9781579651145 ,Hardcover ,Indexed\r\n"
         "Barefoot Contessa Foolproof,Ina Garten, 9780307464873 ,Hardcover ,Member Indexed\r\n"
         "Eat (Like) the Rich - A Cookbook,Olivia Tiedemann, 9780593837320 ,Hardcover ,Indexed\r\n"
         "Creuset: A Collection of Recipes from Our French Table,, 9780847860388 ,Hardcover ,Indexed\r\n"
         ",,,,\r\n").encode("cp1252")
RECIPES = ("Recipe Title,Book Title,Author,Page No.\r\n"
           "Apple pie cr\xe8me br\xfbl\xe9e,Eat (Like) the Rich - A Cookbook,Olivia Tiedemann,(page 257)\r\n"
           "Lamb chops,Eat (Like) the Rich - A Cookbook,Olivia Tiedemann,(page 204)\r\n"
           "Eggs Jeannette (%u0152ufs Jeannette),Let's Cook French a Family Cookbook,Claudine P\xe9pin,\r\n"
           "Collard kimchi,Deep Run Roots,Vivian Howard,(page 101)\r\n"
           ",,,\r\n").encode("cp1252")


@pytest.fixture()
def c():
    return A.app.test_client()


def upload(c, url, raw, **form):
    return c.post(url, data={"file": (io.BytesIO(raw), "x.csv"), **form}, content_type="multipart/form-data")


def test_parse_encoding_and_cleanup():
    kind, rows = E.parse(BOOKS)
    assert kind == "books" and len(rows) == 13
    assert rows[4]["title"] == "Complete Cook’s Country TV Show Cookbook"      # %u2019
    assert rows[5]["title"] == "Provençal Cookbook"                            # cp1252
    assert rows[0]["isbn"] == "9780316381109" and rows[0]["format"] == "Hardcover"
    assert rows[2]["authors"] == ["Julia Child", "Louisette Bertholle", "Simone Beck"]
    kind, rows = E.parse(RECIPES)
    assert kind == "recipes" and len(rows) == 4
    assert rows[0] == {"name": "Apple pie crème brûlée", "book_title": "Eat (Like) the Rich - A Cookbook",
                       "author": "Olivia Tiedemann", "page": "257"}
    assert rows[2]["name"] == "Eggs Jeannette (Œufs Jeannette)" and rows[2]["page"] is None
    with pytest.raises(Exception):
        E.parse(b"just,some,columns\n1,2,3\n")


def test_matching_rules():
    books = [{"id": i, "title": t, "authors": a, "isbn": None, "eyb_title": None, "status": "owned"} for i, (t, a) in enumerate([
        ("Roots Run Deep", ["Vivian Howard"]),
        ("Mastering the Art of French Cooking Vol 1", ["Julia Child"]),
        ("Mastering the Art of French Cooking Vol 2", ["Julia Child"]),
        ("From Julia Child's Kitchen", ["Julia Child"]),
        ("Complete Cooks Country Cookbook", ["America's Test Kitchen"]),
        ("The Provençal Cookbook", ["Pierre Moine"]),
        ("Burma", ["Naomi Duguid"]), ("Taste of Persia", ["Naomi Duguid"]), ("Salt", ["Naomi Duguid"]),
        ("Hot, Sour, Salty, Sweet", ["Naomi Duguid"]),
        ("The Barefoot Contessa At Home", ["Ina Garten"]), ("The Barefoot Contessa Family Style", ["Ina Garten"]),
        ("Le Creuset Cookbook", ["Sheri Castle"]),
    ], start=1)]
    _, rows = E.parse(BOOKS)
    got = {e["title"]: (b["title"] if b else None) for e, (b, _, _) in zip(rows, E.match_books(rows, books))}
    assert got["Deep Run Roots"] == "Roots Run Deep"
    assert got["Mastering the Art of French Cooking Volume One"].endswith("Vol 1")
    assert got["Mastering the Art of French Cooking Volume Two"].endswith("Vol 2")
    assert got["Julia's Kitchen Wisdom"] is None                    # different book by the same author
    assert got["Complete Cook’s Country TV Show Cookbook"] == "Complete Cooks Country Cookbook"
    assert got["Provençal Cookbook"] == "The Provençal Cookbook"
    assert got["Burma - Rivers of Flavor"] == "Burma"                # only remaining Duguid book
    assert got["Miracle of Salt"] == "Salt"
    assert got["Hot Sour Salty Sweet"] == "Hot, Sour, Salty, Sweet"
    assert got["Barefoot Contessa Foolproof"] is None                # two Garten candidates left: don't guess
    assert got["Creuset: A Collection of Recipes from Our French Table"] == "Le Creuset Cookbook"  # shared word
    assert len({v for v in got.values() if v}) == len([v for v in got.values() if v])  # one-to-one


def test_books_then_recipes_round_trip(c):
    ids = {}  # both are in the seeded catalog (from the Google Sheet)
    for t in ("Roots Run Deep", "Kids Cook French"):
        ids[t] = next(b["id"] for b in c.get("/admin/api/books?sort=title").json["books"] if b["title"] == t)
    pv = upload(c, "/admin/api/eyb/preview", BOOKS).json
    assert pv["kind"] == "books" and pv["total"] == 13
    row = next(x for x in pv["rows"] if x["eyb"]["title"] == "Deep Run Roots")
    assert row["match"]["id"] == ids["Roots Run Deep"] and row["retitle"] and row["retitle_default"]
    assert "ISBN" in row["fills"]
    dec = {str(x["i"]): (x["match"]["id"] if x["match"] else "create") for x in pv["rows"]}
    wisdom = next(x["i"] for x in pv["rows"] if x["eyb"]["title"] == "Julia's Kitchen Wisdom")
    dec[str(wisdom)] = "skip"  # e.g. a book EYB still lists but you no longer own
    res = upload(c, "/admin/api/eyb/commit", BOOKS, decisions=json.dumps(dec), retitle=json.dumps([row["i"]])).json
    assert res["retitled"] == 1 and res["skipped"] == 1 and res["updated"] + res["created"] == 12
    assert not c.get("/admin/api/books?q=kitchen wisdom").json["books"]
    b = c.get(f"/admin/api/books/{ids['Roots Run Deep']}").json
    assert b["title"] == "Deep Run Roots" and b["isbn"] == "9780316381109" and b["format"] == "Hardcover"
    assert b["eyb_status"] == "Indexed" and c.get(f"/api/books/{b['id']}").json["format"] == "Hardcover"
    new = next(x for x in c.get("/admin/api/books?q=eat like the rich").json["books"])
    assert new["isbn"] == "9780593837320" and "Eat Your Books" in new["needs_review"]
    # importing the same bookshelf again changes nothing and matches everything by ISBN
    pv2 = upload(c, "/admin/api/eyb/preview", BOOKS).json
    assert all(x["method"] == "ISBN" for x in pv2["rows"] if x["eyb"]["title"] != "Julia's Kitchen Wisdom")

    rp = upload(c, "/admin/api/eyb/preview", RECIPES).json
    groups = {g["eyb_title"]: g for g in rp["groups"]}
    assert groups["Eat (Like) the Rich - A Cookbook"]["match"]["id"] == new["id"]
    assert groups["Deep Run Roots"]["match"]["id"] == ids["Roots Run Deep"]
    assert groups["Let's Cook French a Family Cookbook"]["match"]["id"] == ids["Kids Cook French"]  # Claudine Pépin's only book here
    dec = {t: (g["match"]["id"] if g["match"] else "skip") for t, g in groups.items()}
    dec["Let's Cook French a Family Cookbook"] = ids["Kids Cook French"]
    r = upload(c, "/admin/api/eyb/commit", RECIPES, decisions=json.dumps(dec)).json
    assert r == {"kind": "recipes", "added": 4, "duplicates": 0, "skipped": 0, "books": 3, "created_books": 0}
    assert upload(c, "/admin/api/eyb/commit", RECIPES, decisions=json.dumps(dec)).json["duplicates"] == 4
    hits = c.get("/api/recipes?q=creme brulee").json["recipes"]
    assert hits[0]["name"] == "Apple pie crème brûlée" and hits[0]["page"] == "257"
    # the EYB title is remembered, so the next export page matches without asking
    rp2 = upload(c, "/admin/api/eyb/preview", RECIPES).json
    lcf = next(g for g in rp2["groups"] if g["eyb_title"].startswith("Let's Cook French"))
    assert lcf["match"]["id"] == ids["Kids Cook French"] and lcf["new"] == 0
    assert upload(c, "/admin/api/eyb/preview", b"nope\n1\n").status_code == 400
    assert c.post("/admin/api/eyb/preview", headers={"Cf-Ray": "x"}).status_code == 401


def test_enrich(c, monkeypatch):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 450), "#335").save(buf, "JPEG")
    bid = c.post("/admin/api/books", json={"title": "Enrich Me", "isbn": "0-306-40615-2"})
    if bid.status_code == 409:  # ISBN used by another test module: pick a fresh valid one
        bid = c.post("/admin/api/books", json={"title": "Enrich Me", "isbn": "9781408802526"})
    bid = bid.json["id"]
    calls = []
    monkeypatch.setattr(A, "ol_by_isbn", lambda isbn: calls.append(isbn) or {
        "title": "x", "year": 2011, "publisher": "Norton", "subtitle": None,
        "cover_url": f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"})
    monkeypatch.setattr(A, "google_by_isbn", lambda isbn: None)
    monkeypatch.setattr(A, "http_get", lambda url, binary=False, timeout=10: buf.getvalue())
    before = c.get("/admin/api/enrich").json["remaining"]
    assert before >= 1
    seen = set()
    for _ in range(100):
        r = c.post("/admin/api/enrich", json={"limit": 20}).json
        seen |= {x["id"] for x in r["results"]}
        if not r["remaining"] or not r["results"]:
            break
    assert bid in seen
    b = c.get(f"/admin/api/books/{bid}").json
    assert b["cover"] and b["year"] == 2011 and b["publisher"] == "Norton" and b["title"] == "Enrich Me"
    # nothing is retried immediately
    assert c.post("/admin/api/enrich", json={"limit": 20}).json["results"] == []


def test_combined_export_and_sheet_rewordings(c):
    zuni = next(b["id"] for b in c.get("/admin/api/books?q=zuni cafe").json["books"])
    raw = ("Recipe Title,Book Title,Author,Page No.\r\n"
           "Roast chicken with bread salad,Zuni Cafe Cookbook,Judy Rodgers,(page 342)\r\n"   # re-words the sheet's starred one
           "Zuni Caesar salad,Zuni Cafe Cookbook,Judy Rodgers,(page 154)\r\n"                # same name as the sheet's
           "Recipe Title,Book Title,Author,Page No.\r\n"                                       # header from a 2nd export
           "Poached eggs with duxelles,Zuni Cafe Cookbook,Judy Rodgers,(page 82)\r\n"
           "Poached eggs with creamed spinach,Zuni Cafe Cookbook,Judy Rodgers,(page 82)\r\n"  # close variant, same page: keep
           "Roast chicken with bread salad,Zuni Cafe Cookbook,Judy Rodgers,(page 342)\r\n"   # repeated across exports
           "Bread salad,Zuni Cafe Cookbook,Judy Rodgers,(page 343)\r\n").encode("cp1252")   # different page: keep
    kind, rows = E.parse(raw)
    assert len(rows) == 6 and all(r["book_title"] != "Book Title" for r in rows)
    pv = upload(c, "/admin/api/eyb/preview", raw).json
    g = next(g for g in pv["groups"] if g["eyb_title"] == "Zuni Cafe Cookbook")
    assert g["count"] == 6 and g["new"] == 3
    r = upload(c, "/admin/api/eyb/commit", raw, decisions=json.dumps({"Zuni Cafe Cookbook": zuni})).json
    assert r["added"] == 3 and r["duplicates"] == 3
    recs = c.get(f"/admin/api/books/{zuni}").json["recipes"]
    p342 = [x for x in recs if x["page"] == "342"]
    assert len(p342) == 1 and p342[0]["want_to_try"] == 1          # the starred sheet recipe is kept, not duplicated
    assert len([x for x in recs if x["page"] == "82"]) == 2


def test_recipe_book_matching_and_create(c):
    books = [{"id": i, "title": t, "authors": a, "isbn": None, "eyb_title": None, "status": "owned"} for i, (t, a) in enumerate([
        ("Home Cheesemaking", ["Ricki Carroll"]), ("Asada: The Art of Mexican-Style Grilling", ["Bricia Lopez"]),
        ("Pozole", ["Steve Sando"]), ("The Bean Book", ["Steve Sando"]), ("Kids Cook French", ["Claudine Pépin"]),
        ("Essential Pépin", ["Jacques Pépin"]), ("A Grandfather's Lessons", ["Jacques Pépin"]),
        ("Splendid Soups", ["James Peterson"]), ("The Pho Cookbook", ["Andrea Nguyen"])], start=1)]
    groups = {t: {"author": a} for t, a in [
        ("Home Cheese Making", "Ricki Carroll"), ("Asada", "Bricia Lopez"), ("The Rancho Gordo Pozole Book", "Steve Sando"),
        ("Let's Cook French a Family Cookbook", "Claudine Pépin"), ("Sauces", "James Peterson"),
        ("The Little Book of Soups", "Steve Sando"), ("Vietnamese Food Any Day", "Andrea Nguyen")]}
    got = {t: (b["title"] if b else None) for t, (b, _) in E.match_recipe_books(groups, books).items()}
    assert got == {"Home Cheese Making": "Home Cheesemaking", "Asada": "Asada: The Art of Mexican-Style Grilling",
                   "The Rancho Gordo Pozole Book": "Pozole", "Let's Cook French a Family Cookbook": "Kids Cook French",
                   "Sauces": None, "The Little Book of Soups": None, "Vietnamese Food Any Day": None}

    raw = ("Recipe Title,Book Title,Author,Page No.\r\n"
           "Pork adobada,Persiana,Sabrina Ghayour,(page 12)\r\n"
           "Saffron rice,Persiana,Sabrina Ghayour,(page 40)\r\n").encode()
    r = upload(c, "/admin/api/eyb/commit", raw, decisions=json.dumps({"Persiana": "create"})).json
    assert r["added"] == 2 and r["created_books"] == 1
    b = c.get("/admin/api/books?q=persiana").json["books"][0]
    assert b["authors"] == ["Sabrina Ghayour"] and b["recipe_count"] == 2 and "Eat Your Books" in b["needs_review"]
    pv = upload(c, "/admin/api/eyb/preview", raw).json   # next time it's matched, nothing new
    assert pv["groups"][0]["match"]["id"] == b["id"] and pv["groups"][0]["new"] == 0
