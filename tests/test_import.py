"""The JSON export is a portable backup: export.json -> importer -> the same catalog.

Run: python3 -m pytest -q tests/test_import.py  (conftest.py gives this module its own seeded temp DATA_DIR)
"""
import re

import pytest

import app as A


@pytest.fixture()
def client():
    return A.app.test_client()


def _tag_a_recipe(name="Smoky tomato soup", ingredients=("Tomatoes", "Smoked paprika"), hints=("Paprika",)):
    """One extra recipe carrying ingredient tags, and some tags that are only search hints."""
    con = A.connect()
    bid = con.execute("SELECT id FROM books ORDER BY id LIMIT 1").fetchone()[0]
    rid = con.execute("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?,?,?,?)",
                      (bid, name, "12", 12)).lastrowid
    for n in ingredients:
        iid = A.name_id(con, "ingredients", n)
        con.execute("INSERT OR IGNORE INTO recipe_ingredients VALUES (?,?)", (rid, iid))
    for n in hints:
        iid = A.name_id(con, "ingredients", n)
        con.execute("UPDATE ingredients SET hint=1 WHERE id=?", (iid,))
    con.commit()
    con.close()
    return rid


# ------------------------------------------------------------------ export format
def test_export_has_version_and_timestamp(client):
    j = client.get("/admin/api/export.json").json
    assert j["version"] == A.EXPORT_VERSION == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", j["exported_at"])
    assert "exported" in j  # the older key stays, so nothing that reads it breaks


def test_export_carries_ingredient_tags(client):
    _tag_a_recipe()
    j = client.get("/admin/api/export.json").json
    recipes = [r for b in j["books"] for r in b["recipes"]]
    soup = next(r for r in recipes if r["name"] == "Smoky tomato soup")
    assert soup["ingredients"] == ["Smoked paprika", "Tomatoes"]
    assert all(isinstance(r["ingredients"], list) for r in recipes)  # [] = untagged, not "old export"
    assert "Paprika" in j["ingredient_hints"]


# ------------------------------------------------------------------ an empty catalog to restore into
@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    """A second, unseeded catalog in its own folder; the module's seeded one is put back afterwards."""
    import photos
    monkeypatch.setenv("SKIP_SEED", "1")
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    monkeypatch.setattr(photos, "PHOTO_DIR", tmp_path / "photos")
    A.init_db()
    return tmp_path


def test_skip_seed_leaves_a_new_database_empty(empty_db):
    con = A.connect()
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] == 0
    con.close()


def test_a_new_database_is_seeded_by_default(tmp_path, monkeypatch):
    import photos
    monkeypatch.delenv("SKIP_SEED", raising=False)
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "cookbooks.db")
    monkeypatch.setattr(A, "COVER_DIR", tmp_path / "covers")
    monkeypatch.setattr(photos, "PHOTO_DIR", tmp_path / "photos")
    A.init_db()
    con = A.connect()
    assert con.execute("SELECT count(*) FROM books").fetchone()[0] > 100
    con.close()
