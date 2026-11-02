# GitHub Actions CI Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a robust GitHub Actions CI workflow (`.github/workflows/ci.yml`) for `SDNick484/cookbook-collection` testing Python 3.12 and 3.13 on Ubuntu and running an end-to-end Alpine 3.21 packaging and installer smoke test.

**Architecture:** A two-job GitHub Actions pipeline. Job 1 runs on `ubuntu-latest` across a Python 3.12/3.13 matrix running pytest and shell syntax checks. Job 2 runs inside an `alpine:3.21` container, validating native `apk` packaging compatibility and running an end-to-end installer smoke test using a dedicated OpenRC container stub helper (`.github/scripts/install-openrc-stub.sh`).

**Tech Stack:** GitHub Actions, Python (3.12, 3.13), Alpine Linux (3.21), OpenRC, Gunicorn, Flask, SQLite (FTS5), Pytest.

**Spec:** [docs/superpowers/specs/2026-11-02-ci-github-actions-design.md](file:///mnt/c/Users/neandrad/src/cookbook-collection/docs/superpowers/specs/2026-11-02-ci-github-actions-design.md)

## Global Constraints

- Workflow must trigger on every `push` to `main` and every `pull_request`.
- Job 1 (`pytest`) must run on `ubuntu-latest` across Python matrix `3.12` and `3.13`.
- Job 1 must verify SQLite FTS5 support and run the full test suite with zero failures and zero skipped JWT tests.
- Job 1 must syntax-check all shell scripts via `sh -n installer/build.sh installer/setup-head.sh ops/*`.
- Job 2 (`alpine`) must run in an `alpine:3.21` container (`container: alpine:3.21`).
- Job 2 Step A must test native Alpine `apk` packages (`python3 py3-flask py3-gunicorn py3-pillow py3-jwt py3-cryptography py3-pytest sqlite`) with `pytest -q`.
- Job 2 Step B must build `dist/setup-cookbooks.sh` with GNU `tar` and verify all 6 installer smoke test assertions (permissions, `/healthz`, `/mcp`, backup, idempotency, `--uninstall`).
- Branch must be `ci/github-actions` targeting `main`.

## Review Focus

1. **Alpine BusyBox tar rejecting `--owner=0 --group=0`**: Ensure GNU `tar` (`apk add --no-cache tar`) is installed prior to building `installer/build.sh`.
2. **Missing `ip` command on minimal Alpine during installer run**: Ensure `iproute2` is installed so `ip -4 -o addr show ...` in `installer/setup-head.sh` succeeds under `set -eu`.
3. **OpenRC backgrounding failure in container**: Ensure `/usr/local/bin/rc-service` and `/usr/local/bin/rc-update` properly launch Gunicorn in the background under the `cookbooks` service user with all expected environment variables.
4. **Silent JWT test skipping**: Ensure `pyjwt` and `cryptography` are installed and test execution verifies zero failures and that JWT tests pass.
5. **Gunicorn port collision or hung processes on re-run**: Ensure `rc-service cookbooks stop` terminates previous Gunicorn instances cleanly before restarting.

---

### Task 1: Create `requirements-dev.txt`

**Files:**
- Create: `requirements-dev.txt`

**Interfaces:**
- Consumes: Package dependencies specified in `installer/setup-head.sh` and test requirements in `tests/`.
- Produces: Pinned dependency file for dev and CI environments.

- [x] **Step 1: Write `requirements-dev.txt`**

```text
flask==3.1.0
gunicorn==23.0.0
pillow==11.1.0
pyjwt==2.10.1
cryptography==44.0.1
pytest==8.3.4
```

- [x] **Step 2: Verify `requirements-dev.txt` format and syntax**

Run: `grep -E '^[a-z0-9_-]+==[0-9.]+$' requirements-dev.txt | wc -l`
Expected: `6`

- [x] **Step 3: Commit `requirements-dev.txt`**

```bash
git add requirements-dev.txt
git commit -m "chore(ci): add pinned requirements-dev.txt for local and CI reproducibility"
```

---

### Task 2: Create OpenRC Container Stub Helper (`.github/scripts/install-openrc-stub.sh`)

**Files:**
- Create: `.github/scripts/install-openrc-stub.sh`

**Interfaces:**
- Consumes: OpenRC service configurations in `/etc/conf.d/cookbooks` and `/etc/init.d/cookbooks`.
- Produces: Executables `/usr/local/bin/rc-service` and `/usr/local/bin/rc-update`.

- [x] **Step 1: Write `.github/scripts/install-openrc-stub.sh`**

Implement the script that creates:
1. `/usr/local/bin/rc-update`: exits 0 on all calls.
2. `/usr/local/bin/rc-service`: handles `cookbooks start`, `cookbooks stop`, `cookbooks restart`, `cookbooks status`, and exits 0 for other services like `crond`.
   - `start`: sources `/etc/conf.d/cookbooks`, creates `/var/log/cookbooks.log` (chowned to `cookbooks`), kills any existing cookbooks gunicorn process, and launches Gunicorn in the background as user `cookbooks` with the parameters specified in `/etc/init.d/cookbooks`.
   - `stop`: kills Gunicorn processes owned by `cookbooks` via `pkill -u cookbooks gunicorn 2>/dev/null || true`.
   - `restart`: calls stop, sleeps 1, calls start.
   - `status`: checks `pgrep -u cookbooks gunicorn`.

- [x] **Step 2: Verify script syntax**

Run: `sh -n .github/scripts/install-openrc-stub.sh`
Expected: Exit code 0 (no syntax errors).

- [x] **Step 3: Commit `.github/scripts/install-openrc-stub.sh`**

```bash
git add .github/scripts/install-openrc-stub.sh
git commit -m "ci: add OpenRC container stub helper for Alpine CI smoke test"
```

---

### Task 3: Create GitHub Actions Workflow (`.github/workflows/ci.yml`)

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `requirements-dev.txt`, `.github/scripts/install-openrc-stub.sh`, `installer/build.sh`, `dist/setup-cookbooks.sh`.
- Produces: GitHub Actions CI workflow executing on `push` to `main` and `pull_request`.

- [x] **Step 1: Write `.github/workflows/ci.yml`**

Include:
- Triggers: `push` to `main`, `pull_request` to `main`.
- Job 1: `pytest`
  - `runs-on: ubuntu-latest`
  - `strategy.matrix.python-version: ["3.12", "3.13"]`
  - Step: Checkout code (`actions/checkout@v4`)
  - Step: Set up Python (`actions/setup-python@v5` with matrix version and pip cache)
  - Step: Install dependencies (`pip install -r requirements-dev.txt`)
  - Step: Verify SQLite FTS5 (`python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)')"`)
  - Step: Run pytest suite and verify no failures (`python -m pytest -q`)
  - Step: Verify JWT tests ran and passed (`python -m pytest -q -k jwt`)
  - Step: Syntax-check shell scripts (`sh -n installer/build.sh installer/setup-head.sh ops/* .github/scripts/*`)
- Job 2: `alpine`
  - `runs-on: ubuntu-latest`
  - `container: alpine:3.21`
  - Step: Checkout code (`actions/checkout@v4`)
  - Step A: Install native Alpine packages (`apk add --no-cache python3 py3-flask py3-gunicorn py3-pillow py3-jwt py3-cryptography py3-pytest sqlite`)
  - Step A: Run native pytest (`pytest -q`)
  - Step B: Install CI test prerequisites (`apk add --no-cache tar iproute2 wget curl`)
  - Step B: Install OpenRC stubs (`sh .github/scripts/install-openrc-stub.sh`)
  - Step B: Build installer (`sh installer/build.sh`)
  - Step B: Run installer (`sh dist/setup-cookbooks.sh`)
  - Step B: Verify permissions (`/opt/cookbooks`, `/var/lib/cookbooks`, 640 on `/etc/conf.d/cookbooks`, 600 on `/etc/conf.d/app-ops`)
  - Step B: Verify health check (`wget -qO- http://127.0.0.1:8767/healthz`) returns `ok`
  - Step B: Verify `/mcp` JSON-RPC initialize returns HTTP 200
  - Step B: Verify `/usr/local/bin/app-backup` creates `.db.gz` in `/var/backups/apps`
  - Step B: Verify installer idempotency and pre-upgrade backup creation
  - Step B: Verify installer `--uninstall` keeps data in `/var/lib/cookbooks` while removing `/opt/cookbooks`

- [x] **Step 2: Validate YAML syntax of `.github/workflows/ci.yml`**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: Exit code 0 (valid YAML structure).

- [x] **Step 3: Commit `.github/workflows/ci.yml`**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions CI workflow for pytest and Alpine smoke test"
```

---

### Task 4: Local Shell Validation and Branch Preparation

**Files:**
- Modify/Check: all newly created files

- [x] **Step 1: Check out new branch `ci/github-actions` based on `main`**

```bash
git checkout main
git checkout -b ci/github-actions
```

- [x] **Step 2: Run all syntax checks locally**

Run: `sh -n installer/build.sh installer/setup-head.sh ops/* .github/scripts/*.sh`
Expected: Exit code 0.

- [x] **Step 3: Build the installer locally**

Run: `sh installer/build.sh`
Expected: Output `Built dist/setup-cookbooks.sh` and exit code 0.

- [x] **Step 4: Verify git status and commits**

Run: `git status`
Expected: Clean working tree on branch `ci/github-actions`.

---

### Task 5: Push Branch and Open Pull Request

**Files:**
- None (Remote PR creation via GitHub CLI or MCP)

- [ ] **Step 1: Push branch `ci/github-actions` to origin**

```bash
git push -u origin ci/github-actions
```

- [ ] **Step 2: Create Pull Request targeting `main`**

Title: `ci: add GitHub Actions CI workflow for pytest and Alpine smoke test`
Description linking to requirements, explaining Job 1 (`pytest` matrix) and Job 2 (`alpine` native + installer smoke test).

- [ ] **Step 3: Verify CI execution / PR status**
