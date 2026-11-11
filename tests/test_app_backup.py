"""ops/app-backup: snapshots, and the warning when image files have nowhere to be copied (BACKUP_TARGET empty).
Runs the real script against a fake /var/lib; needs the sqlite3 command-line tool, like the server does."""
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "app-backup"


class Box:
    """A fake server: lib/cookbooks with a database, a notify stub that records its calls, and a log."""

    def __init__(self, root):
        self.root = root
        self.lib, self.local, self.state = root / "lib", root / "backups", root / "lib" / "app-ops"
        self.log, self.calls, self.notify = root / "app-ops.log", root / "notify.calls", root / "notify"
        (self.lib / "cookbooks").mkdir(parents=True)
        con = sqlite3.connect(self.lib / "cookbooks" / "cookbooks.db")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES ('row')")
        con.commit()
        con.close()
        self.notify.write_text(f'#!/bin/sh\necho "$1|$2|$3" >> {self.calls}\n')
        self.notify.chmod(0o755)

    def add_images(self, sub="photos", n=2):
        d = self.lib / "cookbooks" / sub
        d.mkdir(exist_ok=True)
        for i in range(n):
            (d / f"img{i}.jpg").write_bytes(b"x")

    def run(self, *args, target=""):
        env = {k: v for k, v in os.environ.items() if k not in ("BACKUP_TARGET", "HOST_LABEL", "KEEP_DAYS")}
        env.update(LIB_DIR=str(self.lib), LOCAL_DIR=str(self.local), STATE_DIR=str(self.state), LOG=str(self.log),
                   NOTIFY=str(self.notify), OPS_CONF=str(self.root / "no-such-conf"), HOST_LABEL="testbox",
                   BACKUP_TARGET=str(target))
        return subprocess.run([os.environ.get("TEST_SH", "sh"), str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


def test_backup_writes_a_verified_snapshot(box):
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert len(list(box.local.glob("*/cookbooks-cookbooks.db.gz"))) == 1
    assert "WARNING" not in box.log_text() and box.calls_made() == []  # no images: nothing to warn about


def test_images_without_a_target_warn_every_night_but_notify_once(box):
    box.add_images("photos", 3)
    box.add_images("covers", 2)
    for _ in range(3):
        assert box.run().returncode == 0  # a warning is not a failed backup
    assert box.log_text().count("WARNING: image files are not backed up: 5 in") == 3
    assert "cookbooks/photos (3)" in box.log_text() and "cookbooks/covers (2)" in box.log_text()
    calls = box.calls_made()
    assert len(calls) == 1 and calls[0].startswith("Backup coverage|down|On testbox, image files are not backed up")


def test_empty_image_folders_do_not_warn(box):
    (box.lib / "cookbooks" / "photos").mkdir()
    (box.lib / "cookbooks" / "covers").mkdir()
    assert box.run().returncode == 0
    assert "WARNING" not in box.log_text() and box.calls_made() == []


def test_setting_a_target_clears_the_state_and_copies_the_images(box, tmp_path):
    box.add_images("photos", 2)
    box.run()
    assert len(box.calls_made()) == 1 and (box.state / "backup-images-unprotected").exists()
    target = tmp_path / "nas"
    target.mkdir()
    r = box.run(target=target)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in (target / "files/cookbooks/photos").iterdir()) == ["img0.jpg", "img1.jpg"]
    calls = box.calls_made()
    assert len(calls) == 2 and calls[1].startswith("Backup coverage|up|")
    assert not (box.state / "backup-images-unprotected").exists()
    assert box.log_text().count("WARNING") == 1  # none on the covered run


def test_a_relapse_alerts_again(box, tmp_path):
    box.add_images()
    target = tmp_path / "nas"
    target.mkdir()
    box.run()
    box.run(target=target)
    box.run()  # the mount setting was removed again
    assert [c.split("|")[1] for c in box.calls_made()] == ["down", "up", "down"]


def test_check_images_prints_the_warning_and_touches_nothing(box):
    box.add_images("photos", 4)
    r = box.run("--check-images")
    assert r.returncode == 0
    assert r.stdout.startswith("image files are not backed up: 4 in ") and "BACKUP_TARGET" in r.stdout
    assert box.log_text() == "" and box.calls_made() == [] and not box.local.exists()


def test_check_images_is_silent_when_covered_or_empty(box, tmp_path):
    assert (box.run("--check-images").returncode, box.run("--check-images").stdout) == (1, "")
    box.add_images()
    r = box.run("--check-images", target=tmp_path)
    assert (r.returncode, r.stdout) == (1, "")


def test_a_failed_backup_still_alerts_as_before(box):
    (box.lib / "cookbooks" / "cookbooks.db").write_bytes(b"not a database" * 50)
    r = box.run()
    assert r.returncode == 1
    assert "FAILED cookbooks/cookbooks.db" in box.log_text()
    assert [c.split("|")[:2] for c in box.calls_made()] == [["Backups", "down"]]
