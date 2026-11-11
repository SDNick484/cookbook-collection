"""Security headers on every class of response, robots.txt in both PUBLIC_INDEXING modes, and the write gate.

The headers come from one after_request hook (app.security_headers), so these tests sample each kind of
response it has to cover rather than every route. tests/test_csp_browser.py checks in Chromium that the
policy doesn't break the pages.
"""
import io
import json

import pytest

import app as A
import photos

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}   # through Cloudflare, nobody signed in


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture(scope="module")
def files():
    """A cover and a photo on disk, so /covers and /photos serve real files."""
    from PIL import Image
    A.COVER_DIR.mkdir(parents=True, exist_ok=True)
    photos.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "#884422").save(buf, "JPEG")
    (A.COVER_DIR / "hdr-test.jpg").write_bytes(buf.getvalue())
    (photos.PHOTO_DIR / "hdr-test.jpg").write_bytes(buf.getvalue())
    return "hdr-test.jpg"


def responses(c, files):
    """(label, response) for each kind of response the hook must cover."""
    mcp = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    return [
        ("html public", c.get("/")),
        ("html editor", c.get("/admin")),
        ("json public", c.get("/api/books")),
        ("json editor", c.get("/admin/api/me")),
        ("json 401", c.get("/admin/api/me", headers=CF)),
        ("json 404", c.get("/api/books/999999")),
        ("plain 404", c.get("/no/such/page")),
        ("static js", c.get("/static/app.js")),
        ("static css", c.get("/static/style.css")),
        ("manifest", c.get("/static/manifest.json")),
        ("cover", c.get(f"/covers/{files}")),
        ("photo", c.get(f"/photos/{files}")),
        ("mcp", c.post("/mcp", data=mcp, content_type="application/json")),
        ("mcp get", c.get("/mcp")),
        ("robots", c.get("/robots.txt")),
        ("healthz", c.get("/healthz")),
    ]


def test_headers_on_every_route_class(c, files):
    for label, r in responses(c, files):
        h = r.headers
        assert r.status_code < 500, label
        csp = h.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp, label
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0], label
        assert "unsafe-eval" not in csp and "report-only" not in " ".join(h.keys()).lower(), label
        assert h.get("X-Content-Type-Options") == "nosniff", label
        assert h.get("Referrer-Policy") == "strict-origin-when-cross-origin", label
        assert h.get("X-Frame-Options") == "DENY", label
        pp = h.get("Permissions-Policy", "")
        assert "camera=(self)" in pp and "screen-wake-lock=(self)" in pp and "microphone=()" in pp, label
        assert "X-Robots-Tag" not in h, label           # PUBLIC_INDEXING=1 by default


def test_served_files_are_what_they_claim(c, files):
    assert c.get(f"/covers/{files}").status_code == 200
    assert c.get(f"/photos/{files}").headers["Content-Type"] == "image/jpeg"
    assert c.get("/static/app.js").headers["Content-Type"].startswith(("text/javascript", "application/javascript"))


def test_csp_allows_the_cover_hosts():
    img = next(d for d in A.CSP.split("; ") if d.startswith("img-src"))
    for h in A.COVER_HOSTS:
        assert f"https://{h}" in img and f"https://*.{h}" in img
    assert "blob:" in img and "data:" in img and "http:" not in img


def test_robots_default_allows_the_catalog(c):
    r = c.get("/robots.txt")
    assert r.status_code == 200 and r.mimetype == "text/plain"
    lines = r.get_data(as_text=True).splitlines()
    assert lines[0] == "User-agent: *"
    assert "Disallow: /admin" in lines and "Disallow: /mcp" in lines and "Disallow: /" not in lines


def test_robots_and_noindex_when_indexing_is_off(c, files, monkeypatch):
    monkeypatch.setattr(A, "PUBLIC_INDEXING", False)
    lines = c.get("/robots.txt").get_data(as_text=True).splitlines()
    assert "Disallow: /" in lines
    for label, r in responses(c, files):
        assert r.headers.get("X-Robots-Tag") == "noindex, nofollow", label


@pytest.mark.parametrize("raw,on", [(None, True), ("1", True), ('"1"', True), ("0", False), ("", False)])
def test_public_indexing_setting(monkeypatch, raw, on):
    monkeypatch.delenv("PUBLIC_INDEXING", raising=False)
    if raw is not None:
        monkeypatch.setenv("PUBLIC_INDEXING", raw)
    assert A._flag("PUBLIC_INDEXING", "1") is on


def test_public_indexing_reaches_the_service():
    """The installer writes it to conf.d and the init script exports it (else gunicorn never sees it)."""
    head = (A.APP_DIR / "installer" / "setup-head.sh").read_text()
    export = next(l for l in head.splitlines() if l.startswith("export DATA_DIR"))
    assert " PUBLIC_INDEXING " in export + " "
    assert 'add_setting "$CONF" PUBLIC_INDEXING 1 ' in head
    assert "PUBLIC_INDEXING=1" in (A.APP_DIR / ".env.example").read_text()


# ------------------------------------------------------------- the write gate is unchanged
def _write_routes():
    sample = {"book_id": "1", "rid": "1", "uid": "0" * 32, "job_id": "1", "scan_id": "1", "photo_id": "1",
              "iid": "1", "loc_id": "1", "name": "x.jpg", "lid": "1", "pid": "1"}
    out = []
    for r in A.app.url_map.iter_rules():
        for m in sorted(r.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            if r.rule.startswith("/mcp"):
                continue  # JSON-RPC: its write tools are gated by MCP_TOKEN (tests/test_mcp_token.py)
            path = r.rule
            for arg in r.arguments:
                v = sample.get(arg, "1")
                for conv in (f"<int:{arg}>", f"<path:{arg}>", f"<{arg}>"):
                    path = path.replace(conv, v)
            out.append((m, path))
    return out


def test_write_routes_exist_only_under_admin():
    assert _write_routes() and all(p.startswith("/admin/") for _, p in _write_routes())


@pytest.mark.parametrize("method,path", _write_routes())
def test_every_write_route_refuses_anonymous(c, method, path, monkeypatch):
    # through Cloudflare without a sign-in, and directly with LAN editing off
    r = c.open(path, method=method, headers=CF, json={})
    assert r.status_code == 401, (method, path, r.status_code)
    monkeypatch.setattr(A, "LAN_EDIT", False)
    assert c.open(path, method=method, json={}).status_code == 401, (method, path)
