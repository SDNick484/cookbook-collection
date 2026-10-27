"""Run: python3 -m pytest -q tests  (from the cookbooks folder)"""
import io, json, os, shutil, sys, tempfile, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.environ.update(DATA_DIR=str(TMP), CF_TEAM_DOMAIN="team.example.com", CF_AUD="aud123",
                  ADMIN_EMAILS="alice@example.com,bob@example.com", LAN_EDIT="1")
import app as A  # noqa: E402

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}


@pytest.fixture()
def c():
    return A.app.test_client()


# ------------------------------------------------------------- index parser
def test_parse_index_shapes():
    text = """INDEX
A
Almonds, toasted, 72
Chicken
  braised with figs, honey, and vinegar, 350
  roast, with bread salad, 342–344
Chickpeas, 100
    and Swiss chard, with yogurt, 100
• lamb meatballs, 204
Zuni Caesar Salad 154
Onion Soup with Tomato & Poached Egg, 159, 161
Potatoes. See also Fries
Garbure, la (see Soups)
"""
    rows = A.parse_index(text)
    names = [(r["heading"], r["name"], r["page"]) for r in rows]
    assert (None, "Almonds, toasted", "72") in names
    assert ("Chicken", "braised with figs, honey, and vinegar", "350") in names
    assert ("Chicken", "roast, with bread salad", "342–344") in names
    assert (None, "Chickpeas", "100") in names
    assert ("Chickpeas", "and Swiss chard, with yogurt", "100") in names
    assert ("Chickpeas", "lamb meatballs", "204") in names  # bullet => sub-entry
    assert (None, "Zuni Caesar Salad", "154") in names
    assert (None, "Onion Soup with Tomato & Poached Egg", "159, 161") in names
    assert not any(r["name"].lower() == "index" for r in rows)
    assert not any("Garbure" in r["name"] for r in rows)  # no page => heading only


def test_isbn():
    assert A.normalize_isbn("0-306-40615-2") == "9780306406157"
    assert A.normalize_isbn("978-0-306-40615-7") == "9780306406157"
    assert A.normalize_isbn("9780306406158") is None  # bad checksum
    assert A.normalize_isbn("hello") is None


# ------------------------------------------------------------- read API
def test_public_hides_private(c):
    r = c.get("/admin/api/recipes?want=1&limit=500")
    rid = r.json["recipes"][0]["id"]
    bid = r.json["recipes"][0]["book_id"]
    pub = c.get(f"/api/books/{bid}").json
    assert all(set(x) <= {"id", "book_id", "name", "heading", "page"} for x in pub["recipes"])
    assert "private_notes" not in pub and "location" not in pub
    priv = c.get(f"/admin/api/books/{bid}").json
    assert any(x["id"] == rid and x["want_to_try"] == 1 for x in priv["recipes"])
    # public can't ask for the want list
    assert c.get("/api/recipes?want=1").json["count"] == 0


def test_search(c):
    assert [b["title"] for b in c.get("/api/books?q=zuni").json["books"]] == ["The Zuni Cafe Cookbook"]
    assert len(c.get("/api/books?q=pepin").json["books"]) >= 6  # Pépin, accent-insensitive
    assert len(c.get("/api/books?cuisine=French").json["books"]) > 10
    assert c.get("/api/books?tag=Kids").json["books"]
    r = c.get("/api/recipes?q=roast chick").json
    assert r["count"] >= 2 and all("rating" not in x for x in r["recipes"])


# ------------------------------------------------------------- auth
def test_cloudflare_without_jwt_is_rejected(c):
    assert c.get("/admin/api/me", headers=CF).status_code == 401
    assert c.post("/admin/api/books", json={"title": "x"}, headers=CF).status_code == 401
    assert c.get("/api/me", headers=CF).json == {"editor": False}


