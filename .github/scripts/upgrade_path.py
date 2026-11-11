#!/usr/bin/env python3
"""CI: does the current code open a database made by older code?

    python .github/scripts/upgrade_path.py [--old COMMIT]

COMMIT defaults to OLD_COMMIT, the last commit before uids, shelf locations and house recipes existed.

1. Extract COMMIT with `git archive` and build a database with that code. Importing its `app` runs init_db(), which
   seeds an empty catalog from that commit's own seed.json (unchanged since, as of writing).
2. Check the result really is an old-format database, so the run can't quietly test nothing.
3. Copy it and start the CURRENT code on the copy under gunicorn, as the installer runs it (--preload, 2 workers).
4. Assert: the service starts and /healthz is ok; every book and recipe has a unique uid; no pre-existing table lost
   or gained a row (the one expected extra is the house book); the old rows are still there under the same ids; and
   full-text search finds specific old rows.

Needs the packages in requirements-dev.txt and a git checkout that has COMMIT (CI checks out full history).
Standard library only. Exits 1 on any failed check, after listing all of them.
"""
import argparse
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# b25b194, "feat(ui): move Ask to a header button beside the title (#6)". Its schema has no uid columns, no
# locations table and no house book. Keep this fixed: it is the "old" end of the upgrade.
OLD_COMMIT = "b25b19441eb6dd568ab4c18d73cdc932cdbd0927"

FTS_SHADOW = re.compile(r"^(books|recipes)_fts_(config|content|data|docsize|idx)$")
# Tables compared by row count. `meta` is excluded: it is key/value config and gains keys as features arrive.
# The two FTS tables are derived indexes, checked against books and recipes instead.
NOT_COUNTED = {"meta", "books_fts", "recipes_fts"}
UID = re.compile(r"[0-9a-f]{32}")

failures = []


def check(ok, what):
    print(("  ok:   " if ok else "  FAIL: ") + what, flush=True)
    if not ok:
        failures.append(what)


def clean_env(**extra):
    keep = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(extra)
    return env


def step(title):
    print(f"\n== {title}", flush=True)


def build_old_database(commit, workdir):
    """Run `commit`'s own code against an empty data folder; return (code folder, data folder)."""
    code, data = workdir / "old-code", workdir / "old-data"
    code.mkdir()
    data.mkdir()
    try:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"git archive {commit} failed: {e.stderr.decode().strip()}\n"
                 "(the job needs the full history: actions/checkout with fetch-depth: 0)")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(code, filter="data")
    subprocess.run([sys.executable, "-c", "import app"], cwd=code, env=clean_env(DATA_DIR=str(data)), check=True)
    return code, data


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def connect(data):
    con = sqlite3.connect(f"file:{data / 'cookbooks.db'}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def user_tables(con):
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows if not FTS_SHADOW.match(r[0])}


def count(con, table, where=""):
    return con.execute(f'SELECT count(*) FROM "{table}" {where}').fetchone()[0]


def probe_word(text):
    """A word the FTS index should find: the first one of 4+ letters."""
    m = re.search(r"[A-Za-z]{4,}", text or "")
    return m.group(0) if m else None


