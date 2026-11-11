"""Cloudflare Access token checks (audit M-2): the key cache can't be driven or emptied by a client.

Through Cloudflare, /admin requests carry Cf-Access-Jwt-Assertion. Verifying it needs Cloudflare's signing keys,
fetched over the network. These tests pin down when that fetch may happen: only for an unknown kid, at most once
per JWKS_REFRESH_SECONDS per worker, never because a token was bad, and never from the public /api/me.
"""
import pytest

import app as A
from access_jwt import AccessKeys

ORIGINAL_FETCH = A._fetch_jwks  # before any test replaces it

CF = {"Cf-Ray": "abc", "Cf-Connecting-Ip": "203.0.113.9"}


@pytest.fixture()
def c():
    return A.app.test_client()


@pytest.fixture()
def keys(monkeypatch):
    return AccessKeys(monkeypatch)


@pytest.fixture()
def clock(monkeypatch):
    """A controllable time.monotonic() for the refresh cooldown."""
    t = {"now": 10_000.0}
    monkeypatch.setattr(A.time, "monotonic", lambda: t["now"])
    return t


def me(c, token):
    return c.get("/admin/api/me", headers={**CF, "Cf-Access-Jwt-Assertion": token})


def test_valid_token_fetches_once_then_uses_the_cache(c, keys):
    for _ in range(3):
        assert me(c, keys.token()).status_code == 200
    assert keys.fetches == 1


def test_unknown_kid_refetches_at_most_once_per_cooldown(c, keys, clock):
    assert me(c, keys.token()).status_code == 200                    # warm the cache: fetch 1
    forged = keys.token(kid="attacker-kid", key=keys.stranger_key())
    assert me(c, forged).status_code == 401 and keys.fetches == 1    # right after a fetch: no refetch at all
    clock["now"] += A.JWKS_REFRESH_SECONDS
    for _ in range(20):                                               # a burst of unknown kids
        assert me(c, forged).status_code == 401
    assert keys.fetches == 2                                          # one refetch for the whole burst
    clock["now"] += A.JWKS_REFRESH_SECONDS - 1
    assert me(c, forged).status_code == 401 and keys.fetches == 2
    clock["now"] += 2                                                 # cooldown over: one more try
    assert me(c, forged).status_code == 401 and keys.fetches == 3
    assert me(c, keys.token()).status_code == 200 and keys.fetches == 3   # the real key never left the cache


def test_bad_token_does_not_clear_the_cache(c, keys, clock):
    assert me(c, keys.token()).status_code == 200
    cached = dict(A._jwks_keys)
    clock["now"] += A.JWKS_REFRESH_SECONDS                            # so the unknown kid below does try a fetch
    bad = [
        "garbage",
        keys.token(key=keys.stranger_key()),                          # known kid, wrong signature
        keys.token(aud="someone-else"),
        keys.token(exp=-3600),
        keys.token(kid="nope", key=keys.stranger_key()),               # unknown kid: one refetch...
    ]
    keys.fail = OSError("Cloudflare unreachable")                     # ...which fails
    for t in bad:
        assert me(c, t).status_code == 401
    assert A._jwks_keys == cached and keys.fetches == 2
    assert me(c, keys.token()).status_code == 200


def test_failed_fetch_keeps_old_keys_and_respects_the_cooldown(c, keys, clock):
    assert me(c, keys.token()).status_code == 200
    keys.fail = OSError("timed out")
    clock["now"] += A.JWKS_REFRESH_SECONDS
    unknown = keys.token(kid="x", key=keys.stranger_key())
    for _ in range(5):
        assert me(c, unknown).status_code == 401
    assert keys.fetches == 2                                          # one failed attempt, not five
    assert me(c, keys.token()).status_code == 200


def test_cold_start_failure_is_also_rate_limited(c, keys, clock):
    keys.fail = OSError("down")
    for _ in range(5):
        r = me(c, keys.token())
        assert r.status_code == 401 and "CF_TEAM_DOMAIN" in r.json["reason"]
    assert keys.fetches == 1
    keys.fail = None
    clock["now"] += A.JWKS_REFRESH_SECONDS
    assert me(c, keys.token()).status_code == 200 and keys.fetches == 2


