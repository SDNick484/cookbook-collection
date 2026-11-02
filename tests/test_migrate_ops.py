"""ops/migrate-ops: moving the old container-named ops files to the generic names, under a fake root."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "migrate-ops"
OLD = "ct7"  # any container number works (the script matches ct[0-9]*); short so test_hygiene ignores it


def run(root):
    r = subprocess.run([os.environ.get("TEST_SH", "sh"), str(SCRIPT)], env={**os.environ, "ROOT": str(root)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def old_layout(root):
    write(root / f"etc/conf.d/{OLD}-ops", 'HA_URL="http://ha.example:8123"\nHA_WEBHOOK_ID="hook"\n')
    write(root / f"var/backups/{OLD}/2026-10-01/cookbooks-cookbooks.db.gz", "snap1")
    write(root / f"var/backups/{OLD}/cookbooks-before-upgrade-1.db.gz", "pre1")
    write(root / f"var/lib/{OLD}-ops/health/cookbooks.state", "up\n")
    write(root / f"var/log/{OLD}-ops.log", "old line\n")
    write(root / f"usr/local/bin/{OLD}-backup", "#!/bin/sh\n")
    write(root / f"usr/local/bin/{OLD}-notify", "#!/bin/sh\n")
    (root / "etc/periodic/daily").mkdir(parents=True)
    os.symlink(f"/usr/local/bin/{OLD}-backup", root / f"etc/periodic/daily/{OLD}-backup")


def test_fresh_install_is_a_no_op(tmp_path):
    assert run(tmp_path) == ""
    assert list(tmp_path.iterdir()) == []


def test_old_layout_moves_everything(tmp_path):
    old_layout(tmp_path)
    out = run(tmp_path)
    assert "HA_WEBHOOK_ID=\"hook\"" in (tmp_path / "etc/conf.d/app-ops").read_text()
    assert (tmp_path / "var/backups/apps/2026-10-01/cookbooks-cookbooks.db.gz").read_text() == "snap1"
    assert (tmp_path / "var/backups/apps/cookbooks-before-upgrade-1.db.gz").exists()
    assert (tmp_path / "var/lib/app-ops/health/cookbooks.state").read_text() == "up\n"
    assert (tmp_path / "var/log/app-ops.log").read_text() == "old line\n"
    for gone in (f"etc/conf.d/{OLD}-ops", f"var/backups/{OLD}", f"var/lib/{OLD}-ops", f"var/log/{OLD}-ops.log",
                 f"usr/local/bin/{OLD}-backup", f"etc/periodic/daily/{OLD}-backup"):
        assert not os.path.lexists(tmp_path / gone), gone
    notify = tmp_path / f"usr/local/bin/{OLD}-notify"
    assert notify.is_symlink() and os.readlink(notify) == "/usr/local/bin/app-notify"
    assert "moved /etc/conf.d/" in out


def test_rerun_is_idempotent(tmp_path):
    old_layout(tmp_path)
    run(tmp_path)
    snapshot = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert run(tmp_path) == ""
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == snapshot


def test_new_paths_already_exist_nothing_overwritten(tmp_path):
    """The installer's pre-upgrade backup creates /var/backups/apps before the migration runs."""
    old_layout(tmp_path)
    write(tmp_path / "etc/conf.d/app-ops", 'HA_URL="new"\nHA_WEBHOOK_ID=""\n')
    write(tmp_path / "var/backups/apps/cookbooks-before-upgrade-2.db.gz", "pre2")
    write(tmp_path / "var/backups/apps/2026-10-01/cookbooks-cookbooks.db.gz", "newer snap")
    write(tmp_path / "var/log/app-ops.log", "new line\n")
    out = run(tmp_path)
    # config: values set in the new file win; empty ones are filled from the old file, which stays
    conf = (tmp_path / "etc/conf.d/app-ops").read_text()
    assert 'HA_URL="new"' in conf and 'HA_URL="http' not in conf
    assert 'HA_WEBHOOK_ID="hook"' in conf and 'HA_WEBHOOK_ID=""' not in conf
    assert (tmp_path / f"etc/conf.d/{OLD}-ops").exists() and "copied HA_WEBHOOK_ID" in out
    # backups: merged without overwriting the same-named entry
    apps = tmp_path / "var/backups/apps"
    assert (apps / "cookbooks-before-upgrade-1.db.gz").read_text() == "pre1"
    assert (apps / "cookbooks-before-upgrade-2.db.gz").read_text() == "pre2"
    assert (apps / "2026-10-01/cookbooks-cookbooks.db.gz").read_text() == "newer snap"
    assert (tmp_path / f"var/backups/{OLD}/2026-10-01").exists()   # the clash is left in place
    # log: old history first, then new
    assert (tmp_path / "var/log/app-ops.log").read_text() == "old line\nnew line\n"


