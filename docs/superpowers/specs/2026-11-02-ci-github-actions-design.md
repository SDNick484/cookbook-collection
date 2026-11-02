# CI GitHub Actions Workflow Design

## Intent and Why
Provide a robust, automated GitHub Actions CI workflow for `SDNick484/cookbook-collection` triggered on every `push` to `main` and every `pull_request`. This ensures that Python tests pass across supported Python versions (3.12, 3.13), shell scripts are syntactically valid, dependencies are compatible with Alpine Linux package repositories (`apk`), and the self-contained production installer (`dist/setup-cookbooks.sh`) functions correctly end-to-end.

## Goals
1. **Reproducible Environment**: Add `requirements-dev.txt` with pinned versions of `flask`, `gunicorn`, `pillow`, `pyjwt`, `cryptography`, `pytest` to support local and CI test execution.
2. **Job 1: `pytest` (Ubuntu Runner & Matrix)**:
   - Run on `ubuntu-latest` across Python matrix: `3.12`, `3.13`.
   - Verify SQLite FTS5 support via `sqlite3`.
   - Run the full pytest test suite (`python -m pytest -q`) with zero failures and zero skipped JWT tests (`PyJWT` + `cryptography`).
   - Run syntax checking on all shell scripts: `sh -n installer/build.sh installer/setup-head.sh ops/*`.
3. **Job 2: `alpine` (Alpine Package & Installer Smoke Test)**:
   - Run inside an `alpine:3.21` container (`container: alpine:3.21`).
   - **Step A (Native apk test)**:
     - Install exact production packages + `py3-pytest`: `apk add --no-cache python3 py3-flask py3-gunicorn py3-pillow py3-jwt py3-cryptography py3-pytest sqlite`.
     - Run `pytest -q` to verify runtime compatibility against Alpine's packaged libraries.
   - **Step B (End-to-end installer smoke test)**:
     - Install build/runtime prerequisites: GNU `tar` (required by `installer/build.sh` for `--owner=0 --group=0`), `iproute2`, and `curl` or `wget`.
     - Use `.github/scripts/install-openrc-stub.sh` to install `/usr/local/bin/rc-service` and `/usr/local/bin/rc-update` stubs that safely start/stop Gunicorn as the `cookbooks` service user in the container.
     - Build `dist/setup-cookbooks.sh` via `sh installer/build.sh`.
     - Run `sh dist/setup-cookbooks.sh`.
     - Verify:
       1. Directory and file permissions: `/opt/cookbooks`, `/var/lib/cookbooks`, `/etc/conf.d/cookbooks` (640), and `/etc/conf.d/app-ops` (600).
       2. Gunicorn health check: `wget -qO- http://127.0.0.1:8767/healthz` returns `ok`.
       3. MCP JSON-RPC `initialize` POST to `/mcp` returns HTTP 200 with JSON response.
       4. `/usr/local/bin/app-backup` creates a `.db.gz` snapshot in `/var/backups/apps`.
       5. Idempotency & backup: Re-running `sh dist/setup-cookbooks.sh` preserves existing `MCP_TOKEN` in `/etc/conf.d/cookbooks` and creates a pre-upgrade backup `cookbooks-before-upgrade-*.db.gz`.
       6. Uninstall: `sh dist/setup-cookbooks.sh --uninstall` removes `/opt/cookbooks` and `/etc/init.d/cookbooks` while keeping `/var/lib/cookbooks`.
4. Open a pull request from branch `ci/github-actions` targeting `main`.

## Non-Goals
- Automated deployment to live servers.
- Restructuring production Alpine OpenRC scripts beyond supporting standard execution and container CI environments.

## Anti-Goals
- Flaky tests or hangs during CI service startup in unprivileged containers.
- Skipped JWT authentication tests hiding missing cryptographic libraries.

## Constraints
- Alpine BusyBox `tar` does not support `--owner` or `--group` flags, so GNU `tar` must be present when `installer/build.sh` runs.
- In Docker containers without PID 1 init or full cgroup privileges, OpenRC's `supervise-daemon` requires stubs for `rc-service` / `rc-update` to background Gunicorn reliably.

## Approaches Considered
- **Approach A: Monolithic Alpine smoke-test script**: All Alpine smoke tests and verifications packaged into a single shell script.
- **Approach B: OpenRC stub helper with modular CI steps (Selected)**: `.github/scripts/install-openrc-stub.sh` provides the service management shim, while individual test assertions run as distinct steps in `.github/workflows/ci.yml` for clear visibility in the GitHub Actions UI.

## What's Left to the Builder
- Determine pinned versions in `requirements-dev.txt` compatible with Python 3.12/3.13.
- Implement `.github/scripts/install-openrc-stub.sh`.
- Implement `.github/workflows/ci.yml`.
- Verify workflow locally (where possible) and create branch and PR.
