"""Give each test module its own database, cover and photo folders.

Each test file used to share one database: whichever file imported `app` first set DATA_DIR, and every
later file read and changed the same SQLite file, so results depended on test order. Now every module
starts with a fresh, seeded catalog (seed.json) in its own temporary folder. Tests can still monkeypatch
settings for a single test; those changes are restored afterward.
"""
import os
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
