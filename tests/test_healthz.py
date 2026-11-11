"""/healthz: a deep check (database, both FTS indexes, DATA_DIR) that stays unauthenticated and exposes
only ok, fts and a short reason."""
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import A


@pytest.fixture()
def c():
    return A.app.test_client()


def broken_copy(tmp_path, sql):
    """A copy of this module's database with `sql` run on it, and A.DB_PATH pointed there for the test."""
    dst = tmp_path / "cookbooks.db"
    src, out = sqlite3.connect(A.DB_PATH), sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.execute(sql)
    out.commit()
    out.close()
    return dst


def test_healthy_is_200_with_exactly_ok_and_fts(c):
    r = c.get("/healthz")  # no auth headers at all
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "fts": A.HAS_FTS}
    assert A.HAS_FTS  # the CI interpreters have FTS5, so this run really exercised the index checks


def test_without_fts5_it_is_still_healthy(c, monkeypatch):
    monkeypatch.setattr(A, "HAS_FTS", False)
    r = c.get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "fts": False}


def test_unreadable_database_is_503_db(c, monkeypatch, tmp_path):
    bad = tmp_path / "cookbooks.db"
    bad.write_bytes(b"this is not a sqlite database " * 200)
    monkeypatch.setattr(A, "DB_PATH", bad)
    r = c.get("/healthz")
    assert r.status_code == 503
    assert r.get_json() == {"ok": False, "fts": False, "reason": "db"}
    assert "not a database" not in r.get_data(as_text=True) and str(tmp_path) not in r.get_data(as_text=True)


def test_missing_fts_table_is_503_fts(c, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "DB_PATH", broken_copy(tmp_path, "DROP TABLE recipes_fts"))
    r = c.get("/healthz")
    assert r.status_code == 503
    assert r.get_json() == {"ok": False, "fts": False, "reason": "fts"}


def test_missing_data_dir_is_503_data_dir(c, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "DATA_DIR", tmp_path / "gone")
    r = c.get("/healthz")
    assert r.status_code == 503
    assert r.get_json() == {"ok": False, "fts": True, "reason": "data-dir"}  # the indexes did answer


def test_full_disk_is_503_data_dir(c, monkeypatch):
    monkeypatch.setattr(A.os, "statvfs", lambda p: SimpleNamespace(f_bavail=0))
    r = c.get("/healthz")
    assert r.status_code == 503 and r.get_json()["reason"] == "data-dir"


def test_failure_body_never_goes_beyond_ok_fts_reason(c, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "DATA_DIR", tmp_path / "gone")
    assert set(c.get("/healthz").get_json()) == {"ok", "fts", "reason"}


def test_health_check_does_not_write_files(c):
    before = sorted(p.name for p in A.DATA_DIR.iterdir())
    for _ in range(3):
        assert c.get("/healthz").status_code == 200
    assert sorted(p.name for p in A.DATA_DIR.iterdir()) == before  # a public probe can't make the app write
