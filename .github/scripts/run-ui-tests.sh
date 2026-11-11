#!/bin/sh
# CI: run the browser tests and fail if any of them skips.
#
#   sh .github/scripts/run-ui-tests.sh            # PYTHON=python3 to pick the interpreter (default: python)
#
# These four files call pytest.importorskip("playwright.sync_api") and skip a test when Chromium can't launch. On a
# machine with a broken browser install that leaves a green run that tested nothing, so here every file is run on its
# own and must report passes and no skips. Needs Playwright and Chromium (see requirements-ui.txt).
set -eu
cd "$(dirname "$0")/../.."
PYTHON=${PYTHON:-python}
out=$(mktemp)
trap 'rm -f "$out"' EXIT

failed=0
for f in tests/test_ask_ui.py tests/test_ask_gate.py tests/test_frontend_errors.py tests/test_uid_route.py; do
    printf '\n== %s\n' "$f"
    status=0
    "$PYTHON" -m pytest -q -rs -p no:cacheprovider "$f" > "$out" 2>&1 || status=$?
    cat "$out"
    if [ "$status" -ne 0 ]; then
        echo "FAIL: $f: pytest exited $status" >&2
        failed=1
    elif grep -Eq '^SKIPPED |[0-9]+ skipped' "$out"; then
        echo "FAIL: $f: a test was skipped (is Chromium installed?)" >&2
        failed=1
    elif ! grep -Eq '[0-9]+ passed' "$out"; then
        echo "FAIL: $f: no test passed" >&2
        failed=1
    fi
done
exit "$failed"
