"""Who may edit when a request does not come through Cloudflare, and cross-site (CSRF) refusal.

Flask's test client lets us set the socket peer (REMOTE_ADDR) and the Host header directly, which is
exactly what an attacker on the LAN, a DNS-rebinding page, or a cross-site form would control.
"""
import ipaddress
import time

import pytest

import app as A

LAN_PEER = {"REMOTE_ADDR": "192.168.1.20"}
LAN_URL = "http://192.168.1.5:8767"          # the server's own LAN address, as typed in the browser
CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture(autouse=True)
def _lan_on(monkeypatch):
    monkeypatch.setattr(A, "LAN_EDIT", True)
    monkeypatch.setattr(A, "LAN_CIDRS", [ipaddress.ip_network(n) for n in A.PRIVATE_NETS.split(",")])
    monkeypatch.setattr(A, "LAN_HOSTS", {"localhost"})
    monkeypatch.setattr(A, "ALLOWED_ORIGINS", set())


def me(c, base=LAN_URL, peer=LAN_PEER, **headers):
    return c.get("/admin/api/me", base_url=base, environ_base=peer, headers=headers)


def book_count():
    return A.connect().execute("SELECT count(*) FROM books").fetchone()[0]


# ------------------------------------------------------------ LAN identity
def test_lan_peer_with_ip_host_may_edit(c):
    r = me(c)
    assert r.status_code == 200 and r.json["email"] == "lan"


def test_lan_edit_off_refuses(c, monkeypatch):
    monkeypatch.setattr(A, "LAN_EDIT", False)
    r = me(c)
    assert r.status_code == 401 and "LAN_EDIT=0" in r.json["reason"]


@pytest.mark.parametrize("raw,on", [(None, False), ("", False), ("0", False), ("1", True), ('"1"', True),
                                    ("'1' ", True), (" 1", True), ("yes", False), ("10", False)])
def test_flag_parsing(monkeypatch, raw, on):
    """LAN_EDIT and friends: off unless set to 1 (conf.d quoting tolerated)."""
    monkeypatch.delenv("X_TEST_FLAG", raising=False)
    if raw is not None:
        monkeypatch.setenv("X_TEST_FLAG", raw)
    assert A._flag("X_TEST_FLAG") is on


def test_lan_edit_defaults_off(monkeypatch):
    monkeypatch.delenv("LAN_EDIT", raising=False)
    assert A._flag("LAN_EDIT") is False


@pytest.mark.parametrize("peer", ["203.0.113.9", "8.8.8.8", "2001:db8::1", "", "not-an-ip"])
def test_peer_outside_lan_cidrs_refused(c, peer):
    r = me(c, peer={"REMOTE_ADDR": peer})
    assert r.status_code == 401 and "LAN_CIDRS" in r.json["reason"]


@pytest.mark.parametrize("peer", ["127.0.0.1", "10.1.2.3", "172.20.0.4", "192.168.50.9", "::1", "fd00::5",
                                  "fe80::1%eth0"])
def test_peer_inside_default_lan_cidrs_allowed(c, peer):
    assert me(c, peer={"REMOTE_ADDR": peer}).status_code == 200


def test_ipv4_mapped_peers_use_their_ipv4_address(c):
    """A dual-stack bind ([::]) reports IPv4 clients as ::ffff:a.b.c.d."""
    assert me(c, peer={"REMOTE_ADDR": "::ffff:192.168.1.20"}).status_code == 200
    assert me(c, peer={"REMOTE_ADDR": "::ffff:8.8.8.8"}).status_code == 401


def test_custom_lan_cidrs_narrow_access(c, monkeypatch):
    monkeypatch.setattr(A, "LAN_CIDRS", [ipaddress.ip_network("192.168.1.0/24")])
    assert me(c).status_code == 200
    assert me(c, peer={"REMOTE_ADDR": "192.168.2.20"}).status_code == 401


def test_forwarded_headers_do_not_change_the_peer(c):
    """X-Forwarded-For / X-Real-IP are ignored: only the socket peer counts."""
    r = me(c, peer={"REMOTE_ADDR": "203.0.113.9"}, **{"X-Forwarded-For": "192.168.1.20", "X-Real-IP": "10.0.0.1"})
    assert r.status_code == 401


# ------------------------------------------------------------ DNS rebinding
@pytest.mark.parametrize("host", ["http://attacker.example", "http://attacker.example:8767",
                                  "http://192.168.1.5.nip.io:8767"])
def test_rebinding_host_refused(c, host):
    r = me(c, base=host)
    assert r.status_code == 401 and "LAN_HOSTS" in r.json["reason"]
    # and the private export can't be read through a rebound name
    assert c.get("/admin/api/export.json", base_url=host, environ_base=LAN_PEER).status_code == 401


@pytest.mark.parametrize("host", ["http://localhost:8767", "http://127.0.0.1:8767", "http://[::1]:8767",
                                  "http://192.168.1.5"])
def test_ip_and_localhost_hosts_allowed(c, host):
    assert me(c, base=host).status_code == 200


def test_lan_hosts_allows_a_named_host(c, monkeypatch):
    assert me(c, base="http://cookbooks.lan:8767").status_code == 401
    monkeypatch.setattr(A, "LAN_HOSTS", {"localhost", "cookbooks.lan"})
    assert me(c, base="http://Cookbooks.LAN:8767").status_code == 200


@pytest.mark.parametrize("netloc,expect", [
    ("192.168.1.5:8767", "192.168.1.5"), ("192.168.1.5", "192.168.1.5"), ("[::1]:8767", "::1"),
    ("[fd00::5]", "fd00::5"), ("Cookbooks.LAN:80", "cookbooks.lan"), ("", ""), ("[broken", "")])
