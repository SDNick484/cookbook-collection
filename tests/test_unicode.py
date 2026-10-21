"""Accents, special letters and non-Latin scripts: search, matching, imports and export."""
import os, sqlite3, sys, tempfile, unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A  # noqa: E402
import eyb  # noqa: E402


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture()
def book(c):
    bid = c.post("/admin/api/books", json={"title": "Smørrebrød & Œufs", "authors": ["Unicode Test Author"]}).json["id"]
    yield bid
    c.delete(f"/admin/api/books/{bid}")


def test_fold():
    assert A.fold("Crème Brûlée") == "creme brulee"
    assert A.fold("Œufs Smørrebrød Straße Łódź Ærter") == "oeufs smorrebrod strasse lodz aerter"
    assert A.fold(unicodedata.normalize("NFD", "Pépin")) == A.fold("Pépin") == "pepin"
    assert A.fold(None) == ""


def test_clean():
    assert A.clean(unicodedata.normalize("NFD", " Pépin ")) == "Pépin"  # composed, so it compares equal
    assert A.clean("ﬁsh and ﬂatbread") == "fish and flatbread"  # ligatures from PDFs/OCR
    assert A.clean("  ") is None


def rec_names(c, q):
    return {r["name"] for r in c.get("/api/recipes", query_string={"q": q}).json["recipes"]}


@pytest.mark.parametrize("q,name", [
    ("creme brulee", "Crème brûlée"), ("CRÈME", "Crème brûlée"), ("oeufs", "Œufs en meurette"),
    ("œufs", "Œufs en meurette"), ("smorrebrod", "Smørrebrød with herring"), ("strasse", "Straße bread"),
    ("烧肉", "红烧肉"), ("红烧", "红烧肉"), ("ラーメン", "味噌ラーメン"), ("김치", "김치찌개"),
])
def test_recipe_search(c, book, q, name):
    for n in ("Crème brûlée", "Œufs en meurette", "Smørrebrød with herring", "Straße bread", "红烧肉", "味噌ラーメン", "김치찌개"):
        c.post(f"/admin/api/books/{book}/recipes", json={"name": n})
    assert name in rec_names(c, q)


def test_recipe_rename_and_delete_keep_index_right(c, book):
    rid = c.post(f"/admin/api/books/{book}/recipes", json={"name": "Æbleskiver"}).json["id"]
    assert "Æbleskiver" in rec_names(c, "aebleskiver")
    c.put(f"/admin/api/recipes/{rid}", json={"name": "Pfannkuchen"})
    assert not rec_names(c, "aebleskiver") and "Pfannkuchen" in rec_names(c, "pfannkuchen")
    c.delete(f"/admin/api/recipes/{rid}")
    assert not rec_names(c, "pfannkuchen")


def test_book_search(c, book):
    titles = lambda q: {b["title"] for b in c.get("/api/books", query_string={"q": q}).json["books"]}
    assert "Smørrebrød & Œufs" in titles("smorrebrod oeufs")
    assert any("Pépin" in a for b in c.get("/api/books?q=pepin").json["books"] for a in b["authors"])


def test_authors_and_tags_match_without_accents(c):
    a = c.post("/admin/api/books", json={"title": "Accent A", "authors": ["Jose Andres"], "tags": ["Espana"]}).json
    b = c.post("/admin/api/books", json={"title": "Accent B", "authors": [unicodedata.normalize("NFD", "José Andrés")],
                                         "tags": ["España"]}).json
    # one author and one tag, now spelled with the accents
    assert a["id"] != b["id"] and b["authors"] == ["José Andrés"] and b["tags"] == ["España"]
    assert c.get(f"/api/books/{a['id']}").json["authors"] == ["José Andrés"]
    facets = c.get("/api/facets").json
    assert sum(1 for x in facets["authors"] if A.fold(x["name"]) == "jose andres") == 1
    # sorted with E, not after Z
    names = [x["name"] for x in facets["tags"]]
    assert names == sorted(names, key=A.fold)
    for x in (a, b):
        c.delete(f"/admin/api/books/{x['id']}")


def test_ingredient_accents(c, book):
    rid = c.post(f"/admin/api/books/{book}/recipes", json={"name": "Stuffed peppers"}).json["id"]
    con = A.connect()
    iid = A.name_id(con, "ingredients", "Jalapeño")
    con.execute("INSERT INTO recipe_ingredients VALUES (?,?)", (rid, iid))
    con.commit()
    assert A.name_id(con, "ingredients", "jalapeno") == iid
    con.close()
    for q in ("jalapeno", "JALAPEÑO"):
        assert "Stuffed peppers" in rec_names(c, q)
    # the ingredient filter is lenient too, so a link typed without the accent works
    assert c.get("/api/recipes?ingredient=jalapeno").json["count"] >= 1
    c.delete(f"/admin/api/ingredients/{iid}")


def test_eyb_decode():
    assert eyb.decode("Crème brûlée".encode("utf-8")) == "Crème brûlée"
    assert eyb.decode("Crème brûlée".encode("cp1252")) == "Crème brûlée"
    # UTF-8 opened as Windows-1252 and saved again
    mangled = "Crème brûlée, Cook’s Œufs".encode("utf-8").decode("cp1252").encode("utf-8")
    assert eyb.decode(mangled) == "Crème brûlée, Cook’s Œufs"
    assert eyb.decode(unicodedata.normalize("NFD", "Pépin").encode()) == "Pépin"
    assert eyb.decode("Ãbout".encode()) == "Ãbout"  # only repaired when it cleanly reverses


def test_old_index_is_migrated(tmp_path, monkeypatch):
    """A database from before folding: the recipe index read text straight from the recipes table."""
    db = tmp_path / "cookbooks.db"
    con = sqlite3.connect(db)
    con.executescript(A.SCHEMA)
    con.executescript("""
CREATE VIRTUAL TABLE recipes_fts USING fts5(name, heading, content='recipes', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER recipes_ai AFTER INSERT ON recipes BEGIN
  INSERT INTO recipes_fts(rowid, name, heading) VALUES (new.id, new.name, coalesce(new.heading,''));
END;""")
    con.execute("INSERT INTO books(id, title) VALUES (1, 'Old')")
    con.execute("INSERT INTO recipes(book_id, name) VALUES (1, 'Œufs cocotte')")
    con.commit()
    con.close()
    monkeypatch.setattr(A, "DB_PATH", db)
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    A.init_db()
    con = A.connect()
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='recipes_fts'").fetchone()[0]
    assert "content=" not in sql
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='recipes_ai'").fetchone()
    assert con.execute("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'oeufs'").fetchone()
    con.execute("INSERT INTO recipes(book_id, name) VALUES (1, 'Smørrebrød')")
    assert con.execute("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'smorrebrod'").fetchone()
    con.close()


def test_csv_export_opens_in_excel(c):
    assert c.get("/admin/api/export.csv").data.startswith(b"\xef\xbb\xbf")
