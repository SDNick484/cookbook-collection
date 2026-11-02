"""The repo is public: no deployment-specific details in tracked files.

Catches container/VM IDs of 3-6 digits ("CT 999", "vm-9999", "lxc 999", "pct enter 999") and personal home
paths. Not caught: 1-2 digit IDs, IDs glued inside a longer word ("myct999"), and anything that isn't an ID or
a path. Hostnames, names and emails can't be listed here without publishing them, so those stay a review
item (see CLAUDE.md). Test data that needs a container-style name uses a 1-2 digit one ("ct7").
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "container/VM ID": re.compile(r"\b(?:ct|vm|lxc|vmid)[ _-]?\d{3,6}\b|\b(?:pct|qm)\s+[a-z-]+\s+\d{3,6}\b", re.I),
    "home directory path": re.compile(r"(?:/home/(?!user\b|you\b|<)[a-z_][a-z0-9_-]*/|/Users/[A-Za-z]|C:\\Users\\)"),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz"}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout (e.g. the installer's copy of the tests)")
    return [ROOT / p for p in out.decode().split("\0") if p]


def test_no_deployment_details_in_tracked_files():
    hits = []
    for path in tracked_files():
        if path.suffix.lower() in SKIP_SUFFIXES or path == Path(__file__).resolve() or not path.is_file():
            continue
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            for what, rx in RULES.items():
                m = rx.search(line)
                if m:
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {what}: {m.group(0)!r}")
    assert not hits, "Public-repo hygiene:\n" + "\n".join(hits)


@pytest.mark.parametrize("text", ["on CT 999", "ct999-ops", "vm-9999", "LXC 999", "vmid 999999", "pct enter 999",
                                  "qm start 9999", "/home/alice/src/x", "/Users/Alice/x", r"C:\Users\alice\x"])
def test_rules_catch(text):
    assert any(rx.search(text) for rx in RULES.values()), text


@pytest.mark.parametrize("text", ["ct[0-9]*-ops", "ctNNN-ops", "/home/user/x", "/home/<you>/x", "isbn 9780306406157",
                                  "act 2025", "Oct 2025", "ct7", "PRAGMA", "http://192.168.1.5:8767", "section 1234"])
def test_rules_allow(text):
    assert not any(rx.search(text) for rx in RULES.values()), text