def test_host_only(netloc, expect):
    assert A._host_only(netloc) == expect


# ------------------------------------------------------------ CSRF
def post_book(c, title, headers=None, content_type="application/json", base=LAN_URL):
    return c.post("/admin/api/books", base_url=base, environ_base=LAN_PEER, headers=headers or {},
                  data='{"title": "%s"}' % title, content_type=content_type)


def test_cross_site_text_plain_post_refused(c):
    """The 'simple' request a hostile page can send without a CORS preflight."""
    before = book_count()
    r = post_book(c, "csrf", {"Origin": "https://evil.example"}, content_type="text/plain")
    assert r.status_code == 403 and "evil.example" in r.json["reason"]
    assert book_count() == before


@pytest.mark.parametrize("method", ["put", "delete"])
def test_cross_site_put_and_delete_refused(c, method):
    bid = A.connect().execute("SELECT id FROM books LIMIT 1").fetchone()[0]
    r = getattr(c, method)(f"/admin/api/books/{bid}", base_url=LAN_URL, environ_base=LAN_PEER,
                           headers={"Origin": "https://evil.example"}, json={"title": "x"})
    assert r.status_code == 403
    assert A.connect().execute("SELECT count(*) FROM books WHERE id=?", (bid,)).fetchone()[0] == 1


def test_null_origin_refused(c):
    """Sandboxed iframes and some redirects send Origin: null."""
    assert post_book(c, "nullorigin", {"Origin": "null"}).status_code == 403


def test_sec_fetch_site_cross_site_without_origin_refused(c):
    assert post_book(c, "sfs", {"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert post_book(c, "sfs2", {"Sec-Fetch-Site": "same-site"}).status_code == 403


def test_same_origin_post_allowed(c):
    r = post_book(c, "Same origin book", {"Origin": LAN_URL, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 201


def test_script_without_origin_allowed(c):
    """curl and server-to-server calls send no Origin; they still need LAN or Access auth."""
    assert post_book(c, "From a script").status_code == 201


def test_origin_port_must_match(c):
    assert post_book(c, "port", {"Origin": "http://192.168.1.5:9999"}).status_code == 403


def test_allowed_origins(c, monkeypatch):
    assert post_book(c, "allowed", {"Origin": "https://cookbooks.example.com"}).status_code == 403
    monkeypatch.setattr(A, "ALLOWED_ORIGINS", {"https://cookbooks.example.com"})
    assert post_book(c, "allowed", {"Origin": "https://cookbooks.example.com/"}).status_code == 201


def test_reads_are_not_origin_checked(c):
    r = c.get("/admin/api/me", base_url=LAN_URL, environ_base=LAN_PEER, headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_refused_before_auth_runs(c, monkeypatch):
    """A cross-site edit is refused with 403 even when nobody is signed in (no auth oracle)."""
    monkeypatch.setattr(A, "LAN_EDIT", False)
    assert post_book(c, "x", {"Origin": "https://evil.example"}).status_code == 403


# ------------------------------------------------------------ through Cloudflare
def _jwt(monkeypatch, email="alice@example.com"):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": key.public_key()})()
    monkeypatch.setattr(A, "_jwks", FakeJWKS())
    return jwt.encode({"email": email, "aud": "aud123", "iss": "https://team.example.com",
                       "exp": int(time.time()) + 3600}, key, algorithm="RS256")


def test_cloudflare_edit_same_origin(c, monkeypatch):
    h = {**CF, "Cf-Access-Jwt-Assertion": _jwt(monkeypatch), "Origin": "https://cookbooks.example.com"}
    r = c.post("/admin/api/books", base_url="https://cookbooks.example.com", headers=h, json={"title": "Via CF"})
    assert r.status_code == 201


def test_cloudflare_edit_when_tunnel_rewrites_host(c, monkeypatch):
    """If the tunnel rewrites Host to the origin address, X-Forwarded-Host still carries the public name."""
    h = {**CF, "Cf-Access-Jwt-Assertion": _jwt(monkeypatch), "Origin": "https://cookbooks.example.com",
         "X-Forwarded-Host": "cookbooks.example.com"}
    r = c.post("/admin/api/books", base_url="http://127.0.0.1:8767", headers=h, json={"title": "Rewritten host"})
    assert r.status_code == 201


def test_x_forwarded_host_ignored_without_cloudflare(c):
    h = {"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example"}
    assert post_book(c, "xfh", h).status_code == 403


def test_cloudflare_http_origin_refused(c, monkeypatch):
    """Through Cloudflare the site is HTTPS-only: same name over http:// is not this site."""
    h = {**CF, "Cf-Access-Jwt-Assertion": _jwt(monkeypatch), "Origin": "http://cookbooks.example.com"}
    r = c.post("/admin/api/books", base_url="https://cookbooks.example.com", headers=h, json={"title": "http"})
    assert r.status_code == 403


def test_non_http_origin_scheme_refused(c):
    assert post_book(c, "ftp", {"Origin": "ftp://192.168.1.5:8767"}).status_code == 403


def test_cloudflare_cross_site_refused_even_with_valid_jwt(c, monkeypatch):
    """Access's cookie rides along on a cross-site request; the Origin check still refuses it."""
    h = {**CF, "Cf-Access-Jwt-Assertion": _jwt(monkeypatch), "Origin": "https://evil.example"}
    r = c.post("/admin/api/books", base_url="https://cookbooks.example.com", headers=h, json={"title": "csrf"})
    assert r.status_code == 403


def test_cloudflare_request_never_falls_back_to_lan(c):
    """Cloudflare headers but no JWT: refused even from a LAN peer with an IP Host."""
    assert me(c, **CF).status_code == 401