def get_json(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.status, json.load(r)


def start_current_code(data, workdir, port):
    log = open(workdir / "current.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "--preload", "--workers", "2", "--threads", "4",
         "--bind", f"127.0.0.1:{port}", "--chdir", str(ROOT), "app:app"],
        env=clean_env(DATA_DIR=str(data)), stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            return proc, base, None
        try:
            return proc, base, get_json(base + "/healthz")[1]
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    return proc, base, None


def verify(old_data, new_data, base, health, log_text):
    old, new = connect(old_data), connect(new_data)

    step("Service")
    check(health is not None, "the current code starts on the old database and answers /healthz")
    check(bool(health) and health.get("ok") is True, f"/healthz reports ok ({health})")
    check(bool(health) and health.get("fts") is True, "/healthz reports full-text search available")
    check("Traceback" not in log_text, "no traceback in the server log")

    step("Uids")
    for table in ("books", "recipes"):
        uids = [r[0] for r in new.execute(f"SELECT uid FROM {table}")]
        bad = [u for u in uids if not (isinstance(u, str) and UID.fullmatch(u))]
        check(bool(uids) and not bad, f"every {table[:-1]} has a 32-hex uid ({len(uids)} rows, {len(bad)} bad)")
        check(len(set(uids)) == len(uids), f"{table[:-1]} uids are unique")

    step("Row counts")
    for table in sorted(user_tables(old) - NOT_COUNTED):
        if table == "books":
            before, after = count(old, table), count(new, table, "WHERE kind IS NOT 'house'")
            extra = count(new, table, "WHERE kind IS 'house'")
            check(before == after, f"books: {before} before, {after} after (house book excluded)")
            check(extra <= 1, f"books: {extra} house book(s) added (at most 1 expected)")
        else:
            before, after = count(old, table), count(new, table)
            check(before == after, f"{table}: {before} before, {after} after")
    check(count(new, "books_fts") == count(new, "books"), "books_fts has one row per book")
    check(count(new, "recipes_fts") == count(new, "recipes"), "recipes_fts has one row per recipe")

    step("Old rows kept")
    for label, sql in (("books", "SELECT id, title, isbn FROM books WHERE kind IS NOT 'house' ORDER BY id"),
                       ("recipes", "SELECT id, book_id, name, page FROM recipes ORDER BY id")):
        a, b = [tuple(r) for r in old.execute(sql)], [tuple(r) for r in new.execute(sql)]
        check(bool(a) and a == b, f"{label}: the same {len(a)} rows under the same ids")

    step("Full-text search")
    recipe = next((r for r in old.execute("SELECT id, name FROM recipes ORDER BY id") if probe_word(r["name"])), None)
    book = next((r for r in old.execute("SELECT id, title FROM books ORDER BY id") if probe_word(r["title"])), None)
    check(recipe is not None and book is not None, "the old database has rows to search for")
    if recipe is not None:
        word = probe_word(recipe["name"])
        _, found = get_json(f"{base}/api/recipes?q={urllib.parse.quote(word)}")
        ids = [r["id"] for r in found["recipes"]]
        check(recipe["id"] in ids, f"recipe search for '{word}' returns '{recipe['name']}' ({len(ids)} hits)")
    if book is not None:
        word = probe_word(book["title"])
        _, found = get_json(f"{base}/api/books?q={urllib.parse.quote(word)}")
        ids = [b["id"] for b in found["books"]]
        check(book["id"] in ids, f"book search for '{word}' returns '{book['title']}' ({len(ids)} hits)")
    _, everything = get_json(f"{base}/api/books")
    check(everything["total"] == count(old, "books"), f"public book total is still {count(old, 'books')} "
                                                       f"(got {everything['total']}): the house book stays private")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old", default=OLD_COMMIT, help="commit that builds the old database (default: %(default).12s)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="upgrade-path-") as tmp:
        workdir = Path(tmp)
        step(f"Build a database with the code at {args.old[:12]}")
        _, old_data = build_old_database(args.old, workdir)
        old = connect(old_data)
        cols = {t: {r[1] for r in old.execute(f"PRAGMA table_info({t})")} for t in ("books", "recipes")}
        check("uid" not in cols["books"] and "uid" not in cols["recipes"], "it is an old-format database (no uid columns)")
        check("locations" not in user_tables(old), "it has no locations table")
        check(count(old, "books") > 0 and count(old, "recipes") > 0,
              f"it was seeded: {count(old, 'books')} books, {count(old, 'recipes')} recipes")
        old.close()
        if failures:
            return 1

        new_data = workdir / "new-data"
        shutil.copytree(old_data, new_data)
        step("Start the current code on a copy of it")
        port = free_port()
        proc, base, health = start_current_code(new_data, workdir, port)
        try:
            log_text = (workdir / "current.log").read_text()
            verify(old_data, new_data, base, health, log_text)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            if failures:
                print("\n-- server log (tail) --\n" + "\n".join((workdir / "current.log").read_text().splitlines()[-40:]))

    print("\n" + (f"{len(failures)} check(s) FAILED" if failures else "upgrade path OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
