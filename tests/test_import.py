"""The JSON export is a portable backup: export.json -> importer -> the same catalog.

Run: python3 -m pytest -q tests/test_import.py  (conftest.py gives this module its own seeded temp DATA_DIR)
"""
import re

import pytest

import app as A


@pytest.fixture()
def client():
    return A.app.test_client()


# ------------------------------------------------------------------ export format
def test_export_has_version_and_timestamp(client):
    j = client.get("/admin/api/export.json").json
    assert j["version"] == A.EXPORT_VERSION == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", j["exported_at"])
    assert "exported" in j  # the older key stays, so nothing that reads it breaks