def _jwt_setup(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": key.public_key()})()
    monkeypatch.setattr(A, "_jwks", FakeJWKS())

    def make(email, aud="aud123", iss="https://team.example.com", exp=3600):
        return jwt.encode({"email": email, "aud": aud, "iss": iss, "exp": int(time.time()) + exp}, key, algorithm="RS256")
    return make


def test_access_jwt(c, monkeypatch):
    make = _jwt_setup(monkeypatch)
    ok = c.get("/admin/api/me", headers={**CF, "Cf-Access-Jwt-Assertion": make("Bob@example.com")})
    assert ok.status_code == 200 and ok.json["email"] == "bob@example.com"
    for bad in (make("stranger@example.com"), make("alice@example.com", aud="other"),
                make("alice@example.com", iss="https://evil.example.com"), make("alice@example.com", exp=-10)):
        assert c.get("/admin/api/me", headers={**CF, "Cf-Access-Jwt-Assertion": bad}).status_code == 401
    # a bogus token without Cloudflare headers must not fall back to LAN access
    assert c.get("/admin/api/me", headers={"Cf-Access-Jwt-Assertion": "garbage"}).status_code == 401


def test_lan_edit_toggle(c, monkeypatch):
    assert c.get("/admin/api/me").status_code == 200
    monkeypatch.setattr(A, "LAN_EDIT", False)
    assert c.get("/admin/api/me").status_code == 401


# ------------------------------------------------------------- writes
def fake_http(responses):
    def get(url, binary=False, timeout=10):
        for k, v in responses.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise RuntimeError("unexpected url " + url)
    return get


def cover_bytes():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (300, 450), "#884422").save(b, "JPEG")
    return b.getvalue()


def test_isbn_lookup_and_create(c, monkeypatch):
    isbn = "9780306406157"
    monkeypatch.setattr(A, "http_get", fake_http({
        "openlibrary.org/api/books": {f"ISBN:{isbn}": {
            "title": "Ottolenghi Test", "authors": [{"name": "Yotam Ottolenghi"}],
            "publish_date": "Oct 2017"}},  # no publisher: Google should fill it
        "googleapis.com": {"items": [{"volumeInfo": {"title": "Ottolenghi Test", "subtitle": "A Subtitle",
                                                     "publisher": "Ten Speed Press"}}]},
        "covers.openlibrary.org": cover_bytes(),
    }))
    r = c.get("/admin/api/lookup/isbn/0-306-40615-2").json
    res = r["result"]
    assert res["title"] == "Ottolenghi Test" and res["year"] == 2017 and res["publisher"] == "Ten Speed Press"
    assert res["subtitle"] == "A Subtitle"  # gap filled from Google
    body = {"title": res["title"], "authors": res["authors"], "isbn": isbn, "year": res["year"],
            "cuisine": "Middle Eastern", "tags": ["Vegetarian", "New Tag"], "cover_url": res["cover_url"],
            "private_notes": "gift from mom", "location": "Kitchen shelf 2"}
    made = c.post("/admin/api/books", json=body)
    assert made.status_code == 201, made.json
    b = made.json
    assert b["cover"] and (TMP / "covers" / b["cover"]).exists()
    assert "New Tag" in b["tags"] and b["authors"] == ["Yotam Ottolenghi"]
    # duplicate ISBN
    dup = c.post("/admin/api/books", json={**body, "title": "Again"})
    assert dup.status_code == 409 and dup.json["book_id"] == b["id"]
    assert c.get("/admin/api/lookup/isbn/" + isbn).json["existing"]["id"] == b["id"]
    # searchable immediately, private fields hidden publicly
    assert any(x["id"] == b["id"] for x in c.get("/api/books?q=ottolenghi test").json["books"])
    pub = c.get(f"/api/books/{b['id']}").json
    assert "private_notes" not in pub and pub["cover"] == b["cover"]


def test_cover_host_allowlist(c):
    bid = c.post("/admin/api/books", json={"title": "Host test"}).json["id"]
    r = c.put(f"/admin/api/books/{bid}", json={"cover_url": "http://169.254.169.254/latest/meta-data"})
    assert r.status_code == 502 and "not allowed" in r.json["error"]


