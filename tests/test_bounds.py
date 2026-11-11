"""Bounded inputs and outputs: image uploads decode only as the formats phones produce, MCP get_book pages
its recipes, a JSON-RPC batch has a size cap, and Ask cuts an oversized tool result at an item boundary."""
import io
import json

import pytest
from PIL import Image

import app as A
import ask
import mcp as MC
import scan as SC

LAN = dict(base_url="http://127.0.0.1:8767", environ_base={"REMOTE_ADDR": "127.0.0.1"})


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(MC, "MCP_PUBLIC", True)
    return A.app.test_client()


def image_bytes(fmt, size=(80, 120)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


# ------------------------------------------------------------------ image formats
@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_open_image_accepts_phone_formats(fmt):
    assert A.open_image(image_bytes(fmt)).size == (80, 120)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "GIF", "PPM"])
def test_open_image_refuses_other_decoders(fmt):
    with pytest.raises(ValueError, match="not a JPEG, PNG, WebP or HEIC image"):
        A.open_image(image_bytes(fmt))


def test_open_image_refuses_garbage():
    with pytest.raises(ValueError):
        A.open_image(b"8BPS" + b"\0" * 200)  # a PSD signature with nothing behind it


def test_cover_and_photo_uploads_use_the_allow_list(c):
    bid = A.connect().execute("SELECT id FROM books ORDER BY id LIMIT 1").fetchone()[0]
    r = c.post(f"/admin/api/books/{bid}/cover", data={"file": (io.BytesIO(image_bytes("BMP")), "x.bmp")}, **LAN)
    assert r.status_code == 400 and "not a JPEG, PNG, WebP or HEIC image" in r.json["error"]
    r = c.post(f"/admin/api/books/{bid}/cover", data={"file": (io.BytesIO(image_bytes("PNG")), "x.png")}, **LAN)
    assert r.status_code == 200 and r.json["cover"]
    rid = A.connect().execute("SELECT id FROM recipes WHERE book_id=? LIMIT 1", (bid,)).fetchone()
    if rid:
        r = c.post(f"/admin/api/recipes/{rid[0]}/photos", data={"file": (io.BytesIO(image_bytes("TIFF")), "x.tif")}, **LAN)
        assert r.status_code == 400


def test_index_photo_uses_the_allow_list(monkeypatch):
    with pytest.raises(ValueError):
        SC.prepare_photo(image_bytes("BMP"))
    assert SC.prepare_photo(image_bytes("JPEG", (400, 600)))[:2] == b"\xff\xd8"


# ------------------------------------------------------------------ get_book paging
def rpc(c, method, params):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json


def call(c, name, args):
    return rpc(c, "tools/call", {"name": name, "arguments": args})["result"]["structuredContent"]


@pytest.fixture()
def big_book():
    con = A.connect()
    bid = con.execute("INSERT INTO books(title) VALUES ('Big index')").lastrowid
    con.executemany("INSERT INTO recipes(book_id, name, page, page_sort) VALUES (?,?,?,?)",
                    [(bid, f"Recipe {i:04}", str(i), i) for i in range(1, 451)])
    con.commit()
    con.close()
    yield bid
    con = A.connect()
    con.execute("DELETE FROM books WHERE id=?", (bid,))
    con.commit()
    con.close()


def test_get_book_pages_recipes(c, big_book):
    first = call(c, "get_book", {"book_id": big_book})
    assert first["recipe_count"] == 450 and len(first["recipes"]) == MC.GET_BOOK_LIMIT == 200
    assert first["recipes"][0]["name"] == "Recipe 0001" and first["next_offset"] == 200 and "offset" not in first
    second = call(c, "get_book", {"book_id": big_book, "offset": first["next_offset"]})
    assert second["offset"] == 200 and second["recipes"][0]["name"] == "Recipe 0201" and second["next_offset"] == 400
    last = call(c, "get_book", {"book_id": big_book, "offset": 400})
    assert len(last["recipes"]) == 50 and "next_offset" not in last
    clamped = call(c, "get_book", {"book_id": big_book, "limit": 10_000})  # limit clamps to GET_BOOK_MAX (500)
    assert MC.GET_BOOK_MAX == 500 and len(clamped["recipes"]) == 450 and "next_offset" not in clamped
    small = call(c, "get_book", {"book_id": big_book, "limit": 5, "offset": 3})
    assert [r["name"] for r in small["recipes"]] == [f"Recipe {i:04}" for i in range(4, 9)] and small["next_offset"] == 8


def test_get_book_on_a_small_book_is_unchanged(c):
    bid = A.connect().execute("SELECT book_id FROM recipes GROUP BY book_id ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
    out = call(c, "get_book", {"book_id": bid})
    assert len(out["recipes"]) == out["recipe_count"] and "next_offset" not in out and "offset" not in out


def test_get_book_schema_declares_paging(c):
    tools = {t["name"]: t for t in rpc(c, "tools/list", {})["result"]["tools"]}
    props = tools["get_book"]["inputSchema"]["properties"]
    assert props["limit"]["maximum"] == MC.GET_BOOK_MAX and props["limit"]["default"] == MC.GET_BOOK_LIMIT
    assert props["offset"]["minimum"] == 0 and "next_offset" in tools["get_book"]["description"]


# ------------------------------------------------------------------ batch cap
def test_batch_cap(c):
    one = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    ok = c.post("/mcp", json=[dict(one, id=i) for i in range(MC.MCP_BATCH_MAX)])
    assert ok.status_code == 200 and len(ok.json) == MC.MCP_BATCH_MAX
    too_many = c.post("/mcp", json=[dict(one, id=i) for i in range(MC.MCP_BATCH_MAX + 1)])
    assert too_many.status_code == 400
    assert too_many.json["error"]["code"] == -32600 and str(MC.MCP_BATCH_MAX) in too_many.json["error"]["message"]


# ------------------------------------------------------------------ Ask: structural truncation
def test_shrink_cuts_lists_at_item_boundaries(monkeypatch):
    monkeypatch.setattr(ask, "MAX_TOOL_RESULT_CHARS", 600)
    res = {"total": 300, "count": 100, "offset": 25, "recipes": [{"id": i, "name": f"Recipe {i}"} for i in range(100)]}
    out = json.loads(ask.shrink(res))  # valid JSON
    assert out["truncated"] is True and 0 < len(out["recipes"]) < 100
    assert out["next_offset"] == 25 + len(out["recipes"])
    assert out["recipes"] == res["recipes"][:len(out["recipes"])] and out["total"] == 300
    assert len(ask.shrink(res)) <= 600


def test_shrink_leaves_small_results_and_falls_back_for_blobs(monkeypatch):
    monkeypatch.setattr(ask, "MAX_TOOL_RESULT_CHARS", 600)
    small = {"recipes": [{"id": 1, "name": "Soup"}]}
    assert json.loads(ask.shrink(small)) == small
    blob = ask.shrink({"blob": "x" * 5000})
    assert blob.endswith(ask.TRUNCATED_NOTE) and len(blob) == 600 + len(ask.TRUNCATED_NOTE)
    huge_item = ask.shrink({"recipes": [{"id": 1, "notes": "y" * 5000}]})  # one item that can't fit: text cut
    assert huge_item.endswith(ask.TRUNCATED_NOTE)
