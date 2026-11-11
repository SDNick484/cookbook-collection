"""Give each test module its own database, cover and photo folders.

Each test file used to share one database: whichever file imported `app` first set DATA_DIR, and every
later file read and changed the same SQLite file, so results depended on test order. Now every module
starts with a fresh, seeded catalog (seed.json) in its own temporary folder. Tests can still monkeypatch
settings for a single test; those changes are restored afterward.
"""
import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Settings the tests expect when `app` is first imported (test_app.py relies on the same values).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("CF_TEAM_DOMAIN", "team.example.com")
os.environ.setdefault("CF_AUD", "aud123")
os.environ.setdefault("ADMIN_EMAILS", "alice@example.com,bob@example.com")
os.environ.setdefault("LAN_EDIT", "1")
import app as A  # noqa: E402
import photos  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _own_database():
    data = Path(tempfile.mkdtemp())
    saved = (A.DATA_DIR, A.DB_PATH, A.COVER_DIR, photos.PHOTO_DIR)
    A.DATA_DIR, A.DB_PATH, A.COVER_DIR = data, data / "cookbooks.db", data / "covers"
    photos.PHOTO_DIR = data / "photos"
    A.init_db()  # creates the schema and loads seed.json into the empty catalog
    yield
    A.DATA_DIR, A.DB_PATH, A.COVER_DIR, photos.PHOTO_DIR = saved


def _is_local(host):
    if isinstance(host, bytes):
        host = host.decode()
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail any test that opens a connection beyond this machine.

    The tests fake the Claude API and Dinner Log by patching the module that defines the call. A patch that
    silently misses (a re-exported name, a renamed function) would send a real request, so the connection is
    refused here instead of depending on the sandbox's network. Loopback and unix sockets stay open: the
    browser tests start a local server.
    """
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def check(addr):
        if isinstance(addr, tuple) and addr and not _is_local(addr[0]):
            raise AssertionError(f"test tried a real network connection to {addr[0]!r}: patch the call in the module that defines it")

    def connect(self, addr):
        check(addr)
        return real_connect(self, addr)

    def connect_ex(self, addr):
        check(addr)
        return real_connect_ex(self, addr)

    def getaddrinfo(host, *args, **kwargs):
        check((host,))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