def test_config_clash_adds_missing_keys_without_duplicates(tmp_path):
    write(tmp_path / f"etc/conf.d/{OLD}-ops", 'HA_URL="http://ha"\nKEEP_DAYS=30\nBACKUP_TARGET=\n')
    write(tmp_path / "etc/conf.d/app-ops", "# header\nKEEP_DAYS=14\n")
    run(tmp_path)
    run(tmp_path)  # a rerun adds nothing twice
    lines = (tmp_path / "etc/conf.d/app-ops").read_text().splitlines()
    assert lines[0] == "# header" and lines.count('HA_URL="http://ha"') == 1
    assert "KEEP_DAYS=14" in lines and "KEEP_DAYS=30" not in lines   # set in the new file: kept
    assert not any(l.startswith("BACKUP_TARGET") for l in lines)      # empty in both: not copied


def test_symlinked_old_backups_dir_is_left_alone(tmp_path):
    """Backups living on other storage must not be pulled onto the local disk."""
    nas = tmp_path / "mnt/nas/backups"
    write(nas / "2026-10-01/snap.db.gz", "snap")
    (tmp_path / "var/backups").mkdir(parents=True)
    os.symlink(nas, tmp_path / f"var/backups/{OLD}")
    (tmp_path / "var/backups/apps").mkdir()
    out = run(tmp_path)
    assert (nas / "2026-10-01/snap.db.gz").read_text() == "snap"
    assert (tmp_path / f"var/backups/{OLD}").is_symlink()
    assert list((tmp_path / "var/backups/apps").iterdir()) == []
    assert "!!" in out and "symlink" in out


def test_a_file_matching_the_glob_doesnt_hide_the_directory(tmp_path):
    write(tmp_path / "var/backups/ct1.tar", "not a backup dir")
    write(tmp_path / f"var/backups/{OLD}/2026-10-01/snap.db.gz", "snap")
    run(tmp_path)
    assert (tmp_path / "var/backups/apps/2026-10-01/snap.db.gz").read_text() == "snap"
    assert (tmp_path / "var/backups/ct1.tar").exists()


def test_every_matching_old_dir_is_migrated(tmp_path):
    write(tmp_path / "var/backups/ct1/2026-09-01/a.db.gz", "a")
    write(tmp_path / "var/backups/ct22/2026-10-01/b.db.gz", "b")
    run(tmp_path)
    assert (tmp_path / "var/backups/apps/2026-09-01/a.db.gz").exists()
    assert (tmp_path / "var/backups/apps/2026-10-01/b.db.gz").exists()
    assert not (tmp_path / "var/backups/ct1").exists() and not (tmp_path / "var/backups/ct22").exists()


def test_merge_is_one_level_and_reports_clashes(tmp_path):
    """Same-named entries aren't merged recursively: the old one stays put and is reported."""
    write(tmp_path / "var/lib/ct1-ops/health/a.state", "up")
    write(tmp_path / "var/lib/ct22-ops/health/b.state", "up")
    out = run(tmp_path)
    assert (tmp_path / "var/lib/app-ops/health").is_dir()
    left = [p for p in (tmp_path / "var/lib/ct1-ops", tmp_path / "var/lib/ct22-ops") if p.exists()]
    assert len(left) == 1 and (left[0] / "health").is_dir() and "still in" in out


def test_hidden_entries_are_merged(tmp_path):
    write(tmp_path / f"var/lib/{OLD}-ops/.marker", "x")
    (tmp_path / "var/lib/app-ops").mkdir(parents=True)
    run(tmp_path)
    assert (tmp_path / "var/lib/app-ops/.marker").exists()
    assert not (tmp_path / f"var/lib/{OLD}-ops").exists()


def test_notify_link_already_migrated_is_left_alone(tmp_path):
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    os.symlink("/usr/local/bin/app-notify", tmp_path / f"usr/local/bin/{OLD}-notify")
    assert run(tmp_path) == ""


def test_unrelated_names_untouched(tmp_path):
    keep = [write(tmp_path / "etc/conf.d/cookbooks", "PORT=8767\n"), write(tmp_path / "var/backups/other/x", "y"),
            write(tmp_path / "etc/conf.d/ctx-ops", "z")]
    assert run(tmp_path) == ""
    assert all(p.exists() for p in keep)


@pytest.mark.parametrize("script", ["app-backup", "app-health", "app-notify", "migrate-ops"])
def test_ops_scripts_parse(script):
    assert subprocess.run(["sh", "-n", str(SCRIPT.parent / script)]).returncode == 0
