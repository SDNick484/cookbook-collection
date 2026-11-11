#!/bin/sh
# Build the one-file installer: dist/setup-cookbooks.sh (shell header + base64 tarball of the app).
# The Home Assistant webhook ID for health alerts isn't kept in the repo; pass it in if you want it baked in:
#   HA_WEBHOOK_ID=<your-webhook-id> sh installer/build.sh
set -eu
cd "$(dirname "$0")/.."
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p dist "$STAGE/cookbooks"
cp -r app.py extras.py dinnerlog.py cook.py eyb.py ingredients.py photos.py online.py ask.py house.py importer.py seed.json README.md ops static tests "$STAGE/cookbooks/"
find "$STAGE" \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
OUT=dist/setup-cookbooks.sh
ESC_HOOK=$(printf '%s' "${HA_WEBHOOK_ID:-}" | sed 's/[\\&|]/\\&/g')
# Don't leak the builder's user/group into the archive. GNU tar can rewrite it; BusyBox tar (Alpine's
# default) can't, so chown the staged copy when we're root. The installer chowns to root on install anyway.
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
  set -- --owner=0 --group=0 --numeric-owner
else
  set --
  if [ "$(id -u)" = 0 ]; then chown -R 0:0 "$STAGE/cookbooks"; fi
fi
# Build the tarball as its own step: inside a pipeline, `set -e` only sees base64's exit status.
tar "$@" -C "$STAGE" -czf "$STAGE/payload.tgz" cookbooks
{
  sed "s|__HA_WEBHOOK_ID__|$ESC_HOOK|" installer/setup-head.sh
  echo "__PAYLOAD_BELOW__"
  base64 < "$STAGE/payload.tgz"
} > "$OUT"
chmod +x "$OUT"
sh -n "$OUT"
# Round-trip the payload the same way setup-head.sh extracts it.
sed -n '/^__PAYLOAD_BELOW__$/,$p' "$OUT" | tail -n +2 | base64 -d | tar tzf - >/dev/null
echo "Built $OUT"