def test_update_upload_and_delete(c):
    bid = c.post("/admin/api/books", json={"title": "Temp", "authors": ["Solo Author Unique"]}).json["id"]
    r = c.put(f"/admin/api/books/{bid}", json={"title": "Temp 2", "needs_review": "", "signed": True})
    assert r.json["title"] == "Temp 2" and r.json["signed"] == 1 and r.json["needs_review"] is None
    up = c.post(f"/admin/api/books/{bid}/cover", data={"file": (io.BytesIO(cover_bytes()), "c.jpg")},
                content_type="multipart/form-data")
    assert up.status_code == 200 and up.json["cover"]
    bad = c.post(f"/admin/api/books/{bid}/cover", data={"file": (io.BytesIO(b"nope"), "c.jpg")},
                 content_type="multipart/form-data")
    assert bad.status_code == 400
    assert c.put(f"/admin/api/books/{bid}", json={"isbn": "123"}).status_code == 400
    cover = up.json["cover"]
    assert c.delete(f"/admin/api/books/{bid}").status_code == 200
    assert not (TMP / "covers" / cover).exists()
    assert c.get(f"/api/books/{bid}").status_code == 404
    assert not any(a["name"] == "Solo Author Unique" for a in c.get("/api/facets").json["authors"])


def test_book_rating(c):
    bid = c.post("/admin/api/books", json={"title": "Rated Book", "rating": "4.5"}).json["id"]
    assert c.get(f"/admin/api/books/{bid}").json["rating"] == 4.5
    for v in (0, 0.5, 5, "3"):
        assert c.put(f"/admin/api/books/{bid}", json={"rating": v}).json["rating"] == float(v)
    for bad in (5.5, -0.5, 3.3, "lots", "nan"):
        assert c.put(f"/admin/api/books/{bid}", json={"rating": bad}).status_code == 400
    assert c.get(f"/admin/api/books/{bid}").json["rating"] == 3
    # editors only
    assert "rating" not in c.get(f"/api/books/{bid}").json
    assert all("rating" not in b for b in c.get("/api/books").json["books"])
    # top rated first, unrated last; public can't sort by it
    other = c.post("/admin/api/books", json={"title": "Better Book", "rating": 5}).json["id"]
    ids = [b["id"] for b in c.get("/admin/api/books?sort=rating").json["books"]]
    assert ids.index(other) < ids.index(bid) and ids[-1] not in (other, bid)
    assert c.get("/admin/api/books?sort=rating").json["books"][-1]["rating"] is None
    assert "rating" in c.get("/admin/api/export.csv").data.decode().splitlines()[0].split(",")
    # clearing it, and saving the edit form unrated
    assert c.put(f"/admin/api/books/{bid}", json={"rating": None}).json["rating"] is None
    assert c.put(f"/admin/api/books/{other}", json={"rating": ""}).json["rating"] is None
    for x in (bid, other):
        c.delete(f"/admin/api/books/{x}")


def test_recipes_and_index_import(c):
    bid = c.post("/admin/api/books", json={"title": "Index Book"}).json["id"]
    text = "Chicken\n  roast, 42\n  braised, 50\nBraised Fennel, 255\n"
    rows = c.post(f"/admin/api/books/{bid}/index/parse", json={"text": text}).json["rows"]
    assert len(rows) == 3 and not any(r["duplicate"] for r in rows)
    r = c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": rows}).json
    assert r == {"added": 3, "skipped": 0}
    again = c.post(f"/admin/api/books/{bid}/index/parse", json={"text": text}).json["rows"]
    assert all(x["duplicate"] for x in again)
    assert c.post(f"/admin/api/books/{bid}/index/commit", json={"rows": again}).json == {"added": 0, "skipped": 3}
    # FTS sees the heading
    hits = c.get("/api/recipes?q=chicken braised").json["recipes"]
    assert any(h["book_id"] == bid and h["name"] == "braised" for h in hits)
    # page order
    names = [x["name"] for x in c.get(f"/api/books/{bid}").json["recipes"]]
    assert names == ["roast", "braised", "Braised Fennel"]
    # manual recipe + edit
    rec = c.post(f"/admin/api/books/{bid}/recipes", json={"name": "Pan sauce", "page": "12", "rating": "8.5"}).json
    assert rec["rating"] == 8.5 and rec["page_sort"] == 12
    upd = c.put(f"/admin/api/recipes/{rec['id']}", json={"want_to_try": True, "notes": "try with shallots", "name": "Pan Sauce"}).json
    assert upd["want_to_try"] == 1 and upd["name"] == "Pan Sauce"
    assert any(x["name"] == "Pan Sauce" for x in c.get("/api/recipes?q=pan sauce").json["recipes"])
    assert c.put(f"/admin/api/recipes/{rec['id']}", json={"rating": 11}).status_code == 400
    assert c.delete(f"/admin/api/recipes/{rec['id']}").status_code == 200
    assert not c.get("/api/recipes?q=pan sauce").json["recipes"]