def test_key_rotation_is_picked_up(c, keys, clock):
    assert me(c, keys.token()).status_code == 200
    old = keys.token()
    keys.rotate(keep_old=True)                                        # Cloudflare lists both for a while
    clock["now"] += A.JWKS_REFRESH_SECONDS                            # the last fetch was a while ago
    assert me(c, keys.token()).status_code == 200                     # new kid: refetch, accepted
    assert me(c, old).status_code == 200                              # old kid still listed: still accepted
    keys.rotate()                                                     # old key dropped upstream
    clock["now"] += A.JWKS_REFRESH_SECONDS
    assert me(c, keys.token()).status_code == 200
    assert me(c, old).status_code == 401                              # gone from the endpoint, gone here


def test_rotation_within_the_cooldown_waits_for_it(c, keys, clock):
    """A rotation right after a fetch is accepted once the cooldown passes, not before (the price of the limit)."""
    assert me(c, keys.token()).status_code == 200
    keys.rotate()
    assert me(c, keys.token()).status_code == 401
    clock["now"] += A.JWKS_REFRESH_SECONDS
    assert me(c, keys.token()).status_code == 200


@pytest.mark.parametrize("claim", ["exp", "iat"])
def test_exp_and_iat_are_required(c, keys, claim):
    r = me(c, keys.token(drop=(claim,)))
    assert r.status_code == 401 and claim in r.json["reason"]


def test_small_clock_skew_is_tolerated(c, keys):
    assert me(c, keys.token(iat=10)).status_code == 200               # issued 10 s "in the future"
    assert me(c, keys.token(exp=-10)).status_code == 200              # expired 10 s ago
    assert me(c, keys.token(iat=600)).status_code == 401
    assert me(c, keys.token(exp=-600)).status_code == 401


def test_only_rs256(c, keys):
    assert me(c, keys.token(alg="HS256")).status_code == 401
    assert keys.fetches == 0                                          # refused before any key lookup


def test_401_body_never_contains_aud_values(c, keys):
    for t in (keys.token(aud="the-other-apps-aud"), keys.token(aud=["a1", "a2"]), keys.token(kid="k", key=keys.stranger_key())):
        body = me(c, t).get_data(as_text=True)
        assert A.CF_AUD not in body and "the-other-apps-aud" not in body and '"a1"' not in body


def test_public_me_never_verifies(c, keys, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "access_identity", lambda: calls.append(1) or "alice@example.com")
    for h in ({}, CF, {**CF, "Cf-Access-Jwt-Assertion": keys.token()},
              {**CF, "Cf-Access-Jwt-Assertion": keys.token(kid="unknown", key=keys.stranger_key())}):
        r = c.get("/api/me", headers=h)
        assert r.status_code == 200 and r.get_json() == {"editor": False}
    assert calls == [] and keys.fetches == 0


def test_real_fetch_parses_a_jwks_document(monkeypatch, keys):
    """_fetch_jwks itself, with urlopen stubbed: a JWKS document becomes kid -> usable public key."""
    import io, json
    jwt = pytest.importorskip("jwt")
    from jwt.algorithms import RSAAlgorithm
    pub = keys.keys[keys.kid].public_key()
    doc = {"keys": [{**json.loads(RSAAlgorithm.to_jwk(pub)), "kid": keys.kid, "alg": "RS256", "use": "sig"}]}
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return Resp(json.dumps(doc).encode())
    monkeypatch.setattr(A.urllib.request, "urlopen", urlopen)
    out = ORIGINAL_FETCH()   # AccessKeys replaced A._fetch_jwks; this is the module's own function
    assert set(out) == {keys.kid} and seen["url"] == "https://team.example.com/cdn-cgi/access/certs"
    assert seen["timeout"] <= 10
    assert jwt.decode(keys.token(), out[keys.kid], algorithms=["RS256"], audience="aud123")["email"]
