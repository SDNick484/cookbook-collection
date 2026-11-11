"""ops/app-restore against a fake server: snapshots, a stub rc-service, a database in a temp folder.
Needs the sqlite3 command-line tool, like the server does."""
import gzip
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "app-restore"


def make_db(path, value):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t(x)")
    con.execute("DELETE FROM t")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


def value_of(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT x FROM t").fetchone()[0]
    finally:
        con.close()


class Server:
    def __init__(self, root):
        self.root = root
        self.data, self.local = root / "data", root / "backups"
        self.data.mkdir()
        self.local.mkdir()
        self.db = self.data / "cookbooks.db"
        self.state, self.calls = root / "running", root / "rc.calls"
        self.state.write_text("")
        self.rc = root / "rc-service"
        # stub: stop/start/status like OpenRC; STUCK makes stop a no-op so the service stays up
        self.rc.write_text(f'''#!/bin/sh
echo "$2" >> {self.calls}
case "$2" in
  stop) [ -e {root}/STUCK ] || rm -f {self.state} ;;
  start) : > {self.state} ;;
  status) [ -e {self.state} ] ;;
esac
''')
        self.rc.chmod(0o755)
        make_db(self.db, "current")

    def snapshot(self, name, value, dated=True):
        """A nightly snapshot (name = a date) or a safety copy (name = file name without .db.gz)."""
        tmp = self.root / "snap.db"
        tmp.unlink(missing_ok=True)
        make_db(tmp, value)
        for suffix in ("-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
        dest = self.local / name / "cookbooks-cookbooks.db.gz" if dated else self.local / f"{name}.db.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.compress(tmp.read_bytes()))
        return dest

    def run(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("HEALTH_URL", "OWNER", "SERVICE")}
        env.update(CONF=str(self.root / "no-such-conf"), LOCAL_DIR=str(self.local), DATA_DIR=str(self.data),
                   RC_SERVICE=str(self.rc), OWNER=f"{os.getuid()}:{os.getgid()}", HEALTH_URL="",
                   LOG=str(self.root / "app-ops.log"), TMPDIR=str(self.root))
        return subprocess.run([os.environ.get("TEST_SH", "sh"), str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def rc_calls(self):
        return self.calls.read_text().split() if self.calls.exists() else []

    def safety_copies(self):
        return sorted(self.local.glob("cookbooks-before-restore-*.db.gz"))

    def data_listing(self):
        return sorted(p.name for p in self.data.iterdir())


@pytest.fixture()
def srv(tmp_path):
    s = Server(tmp_path)
    s.snapshot("2026-11-08", "old")
    s.snapshot("2026-11-10", "newest")
    s.snapshot("2026-11-09", "middle")
    return s


def test_list_shows_nightly_snapshots_newest_first_then_safety_copies(srv):
    srv.snapshot("cookbooks-before-upgrade-20261105-101500", "pre", dated=False)
    r = srv.run("--list")
    assert r.returncode == 0, r.stderr
    names = [line.split()[0] for line in r.stdout.splitlines()[1:]]
    assert names == ["2026-11-10", "2026-11-09", "2026-11-08", "cookbooks-before-upgrade-20261105-101500"]
    assert "latest" in r.stdout and srv.rc_calls() == []


def test_list_with_no_snapshots_says_so(tmp_path):
    r = Server(tmp_path).run("--list")
    assert r.returncode == 0 and "No snapshots" in r.stdout


def test_dry_run_prints_the_plan_and_changes_nothing(srv):
    before = srv.data_listing()
    r = srv.run("--dry-run", "2026-11-09")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Dry run" in out and "2026-11-09/cookbooks-cookbooks.db.gz" in out and "integrity_check: ok" in out
    for step in ("stop the 'cookbooks' service", "save the current database as", "-wal", "set the owner",
                 "PRAGMA integrity_check", "start the 'cookbooks' service"):
        assert step in out
    assert value_of(srv.db) == "current"
    assert srv.rc_calls() == [] and srv.safety_copies() == []
    assert srv.data_listing() == before  # no staging files left behind


def test_dry_run_without_a_name_uses_the_latest(srv):
    r = srv.run("--dry-run")
    assert r.returncode == 0 and "2026-11-10/cookbooks-cookbooks.db.gz" in r.stdout


def test_real_restore_replaces_the_database_and_saves_the_old_one(srv):
    (srv.data / "cookbooks.db-wal").write_bytes(b"stale wal from the database being replaced")
    (srv.data / "cookbooks.db-shm").write_bytes(b"stale shm")
    r = srv.run("2026-11-09")
    assert r.returncode == 0, r.stderr + r.stdout
    assert value_of(srv.db) == "middle"
    # stop first, start last, nothing else in between that changes state
    calls = srv.rc_calls()
    assert calls[0] == "stop" and calls[-1] == "start" and srv.state.exists()
    # the stale side files are gone, and so is anything staged
    assert not (srv.data / "cookbooks.db-wal").exists() and not (srv.data / "cookbooks.db-shm").exists()
    assert srv.data_listing() == ["cookbooks.db"]
    assert os.stat(srv.db).st_uid == os.getuid()
    # the database it replaced was saved first, and is itself a usable snapshot
    (saved,) = srv.safety_copies()
    out = srv.root / "saved.db"
    out.write_bytes(gzip.decompress(saved.read_bytes()))
    assert value_of(out) == "current"
    assert str(saved) in r.stdout
    assert sqlite3.connect(srv.db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_latest_and_a_date_and_a_safety_copy_name_and_a_path_all_select_a_snapshot(srv):
    srv.snapshot("cookbooks-before-upgrade-20261105-101500", "pre-upgrade", dated=False)
    for arg, want in (("latest", "newest"), ("2026-11-08", "old"),
                      ("cookbooks-before-upgrade-20261105-101500", "pre-upgrade"),
                      (str(srv.local / "2026-11-09" / "cookbooks-cookbooks.db.gz"), "middle")):
        r = srv.run(arg)
        assert r.returncode == 0, (arg, r.stderr)
        assert value_of(srv.db) == want, arg


def test_a_second_restore_in_the_same_second_keeps_both_safety_copies(srv):
    assert srv.run("2026-11-08").returncode == 0
    assert srv.run("2026-11-09").returncode == 0
    assert len(srv.safety_copies()) == 2  # the first restore's copy was not overwritten
    assert value_of(srv.db) == "middle"


def test_an_unknown_snapshot_is_refused_before_anything_happens(srv):
    r = srv.run("2020-01-01")
    assert r.returncode == 1 and "no snapshot called '2020-01-01'" in r.stderr
    assert srv.rc_calls() == [] and value_of(srv.db) == "current"


def test_a_corrupt_snapshot_is_refused_without_stopping_the_service(srv):
    bad = srv.local / "2026-11-11"
    bad.mkdir()
    (bad / "cookbooks-cookbooks.db.gz").write_bytes(b"this is not gzip")
    r = srv.run("2026-11-11")
    assert r.returncode == 1 and "not a valid gzip" in r.stderr
    # a valid gzip of something that is not a database
    (bad / "cookbooks-cookbooks.db.gz").write_bytes(gzip.compress(b"garbage, not sqlite " * 500))
    r = srv.run("latest")
    assert r.returncode == 1 and "integrity_check" in r.stderr
    assert srv.rc_calls() == [] and value_of(srv.db) == "current" and srv.data_listing() == ["cookbooks.db"]


def test_a_service_that_will_not_stop_aborts_with_the_database_untouched(srv):
    (srv.root / "STUCK").write_text("")
    r = srv.run("latest")
    assert r.returncode == 1 and "couldn't stop" in r.stderr
    assert value_of(srv.db) == "current" and srv.safety_copies() == [] and srv.data_listing() == ["cookbooks.db"]
    assert "start" not in srv.rc_calls()


def test_a_damaged_current_database_can_still_be_replaced(srv):
    srv.db.write_bytes(b"\x00damaged" * 4000)
    r = srv.run("latest")
    assert r.returncode == 0, r.stderr + r.stdout
    assert value_of(srv.db) == "newest"
    (saved,) = srv.safety_copies()
    assert gzip.decompress(saved.read_bytes()).startswith(b"\x00damaged")  # the raw file was kept


def test_restoring_when_there_is_no_database_yet(srv):
    srv.db.unlink()
    for side in srv.data.glob("cookbooks.db-*"):
        side.unlink()
    r = srv.run("latest")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "No current database" in r.stdout and value_of(srv.db) == "newest" and srv.safety_copies() == []


def test_usage_errors(srv):
    assert srv.run().returncode == 2  # a real run must name its snapshot
    assert srv.run("--nope").returncode == 2
    assert srv.run("2026-11-08", "2026-11-09").returncode == 2
    assert srv.rc_calls() == [] and value_of(srv.db) == "current"