def test_export(c):
    j = json.loads(c.get("/admin/api/export.json").data)
    assert len(j["books"]) >= 153 and any(b["recipes"] for b in j["books"])
    csv_text = c.get("/admin/api/export.csv").data.decode()
    assert csv_text.startswith("\ufeffid,title")  # BOM so Excel reads it as UTF-8
    assert c.get("/admin/api/export.json", headers=CF).status_code == 401


def test_reseed_is_idempotent():
    A.init_db()  # second init must not duplicate the seed
    con = A.connect()
    assert con.execute("SELECT count(*) FROM books WHERE source='sheet'").fetchone()[0] == 153


def test_auth_reasons(c, monkeypatch):
    make = _jwt_setup(monkeypatch)
    me = lambda h: c.get("/admin/api/me", headers=h).json
    assert "covers the path" in me(CF)["reason"]                                   # no token via Cloudflare
    r = me({**CF, "Cf-Access-Jwt-Assertion": make("alice@example.com", aud="wishlist-aud")})
    assert "Audience" in r["reason"] and "wishlist-aud" in r["reason"] and "aud123" in r["reason"]
    assert "ADMIN_EMAILS" in me({**CF, "Cf-Access-Jwt-Assertion": make("stranger@example.com")})["reason"]
    monkeypatch.setattr(A, "CF_AUD", "")
    assert "isn't configured" in me({**CF, "Cf-Access-Jwt-Assertion": make("alice@example.com")})["reason"]


def test_team_domain_normalized(monkeypatch):
    import importlib
    monkeypatch.setenv("CF_TEAM_DOMAIN", "https://team.example.com/")
    monkeypatch.setenv("CF_AUD", '"aud123"')
    src = (ROOT / "app.py").read_text()
    line = next(l for l in src.splitlines() if l.startswith("CF_TEAM_DOMAIN ="))
    aud = next(l for l in src.splitlines() if l.startswith("CF_AUD ="))
    ns = {"os": os, "re": __import__("re")}
    exec(line + "\n" + aud, ns)
    assert ns["CF_TEAM_DOMAIN"] == "team.example.com" and ns["CF_AUD"] == "aud123"


def test_update_book_persists_edits_when_cover_fetch_fails(c, monkeypatch):
    b = c.post("/admin/api/books", json={"title": "Original Title"}).json
    monkeypatch.setattr(A, "http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("timeout")))
    r = c.put(f"/admin/api/books/{b['id']}", json={
        "title": "Updated Title",
        "cover_url": "https://covers.openlibrary.org/b/id/123-L.jpg",
    })
    assert r.status_code == 502
    saved = c.get(f"/admin/api/books/{b['id']}").json
    assert saved["title"] == "Updated Title"
    c.delete(f"/admin/api/books/{b['id']}")


def test_cover_url_rejects_bad_scheme_and_disallowed_redirect():
    with pytest.raises(ValueError, match="cover host not allowed"):
        A.save_cover_from_url(1, "file:///etc/passwd")
    with pytest.raises(ValueError, match="cover host not allowed"):
        A.save_cover_from_url(1, "ftp://covers.openlibrary.org/b/id/123-L.jpg")

