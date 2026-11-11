"""Test helper: a stand-in for Cloudflare Access's signing keys.

`AccessKeys(monkeypatch)` empties the app's key cache, replaces `app._fetch_jwks` with a fake that serves this
object's current keys (counting calls), and signs tokens the way Access does: RS256 with a `kid` header and
email, aud, iss, iat and exp claims. `rotate()` swaps in a new key, as Cloudflare does every few weeks.
"""
import time

import pytest

import app as A


class AccessKeys:
    def __init__(self, monkeypatch, aud="aud123", iss="https://team.example.com"):
        self.jwt = pytest.importorskip("jwt")  # PyJWT; production gets it from py3-jwt
        self.rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
        self.aud, self.iss = aud, iss
        self.fetches = 0
        self.fail = None  # set to an exception to make the next fetches fail
        self.keys = {}
        self.kid = None
        self.rotate()
        monkeypatch.setattr(A, "_jwks_keys", {})
        monkeypatch.setattr(A, "_jwks_last_fetch", None)
        monkeypatch.setattr(A, "_fetch_jwks", self._fetch)

    def _fetch(self):
        self.fetches += 1
        if self.fail:
            raise self.fail
        return {kid: k.public_key() for kid, k in self.keys.items()}

    def rotate(self, keep_old=False):
        """A new signing key; with keep_old the endpoint still lists the previous one (Cloudflare's overlap)."""
        self.kid = f"kid-{len(self.keys) + 1}-{time.monotonic_ns()}"
        key = self.rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys = {**(self.keys if keep_old else {}), self.kid: key}
        return self.kid

    def token(self, email="alice@example.com", aud=None, iss=None, exp=3600, iat=0, kid=None, key=None,
              drop=(), alg="RS256"):
        now = int(time.time())
        claims = {"email": email, "aud": aud or self.aud, "iss": iss or self.iss, "iat": now + iat, "exp": now + exp}
        for c in drop:
            claims.pop(c)
        signer = key or self.keys[self.kid]
        if alg == "HS256":
            return self.jwt.encode(claims, "an-hmac-secret-that-is-not-the-rsa-key", algorithm="HS256", headers={"kid": kid or self.kid})
        return self.jwt.encode(claims, signer, algorithm=alg, headers={"kid": kid or self.kid})

    def stranger_key(self):
        """A key Cloudflare never published."""
        return self.rsa.generate_private_key(public_exponent=65537, key_size=2048)
